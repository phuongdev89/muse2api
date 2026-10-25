"""Tests for media thread caching and 1-hour rolling inactivity expiration."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from muse2api.accounts import Account
from muse2api.config import Settings
from muse2api.drivers.base import ImageRequest, VideoRequest
from muse2api.drivers.browser.driver import BrowserDriver, _Tab
from muse2api.drivers.browser.media_threads import MediaThreadCache, MediaThreadEntry


def test_media_thread_cache_isolation_and_kinds():
    cache = MediaThreadCache(ttl=3600.0)

    # Initially empty
    assert cache.get("acc1", "image") is None
    assert cache.get("acc1", "video") is None

    # Save thread for acc1 image
    cache.touch("acc1", "image", "https://muse.ai/thread/img1")
    assert cache.get("acc1", "image") == "https://muse.ai/thread/img1"

    # Separate thread for acc1 video
    assert cache.get("acc1", "video") is None
    cache.touch("acc1", "video", "https://muse.ai/thread/vid1")
    assert cache.get("acc1", "video") == "https://muse.ai/thread/vid1"
    assert cache.get("acc1", "image") == "https://muse.ai/thread/img1"

    # Separate account acc2
    assert cache.get("acc2", "image") is None
    cache.touch("acc2", "image", "https://muse.ai/thread/acc2_img")
    assert cache.get("acc2", "image") == "https://muse.ai/thread/acc2_img"
    assert cache.get("acc1", "image") == "https://muse.ai/thread/img1"


def test_media_thread_cache_1h_rolling_expiration():
    cache = MediaThreadCache(ttl=3600.0)
    t0 = 1000.0

    # Create thread at t0
    cache.touch("acc1", "image", "https://muse.ai/thread/img1", now=t0)
    assert cache.get("acc1", "image", now=t0) == "https://muse.ai/thread/img1"

    # Check at 30 minutes (1800s): still valid
    t_30m = t0 + 1800.0
    assert cache.get("acc1", "image", now=t_30m) == "https://muse.ai/thread/img1"

    # Touch at 45 minutes (2700s): resets 1h inactivity timer
    t_45m = t0 + 2700.0
    cache.touch("acc1", "image", "https://muse.ai/thread/img1", now=t_45m)

    # Check at 75 minutes from t0 (4500s total, but only 30m after last call): STILL VALID
    t_75m = t0 + 4500.0
    assert cache.get("acc1", "image", now=t_75m) == "https://muse.ai/thread/img1"

    # Check at 106 minutes from t0 (6360s total, which is 3660s after last touch at 2700s):
    # Over 1h since last call -> expired and evicted!
    t_expired = t_45m + 3601.0
    assert cache.get("acc1", "image", now=t_expired) is None
    # Confirm it was evicted from cache
    assert cache.get("acc1", "image", now=t_expired) is None


def test_media_thread_cache_max_turns_eviction():
    cache = MediaThreadCache(ttl=3600.0, max_turns=3)
    t0 = 100.0

    cache.touch("acc1", "image", "https://muse.ai/thread/img1", now=t0)  # turn 1
    assert cache.get("acc1", "image", now=t0) == "https://muse.ai/thread/img1"

    cache.touch("acc1", "image", "https://muse.ai/thread/img1", now=t0)  # turn 2
    assert cache.get("acc1", "image", now=t0) == "https://muse.ai/thread/img1"

    cache.touch("acc1", "image", "https://muse.ai/thread/img1", now=t0)  # turn 3
    # At max_turns = 3, thread should now be evicted to prevent memory bloat
    assert cache.get("acc1", "image", now=t0) is None


def test_media_thread_cache_invalidation_and_clear():
    cache = MediaThreadCache(ttl=3600.0)
    cache.touch("acc1", "image", "https://muse.ai/thread/img1")
    cache.touch("acc1", "video", "https://muse.ai/thread/vid1")

    cache.invalidate("acc1", "image")
    assert cache.get("acc1", "image") is None
    assert cache.get("acc1", "video") == "https://muse.ai/thread/vid1"

    cache.clear()
    assert cache.get("acc1", "video") is None


class _FakeCDPSession:
    closed = False

    async def close(self) -> None:
        self.closed = True

    async def send(self, method: str, params: dict | None = None) -> dict:
        return {}

    async def evaluate(self, expr: str, await_promise: bool = False, timeout: float | None = None):
        return {}


@pytest.fixture
def browser_driver(tmp_path, monkeypatch) -> BrowserDriver:
    drv = BrowserDriver(Settings(_env_file=None, data_dir=tmp_path, account_max_concurrency=2))

    async def fake_open(account: Account) -> _Tab:
        return _Tab(account.id, "ctx", "tab1", _FakeCDPSession())

    monkeypatch.setattr(drv, "_open_tab", fake_open)
    return drv


@pytest.mark.asyncio
async def test_browser_driver_media_thread_reuse(browser_driver, monkeypatch):
    account = Account(id="acc_test", cookies={"hatch_sess": "val"})
    tab = await browser_driver._checkout(account)

    new_thread_calls = []
    saved_open_calls = []

    async def fake_new_thread(t):
        new_thread_calls.append(t)

    async def fake_open_saved(t):
        saved_open_calls.append(t.thread_url)

    async def fake_on_thread(t):
        return bool(t.thread_url)

    async def fake_state(t):
        return {"agentCount": 0, "attachments": []}

    async def fake_attach(t, imgs):
        pass

    async def fake_send(t, prompt):
        pass

    async def fake_wait_for_submission(t, base, timeout=None):
        pass

    href_counter = 0

    async def fake_href(t):
        nonlocal href_counter
        href_counter += 1
        return "https://muse.ai/thread/generated_img_thread_123"

    monkeypatch.setattr(browser_driver, "_new_thread", fake_new_thread)
    monkeypatch.setattr(browser_driver, "_open_saved", fake_open_saved)
    monkeypatch.setattr(browser_driver, "_on_thread", fake_on_thread)
    monkeypatch.setattr(browser_driver, "_state", fake_state)
    monkeypatch.setattr(browser_driver, "_attach", fake_attach)
    monkeypatch.setattr(browser_driver, "_send", fake_send)
    monkeypatch.setattr(browser_driver, "_wait_for_submission", fake_wait_for_submission)
    monkeypatch.setattr(browser_driver, "_href", fake_href)

    # 1st call: image -> starts new thread, saves url
    await browser_driver._prepare_media(tab, account, "a photo of a cat", [], "image")
    assert len(new_thread_calls) == 1
    assert browser_driver.media_threads.get("acc_test", "image") == "https://muse.ai/thread/generated_img_thread_123"

    # 2nd call: image -> reuses hot thread, no new thread created
    await browser_driver._prepare_media(tab, account, "a photo of a dog", [], "image")
    assert len(new_thread_calls) == 1  # Still 1, didn't create a new thread!

    # 3rd call: video -> has its own thread, starts a new thread for video
    async def fake_video_href(t):
        return "https://muse.ai/thread/generated_video_thread_456"

    monkeypatch.setattr(browser_driver, "_href", fake_video_href)
    await browser_driver._prepare_media(tab, account, "a video of a bird", [], "video")
    assert len(new_thread_calls) == 2  # New thread created for video!
    assert browser_driver.media_threads.get("acc_test", "video") == "https://muse.ai/thread/generated_video_thread_456"
    assert browser_driver.media_threads.get("acc_test", "image") == "https://muse.ai/thread/generated_img_thread_123"


@pytest.mark.asyncio
async def test_gateway_passes_affinity_hint_for_media(monkeypatch):
    from muse2api.accounts.pool import AccountPool
    from muse2api.accounts.store import JsonAccountStore
    from muse2api.drivers.mock import MockDriver
    from muse2api.services.gateway import Gateway

    store = AsyncMock(spec=JsonAccountStore)
    account = Account(id="acc_main", cookies={"hatch_sess": "xyz"})
    store.load = AsyncMock(return_value=[account])

    pool = AccountPool(store, strategy="affinity")
    await pool.load()
    driver = MockDriver()
    gateway = Gateway(pool, driver)

    captured_hints = []
    orig_lease = pool.lease

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def spy_lease(exclude=None, hint=None, account_id=None):
        captured_hints.append(hint)
        async with orig_lease(exclude=exclude, hint=hint, account_id=account_id) as lease:
            yield lease

    monkeypatch.setattr(pool, "lease", spy_lease)

    img_req = ImageRequest(prompt="test image", model="muse-image")
    await gateway.generate_image(img_req)
    assert captured_hints[-1] == "media:image"

    vid_req = VideoRequest(prompt="test video", model="muse-video")
    await gateway.generate_video(vid_req)
    assert captured_hints[-1] == "media:video"
