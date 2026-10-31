"""Admin API: account pool management, status, tasks, client keys and the request log.

The ``/dashboard`` page is built on these endpoints; a browser extension for
one-click cookie import is planned on top of them too (see docs/ARCHITECTURE.md).
"""

from __future__ import annotations

import time
from typing import Literal

from fastapi import APIRouter, Depends, Query

from ... import __version__
from ...accounts.keepalive import renew_account
from ...accounts.model import Account, AccountStatus
from ...auth.keys import ApiKey, _DISPLAY_LEN, hash_key
from ...errors import InvalidRequest, Muse2APIError, NotFound
from ...services.container import Services
from ...upstream.muse import missing_session_cookies
from ..deps import get_services, require_admin_key
from ..schemas import AccountCreate, AccountsImportRequest, AccountUpdate, KeyCreate, KeyUpdate

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin_key)])


def parse_cookies(value: dict[str, str] | str | list[dict]) -> tuple[dict[str, str], dict[str, int]]:
    """Normalise the three cookie import formats into ``(cookies, cookie_expires)``.

    ``cookie_expires`` maps cookie name -> unix expiry seconds; only cookie-export
    lists carry expiry info, the other formats return an empty dict.
    """
    if isinstance(value, dict):
        return dict(value), {}
    if isinstance(value, str):
        cookies: dict[str, str] = {}
        for part in value.split(";"):
            part = part.strip()
            if not part or "=" not in part:
                continue
            name, _, val = part.partition("=")
            name = name.strip()
            if name:
                cookies[name] = val.strip()
        return cookies, {}
    if isinstance(value, list):
        cookies, expires = {}, {}
        for i, entry in enumerate(value):
            if not isinstance(entry, dict) or not entry.get("name"):
                raise InvalidRequest(f"cookies[{i}] must be an object with 'name' and 'value'")
            name = str(entry["name"])
            cookies[name] = str(entry.get("value", ""))
            exp = entry.get("expirationDate")
            if exp is not None:
                try:
                    expires[name] = int(float(exp))
                except (TypeError, ValueError):
                    raise InvalidRequest(f"cookies[{i}].expirationDate is not a number") from None
        return cookies, expires
    raise InvalidRequest("cookies must be a dict, a raw 'Cookie' header string, or a cookie-export list")


def _build_account(item: AccountCreate) -> Account:
    """Build an Account from a create payload, normalising the cookie format first."""
    cookies, expires = parse_cookies(item.cookies)
    cookies = {k: v for k, v in cookies.items() if v}
    expires = {k: v for k, v in expires.items() if k in cookies}
    if not cookies:
        raise InvalidRequest("cookies must not be empty")
    return Account(label=item.label, cookies=cookies,
                   cookie_expires={**expires, **item.cookie_expires}, enabled=item.enabled)


def _get(svc: Services, account_id: str) -> Account:
    acc = svc.pool.get(account_id)
    if acc is None:
        raise NotFound(f"account '{account_id}' not found")
    return acc


@router.get("/status")
async def status(svc: Services = Depends(get_services)) -> dict:
    """Server version, driver health, driver capabilities and pool stats."""
    return {
        "version": __version__,
        "driver": await svc.driver.health(),
        "capabilities": svc.driver.capabilities.__dict__,
        "accounts": svc.pool.stats(),
    }


@router.get("/accounts")
async def list_accounts(svc: Services = Depends(get_services)) -> dict:
    """All pool accounts with status, cookies and quota metadata."""
    return {"data": [a.public() for a in svc.pool.all()]}


@router.post("/accounts")
async def create_account(body: AccountCreate, svc: Services = Depends(get_services)) -> dict:
    """Add an account from any of the supported cookie formats."""
    acc = _build_account(body)
    await svc.pool.upsert(acc)
    return {"account": acc.public(), "missing_cookies": missing_session_cookies(acc.cookies)}


@router.post("/accounts/import")
async def import_accounts(body: AccountsImportRequest,
                          svc: Services = Depends(get_services)) -> dict:
    """Import several accounts in one request; a failing entry does not abort the batch."""
    results = []
    for i, item in enumerate(body.accounts):
        try:
            acc = _build_account(item)
        except Muse2APIError as e:
            results.append({"index": i, "ok": False, "error": e.message, "code": e.code})
            continue
        await svc.pool.upsert(acc)
        results.append({"index": i, "ok": True, "account": acc.public(),
                        "missing_cookies": missing_session_cookies(acc.cookies)})
    return {"data": results}


@router.patch("/accounts/{account_id}")
async def update_account(account_id: str, body: AccountUpdate,
                         svc: Services = Depends(get_services)) -> dict:
    """Update label / enabled / routing / cookies. Fresh cookies re-enable an invalid account."""
    acc = _get(svc, account_id)
    if body.label is not None:
        acc.label = body.label
    if body.enabled is not None:
        acc.enabled = body.enabled
    if body.routing is not None:
        acc.routing = body.routing
    if body.cookies is not None:
        acc.cookies = {k: v for k, v in body.cookies.items() if v}
        # Fresh cookies give an invalid account another chance.
        acc.status = AccountStatus.ACTIVE
        acc.last_error = None
    if body.cookie_expires is not None:
        acc.cookie_expires = body.cookie_expires
    await svc.pool.upsert(acc)
    return {"account": acc.public()}


@router.delete("/accounts/{account_id}")
async def delete_account(account_id: str, svc: Services = Depends(get_services)) -> dict:
    """Remove an account from the pool."""
    if not await svc.pool.remove(account_id):
        raise NotFound(f"account '{account_id}' not found")
    return {"deleted": account_id}


@router.post("/accounts/{account_id}/reset")
async def reset_account(account_id: str, svc: Services = Depends(get_services)) -> dict:
    """Clear cooldown/error state and mark the account active again."""
    acc = _get(svc, account_id)
    acc.status, acc.cooldown_until, acc.last_error = AccountStatus.ACTIVE, 0.0, None
    await svc.pool.upsert(acc)
    return {"account": acc.public()}


@router.post("/accounts/{account_id}/renew")
async def renew(account_id: str, svc: Services = Depends(get_services)) -> dict:
    """Ask the driver to renew this account's session cookies."""
    _get(svc, account_id)
    return await renew_account(svc.pool, svc.driver, account_id)


@router.get("/accounts/{account_id}/quota")
async def account_quota(account_id: str, svc: Services = Depends(get_services)) -> dict:
    """Ask the active driver for the account's quota snapshot and persist it in ``meta``.

    Drivers that do not implement ``MuseDriver.quota`` raise
    ``FeatureNotImplemented``, which the app renders as HTTP 501.
    """
    acc = _get(svc, account_id)
    snapshot = await svc.driver.quota(acc)
    acc.meta["quota"] = {"checked_at": time.time(), **snapshot}
    await svc.pool.upsert(acc)
    return {"account_id": acc.id, "quota": acc.meta["quota"]}


@router.get("/tasks")
async def list_tasks(limit: int = Query(default=100, ge=1, le=500),
                     svc: Services = Depends(get_services)) -> dict:
    """Recent background tasks (image and video), newest first."""
    return {"data": [t.model_dump(mode="json") for t in svc.tasks.list(limit)]}


@router.post("/tasks/{task_id}/cancel")
async def cancel_task(task_id: str, svc: Services = Depends(get_services)) -> dict:
    """Request cancellation of a running task (succeeds best-effort)."""
    return {"cancelled": await svc.tasks.cancel(task_id)}


# ---- client API keys ----
def _get_key(svc: Services, key_id: str) -> ApiKey:
    key = svc.keys.get(key_id)
    if key is None:
        raise NotFound(f"key '{key_id}' not found")
    return key


# Identities that authenticate without a stored key (see deps.require_api_key).
BUILTIN_KEYS = ("legacy", "admin")
_NO_USAGE = {"total": 0, "requests_24h": 0, "errors_24h": 0, "last_request_at": None}


def _usage(row: dict | None) -> dict:
    if row is None:
        return dict(_NO_USAGE)
    return {k: row[k] for k in _NO_USAGE}


@router.get("/keys")
async def list_keys(svc: Services = Depends(get_services)) -> dict:
    """Stored keys plus the built-in identities, each with request counts from the log."""
    usage = {r["key_id"]: r for r in (await svc.requests.key_usage())["data"]}
    return {
        "data": [{**k.public(), "usage": _usage(usage.get(k.id))} for k in svc.keys.all()],
        "builtin": [
            {
                "id": b,
                "name": b,
                "key": svc.settings.api_key if b == "legacy" else svc.settings.effective_admin_key,
                "usage": _usage(usage.get(b)),
            }
            for b in BUILTIN_KEYS
        ],
    }


@router.get("/keys/usage")
async def key_usage(svc: Services = Depends(get_services)) -> dict:
    """Raw per-``key_id`` counts, including deleted keys still present in the log."""
    return await svc.requests.key_usage()


@router.post("/keys")
async def create_key(body: KeyCreate, svc: Services = Depends(get_services)) -> dict:
    """Create a client key; returns the plaintext value once (custom value if given)."""
    key, plaintext = await svc.keys.create(
        body.name.strip(), body.note, custom_key=body.key or ""
    )
    return {"key": key.public(), "api_key": plaintext}


@router.patch("/keys/{key_id}")
async def update_key(key_id: str, body: KeyUpdate, svc: Services = Depends(get_services)) -> dict:
    """Rename, revoke, re-note or replace a key's value."""
    key = _get_key(svc, key_id)
    if body.name is not None:
        key.name = body.name.strip()
    if body.note is not None:
        key.note = body.note
    if body.revoked is not None:
        key.revoked = body.revoked
    if body.key is not None and body.key.strip():
        new_key = body.key.strip()
        key.key = new_key
        key.prefix = new_key[:_DISPLAY_LEN]
        key.hash = hash_key(new_key)
    await svc.keys.save()
    return {"key": key.public()}


@router.delete("/keys/{key_id}")
async def delete_key(key_id: str, svc: Services = Depends(get_services)) -> dict:
    """Delete a client key; requests using it start failing immediately."""
    if not await svc.keys.remove(key_id):
        raise NotFound(f"key '{key_id}' not found")
    return {"deleted": key_id}


# ---- request log ----
@router.get("/requests")
async def list_requests(
    key_id: str | None = Query(default=None, description="Filter by client key id"),
    account_id: str | None = Query(default=None, description="Filter by pool account id"),
    status: Literal["2xx", "3xx", "4xx", "5xx", "failed"] | None = Query(
        default=None, description="Filter by response status class (or internal failure)"),
    path: str | None = Query(default=None, description="Filter by request path substring"),
    since: float | None = Query(default=None, description="Unix seconds"),
    hide_polls: bool = Query(default=False, description="Hide task-polling GETs"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    svc: Services = Depends(get_services),
) -> dict:
    """Recent requests from the log, filterable by key, account, status class or path."""
    return await svc.requests.query(key_id=key_id, account_id=account_id, status=status,
                                    path=path, since=since, hide_polls=hide_polls,
                                    limit=limit, offset=offset)


@router.get("/stats")
async def stats(window: Literal["1h", "24h", "7d"] = Query(default="24h"),
                svc: Services = Depends(get_services)) -> dict:
    """Request counters and latency percentiles over the given time window."""
    return {"now": time.time(), **await svc.requests.stats(window)}
