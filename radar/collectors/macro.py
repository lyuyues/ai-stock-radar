"""Macro actuals (FRED, keyless CSV) and expectations (Kalshi, Polymarket)."""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from radar.db import _ts
from radar.http import Getter

log = logging.getLogger(__name__)

FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}&cosd={start}"
KALSHI_EVENTS = "https://api.elections.kalshi.com/trade-api/v2/events"
POLY_EVENTS = "https://gamma-api.polymarket.com/events"


# ---- FRED ----

def parse_fred_csv(text: str) -> list[tuple[str, float]]:
    out = []
    for line in text.strip().splitlines()[1:]:
        d, _, v = line.partition(",")
        try:
            out.append((d, float(v)))
        except ValueError:  # "." = missing
            continue
    return out


def collect_fred(conn: sqlite3.Connection, series: list[str], get: Getter, today: date,
                 history_days: int = 800) -> int:
    start = (today - timedelta(days=history_days)).isoformat()
    n = 0
    for s in series:
        try:
            rows = parse_fred_csv(get(FRED_CSV.format(series=s, start=start)).text)
        except Exception as e:
            log.warning("FRED %s failed: %s", s, e)
            continue
        conn.executemany("INSERT OR REPLACE INTO macro_obs VALUES (?,?,?)", [(s, d, v) for d, v in rows])
        n += len(rows)
    conn.commit()
    return n


def last_obs(conn: sqlite3.Connection, series: str, n: int = 2) -> list[tuple[str, float]]:
    rows = conn.execute(
        "SELECT date, value FROM macro_obs WHERE series=? ORDER BY date DESC LIMIT ?", (series, n)
    ).fetchall()
    return [(r["date"], r["value"]) for r in rows[::-1]]


# ---- prediction markets ----

@dataclass(frozen=True)
class Market:
    source: str
    market_id: str
    event_id: str
    event_title: str
    label: str
    close_time: str | None
    prob: float


def _f(v) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def parse_kalshi_events(data: dict) -> list[Market]:
    out = []
    for e in data.get("events", []):
        title = e.get("title") or e["event_ticker"]
        if e.get("sub_title"):
            title = f"{title}（{e['sub_title']}）"
        for m in e.get("markets", []):
            prob = _f(m.get("last_price_dollars"))
            bid, ask = _f(m.get("yes_bid_dollars")), _f(m.get("yes_ask_dollars"))
            if bid is not None and ask is not None and ask > 0 and ask - bid <= 0.1:
                prob = (bid + ask) / 2  # tight book: mid is fresher than last trade
            if prob is None:
                continue
            out.append(Market("kalshi", m["ticker"], e["event_ticker"], title,
                              m.get("yes_sub_title") or m["ticker"], m.get("close_time"), prob))
    return out


def parse_polymarket_events(data: list) -> list[Market]:
    out = []
    for e in data:
        for m in e.get("markets", []):
            if m.get("closed"):
                continue
            try:
                prob = float(json.loads(m.get("outcomePrices") or "[]")[0])
            except (ValueError, IndexError, TypeError):
                continue
            out.append(Market("polymarket", str(m["id"]), str(e.get("slug") or e["id"]), e.get("title", ""),
                              m.get("groupItemTitle") or m.get("question") or "", m.get("endDate"), prob))
    return out


def fetch_markets(cfg_markets: dict, get: Getter) -> list[Market]:
    out: list[Market] = []
    for series in cfg_markets.get("kalshi_series", []):
        try:
            data = get(f"{KALSHI_EVENTS}?series_ticker={series}&status=open&with_nested_markets=true"
                       f"&limit={cfg_markets.get('kalshi_max_events', 3)}").json()
            out += parse_kalshi_events(data)
        except Exception as e:
            log.warning("kalshi %s failed: %s", series, e)
    for tag in cfg_markets.get("polymarket_tags", []):
        try:
            data = get(f"{POLY_EVENTS}?tag_slug={tag}&closed=false&active=true"
                       f"&limit={cfg_markets.get('polymarket_max_events', 10)}").json()
            out += parse_polymarket_events(data)
        except Exception as e:
            log.warning("polymarket %s failed: %s", tag, e)
    return out


def store_markets(conn: sqlite3.Connection, markets: list[Market], now: datetime) -> None:
    ts = _ts(now)
    conn.executemany(
        "INSERT OR REPLACE INTO predictions VALUES (?,?,?,?,?,?,?,?)",
        [(m.source, m.market_id, ts, m.event_id, m.event_title, m.label, m.close_time, m.prob) for m in markets],
    )
    conn.commit()


def shifts(conn: sqlite3.Connection, markets: list[Market], now: datetime, hours: float = 24) -> list[dict]:
    """Current prob vs the latest snapshot at least `hours` old (None if no history yet)."""
    cutoff = _ts(now - timedelta(hours=hours))
    out = []
    for m in markets:
        row = conn.execute(
            "SELECT prob FROM predictions WHERE source=? AND market_id=? AND ts<=? ORDER BY ts DESC LIMIT 1",
            (m.source, m.market_id, cutoff),
        ).fetchone()
        out.append({"source": m.source, "market_id": m.market_id, "event_title": m.event_title,
                    "label": m.label, "prob": m.prob, "prob_before": row["prob"] if row else None})
    return out


def implied_rate(strikes: list[tuple[float, float]]) -> float | None:
    """Expected rate from an 'above X%' ladder [(X, P(rate > X))].

    Rates move in discrete steps, so "above x_i" means "at least x_i + step":
    E[r] = x_1 + Σ_i P(r > x_i) · step_i, where step_i is the spacing to the next strike
    (the last strike reuses the previous spacing). Assumes P(r > x_1) covers the floor.
    """
    pts = sorted(strikes)
    if len(pts) < 2:
        return None
    est = pts[0][0]
    for i, (x, p) in enumerate(pts):
        step = (pts[i + 1][0] - x) if i + 1 < len(pts) else (x - pts[i - 1][0])
        est += p * step
    return est


# ---- earnings (yfinance) ----

def fetch_earnings(tickers: list[str], ticker_factory=None) -> list[dict]:
    if ticker_factory is None:
        import yfinance as yf

        ticker_factory = yf.Ticker
    out = []
    for sym in tickers:
        try:
            df = ticker_factory(sym).earnings_dates
        except Exception as e:
            log.warning("earnings failed for %s: %s", sym, e)
            continue
        if df is None:
            continue
        for ts, row in df.iterrows():
            out.append({
                "ticker": sym, "date": ts.date().isoformat(),
                "eps_est": _nan(row.get("EPS Estimate")), "eps_actual": _nan(row.get("Reported EPS")),
                "surprise_pct": _nan(row.get("Surprise(%)")),
            })
    return out


def _nan(v) -> float | None:
    v = _f(v)
    return None if v is None or v != v else v


def store_earnings(conn: sqlite3.Connection, rows: list[dict]) -> list[dict]:
    """Upsert; return rows whose actual EPS is newly known."""
    newly = []
    for r in rows:
        prev = conn.execute("SELECT eps_actual FROM earnings WHERE ticker=? AND date=?",
                            (r["ticker"], r["date"])).fetchone()
        if r["eps_actual"] is not None and (prev is None or prev["eps_actual"] is None):
            newly.append(r)
        conn.execute("INSERT OR REPLACE INTO earnings VALUES (?,?,?,?,?)",
                     (r["ticker"], r["date"], r["eps_est"], r["eps_actual"], r["surprise_pct"]))
    conn.commit()
    return newly
