"""Channel routing and bounded in-process rate limits."""

from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass


@dataclass(frozen=True)
class RateLimitConfig:
    requests: int
    per_s: float


class SlidingWindow:
    def __init__(self, config: RateLimitConfig) -> None:
        self.config = config
        self._timestamps: deque[float] = deque()

    def _prune(self, current: float) -> None:
        cutoff = current - self.config.per_s
        while self._timestamps and self._timestamps[0] <= cutoff:
            self._timestamps.popleft()

    def peek(self, now: float | None = None) -> tuple[bool, float]:
        """Check whether a slot is available without consuming it."""
        current = time.monotonic() if now is None else now
        self._prune(current)
        if len(self._timestamps) >= self.config.requests:
            retry = max(0, self.config.per_s - (current - self._timestamps[0]))
            return False, retry
        return True, 0

    def commit(self, now: float | None = None) -> None:
        """Consume one slot. Call only after every other check has passed."""
        current = time.monotonic() if now is None else now
        self._prune(current)
        self._timestamps.append(current)


class RateLimiter:
    def __init__(
        self,
        *,
        per_user: RateLimitConfig | None = None,
        global_limit: RateLimitConfig | None = None,
        max_concurrent_per_user: int = 1,
    ) -> None:
        self.per_user = per_user or RateLimitConfig(10, 600)
        self.global_window = SlidingWindow(global_limit or RateLimitConfig(60, 600))
        self.user_windows: defaultdict[str, SlidingWindow] = defaultdict(
            lambda: SlidingWindow(self.per_user)
        )
        self.max_concurrent_per_user = max_concurrent_per_user
        self._active: defaultdict[str, int] = defaultdict(int)

    def acquire(self, user_id: str) -> tuple[bool, float]:
        if self._active[user_id] >= self.max_concurrent_per_user:
            return False, 1
        now = time.monotonic()
        user_window = self.user_windows[user_id]
        # Peek both windows before committing either: a request rejected by the
        # global limit must not still consume the user's per-user quota slot.
        user_ok, user_retry = user_window.peek(now)
        if not user_ok:
            return False, user_retry
        global_ok, global_retry = self.global_window.peek(now)
        if not global_ok:
            return False, global_retry
        user_window.commit(now)
        self.global_window.commit(now)
        self._active[user_id] += 1
        return True, 0

    def release(self, user_id: str) -> None:
        if self._active[user_id] > 0:
            self._active[user_id] -= 1


def clusters_for_channel(channel_id: str, mapping: dict[str, list[str]]) -> list[str]:
    return list(mapping.get(channel_id, ()))
