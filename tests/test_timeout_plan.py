from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from muse2api.accounts import Account, AccountPool, JsonAccountStore
from muse2api.app import create_app
from muse2api.config import Settings
from muse2api.drivers.base import ChatRequest, ImageRequest, InputImage, MuseDriver
from muse2api.drivers.browser.driver import BrowserDriver, _Tab
from muse2api.drivers.mock import MockDriver
from muse2api.errors import UpstreamError, UpstreamTimeout
from muse2api.services.gateway import Gateway


async def _make_pool(tmp_path, n=2) -> AccountPool:
    store = JsonAccountStore(tmp_path / "accounts.json")
    pool = AccountPool(store, acquire_timeout=1.0)
    for i in range(1, n + 1):
        await pool.upsert(Account(id=f"acc-{i}", cookies={"hatch_sess": f"sess-{i}"}))
    return pool


def test_upstream_timeout_not_retryable():
    """Requirement: UpstreamTimeout.retryable must be False."""
    assert UpstreamTimeout.retryable is False
    assert UpstreamTimeout().retryable is False
    assert UpstreamError.retryable is True


@pytest.mark.asyncio
async def test_gateway_no_failover_on_upstream_timeout(tmp_path):
    """Requirement: UpstreamTimeout should not trigger gateway failover to alternative accounts."""
    pool = await _make_pool(tmp_path, n=2)
    called_accounts: list[str] = []

    class TimeoutDriver(MuseDriver):
        async def chat_stream(self, account, req):
            called_accounts.append(account.id)
            if False:
                yield
            raise UpstreamTimeout("assistant reply timed out")

    gw = Gateway(pool, TimeoutDriver(), max_failover=2)
    req = ChatRequest(prompt="hello", model="muse-chat")

    with pytest.raises(UpstreamTimeout):
        async for _ in gw.chat_stream(req):
            pass

    # Must only call acc-1; no failover to acc-2!
    assert called_accounts == ["acc-1"]


@pytest.mark.asyncio
async def test_gateway_no_failover_when_submitted(tmp_path):
    """Requirement: Once submitted flag is True, no retry/failover is allowed even for retryable UpstreamError."""
    pool = await _make_pool(tmp_path, n=2)
    called_accounts: list[str] = []

    class SubmittedDriver(MuseDriver):
        async def chat_stream(self, account, req):
            called_accounts.append(account.id)
            req.submitted = True
            if False:
                yield
            raise UpstreamError("upstream connection dropped after submit")

    gw = Gateway(pool, SubmittedDriver(), max_failover=2)
    req = ChatRequest(prompt="hello", model="muse-chat")

    with pytest.raises(UpstreamError):
        async for _ in gw.chat_stream(req):
            pass

    # Must not retry on acc-2 because req.submitted is True
    assert called_accounts == ["acc-1"]


@pytest.mark.asyncio
async def test_gateway_failover_when_not_yet_submitted(tmp_path):
    """Requirement: If an error occurs BEFORE submission and is retryable, gateway still fails over."""
    pool = await _make_pool(tmp_path, n=2)
    called_accounts: list[str] = []

    class FailBeforeSubmitDriver(MuseDriver):
        async def chat_stream(self, account, req):
            called_accounts.append(account.id)
            if account.id == "acc-1":
                # Fails before submit
                assert not req.submitted
                raise UpstreamError("tab checkout failed before submit")
            yield "success"

    gw = Gateway(pool, FailBeforeSubmitDriver(), max_failover=2)
    req = ChatRequest(prompt="hello", model="muse-chat")

    tokens = [t async for t in gw.chat_stream(req)]
    assert tokens == ["success"]
    assert called_accounts == ["acc-1", "acc-2"]


@pytest.mark.asyncio
async def test_wait_for_submission_does_not_timeout_at_10s(tmp_path):
    """Requirement: _wait_for_submission does not have the old 10s hardcap and respects extended timeouts."""
    settings = Settings(
        _env_file=None,
        chat_timeout=300.0,
        first_token_timeout=300.0,
        data_dir=tmp_path / "data",
    )
    driver = BrowserDriver(settings)

    # Mock tab session
    mock_session = MagicMock()
    # evaluate returns INPUT_EMPTY as False for the first few polls, then True
    evaluate_calls = 0

    async def mock_evaluate(expr, **kwargs):
        nonlocal evaluate_calls
        evaluate_calls += 1
        if evaluate_calls >= 3:
            return True  # input is now empty (submitted)
        return False

    mock_session.evaluate = AsyncMock(side_effect=mock_evaluate)

    tab = _Tab("acc-1", "ctx-1", "t1", mock_session)
    base = {"userCount": 0}

    # Mock _state to return generating=False, userCount=0
    driver._state = AsyncMock(return_value={"generating": False, "userCount": 0})

    # Call _wait_for_submission with timeout=45.0
    await driver._wait_for_submission(tab, base, timeout=45.0)
    assert evaluate_calls >= 3


def test_sse_heartbeat_sent_immediately(tmp_path):
    """Requirement: stream_heartbeat_interval > 0 sends ': keepalive' immediately before tokens."""
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        api_key="test-key",
        admin_key="admin-key",
        driver="mock",
        stream_heartbeat_interval=5.0,
    )
    # MockDriver with a small delay so first token takes time
    app = create_app(settings, MockDriver(delay=0.1))
    auth = {"Authorization": "Bearer test-key"}

    with TestClient(app) as c:
        with c.stream("POST", "/v1/chat/completions", headers=auth, json={
            "messages": [{"role": "user", "content": "hi"}], "stream": True,
        }) as r:
            assert r.status_code == 200
            lines = []
            for ln in r.iter_lines():
                lines.append(ln)
                if ln.startswith("data: "):
                    break

    # First non-empty line must be the keepalive comment
    assert lines[0].startswith(":")
    assert "keepalive" in lines[0]
