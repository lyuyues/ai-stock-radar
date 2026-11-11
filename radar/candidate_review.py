"""After discovery: run the DCF engine on the top candidates and have Claude write a one-line
"why look / main risk" for each."""
from __future__ import annotations

import logging
import re
import sqlite3
from datetime import datetime

from radar import db, valuation
from radar.weekly import Runner, _fmt

log = logging.getLogger(__name__)

PROMPT = """你是用户的投资研究助理。下面是本周 Invest Radar 发现的候选股票（用户还没跟踪），每只附带命中的渠道和 DCF 估值。
为每只写一句点评。可以用网络搜索快速核实最新情况（只查必要的，别展开）。不编造数字，不给买卖指令。

严格按下面格式，每只一行，不要别的内容：
代码 | 值得看的理由（≤40字） | 主要风险（≤30字）

她的投资原则：
{principles}

# 候选
{cands}"""

LINE = re.compile(r"^\s*([A-Z][A-Z0-9.\-]{0,9})\s*[|｜]\s*(.+?)\s*[|｜]\s*(.+?)\s*$", re.M)


def pick(rows: list[dict], tracked: set[str], n: int) -> list[dict]:
    """Highest-scoring untracked candidates (rows are already sorted by score)."""
    return [r for r in rows if r["ticker"] not in tracked][:n]


def build_prompt(conn: sqlite3.Connection, cands: list[dict], val: dict[str, dict]) -> str:
    th = conn.execute("SELECT body FROM theses WHERE ticker='_GENERAL'").fetchone()
    L = []
    for c in cands:
        v = val.get(c["ticker"]) or {}
        notes = "；".join(n for ns in c["detail"].values() for n in ns[:3])
        fair = v.get("auto")
        L.append(f"- {c['ticker']}（渠道：{'+'.join(c['channels'])}）：{notes[:400]}。"
                 f"价格 {_fmt(v.get('price'))}，P/E {_fmt(v.get('pe'), '{:.1f}')}，"
                 + (f"DCF 公允价值 {fair:.2f}（上涨空间 {_fmt(v.get('upside'), '{:+.0f}%')}，FCF 利润率 {v['fcf_margin']:.0%}）"
                    if fair and fair > 0 else f"DCF：{v.get('error') or '不适用'}"))
    return PROMPT.format(principles=th["body"] if th else "（未填写）", cands="\n".join(L))


def parse(text: str) -> dict[str, tuple[str, str]]:
    return {m.group(1): (m.group(2), m.group(3)) for m in LINE.finditer(text)}


def run(conn: sqlite3.Connection, run_date: str, rows: list[dict], tracked: set[str], n: int,
        now: datetime, runner: Runner | None, fetch_quotes=None, builder=None) -> dict:
    cands = pick(rows, tracked, n)
    if not cands:
        return {"valued": 0, "reviewed": 0}
    tickers = [c["ticker"] for c in cands]
    if fetch_quotes is not None:  # price + P/E for upside (untracked tickers have no quotes yet)
        for q in fetch_quotes(tickers, now):
            db.insert_quote(conn, q)
        conn.commit()
    res = valuation.refresh(conn, tickers, now, **({"builder": builder} if builder else {}))
    val = {r["ticker"]: r for r in valuation.summary(conn, tickers)}
    reviewed = 0
    if runner is not None:
        try:
            notes = parse(runner(build_prompt(conn, cands, val)))
            for t, (why, risk) in notes.items():
                if t in tickers:
                    conn.execute("INSERT OR REPLACE INTO candidate_notes VALUES (?,?,?,?)", (run_date, t, why, risk))
                    reviewed += 1
            conn.commit()
        except Exception as e:
            log.warning("candidate review failed: %s", e)
    return {"valued": sum(v == "ok" for v in res.values()), "reviewed": reviewed}
