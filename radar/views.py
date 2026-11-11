"""Read-side queries shared by the web dashboard and the weekly report."""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta

from radar.collectors.macro import implied_rate
from radar.db import _ts

_STRIKE = re.compile(r"-T(\d+(?:\.\d+)?)$")


def latest_markets(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Latest snapshot per market."""
    return conn.execute(
        "SELECT p.* FROM predictions p JOIN (SELECT source, market_id, MAX(ts) AS ts FROM predictions "
        "GROUP BY source, market_id) m USING (source, market_id, ts) ORDER BY p.event_id, p.label"
    ).fetchall()


def market_changes(conn: sqlite3.Connection, now: datetime, days: float = 7) -> list[dict]:
    """Latest prob vs the latest snapshot at least `days` old, largest moves first."""
    cutoff = _ts(now - timedelta(days=days))
    out = []
    for m in latest_markets(conn):
        row = conn.execute(
            "SELECT prob FROM predictions WHERE source=? AND market_id=? AND ts<=? ORDER BY ts DESC LIMIT 1",
            (m["source"], m["market_id"], cutoff)).fetchone()
        before = row["prob"] if row else None
        out.append({"source": m["source"], "event": m["event_title"], "label": m["label"], "prob": m["prob"],
                    "before": before, "delta_pp": (m["prob"] - before) * 100 if before is not None else None})
    return sorted(out, key=lambda r: -abs(r["delta_pp"] or 0))


def _kalshi_fed_snapshot(conn, event_id: str, before_ts: str | None = None) -> tuple[float | None, str | None]:
    """Implied upper bound from the KXFED ladder at the latest snapshot (optionally before a time)."""
    q = "SELECT MAX(ts) AS ts FROM predictions WHERE source='kalshi' AND event_id=?"
    args: list = [event_id]
    if before_ts:
        q += " AND ts<=?"
        args.append(before_ts)
    ts = conn.execute(q, args).fetchone()["ts"]
    if not ts:
        return None, None
    rows = conn.execute("SELECT market_id, prob FROM predictions WHERE source='kalshi' AND event_id=? AND ts=?",
                        (event_id, ts)).fetchall()
    strikes = [(float(m.group(1)), r["prob"]) for r in rows if (m := _STRIKE.search(r["market_id"]))]
    return implied_rate(strikes), ts


def fed_path(conn: sqlite3.Connection, now: datetime) -> list[dict]:
    """Per Fed meeting (Kalshi KXFED event): market-implied upper bound; after the meeting,
    compare the last pre-meeting expectation with the actual target (FRED DFEDTARU)."""
    events = conn.execute(
        "SELECT event_id, event_title, MAX(close_time) AS close FROM predictions "
        "WHERE source='kalshi' AND event_id LIKE 'KXFED-%' GROUP BY event_id ORDER BY close"
    ).fetchall()
    out = []
    for e in events:
        close = e["close"]
        closed = close is not None and close <= _ts(now)
        implied, ts = _kalshi_fed_snapshot(conn, e["event_id"], close if closed else None)
        actual = None
        if closed:
            row = conn.execute("SELECT value FROM macro_obs WHERE series='DFEDTARU' AND date>=? ORDER BY date LIMIT 1",
                               (close[:10],)).fetchone()
            actual = row["value"] if row else None
        out.append({"event": e["event_title"], "close": close[:10] if close else None, "implied": implied,
                    "as_of": ts, "actual": actual,
                    "gap_bp": (actual - implied) * 100 if actual is not None and implied is not None else None})
    return out


def macro_table(conn: sqlite3.Connection, labels: dict[str, str], days: int = 7) -> list[dict]:
    out = []
    for series, label in labels.items():
        rows = conn.execute("SELECT date, value FROM macro_obs WHERE series=? ORDER BY date DESC LIMIT 400",
                            (series,)).fetchall()
        if not rows:
            out.append({"series": series, "label": label, "value": None})
            continue
        latest = rows[0]
        cutoff = (datetime.fromisoformat(latest["date"]) - timedelta(days=days)).date().isoformat()
        prev = next((r for r in rows if r["date"] <= cutoff), None)
        yoy = None
        if series == "CPIAUCSL":  # index level → show YoY inflation
            year_ago = next((r for r in rows if r["date"] <= f"{int(latest['date'][:4]) - 1}{latest['date'][4:]}"), None)
            yoy = (latest["value"] / year_ago["value"] - 1) * 100 if year_ago else None
        out.append({"series": series, "label": label, "date": latest["date"], "value": latest["value"],
                    "change": latest["value"] - prev["value"] if prev else None, "yoy": yoy,
                    "history": [r["value"] for r in rows[:120]][::-1]})
    return out


def week_moves(conn: sqlite3.Connection, tickers: list[str], now: datetime, days: int = 7) -> list[dict]:
    cutoff = _ts(now - timedelta(days=days))
    out = []
    for t in tickers:
        last = conn.execute("SELECT * FROM quotes WHERE ticker=? ORDER BY ts DESC LIMIT 1", (t,)).fetchone()
        if not last:
            continue
        # Need a price from before the window; otherwise "week change" would be fake (e.g. 0%).
        base = conn.execute("SELECT price FROM quotes WHERE ticker=? AND ts<? ORDER BY ts DESC LIMIT 1",
                            (t, cutoff)).fetchone()
        out.append({"ticker": t, "price": last["price"], "pe": last["pe"], "ts": last["ts"],
                    "day_pct": (last["price"] / last["prev_close"] - 1) * 100 if last["prev_close"] else None,
                    "week_pct": (last["price"] / base["price"] - 1) * 100 if base else None,
                    "from_high": (last["price"] / last["high52"] - 1) * 100 if last["high52"] else None})
    return out


def ticker_events(conn: sqlite3.Connection, ticker: str, limit: int = 20) -> dict:
    return {
        "filings": conn.execute("SELECT f.*, n.summary FROM filings f LEFT JOIN filing_notes n USING (accession) "
                                "WHERE f.ticker=? AND f.form='8-K' ORDER BY f.filed DESC LIMIT ?",
                                (ticker, limit)).fetchall(),
        "insiders": conn.execute("SELECT * FROM insider_tx WHERE ticker=? AND code IN ('P','S') "
                                 "ORDER BY tx_date DESC LIMIT ?", (ticker, limit)).fetchall(),
        "earnings": conn.execute("SELECT * FROM earnings WHERE ticker=? ORDER BY date DESC LIMIT 8",
                                 (ticker,)).fetchall(),
    }


def _close_before(conn: sqlite3.Connection, ticker: str, day: str) -> float | None:
    row = conn.execute("SELECT price FROM quotes WHERE ticker=? AND ts<? ORDER BY ts DESC LIMIT 1",
                       (ticker, day)).fetchone()
    return row["price"] if row else None


def earnings_board(conn: sqlite3.Connection, tickers: list[str], today: datetime, recent_days: int = 120) -> dict:
    """Upcoming report per tracked ticker + recent results with the stock's reaction.

    Reaction = close before the report day → close of the day after (covers both pre-market
    and after-close releases; yfinance doesn't say which)."""
    d0 = today.date()
    upcoming, recent = [], []
    for t in tickers:
        rows = conn.execute("SELECT * FROM earnings WHERE ticker=? ORDER BY date DESC", (t,)).fetchall()
        past = [r for r in rows if r["eps_actual"] is not None]
        last4 = [r["surprise_pct"] for r in past[:4] if r["surprise_pct"] is not None]
        nxt = next((r for r in reversed(rows) if r["date"] >= d0.isoformat() and r["eps_actual"] is None), None)
        if nxt:
            upcoming.append({"ticker": t, "date": nxt["date"], "eps_est": nxt["eps_est"],
                             "days": (datetime.fromisoformat(nxt["date"]).date() - d0).days,
                             "beats": sum(s > 0 for s in last4), "of": len(last4),
                             "last": past[0]["date"] if past else None})
        cutoff = (d0 - timedelta(days=recent_days)).isoformat()
        for r in past:
            if r["date"] < cutoff:
                break
            day = datetime.fromisoformat(r["date"]).date()
            before = _close_before(conn, t, day.isoformat())
            after = None
            nxt_row = conn.execute("SELECT substr(ts,1,10) AS d FROM quotes WHERE ticker=? AND ts>=? ORDER BY ts LIMIT 1",
                                   (t, (day + timedelta(days=1)).isoformat())).fetchone()
            if nxt_row and nxt_row["d"] < d0.isoformat():  # first trading day after the report has closed
                nd = datetime.fromisoformat(nxt_row["d"]).date()
                after = _close_before(conn, t, (nd + timedelta(days=1)).isoformat())
            recent.append({"ticker": t, "date": r["date"], "eps_est": r["eps_est"], "eps_actual": r["eps_actual"],
                           "surprise_pct": r["surprise_pct"],
                           "move_pct": (after / before - 1) * 100 if before and after else None})
    from radar.earnings_notes import notes_map

    notes = notes_map(conn)
    for r in recent:
        r["note"] = notes.get((r["ticker"], r["date"]))
    upcoming.sort(key=lambda r: r["date"])
    recent.sort(key=lambda r: r["date"], reverse=True)
    return {"upcoming": upcoming, "recent": recent}
