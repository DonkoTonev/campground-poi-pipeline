"""Client-side rate limiting for free-tier quotas: requests/minute, tokens/minute, requests/day.

Waiting here is cheaper and politer than collecting 429s. The daily budget includes calls
already made today (read from the ledger by the caller), and running out stops the run
cleanly instead of failing request by request.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field


class DailyQuotaExhaustedError(RuntimeError):
    """The daily request budget is used up; resume after the provider's daily reset."""


@dataclass
class RateLimiter:
    rpm: int
    tpm: int
    rpd: int
    used_today: int = 0
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    _requests: deque[float] = field(default_factory=deque)
    _tokens: deque[tuple[float, int]] = field(default_factory=deque)

    def _trim(self, now: float) -> None:
        while self._requests and now - self._requests[0] >= 60:
            self._requests.popleft()
        while self._tokens and now - self._tokens[0][0] >= 60:
            self._tokens.popleft()

    def acquire(self, tokens: int) -> float:
        """Block until a request of `tokens` input tokens fits; returns seconds waited."""
        if self.used_today >= self.rpd:
            raise DailyQuotaExhaustedError(f"daily request budget of {self.rpd} used")
        waited = 0.0
        while True:
            now = self.clock()
            self._trim(now)
            in_window = sum(t for _, t in self._tokens)
            wait = 0.0
            if len(self._requests) >= self.rpm:
                wait = max(wait, 60 - (now - self._requests[0]))
            if self._tokens and in_window + tokens > self.tpm:
                wait = max(wait, 60 - (now - self._tokens[0][0]))
            if wait <= 0:
                break
            self.sleep(wait)
            waited += wait
        now = self.clock()
        self._requests.append(now)
        self._tokens.append((now, tokens))
        self.used_today += 1
        return waited
