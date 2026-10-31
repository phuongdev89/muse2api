from __future__ import annotations

import asyncio
import time

import pytest

from muse2api.accounts import Account, AccountPool, AccountStatus, JsonAccountStore
from muse2api.drivers.base import ChatRequest, VideoRequest
from muse2api.drivers.mock import MockDriver
from muse2api.errors import NoAccountAvailable, UpstreamAuthError, UpstreamError
from muse2api.services.gateway import Gateway


async def _pool(tmp_path, n=2, **kw) -> AccountPool:
    pool = AccountPool(JsonAccountStore(tmp_path / "acc.json"), acquire_timeout=0.2, **kw)
    for i in range(n):
        await pool.upsert(Account(id=f"a{i}", cookies={"hatch_sess": "x"}))
    return pool


async def test_empty_pool_raises(tmp_path):
    pool = await _pool(tmp_path, n=0)
    with pytest.raises(NoAccountAvailable):
        await pool.acquire()


async def test_lru_spreads_load(tmp_path):
    pool = await _pool(tmp_path, strategy="lru")
    first = await pool.acquire()
    await pool.release(first)
    second = await pool.acquire()
    assert first.id != second.id


async def test_concurrency_limit_waits_then_times_out(tmp_path):
    pool = await _pool(tmp_path, n=1)
    held = await pool.acquire()
    with pytest.raises(NoAccountAvailable, match="busy"):
        await pool.acquire()
    waiter = asyncio.create_task(pool.acquire())
    await asyncio.sleep(0.05)
    await pool.release(held)
    assert (await waiter).id == held.id


async def test_auth_error_invalidates(tmp_path):
    pool = await _pool(tmp_path, n=1)
    with pytest.raises(UpstreamAuthError):
        async with pool.lease():
            raise UpstreamAuthError("bad cookie")
    assert pool.get("a0").status == AccountStatus.INVALID
    with pytest.raises(NoAccountAvailable):
        await pool.acquire()


async def test_persistence_roundtrip(tmp_path):
    await _pool(tmp_path, n=2)
    reloaded = AccountPool(JsonAccountStore(tmp_path / "acc.json"))
    await reloaded.load()
    assert {a.id for a in reloaded.all()} == {"a0", "a1"}


class _FlakyDriver(MockDriver):
    requires_account = True

    def __init__(self, bad: set[str]):
        super().__init__(delay=0)
        self.bad = bad

    async def chat_stream(self, account, req):
        if account.id in self.bad:
            raise UpstreamError("boom")
        async for d in super().chat_stream(account, req):
            yield d


async def test_gateway_fails_over(tmp_path):
    pool = await _pool(tmp_path, n=2, strategy="round_robin")
    gw = Gateway(pool, _FlakyDriver({"a0"}), max_failover=2)
    text = "".join([d async for d in gw.chat_stream(ChatRequest(prompt="hi", model="m"))])
    assert "hi" in text
    assert pool.get("a0").status == AccountStatus.COOLING
    assert pool.get("a1").success_count == 1


async def test_single_account_failure_keeps_original_error(tmp_path):
    pool = await _pool(tmp_path, n=1)
    driver = _FlakyDriver({"a0"})
    gw = Gateway(pool, driver, max_failover=2)
    # No other account: no pointless retry that would hide "boom" behind NoAccountAvailable.
    with pytest.raises(UpstreamError, match="boom"):
        _ = [d async for d in gw.chat_stream(ChatRequest(prompt="hi", model="m"))]
    assert pool.get("a0").fail_count == 1


async def test_acquire_waits_for_cooldown_to_end(tmp_path):
    pool = await _pool(tmp_path, n=1)
    acc = pool.get("a0")
    acc.status, acc.cooldown_until = AccountStatus.COOLING, time.time() + 0.1
    got = await pool.acquire()  # acquire_timeout is 0.2s, cooldown ends after 0.1s
    assert got.id == "a0"


async def test_acquire_fails_fast_when_cooldown_outlasts_timeout(tmp_path):
    pool = await _pool(tmp_path, n=1)
    acc = pool.get("a0")
    acc.status, acc.cooldown_until = AccountStatus.COOLING, time.time() + 60
    started = time.monotonic()
    with pytest.raises(NoAccountAvailable, match="cooling"):
        await pool.acquire()
    assert time.monotonic() - started < 0.1


async def test_timeout_does_not_cool_the_account(tmp_path):
    from muse2api.errors import UpstreamTimeout

    pool = await _pool(tmp_path, n=1)
    with pytest.raises(UpstreamTimeout):
        async with pool.lease():
            raise UpstreamTimeout("video generation timed out")
    acc = pool.get("a0")
    assert acc.status == AccountStatus.ACTIVE and acc.fail_count == 1
    assert (await pool.acquire()).id == "a0"


async def test_other_upstream_errors_still_cool_down(tmp_path):
    pool = await _pool(tmp_path, n=1, cooldown=30)
    with pytest.raises(UpstreamError):
        async with pool.lease():
            raise UpstreamError("browser error")
    assert pool.get("a0").status == AccountStatus.COOLING


# ---------------------------------------------------------------- routing kinds
async def test_routing_kind_filter(tmp_path):
    pool = await _pool(tmp_path, n=2)
    pool.get("a0").routing.video = False

    # Video never lands on the account that disabled it.
    got = await pool.acquire(kind="video")
    assert got.id == "a1"
    await pool.release(got)

    # Chat can still use it; force a0 to be the LRU choice first.
    pool.get("a1").last_used_at = time.time()
    got = await pool.acquire(kind="chat")
    assert got.id == "a0"
    await pool.release(got)

    # has_alternative is kind-aware.
    assert pool.has_alternative({"a1"}, kind="chat")
    assert not pool.has_alternative({"a1"}, kind="video")

    # Nothing else can serve video: fail fast with a clear error, no waiting.
    started = time.monotonic()
    with pytest.raises(NoAccountAvailable, match="video"):
        await pool.acquire(exclude=["a1"], kind="video")
    assert time.monotonic() - started < 0.1


async def test_routing_kind_skips_busy_account(tmp_path):
    pool = await _pool(tmp_path, n=2)
    pool.get("a0").routing.video = False
    held = await pool.acquire(kind="video")
    assert held.id == "a1"
    # a0 is not eligible for video, so the only blocker is a1 being busy.
    started = time.monotonic()
    with pytest.raises(NoAccountAvailable, match="busy"):
        await pool.acquire(kind="video")
    assert time.monotonic() - started < 0.5
    await pool.release(held)


async def test_pinned_account_bypasses_routing(tmp_path):
    pool = await _pool(tmp_path, n=1)
    pool.get("a0").routing.video = False
    got = await pool.acquire(kind="video", account_id="a0")
    assert got.id == "a0"
    await pool.release(got)


class _RecordingVideoDriver(MockDriver):
    requires_account = True

    def __init__(self) -> None:
        super().__init__(delay=0)
        self.served: list[str] = []

    async def generate_video(self, account, req):
        self.served.append(account.id)
        return await super().generate_video(account, req)


async def test_gateway_video_routes_by_kind(tmp_path):
    pool = await _pool(tmp_path, n=2, strategy="round_robin")
    pool.get("a0").routing.video = False
    driver = _RecordingVideoDriver()
    result = await Gateway(pool, driver, max_failover=2).generate_video(
        VideoRequest(prompt="hi", model="m"))
    assert result.kind == "video"
    assert driver.served == ["a1"]  # a0 was never considered for video


async def test_gateway_video_without_kind_account_raises(tmp_path):
    pool = await _pool(tmp_path, n=1)
    pool.get("a0").routing.video = False
    driver = _RecordingVideoDriver()
    with pytest.raises(NoAccountAvailable, match="video"):
        await Gateway(pool, driver, max_failover=2).generate_video(
            VideoRequest(prompt="hi", model="m"))
    assert driver.served == []
