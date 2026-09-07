"""Bounded process-local admission controls for the closed alpha API."""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable


class FixedWindowRateLimiter:
    """Bound memory while limiting one API instance by trusted peer address."""

    def __init__(
        self,
        requests_per_minute: int,
        *,
        max_peers: int = 4096,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        if requests_per_minute < 1 or max_peers < 1:
            raise ValueError("rate-limit bounds must be positive")
        self._limit = requests_per_minute
        self._max_peers = max_peers
        self._now = now
        self._windows: OrderedDict[str, tuple[int, int]] = OrderedDict()

    def allow(self, peer: str) -> bool:
        minute = int(self._now() // 60)
        previous_minute, count = self._windows.pop(peer, (minute, 0))
        if previous_minute != minute:
            count = 0
        allowed = count < self._limit
        self._windows[peer] = (minute, count + 1 if allowed else count)
        while len(self._windows) > self._max_peers:
            self._windows.popitem(last=False)
        return allowed
