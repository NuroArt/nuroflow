"""Retry backoff policy (ADR-0003).

delay(n) = min(cap, base · 2^(n-1)), then "equal jitter": half fixed, half random.

Why equal jitter rather than full jitter (``uniform(0, delay)``)? Full jitter spreads
load best, but it can retry almost immediately, which is pointless against a
rate-limited LLM API. Equal jitter guarantees at least ``delay/2`` of rest while still
de-synchronising a thundering herd of failed tasks.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import timedelta


@dataclass(frozen=True)
class RetryPolicy:
    base_seconds: float
    max_seconds: float
    rng: random.Random = field(default_factory=random.Random, compare=False, repr=False)

    def delay(self, attempt_number: int) -> timedelta:
        """Delay before the attempt *after* ``attempt_number`` (1-based)."""
        if attempt_number < 1:
            raise ValueError("attempt_number is 1-based")
        exponent = min(attempt_number - 1, 30)  # avoid float overflow on silly inputs
        raw = min(self.max_seconds, self.base_seconds * (2**exponent))
        half = raw / 2
        return timedelta(seconds=half + self.rng.uniform(0, half))
