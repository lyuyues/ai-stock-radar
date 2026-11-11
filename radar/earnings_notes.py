"""Per-report earnings write-ups by Claude (web search), with event tags and importance.

Runs at the end of the daily earnings job: newly reported results first, then backfills the latest
reports of tracked tickers a few at a time."""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from radar import views
from radar.config import Config
from radar.db import _ts
from radar.weekly import Runner, _fmt

log = logging.getLogger(__name__)
PROMPT = Path(__file__).parent / "prompts" / "earnings_note.md"
TAG_RE = re.compile(r"^\s*TAGS:\s*(.*?)\s*\|\s*重要度[:：]\s*(高|中|低)\s*$", re.M)
LEVELS = {"高": 3, "中": 2, "低": 1}


def parse(text: str) -> tuple[str, list[str], str | None]:
    """Split Claude output into (markdown, tags, importance)."""
    m = None
    for m in TAG_RE.finditer(text):
        pass
    if not m:
        return text.strip(), [], None
    tags = [t.strip() for t in re.split(r"[,，、]", m.group(1)) if t.strip()][:5]
    body = (text[:m.start()] + text[m.end():]).strip()
    return body, tags, m.group(2)


def pending(conn: sqlite3.Connection, tickers: list[str], today, days: int = 120) -> list[sqlite3.Row]:
    """Reported results without a note, newest first; only the latest two per ticker."""
    q = ",".join("?" * len(tickers)) or "''"
    rows = conn.execute(
        f"SELECT e.* FROM earnings e LEFT JOIN earnings_notes n USING (ticker, date) "
        f"WHERE n.ticker IS NULL AND e.eps_actual IS NOT NULL AND e.date>=? AND e.ticker IN ({q}) "
        f"ORDER BY e.date DESC", ((today - timedelta(days=days)).isoformat(), *tickers)).fetchall()
    seen: dict[str, int] = {}
    out = []
    for r in rows:
        seen[r["ticker"]] = seen.get(r["ticker"], 0) + 1
        if seen[r["ticker"]] <= 2:
            out.append(r)
    return out


def build_input(conn: sqlite3.Connection, e: sqlite3.Row, now: datetime) -> str:
    rec = next((r for r in views.earnings_board(conn, [e["ticker"]], now, recent_days=400)["recent"]
                if r["date"] == e["date"]), None)
    th = conn.execute("SELECT body, is_default FROM theses WHERE ticker=?", (e["ticker"],)).fetchone()
    filing = conn.execute("SELECT url FROM filings WHERE ticker=? AND form='8-K' AND items LIKE '%2.02%' "
                          "AND filed BETWEEN ? AND ? ORDER BY filed LIMIT 1",
                          (e["ticker"], e["date"], (datetime.fromisoformat(e["date"]) + timedelta(days=3)).date().isoformat())
                          ).fetchone()
    L = [f"- 标的：{e['ticker']}", f"- 财报日期：{e['date']}（Yahoo Finance 记录，盘前/盘后未区分）",
         f"- EPS：实际 {_fmt(e['eps_actual'])} vs 预期 {_fmt(e['eps_est'])}（{_fmt(e['surprise_pct'], '{:+.1f}%')}）",
         f"- 股价反应（财报前一交易日收盘 → 财报后第一个交易日收盘）："
         f"{_fmt(rec['move_pct'] if rec else None, '{:+.1f}%', '尚无数据')}"]
    if filing:
        L.append(f"- SEC 8-K（2.02 业绩公告）：{filing['url']}")
    L += ["", "## 投资逻辑" + ("（默认模板，用户未修改）" if th and th["is_default"] else ""),
          th["body"] if th else "（无）"]
    return "\n".join(L)


def generate(conn: sqlite3.Connection, e: sqlite3.Row, now: datetime, runner: Runner) -> dict:
    body, tags, level = parse(runner(PROMPT.read_text() + "\n" + build_input(conn, e, now)))
    conn.execute("INSERT OR REPLACE INTO earnings_notes VALUES (?,?,?,?,?,?)",
                 (e["ticker"], e["date"], body, json.dumps(tags, ensure_ascii=False), level, _ts(now)))
    conn.commit()
    return {"ticker": e["ticker"], "date": e["date"], "tags": tags, "level": level}


def run(conn: sqlite3.Connection, cfg: Config, now: datetime, runner: Runner, limit: int = 10) -> list[dict]:
    done = []
    for e in pending(conn, cfg.all_tickers, now.date())[:limit]:
        try:
            done.append(generate(conn, e, now, runner))
        except Exception as ex:  # one failure must not block the rest
            log.warning("earnings note failed %s %s: %s", e["ticker"], e["date"], ex)
    return done


def notes_map(conn: sqlite3.Connection) -> dict[tuple[str, str], dict]:
    return {(r["ticker"], r["date"]): {"markdown": r["markdown"], "tags": json.loads(r["tags"] or "[]"),
                                       "level": r["level"], "created": r["created"]}
            for r in conn.execute("SELECT * FROM earnings_notes")}
