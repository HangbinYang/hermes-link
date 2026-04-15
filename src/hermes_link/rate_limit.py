from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass
from threading import Lock


@dataclass(slots=True)
class RateLimitExceeded(Exception):
    key: str
    retry_after_seconds: int


class InMemoryRateLimiter:
    def __init__(self) -> None:
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = Lock()

    def check(self, *, key: str, limit: int, window_seconds: int = 60) -> None:
        if limit <= 0:
            return

        now = time.monotonic()
        cutoff = now - window_seconds

        with self._lock:
            bucket = self._events[key]
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()

            if len(bucket) >= limit:
                retry_after = max(1, int(window_seconds - (now - bucket[0])))
                raise RateLimitExceeded(key=key, retry_after_seconds=retry_after)

            bucket.append(now)
