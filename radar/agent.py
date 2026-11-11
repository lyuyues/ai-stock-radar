"""Dashboard chat agent: `claude -p` in stream-json mode, resumable by session id.

The first turn gets the full data bundle (same one the weekly report uses); every turn gets
the context of the page the user is looking at. Tools: web search / fetch only.
"""
from __future__ import annotations

import asyncio
import json
import os
import shlex
import sqlite3
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path

from radar import views, weekly
from radar.collectors.edgar import item_labels
from radar.config import Config

SYSTEM = """你是用户的投资研究助手，嵌在用户自建的 Invest Radar 仪表盘右侧。
- 用中文回答，简洁、直接，先给结论再给依据。
- 用户消息里附带了仪表盘的最新数据（价格、提醒、8-K、内部人交易、宏观、预测市场、候选池）；优先基于这些数据回答，需要新闻或背景再用 WebSearch / WebFetch。
- 任何事实都标注时间点：事件发生时间 和 披露/报道时间 分开写（例如 13F：操作发生在哪个季度、哪天披露）。引用网页要给来源链接。
- 不编造数据；不确定就说不确定，并说明怎么核实。
- 可以给观点和推理，但明确区分"数据显示"和"我的判断"。"""

MAX_SECONDS = 300


def agent_argv(session_id: str | None) -> list[str]:
    """Reuse the weekly report's Claude binary (CLAUDE_CMD) with streaming flags."""
    base = os.environ.get("AGENT_CMD")
    if base:
        argv = shlex.split(base)
    else:
        cmd = shlex.split(os.environ.get("CLAUDE_CMD") or weekly.DEFAULT_CLAUDE)
        argv = [cmd[0], "-p", "--output-format", "stream-json", "--verbose", "--include-partial-messages",
                "--tools", "WebSearch,WebFetch", "--allowedTools", "WebSearch,WebFetch", "--strict-mcp-config",
                "--append-system-prompt", SYSTEM]
    if session_id:
        argv += ["--resume", session_id]
    return argv


def page_context(conn: sqlite3.Connection, page: str) -> str:
    page = (page or "/").split("?")[0]
    if page.startswith("/t/"):
        t = page[3:].upper()
        ev = views.ticker_events(conn, t, limit=8)
        last = conn.execute("SELECT * FROM quotes WHERE ticker=? ORDER BY ts DESC LIMIT 1", (t,)).fetchone()
        L = [f"用户正在看 {t} 的个股页。"]
        th = conn.execute("SELECT body, is_default FROM theses WHERE ticker=?", (t,)).fetchone()
        if th:
            L.append(f"用户的投资逻辑{'（默认值，未修改）' if th['is_default'] else ''}：\n{th['body']}")
        if last:
            L.append(f"最新价 {last['price']:.2f}（{last['ts'][:16]} UTC），P/E {last['pe'] or '—'}，"
                     f"52 周 {last['low52'] or '—'}–{last['high52'] or '—'}")
        L += [f"- 8-K {f['filed']}：{item_labels(f['items'] or '')}{('：' + f['summary']) if f['summary'] else ''} {f['url']}"
              for f in ev["filings"]]
        L += [f"- 内部人 {r['tx_date']} {r['owner']}（{r['title'] or ''}）{r['code']} {r['shares'] or 0:,.0f} 股 "
              f"@ {r['price'] or 0:.2f}" for r in ev["insiders"]]
        L += [f"- 财报 {r['date']}：EPS 实际 {r['eps_actual']} / 预期 {r['eps_est']}" for r in ev["earnings"]]
        return "\n".join(L)
    if page == "/earnings":
        cfg_t = [r["ticker"] for r in conn.execute("SELECT ticker FROM tracked")]
        b = views.earnings_board(conn, cfg_t, datetime.now())
        L = ["用户正在看「财报」页。即将发布："]
        L += [f"- {e['ticker']} {e['date']}（{e['days']} 天后）EPS 预期 {e['eps_est']}，近 4 季超预期 {e['beats']}/{e['of']}"
              for e in b["upcoming"]]
        L.append("近期已发布：")
        L += [f"- {e['ticker']} {e['date']} EPS {e['eps_actual']} vs 预期 {e['eps_est']}，"
              f"股价反应 {'%+.1f%%' % e['move_pct'] if e['move_pct'] is not None else '—'}" for e in b["recent"]]
        return "\n".join(L)
    if page.startswith("/daily"):
        rid = page.rsplit("/", 1)[-1]
        r = conn.execute("SELECT title, markdown FROM reports WHERE kind='daily' AND " +
                         ("id=?" if rid.isdigit() else "1=1 ORDER BY id DESC LIMIT 1"),
                         (int(rid),) if rid.isdigit() else ()).fetchone()
        return f"用户正在看「{r['title']}」：\n{r['markdown'][:6000]}" if r else "用户正在看日报页（还没有日报）。"
    if page.startswith("/v/") or page == "/valuation":
        from radar import valuation
        ts = [page[3:].upper()] if page.startswith("/v/") else [r["ticker"] for r in conn.execute("SELECT ticker FROM tracked")]
        L = ["用户正在看「估值」页（DCF：前两年分析师收入预期，之后增长线性降到永续；FCF 利润率 × 收入，折现）。"]
        for r in valuation.summary(conn, ts):
            v = valuation.load(conn, r["ticker"])
            a = v["data"]["auto"] if v and v["data"] else None
            L.append(f"- {r['ticker']}：价格 {r['price']}，公允价值 自动 {r['auto'] and round(r['auto'], 2)} / 我的 {r['mine'] and round(r['mine'], 2)}，"
                     f"上涨空间 {r['upside'] and round(r['upside'])}%" + (
                f"；自动假设 rr {a['rr']:.1%} 永续 {a['terminal']:.1%} FCF 利润率 {a['fcf_margin']:.1%} 分析师收入 {a['analyst']} 起始增长 {a['g_start']:.1%}；我的 {v['mine']}" if a else f"；{r['error']}"))
        return "\n".join(L)
    if page == "/theses":
        from radar import theses
        return "用户正在看「投资逻辑」页：\n" + "\n\n".join(theses.for_prompt(conn, [r["ticker"] for r in conn.execute("SELECT ticker FROM tracked")]))
    names = {"/": "总览", "/macro": "宏观", "/discover": "发现（候选池）", "/reports": "周报列表"}
    if page.startswith("/reports/"):
        return f"用户正在看周报 {page}。"
    return f"用户正在看「{names.get(page, page)}」页。"


def build_prompt(conn: sqlite3.Connection, cfg: Config, now: datetime, question: str, page: str,
                 first: bool) -> str:
    parts = []
    if first:
        parts.append("# 仪表盘数据快照\n" + weekly.build_bundle(conn, cfg, now))
    parts.append("# 当前页面\n" + page_context(conn, page))
    parts.append("# 问题\n" + question.strip())
    return "\n\n".join(parts)


def parse_event(line: str) -> dict | None:
    """stream-json line → UI event: session / text / tool / done / error."""
    try:
        e = json.loads(line)
    except ValueError:
        return None
    t = e.get("type")
    if t == "system" and e.get("subtype") == "init":
        return {"t": "session", "v": e.get("session_id")}
    if t == "stream_event":
        ev = e.get("event") or {}
        if ev.get("type") == "content_block_delta" and (ev.get("delta") or {}).get("type") == "text_delta":
            return {"t": "text", "v": ev["delta"]["text"]}
        if ev.get("type") == "content_block_start" and (ev.get("content_block") or {}).get("type") in (
                "tool_use", "server_tool_use"):
            return {"t": "tool", "v": ev["content_block"].get("name", "")}
    if t == "result":
        if e.get("is_error") or e.get("subtype") != "success":
            return {"t": "error", "v": str(e.get("result") or e.get("subtype"))[:300]}
        return {"t": "done", "v": e.get("session_id")}
    return None


async def run(argv: list[str], prompt: str, cwd: Path) -> AsyncIterator[dict]:
    cwd.mkdir(parents=True, exist_ok=True)
    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=str(cwd), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, limit=8 * 1024 * 1024)
    proc.stdin.write(prompt.encode())
    await proc.stdin.drain()
    proc.stdin.close()
    finished = False
    try:
        async with asyncio.timeout(MAX_SECONDS):
            async for raw in proc.stdout:
                ev = parse_event(raw.decode(errors="ignore"))
                if ev:
                    finished |= ev["t"] in ("done", "error")
                    yield ev
        await proc.wait()
        if not finished:
            err = (await proc.stderr.read()).decode(errors="ignore").strip()
            yield {"t": "error", "v": (err or f"claude exited {proc.returncode}")[:300]}
    except TimeoutError:
        yield {"t": "error", "v": f"超过 {MAX_SECONDS}s 未完成"}
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
