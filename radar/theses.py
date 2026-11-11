"""Investment theses, editable on the dashboard (/theses). Seeded with starter text marked as default."""
from __future__ import annotations

import sqlite3
from datetime import datetime

from radar.db import _ts

GENERAL = "_GENERAL"

_T = "买入逻辑：{why}\n关键指标：{watch}\n逻辑破坏（考虑减仓）：{breaks}"

DEFAULTS: dict[str, str] = {
    GENERAL: "长期持有优质公司。\n"
             "- 单一个股不超过总仓位 15%；科技/半导体合计设上限。\n"
             "- 财报后先看指引和逻辑是否变化，不因单日涨跌操作。\n"
             "- 加仓前写下理由和退出条件。",
}

TEMPLATE = _T.format(why="（为什么买 / 关注）", watch="（看哪些数据）", breaks="（什么情况下逻辑不成立）")


def seed(conn: sqlite3.Connection, tickers: list[str], now: datetime) -> None:
    """Fill in starter text for tickers without a thesis. Never overwrites."""
    for t in [GENERAL, *tickers]:
        conn.execute("INSERT OR IGNORE INTO theses VALUES (?,?,?,1)", (t, DEFAULTS.get(t, TEMPLATE), _ts(now)))
    conn.commit()


def save(conn: sqlite3.Connection, ticker: str, body: str, now: datetime) -> None:
    conn.execute("INSERT OR REPLACE INTO theses VALUES (?,?,?,0)", (ticker, body.strip()[:5000], _ts(now)))
    conn.commit()


def all_theses(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    return {r["ticker"]: r for r in conn.execute("SELECT * FROM theses")}


def for_prompt(conn: sqlite3.Connection, tickers: list[str]) -> list[str]:
    th = all_theses(conn)
    out = []
    for t in [GENERAL, *tickers]:
        r = th.get(t)
        if r is None or r["body"] == TEMPLATE:
            continue
        name = "总体原则" if t == GENERAL else t
        out.append(f"### {name}{'（默认值，用户尚未修改）' if r['is_default'] else ''}\n{r['body']}")
    return out
