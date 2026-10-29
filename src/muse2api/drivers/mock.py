"""Offline driver used for development, tests and front-end work.

It needs no accounts and returns deterministic output, so the whole API surface
can be exercised without touching muse.ai.
"""

from __future__ import annotations

import asyncio
import struct
import time
import zlib
from collections.abc import AsyncIterator

from ..accounts.model import Account
from ..errors import NotFound
from ..upstream.muse import parse_thread_ref, thread_url
from .base import (
    ChatRequest,
    DriverCapabilities,
    ImageRequest,
    MediaResult,
    MuseDriver,
    SessionInfo,
    ThreadInfo,
    VideoRequest,
)


def _solid_png(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    row = b"\x00" + bytes(rgb) * width
    raw = row * height
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


class MockDriver(MuseDriver):
    name = "mock"
    requires_account = False
    capabilities = DriverCapabilities(
        chat=True, chat_images=True, image=True, image_edit=True, video=True, renew_session=True,
        quota=True,
    )

    def __init__(self, delay: float = 0.01) -> None:
        self.delay = delay
        self._clock = time.time
        # thread id -> {"title", "created_at", "updated_at", "messages": [(role, text, ts)]}
        self.threads: dict[str, dict] = {}
        self._seed_threads()

    def _seed_threads(self) -> None:
        now = int(self._clock())
        seeds = [
            ("6b6e1a2c-0000-4000-8000-000000000001", "Seed alpha", [
                ("user", "hello alpha"), ("assistant", "Hi! Mock alpha here."),
            ]),
            ("6b6e1a2c-0000-4000-8000-000000000002", "Seed beta", [
                ("user", "hello beta"), ("assistant", "Mock beta reporting."),
            ]),
            ("6b6e1a2c-0000-4000-8000-000000000003", "Seed gamma", [
                ("user", "hello gamma"), ("assistant", "Mock gamma at your service."),
            ]),
        ]
        for i, (tid, title, msgs) in enumerate(seeds):
            updated = now - (i + 1) * 600
            self.threads[tid] = {
                "title": title,
                "created_at": updated - len(msgs) * 30,
                "updated_at": updated,
                "messages": [(role, text, updated - (len(msgs) - j) * 30)
                             for j, (role, text) in enumerate(msgs)],
            }

    def _thread(self, thread_id: str) -> dict:
        try:
            tid = parse_thread_ref(thread_id)
        except ValueError as exc:
            raise NotFound(str(exc)) from exc
        if tid not in self.threads:
            raise NotFound(f"thread '{tid}' not found")
        return self.threads[tid]

    def _view(self, tid: str, data: dict) -> ThreadInfo:
        return ThreadInfo(id=tid, title=data["title"], url=thread_url(tid),
                          created_at=data["created_at"], updated_at=data["updated_at"])

    async def chat_stream(self, account: Account, req: ChatRequest) -> AsyncIterator[str]:
        reply = f"[mock:{req.model}] You said: {req.prompt[-200:]}"
        if req.images:
            reply += f" (with {len(req.images)} image(s))"
        for i in range(0, len(reply), 8):
            if req.cancel and req.cancel.is_set():
                return
            await asyncio.sleep(self.delay)
            yield reply[i : i + 8]

    async def generate_image(self, account: Account, req: ImageRequest) -> list[MediaResult]:
        results = []
        for i in range(max(1, req.n)):
            if req.on_progress:
                req.on_progress(int((i + 1) / max(1, req.n) * 100))
            await asyncio.sleep(self.delay)
            color = (40 + 50 * i % 200, 120, 200)
            results.append(
                MediaResult(
                    data=_solid_png(64, 64, color),
                    mime="image/png",
                    kind="image",
                    width=64,
                    height=64,
                    revised_prompt=req.prompt,
                )
            )
        return results

    async def generate_video(self, account: Account, req: VideoRequest) -> MediaResult:
        for p in (10, 40, 70, 100):
            await asyncio.sleep(self.delay)
            if req.on_progress:
                req.on_progress(p)
        # Not a playable video; placeholder bytes with the right mime for plumbing tests.
        return MediaResult(data=b"\x00\x00\x00\x18ftypmp42mock", mime="video/mp4", kind="video")

    # ------------------------------------------------------------ threads
    async def list_threads(
        self, account: Account, limit: int = 20, cursor: str | None = None
    ) -> tuple[list[ThreadInfo], str | None]:
        offset = 0
        if cursor:
            try:
                offset = max(0, int(cursor))
            except ValueError:
                offset = 0
        ordered = sorted(self.threads.items(),
                         key=lambda kv: kv[1]["updated_at"], reverse=True)
        page = [self._view(tid, data) for tid, data in ordered[offset:offset + limit]]
        next_cursor = str(offset + limit) if offset + limit < len(ordered) else None
        return page, next_cursor

    async def thread_messages(self, account: Account, thread_id: str) -> list[dict]:
        data = self._thread(thread_id)
        return [{"role": role, "content": text, "created_at": ts}
                for role, text, ts in data["messages"]]

    async def rename_thread(self, account: Account, thread_id: str, title: str) -> ThreadInfo:
        data = self._thread(thread_id)
        data["title"] = title
        data["updated_at"] = int(self._clock())
        try:
            tid = parse_thread_ref(thread_id)
        except ValueError as exc:
            raise NotFound(str(exc)) from exc
        return self._view(tid, data)

    async def delete_thread(self, account: Account, thread_id: str) -> None:
        self._thread(thread_id)  # raises NotFound when missing
        del self.threads[parse_thread_ref(thread_id)]

    async def renew_session(self, account: Account) -> SessionInfo:
        return SessionInfo(ok=True, meta={"driver": "mock"})

    async def quota(self, account: Account) -> dict:
        """Deterministic fake quota snapshot for plumbing tests and front-end work."""
        return {
            "plan": "free",
            "messages_used": 0,
            "messages_limit": 100,
            "messages_remaining": 100,
            "media_used": 0,
            "media_limit": 20,
            "media_remaining": 20,
            "resets_at": None,
        }
