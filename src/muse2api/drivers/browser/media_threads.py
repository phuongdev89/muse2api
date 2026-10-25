"""In-memory thread cache for media generation (image & video).

Maintains separate threads for image and video per account, automatically
reusing the existing thread within a 1-hour rolling inactivity window.
"""

from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass
class MediaThreadEntry:
    thread_url: str
    last_used_at: float
    turns: int = 0


class MediaThreadCache:
    """In-memory cache for media threads per account and per kind.

    Keeps 1 thread per (account_id, kind) alive as long as requests arrive within
    ``ttl`` seconds (default 3600.0 = 1 hour) of the last request. If no request
    arrives within 1 hour, the thread is evicted and subsequent calls will start a
    fresh thread.
    """

    def __init__(self, ttl: float = 3600.0, max_turns: int = 16) -> None:
        self.ttl = ttl
        self.max_turns = max_turns
        self._entries: dict[tuple[str, str], MediaThreadEntry] = {}

    def get(self, account_id: str, kind: str, now: float | None = None) -> str | None:
        """Return the active thread URL if valid and within TTL, else evict and return None."""
        key = (account_id, kind)
        entry = self._entries.get(key)
        if entry is None:
            return None
        current_time = time.monotonic() if now is None else now
        if current_time - entry.last_used_at > self.ttl:
            self._entries.pop(key, None)
            return None
        if entry.turns >= self.max_turns:
            self._entries.pop(key, None)
            return None
        return entry.thread_url

    def touch(self, account_id: str, kind: str, thread_url: str, now: float | None = None) -> None:
        """Save or refresh the thread URL for (account_id, kind) and reset the inactivity timer."""
        key = (account_id, kind)
        current_time = time.monotonic() if now is None else now
        entry = self._entries.get(key)
        if entry and entry.thread_url == thread_url:
            entry.last_used_at = current_time
            entry.turns += 1
        else:
            self._entries[key] = MediaThreadEntry(
                thread_url=thread_url,
                last_used_at=current_time,
                turns=1,
            )

    def invalidate(self, account_id: str, kind: str) -> None:
        """Evict the cached thread for (account_id, kind)."""
        self._entries.pop((account_id, kind), None)

    def clear(self) -> None:
        """Clear all cached threads."""
        self._entries.clear()
