"""Presentation helpers (pure)."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo


def quote_view(ticker: str, row) -> dict:
    """Flatten a quotes row into display fields; row may be None (no data yet)."""
    if row is None:
        return {"ticker": ticker, "price": None}
    price, prev, high = row["price"], row["prev_close"], row["high52"]
    return {
        "ticker": ticker,
        "price": price,
        "pct": (price / prev - 1) * 100 if prev else None,
        "pe": row["pe"],
        "from_high": (price / high - 1) * 100 if high else None,
        "ts": row["ts"],
    }


def sparkline_points(values: list[float], width: int = 320, height: int = 80) -> str:
    """SVG polyline points scaled to the box; empty string if <2 values."""
    if len(values) < 2:
        return ""
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1.0
    step = width / (len(values) - 1)
    return " ".join(
        f"{i * step:.1f},{height - (v - lo) / span * height:.1f}" for i, v in enumerate(values)
    )


def local_time(iso: str, tz: str) -> str:
    return datetime.fromisoformat(iso).astimezone(ZoneInfo(tz)).strftime("%m-%d %H:%M")
