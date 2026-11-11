"""Price / valuation snapshot via yfinance (unofficial; failures are per-ticker)."""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

from radar.models import Quote

log = logging.getLogger(__name__)


def _num(v: Any) -> float | None:
    try:
        f = float(v) if v is not None else None
        return None if f is None or f != f else f   # NaN → None
    except (TypeError, ValueError):
        return None


def fetch_quotes(tickers: list[str], now: datetime,
                 ticker_factory: Callable[[str], Any] | None = None) -> list[Quote]:
    if ticker_factory is None:
        import yfinance as yf

        ticker_factory = yf.Ticker
    out: list[Quote] = []
    for sym in tickers:
        try:
            t = ticker_factory(sym)
            fi = t.fast_info
            price = _num(fi["lastPrice"])
            if price is None:
                raise ValueError("no lastPrice")
            try:
                pe = _num(t.info.get("trailingPE"))
            except Exception:  # .info is slow/flaky; P/E is best-effort
                pe = None
            out.append(Quote(sym, now, price, _num(fi["previousClose"]), pe,
                             _num(fi["yearHigh"]), _num(fi["yearLow"])))
        except Exception as e:
            log.warning("quote failed for %s: %s", sym, e)
    return out


def backfill_history(conn, tickers: list[str], ticker_factory: Callable[[str], Any] | None = None,
                     period: str = "1y", min_age_days: int = 8) -> int:
    """Insert daily closes for tickers lacking history older than `min_age_days`.

    Needed so week-over-week change and charts work on a fresh database.
    """
    from datetime import timedelta, timezone

    from radar import db

    if ticker_factory is None:
        import yfinance as yf

        ticker_factory = yf.Ticker
    cutoff = datetime.now(timezone.utc) - timedelta(days=min_age_days)
    n = 0
    for sym in tickers:
        if db.last_price_before(conn, sym, cutoff) is not None:
            continue
        try:
            hist = ticker_factory(sym).history(period=period, interval="1d", auto_adjust=False)
        except Exception as e:
            log.warning("history failed for %s: %s", sym, e)
            continue
        prev = None
        for ts, row in hist.iterrows():
            close = _num(row.get("Close"))
            if close is None:
                continue
            db.insert_quote(conn, Quote(sym, ts.to_pydatetime().astimezone(timezone.utc), close, prev))
            prev = close
            n += 1
    conn.commit()
    return n
