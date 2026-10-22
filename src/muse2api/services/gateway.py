"""Gateway: runs driver calls on pooled accounts with automatic failover."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any, TypeVar

from ..accounts.model import Account
from ..accounts.pool import AccountPool, Lease
from ..drivers.base import ChatRequest, ImageRequest, MediaResult, MuseDriver, VideoRequest
from ..errors import UpstreamError
from .request_log import note_account

log = logging.getLogger(__name__)
T = TypeVar("T")

_ANONYMOUS = Account(id="anonymous", label="driver without accounts")


class Gateway:
    def __init__(self, pool: AccountPool, driver: MuseDriver, *, max_failover: int = 2) -> None:
        self.pool = pool
        self.driver = driver
        self.max_failover = max_failover

    @asynccontextmanager
    async def _lease(
        self, exclude: list[str], hint: str | None, account_id: str | None = None
    ) -> AsyncIterator[Lease]:
        if not self.driver.requires_account and not self.pool.all():
            note_account(_ANONYMOUS.id)
            yield Lease(_ANONYMOUS)
            return
        async with self.pool.lease(exclude=exclude, hint=hint, account_id=account_id) as lease:
            note_account(lease.account.id)
            yield lease

    def _should_retry(
        self,
        exc: UpstreamError,
        attempt: int,
        tried: list[str],
        account_id: str | None = None,
        submitted: bool = False,
    ) -> bool:
        if account_id or submitted:
            return False
        # With no other account to switch to, a retry would only replace the real
        # error with "no usable account in pool".
        return (exc.retryable and attempt < self.max_failover
                and self.pool.has_alternative(tried))

    async def chat_stream(
        self, req: ChatRequest, *, account_id: str | None = None
    ) -> AsyncIterator[str]:
        tried: list[str] = []
        for attempt in range(self.max_failover + 1):
            async with self._lease(tried, req.conversation_hint, account_id=account_id) as lease:
                emitted = False
                try:
                    async for delta in self.driver.chat_stream(lease.account, req):
                        emitted = True
                        yield delta
                    return
                except UpstreamError as exc:
                    lease.fail(exc)
                    tried.append(lease.account.id)
                    # Once text reached the client or the prompt was submitted upstream,
                    # we cannot switch accounts.
                    submitted = getattr(req, "submitted", False)
                    if emitted or submitted or not self._should_retry(
                        exc, attempt, tried, account_id=account_id, submitted=submitted
                    ):
                        raise
                    log.warning("chat failed on %s (%s), failing over", lease.account.id, exc)

    async def _run(
        self,
        fn: Callable[[Account], Awaitable[T]],
        hint: str | None = None,
        *,
        account_id: str | None = None,
        req: Any = None,
    ) -> T:
        tried: list[str] = []
        for attempt in range(self.max_failover + 1):
            async with self._lease(tried, hint, account_id=account_id) as lease:
                try:
                    return await fn(lease.account)
                except UpstreamError as exc:
                    lease.fail(exc)
                    tried.append(lease.account.id)
                    submitted = getattr(req, "submitted", False) if req else False
                    if submitted or not self._should_retry(
                        exc, attempt, tried, account_id=account_id, submitted=submitted
                    ):
                        raise
                    log.warning("call failed on %s (%s), failing over", lease.account.id, exc)
        raise AssertionError("unreachable")

    async def generate_image(
        self, req: ImageRequest, *, account_id: str | None = None
    ) -> list[MediaResult]:
        return await self._run(
            lambda acc: self.driver.generate_image(acc, req), account_id=account_id, req=req
        )

    async def generate_video(
        self, req: VideoRequest, *, account_id: str | None = None
    ) -> MediaResult:
        return await self._run(
            lambda acc: self.driver.generate_video(acc, req), account_id=account_id, req=req
        )
