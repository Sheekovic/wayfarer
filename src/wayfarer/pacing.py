"""Per-host request pacing and retry timing."""

from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Callable, Dict, FrozenSet, Optional, Tuple, Union

Delay = Union[float, Tuple[float, float]]


class HostPacer:
    """Spaces out requests to the same host.

    ``reserve(host)`` books the next slot for that host and returns how long
    the caller must wait before sending. The first request to a host never
    waits. Safe to share between threads and asyncio tasks.
    """

    def __init__(
        self,
        delay: Delay = (0.5, 1.5),
        *,
        clock: Callable[[], float] = time.monotonic,
        rng: Callable[[float, float], float] = random.uniform,
    ) -> None:
        if isinstance(delay, (int, float)):
            delay = (float(delay), float(delay))
        lo, hi = delay
        if lo < 0 or hi < lo:
            raise ValueError("delay must be a non-negative number or a (low, high) range")
        self._range = (lo, hi)
        self._clock = clock
        self._rng = rng
        self._next: Dict[str, float] = {}
        self._floor: Dict[str, float] = {}
        self._lock = threading.Lock()

    def set_floor(self, host: str, seconds: float) -> None:
        """Never go faster than ``seconds`` for this host (e.g. robots Crawl-delay)."""
        with self._lock:
            self._floor[host] = max(seconds, self._floor.get(host, 0.0))

    def reserve(self, host: str) -> float:
        with self._lock:
            now = self._clock()
            ready = max(now, self._next.get(host, now))
            gap = max(self._rng(*self._range), self._floor.get(host, 0.0))
            self._next[host] = ready + gap
            return ready - now


def parse_retry_after(value: Optional[str], *, now: Optional[datetime] = None) -> Optional[float]:
    """Seconds requested by a Retry-After header, or None if absent/unparseable."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return max(0.0, (when - now).total_seconds())


@dataclass(frozen=True)
class RetryPolicy:
    """When and how long to wait before retrying a request.

    Only idempotent methods are retried. A server-sent Retry-After is
    honoured; if it asks for longer than ``max_retry_after`` the response is
    returned to the caller instead of blocking.
    """

    attempts: int = 3
    backoff: float = 1.0
    max_backoff: float = 30.0
    max_retry_after: float = 60.0
    statuses: FrozenSet[int] = field(default_factory=lambda: frozenset({429, 502, 503, 504}))
    methods: FrozenSet[str] = field(default_factory=lambda: frozenset({"GET", "HEAD", "OPTIONS"}))

    def wait_before_retry(
        self,
        attempt: int,
        method: str,
        *,
        status: Optional[int] = None,
        retry_after: Optional[str] = None,
    ) -> Optional[float]:
        """Seconds to wait before the next try, or None to stop.

        ``attempt`` counts tries already made (1 after the first).
        ``status`` is None when the try failed with a network error.
        """
        if attempt >= self.attempts or method.upper() not in self.methods:
            return None
        if status is not None and status not in self.statuses:
            return None
        asked = parse_retry_after(retry_after)
        if asked is not None:
            return asked if asked <= self.max_retry_after else None
        ceiling = min(self.max_backoff, self.backoff * 2 ** (attempt - 1))
        return random.uniform(ceiling / 2, ceiling)
