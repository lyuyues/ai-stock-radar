"""Company profiles: name, sector/industry (yfinance) + a Chinese name and one-line description (Claude),
so unfamiliar tickers on the dashboard explain themselves. Filled for tracked tickers and candidates."""
from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import Callable
from datetime import datetime
from typing import Any

from radar.db import _ts
from radar.weekly import Runner

log = logging.getLogger(__name__)

PROMPT = """下面是一些美股代码和它们的英文公司简介。为每家公司写：中文名（常用译名，没有就保留英文名） 和 一句话说明它是做什么的（≤25 字，让不熟悉的人一眼看懂；ETF 就说跟踪什么）。
严格每行一个，格式：代码 | 中文名 | 一句话
不要其他内容。

{items}"""
LINE = re.compile(r"^\s*([A-Z][A-Z0-9.\-]{0,9})\s*[|｜]\s*(.+?)\s*[|｜]\s*(.+?)\s*$", re.M)


def fetch_info(tickers: list[str], ticker_factory: Callable[[str], Any] | None = None) -> dict[str, dict]:
    if ticker_factory is None:
        import yfinance as yf

        ticker_factory = yf.Ticker
    out = {}
    for t in tickers:
        try:
            i = ticker_factory(t).info or {}
        except Exception as e:
            log.warning("profile info failed %s: %s", t, e)
            continue
        out[t] = {"name": i.get("longName") or i.get("shortName") or t, "sector": i.get("sector") or "",
                  "industry": i.get("industry") or i.get("category") or i.get("quoteType") or "",
                  "summary": (i.get("longBusinessSummary") or "")[:500]}
    return out


def missing(conn: sqlite3.Connection, tickers: list[str]) -> list[str]:
    have = {r["ticker"] for r in conn.execute("SELECT ticker FROM profiles WHERE desc_zh IS NOT NULL")}
    return [t for t in dict.fromkeys(tickers) if t not in have]


def fill(conn: sqlite3.Connection, tickers: list[str], now: datetime, runner: Runner | None,
         ticker_factory=None, batch: int = 40) -> int:
    todo = missing(conn, tickers)
    done = 0
    for i in range(0, len(todo), batch):
        chunk = todo[i:i + batch]
        info = fetch_info(chunk, ticker_factory)
        zh: dict[str, tuple[str, str]] = {}
        if runner is not None and info:
            items = "\n".join(f"- {t}（{v['name']}；{v['sector']} / {v['industry']}）：{v['summary']}" for t, v in info.items())
            try:
                zh = {m.group(1): (m.group(2), m.group(3)) for m in LINE.finditer(runner(PROMPT.format(items=items)))}
            except Exception as e:
                log.warning("profile descriptions failed: %s", e)
        for t, v in info.items():
            name_zh, desc = zh.get(t, (None, None))
            conn.execute("INSERT OR REPLACE INTO profiles VALUES (?,?,?,?,?,?,?)",
                         (t, v["name"], name_zh, v["sector"], v["industry"], desc, _ts(now)))
            done += desc is not None
        conn.commit()
    return done


def lookup(conn: sqlite3.Connection, tickers: list[str] | None = None) -> dict[str, dict]:
    rows = conn.execute("SELECT * FROM profiles").fetchall()
    want = set(tickers) if tickers is not None else None
    return {r["ticker"]: dict(r) for r in rows if want is None or r["ticker"] in want}
