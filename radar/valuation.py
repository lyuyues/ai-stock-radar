"""Per-ticker valuation using the bundled DCF engine (radar/dcf/builder.py).

The engine output is stored as JSON; the editable model below reproduces its projection so the
dashboard can recompute fair value from user-adjusted assumptions (same math in valuation.js).

Model (10 years):
  - analyst years: revenue = consensus estimate (yfinance "0y", "+1y")
  - remaining years: revenue growth tapers linearly from `g_start` to terminal growth
  - FCF = revenue × FCF margin; discounted at required return; Gordon terminal value
  - fair value = (Σ PV(FCF) + PV(terminal)) / shares
"""
from __future__ import annotations

import json
import logging
import math
import sqlite3
from datetime import datetime

from radar.db import _ts

log = logging.getLogger(__name__)
YEARS = 10


def _clean(v):
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return v if isinstance(v, str) else None
    return None if math.isnan(f) or math.isinf(f) else f


def _table(df) -> dict:
    """DataFrame (metrics × years) → {"cols": [...], "rows": {metric: [values]}}."""
    cols = [str(c) for c in df.columns]
    return {"cols": cols, "rows": {str(k): [_clean(v) for v in row] for k, row in df.iterrows()}}


def auto_inputs(hist, proj, meta) -> dict:
    """Assumptions the engine used, in the editable model's terms."""
    rev = proj.loc["Revenue (M)"].astype(float).tolist()
    fcf = proj.loc["Free Cash Flow (M)"].astype(float).tolist()
    growth = [float(g) / 100 for g in proj.loc["Growth Rate (%)"].tolist()]
    src = proj.loc["Estimate Source"].tolist()
    n_analyst = sum(s == "Analyst" for s in src)
    hist_rev = hist.loc["Revenue (M)"].astype(float)
    base_year = int(proj.columns[0]) - 1
    base_rev = float(rev[0] / (1 + growth[0]))
    return {
        "base_year": base_year, "base_rev": base_rev,
        "analyst": rev[:n_analyst],
        "g_start": growth[n_analyst] if n_analyst < len(growth) else growth[-1],
        "path": growth,  # exact engine path (3-stage when there are no analyst estimates)
        "terminal": float(meta["Terminal Growth (%)"]) / 100,
        "rr": float(meta["Required Return (%)"]) / 100,
        "fcf_margin": fcf[0] / rev[0] if rev[0] else 0.0,
        "shares": float(meta["Shares Outstanding (M)"]),
        "last_hist_rev": _clean(hist_rev.dropna().iloc[-1]) if not hist_rev.dropna().empty else None,
    }


def project(inp: dict) -> dict:
    """Recompute the DCF from (possibly user-edited) inputs."""
    rr, tg = inp["rr"], inp["terminal"]
    if rr <= tg:
        raise ValueError("必要回报率必须大于永续增长率")
    analyst = [a for a in inp.get("analyst") or [] if a]
    n = len(analyst)
    if inp.get("use_path") and inp.get("path"):
        growth = list(inp["path"])[:YEARS]
    else:
        prev = inp["base_rev"]
        growth = []
        for a in analyst:
            growth.append(a / prev - 1)
            prev = a
        rest = YEARS - n
        g0 = inp["g_start"]
        growth += [g0 + (tg - g0) * i / (rest - 1) if rest > 1 else tg for i in range(rest)] if rest > 0 else []
    revs, cur = [], inp["base_rev"]
    for i, g in enumerate(growth):
        cur = analyst[i] if i < n else cur * (1 + g)
        revs.append(cur)
    fcfs = [r * inp["fcf_margin"] for r in revs]
    pvs = [f / (1 + rr) ** (i + 1) for i, f in enumerate(fcfs)]
    tv = fcfs[-1] * (1 + tg) / (rr - tg)
    pv_tv = tv / (1 + rr) ** len(fcfs)
    ev = sum(pvs) + pv_tv
    return {"years": [inp["base_year"] + i + 1 for i in range(len(revs))], "revenue": revs, "growth": growth,
            "fcf": fcfs, "pv": pvs, "pv_terminal": pv_tv, "ev": ev,
            "fair": ev / inp["shares"] if inp["shares"] else None,
            "terminal_share": pv_tv / ev if ev else None}


def market_shares(ticker: str) -> float | None:
    """Shares (M) implied by market cap / price — the unit the quoted price is in."""
    try:
        import yfinance as yf

        fi = yf.Ticker(ticker).fast_info
        cap, price = fi["marketCap"], fi["lastPrice"]
        return cap / price / 1e6 if cap and price else None
    except Exception:
        return None


def fix_share_units(auto: dict, implied: float | None) -> str | None:
    """SEC share counts can be in another unit than the quoted share (e.g. BRK: class-A equivalents).
    If they differ by >3×, use market-implied shares so fair value is per quoted share."""
    sec = auto.get("shares")
    if not implied or not sec or 1 / 3 < sec / implied < 3:
        return None
    auto["shares"] = implied
    return f"SEC 股数 {sec:,.0f}M 与报价口径不符（如 A/B 股），改用市值/股价推算的 {implied:,.0f}M"


def refresh(conn: sqlite3.Connection, tickers: list[str], now: datetime, builder=None,
            shares_of=market_shares) -> dict[str, str]:
    """Run the engine for each ticker; store results (or the error). Returns ticker -> 'ok' / error."""
    if builder is None:
        from radar.dcf import builder
    out = {}
    for t in tickers:
        try:
            hist, proj, meta = builder.build_dcf(t)
            try:
                quarterly = _table(builder.build_quarterly_table(t, num_quarters=10))
            except Exception as e:  # quarterly is a nice-to-have
                log.warning("quarterly failed %s: %s", t, e)
                quarterly = None
            auto = auto_inputs(hist, proj, meta)
            note = fix_share_units(auto, shares_of(t))
            data = {"hist": _table(hist), "proj": _table(proj), "quarterly": quarterly,
                    "meta": {k.strip(): _clean(v) for k, v in meta.items()}, "auto": auto, "note": note}
            conn.execute("INSERT OR REPLACE INTO valuations VALUES (?,?,?,NULL)",
                         (t, _ts(now), json.dumps(data, ensure_ascii=False)))
            out[t] = "ok"
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"[:300]
            prev = conn.execute("SELECT data FROM valuations WHERE ticker=?", (t,)).fetchone()
            conn.execute("INSERT OR REPLACE INTO valuations VALUES (?,?,?,?)",
                         (t, _ts(now), prev["data"] if prev else None, msg))
            out[t] = msg
            log.warning("valuation failed %s: %s", t, msg)
        conn.commit()
    return out


def load(conn: sqlite3.Connection, ticker: str) -> dict | None:
    r = conn.execute("SELECT * FROM valuations WHERE ticker=?", (ticker,)).fetchone()
    if r is None:
        return None
    mine = conn.execute("SELECT json, updated FROM valuation_inputs WHERE ticker=?", (ticker,)).fetchone()
    return {"ticker": ticker, "updated": r["updated"], "error": r["error"],
            "data": json.loads(r["data"]) if r["data"] else None,
            "mine": json.loads(mine["json"]) if mine else None, "mine_updated": mine["updated"] if mine else None}


def save_inputs(conn: sqlite3.Connection, ticker: str, inputs: dict | None, now: datetime) -> None:
    if inputs is None:
        conn.execute("DELETE FROM valuation_inputs WHERE ticker=?", (ticker,))
    else:
        conn.execute("INSERT OR REPLACE INTO valuation_inputs VALUES (?,?,?)",
                     (ticker, json.dumps(inputs), _ts(now)))
    conn.commit()


EDITABLE = ("rr", "terminal", "fcf_margin", "shares", "g_start", "analyst")


def merged(auto: dict, mine: dict | None) -> dict:
    inp = dict(auto)
    if mine:
        inp.update({k: v for k, v in mine.items() if k in EDITABLE})
        inp["use_path"] = False
    else:
        inp["use_path"] = True  # untouched → exactly the engine's own path
    return inp


def summary(conn: sqlite3.Connection, tickers: list[str]) -> list[dict]:
    out = []
    for t in tickers:
        v = load(conn, t)
        q = conn.execute("SELECT price, pe FROM quotes WHERE ticker=? ORDER BY ts DESC LIMIT 1", (t,)).fetchone()
        row = {"ticker": t, "price": q["price"] if q else None, "pe": q["pe"] if q else None,
               "error": v["error"] if v else "尚未计算", "updated": v["updated"] if v else None,
               "auto": None, "mine": None}
        if v and v["data"]:
            a = v["data"]["auto"]
            try:
                row["auto"] = project(merged(a, None))["fair"]
                row["mine"] = project(merged(a, v["mine"]))["fair"] if v["mine"] else None
            except (ValueError, ZeroDivisionError) as e:
                row["error"] = str(e)
            row["fcf_margin"] = a["fcf_margin"]
            row["g1"] = (a["analyst"][0] / a["base_rev"] - 1) if a["analyst"] else a["path"][0]
        fair = row["mine"] if row["mine"] is not None else row["auto"]
        row["negative"] = fair is not None and fair <= 0
        row["upside"] = (fair / row["price"] - 1) * 100 if fair and fair > 0 and row["price"] else None
        out.append(row)
    return out
