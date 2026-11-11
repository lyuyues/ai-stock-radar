"""Weekly report: build a data bundle from the DB, have Claude (headless) write the report."""
from __future__ import annotations

import logging
import os
import shlex
import sqlite3
import subprocess
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from radar import views
from radar.collectors.edgar import item_labels
from radar.config import Config
from radar.db import _ts
from radar.discovery import latest_candidates

log = logging.getLogger(__name__)
PROMPT = Path(__file__).parent / "prompts" / "weekly.md"
DEFAULT_CLAUDE = "claude -p --output-format text --allowedTools WebSearch,WebFetch,mcp__financial-datesets"

Runner = Callable[[str], str]


def _fmt(v, spec="{:.2f}", none="—"):
    return none if v is None else spec.format(v)


def _theses(cfg: Config, tickers: list[str]) -> list[str]:
    if not cfg.theses_dir or not cfg.theses_dir.is_dir():
        return ["（未配置 THESES_DIR）"]
    out = []
    for p in sorted(cfg.theses_dir.glob("*.md")):
        if p.name.startswith("_"):
            continue
        ticker_part = p.name.split("-")[0].upper()
        if any(t in ticker_part.split("_") or t == ticker_part for t in tickers) or ticker_part == "LESSONS":
            text = p.read_text(errors="ignore").strip()
            out.append(f"### {p.name}\n{text[:1500]}{' …' if len(text) > 1500 else ''}")
    return out or ["（没有匹配的 thesis 文件）"]


def build_bundle(conn: sqlite3.Connection, cfg: Config, now: datetime) -> str:
    week_ago = now - timedelta(days=7)
    tz = ZoneInfo(cfg.tz)
    L: list[str] = [f"生成时间：{now.astimezone(tz):%Y-%m-%d %H:%M} ({cfg.tz})", ""]

    L += ["## 持仓 & 关注", "| 标的 | 层级 | 价格 | 价格时间(UTC) | 当日(vs 前收) | 周涨跌 | P/E | 距52周高 |",
          "|---|---|---|---|---|---|---|---|"]
    for m in views.week_moves(conn, cfg.all_tickers, now):
        L.append(f"| {m['ticker']} | {'持仓' if cfg.tier_of(m['ticker']) == 'holdings' else '关注'} | "
                 f"{m['price']:.2f} | {m['ts'][:16]} | {_fmt(m['day_pct'], '{:+.1f}%')} | "
                 f"{_fmt(m['week_pct'], '{:+.1f}%')} | {_fmt(m['pe'], '{:.1f}')} | "
                 f"{_fmt(m['from_high'], '{:+.0f}%')} |")

    since = _ts(week_ago)
    L += ["", "## 本周提醒"]
    alerts = conn.execute("SELECT * FROM alerts WHERE ts>=? ORDER BY ts", (since,)).fetchall()
    L += [f"- [{a['level']}] {a['ts'][:10]} {a['title']} — {a['body'][:160]}" for a in alerts] or ["- 无"]

    L += ["", "## 8-K 重大事件（本周）"]
    for f in conn.execute("SELECT f.*, n.summary FROM filings f LEFT JOIN filing_notes n USING (accession) WHERE f.form='8-K' AND f.filed>=? ORDER BY f.filed",
                          (week_ago.date().isoformat(),)):
        L.append(f"- {f['filed']} {f['ticker']}：{item_labels(f['items'] or '')}{('：' + f['summary']) if f['summary'] else ''} {f['url']}")
    if L[-1].startswith("## 8-K"):
        L.append("- 无")

    L += ["", "## 内部人交易（本周，公开市场买卖）"]
    rows = conn.execute("SELECT ticker, code, COUNT(*) n, SUM(value) v, GROUP_CONCAT(DISTINCT owner) owners "
                        "FROM insider_tx WHERE tx_date>=? AND code IN ('P','S') GROUP BY ticker, code",
                        (week_ago.date().isoformat(),)).fetchall()
    L += [f"- {r['ticker']} {'买入' if r['code'] == 'P' else '卖出'} {r['n']} 笔 ${(r['v'] or 0) / 1e6:.2f}M（{r['owners']}）"
          for r in rows] or ["- 无"]

    L += ["", "## 财报"]
    for e in conn.execute("SELECT * FROM earnings WHERE date>=? AND date<=? ORDER BY date",
                          (week_ago.date().isoformat(), (now + timedelta(days=14)).date().isoformat())):
        if e["eps_actual"] is not None:
            L.append(f"- 已发布 {e['date']} {e['ticker']}：EPS {e['eps_actual']} vs 预期 {e['eps_est']}（{_fmt(e['surprise_pct'], '{:+.1f}%')}）")
        else:
            L.append(f"- 即将发布 {e['date']} {e['ticker']}：预期 EPS {e['eps_est']}")
    if L[-1] == "## 财报":
        L.append("- 无")
    for n in conn.execute("SELECT * FROM earnings_notes WHERE date>=? ORDER BY date", (week_ago.date().isoformat(),)):
        L.append(f"\n### 财报摘要 {n['ticker']} {n['date']}（重要度 {n['level']}）\n{n['markdown'][:1500]}")

    from radar import valuation
    L += ["", "## 估值（DCF，用户的估值引擎；我的 = 用户保存的假设）", "| 标的 | 价格 | 公允价值·自动 | 公允价值·我的 | 上涨空间 |", "|---|---|---|---|---|"]
    for r in valuation.summary(conn, cfg.all_tickers):
        if r["auto"] is not None:
            L.append(f"| {r['ticker']} | {_fmt(r['price'])} | {_fmt(r['auto'])} | {_fmt(r['mine'])} | {_fmt(r['upside'], '{:+.0f}%')} |")

    labels = cfg.macro.get("fred") or {}
    L += ["", "## 宏观实际值（FRED，滞后）", "| 指标 | 最新 | 日期 | 周变化 | 备注 |", "|---|---|---|---|---|"]
    for m in views.macro_table(conn, labels):
        if m["value"] is None:
            continue
        note = f"CPI 同比 {m['yoy']:.1f}%" if m.get("yoy") is not None else ""
        L.append(f"| {m['label']} | {m['value']:.2f} | {m['date']} | {_fmt(m['change'], '{:+.2f}')} | {note} |")

    L += ["", "## Fed 路径（Kalshi 隐含利率上限）与预期差"]
    for f in views.fed_path(conn, now):
        line = f"- {f['event']}：隐含 {_fmt(f['implied'])}%"
        if f["actual"] is not None:
            line += f"；实际 {f['actual']:.2f}% → 预期差 {f['gap_bp']:+.0f}bp"
        L.append(line)
    if L[-1].startswith("## Fed"):
        L.append("- 暂无数据")

    L += ["", "## 预测市场：本周变化最大的合约"]
    changes = [c for c in views.market_changes(conn, now) if c["delta_pp"] is not None][:15]
    L += [f"- [{c['source']}] {c['event']} · {c['label']}：{c['before']:.0%} → {c['prob']:.0%}（{c['delta_pp']:+.0f}pp）"
          for c in changes] or ["- 历史不足一周，暂无变化数据"]

    d, cands = latest_candidates(conn, cfg.discovery.get("top_n", 10))
    from radar import profiles
    prof = profiles.lookup(conn)
    L += ["", f"## 候选池（{d or '尚未运行'}，按渠道重叠打分）"]
    for c in cands:
        notes = "；".join(n for v in c["detail"].values() for n in v[:2])
        p = prof.get(c["ticker"]) or {}
        who = f"{p.get('name_zh') or p.get('name') or ''}，{p.get('desc_zh') or ''}".strip("，")
        L.append(f"- {c['ticker']}{('（' + who + '）') if who else ''}（{c['score']:.1f}，{'+'.join(c['channels'])}）：{notes[:200]}")

    from radar import theses
    L += ["", "## Thesis 摘要（仪表盘「逻辑」页）"] + (theses.for_prompt(conn, cfg.all_tickers) or ["- 无"])
    if cfg.theses_dir:
        L += _theses(cfg, cfg.all_tickers)
    return "\n".join(L)


def claude_runner(cmd: str | None = None, timeout: int = 1200) -> Runner:
    argv = shlex.split(cmd or os.environ.get("CLAUDE_CMD") or DEFAULT_CLAUDE)

    def run(prompt: str) -> str:
        res = subprocess.run(argv, input=prompt, capture_output=True, text=True, timeout=timeout)
        if res.returncode != 0 or not res.stdout.strip():
            raise RuntimeError(f"claude exit {res.returncode}: {res.stderr.strip()[:300]}")
        return res.stdout.strip()

    return run


def generate(conn: sqlite3.Connection, cfg: Config, now: datetime, runner: Runner) -> tuple[int, bool]:
    bundle = build_bundle(conn, cfg, now)
    title = f"周报 {now.astimezone(ZoneInfo(cfg.tz)):%Y-%m-%d}"
    try:
        md, ok = runner(PROMPT.read_text() + "\n" + bundle), True
    except Exception as e:
        log.warning("claude failed: %s", e)
        md, ok = f"> ⚠️ Claude 生成失败（{e}），以下为原始数据。\n\n{bundle}", False
    cur = conn.execute("INSERT INTO reports (created, title, markdown, ok) VALUES (?,?,?,?)",
                       (_ts(now), title, md, int(ok)))
    conn.commit()
    return cur.lastrowid, ok
