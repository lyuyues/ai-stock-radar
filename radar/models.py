"""Core value types shared by collectors, rules and storage."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

# Ordered from least to most severe.
LEVELS = ("log", "notice", "urgent")


@dataclass(frozen=True)
class Quote:
    ticker: str
    ts: datetime
    price: float
    prev_close: float | None = None
    pe: float | None = None
    high52: float | None = None
    low52: float | None = None

    @property
    def pct_change(self) -> float | None:
        """Day change in percent vs previous close."""
        if not self.prev_close:
            return None
        return (self.price / self.prev_close - 1) * 100


@dataclass(frozen=True)
class Fire:
    """A triggered alert rule, before delivery policy is applied."""

    rule_id: str
    ticker: str
    level: str
    title: str
    body: str
    once: bool = False  # one-off event: never re-fire once recorded (cooldown is irrelevant)
