"""Scheduled jobs. Each returns a short detail string; `run_job` records success/failure."""
from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta

from radar import db, earnings_notes
from radar.alerts import events
from radar.collectors import edgar, macro
from radar.collectors.prices import fetch_quotes
from radar.config import Config
from radar.http import Getter
from radar.models import Fire
from radar.pipeline import deliver, run_cycle

log = logging.getLogger(__name__)
Push = Callable[[Fire], None]


def run_job(name: str, fn: Callable[[], str], conn: sqlite3.Connection, now: datetime) -> bool:
    try:
        detail = fn()
        db.record_run(conn, name, now, True, detail)
        log.info("%s ok: %s", name, detail)
        return True
    except Exception as e:
        log.exception("%s failed", name)
        db.record_run(conn, name, now, False, f"{type(e).__name__}: {e}"[:500])
        return False


def prices(conn, cfg: Config, now: datetime, push: Push, fetch=fetch_quotes, backfill=None) -> str:
    backfilled = backfill(conn, cfg.all_tickers) if backfill else 0
    quotes = fetch(cfg.all_tickers, now)
    if cfg.all_tickers and not quotes:
        raise RuntimeError("no quotes fetched")
    d = run_cycle(conn, cfg, quotes, now, push)
    return f"quotes={len(quotes)}/{len(cfg.all_tickers)} backfilled={backfilled} pushed={len(d.push)}"


def edgar_job(conn, cfg: Config, now: datetime, push: Push, get: Getter, runner=None) -> str:
    today = now.date()
    new_8k, new_tx = edgar.collect(conn, cfg.all_tickers, get, today)
    fires = events.filing_fires(cfg.rules, new_8k, cfg.tier_of, today, edgar.item_labels)
    fires += events.insider_buy_fires(cfg.rules, new_tx, cfg.tier_of, today)
    fires += events.insider_cluster_fires(cfg.rules, cluster_purchases(conn, cfg, today, new_tx), cfg.tier_of)
    d = deliver(conn, cfg, fires, now, push)
    from radar import filing_notes
    notes = filing_notes.run(conn, get, runner, now) if runner else 0
    return f"8k={len(new_8k)} form4_tx={len(new_tx)} pushed={len(d.push)} summaries={notes}"


def cluster_purchases(conn, cfg: Config, today, new_tx) -> dict[str, list[dict]]:
    """Window purchases for tickers that got a *new* open-market buy this run."""
    rule = next((r for r in cfg.rules if r["kind"] == "insider_cluster"), None)
    tickers = {t.ticker for t in new_tx if t.code == "P"}
    if rule is None or not tickers:
        return {}
    since = (today - timedelta(days=rule.get("days", 30))).isoformat()
    out = {}
    for t in tickers:
        rows = conn.execute(
            "SELECT owner, accession, value FROM insider_tx WHERE ticker=? AND code='P' AND tx_date>=? "
            "ORDER BY accession", (t, since)).fetchall()
        out[t] = [dict(r) for r in rows]
    return out


def macro_job(conn, cfg: Config, now: datetime, push: Push, get: Getter) -> str:
    series = list((cfg.macro.get("fred") or {}).keys())
    n = macro.collect_fred(conn, series, get, now.date())
    latest = {s: macro.last_obs(conn, s, 2) for s in series}
    d = deliver(conn, cfg, events.macro_cross_fires(cfg.rules, latest), now, push)
    return f"fred_obs={n} pushed={len(d.push)}"


def markets_job(conn, cfg: Config, now: datetime, push: Push, get: Getter) -> str:
    mcfg = cfg.macro.get("markets") or {}
    markets = macro.fetch_markets(mcfg, get)
    if not markets:
        raise RuntimeError("no prediction markets fetched")
    sh = macro.shifts(conn, markets, now, mcfg.get("shift_hours", 24))
    macro.store_markets(conn, markets, now)
    d = deliver(conn, cfg, events.prob_shift_fires(cfg.rules, sh), now, push)
    return f"markets={len(markets)} pushed={len(d.push)}"


def earnings_job(conn, cfg: Config, now: datetime, push: Push, fetch=macro.fetch_earnings, runner=None) -> str:
    rows = fetch(cfg.all_tickers)
    newly = macro.store_earnings(conn, rows)
    d = deliver(conn, cfg, events.earnings_fires(cfg.rules, newly, cfg.tier_of, now.date()), now, push)
    notes = earnings_notes.run(conn, cfg, now, runner) if runner else []
    for n in notes:  # push only fresh reports, not backfill
        if n["date"] >= (now.date() - timedelta(days=3)).isoformat():
            link = f"{cfg.public_url}/earnings#e-{n['ticker']}-{n['date']}" if cfg.public_url else "/earnings"
            push(Fire(f"enote:{n['ticker']}:{n['date']}", n["ticker"], "notice",
                      f"📑 {n['ticker']} 财报摘要 · 重要度{n['level'] or '?'}", "、".join(n["tags"]) or "已生成", link))
    return f"rows={len(rows)} new_reports={len(newly)} pushed={len(d.push)} notes={len(notes)}"


def heartbeat_fires(last: dict[str, sqlite3.Row], max_age_hours: dict[str, float], now: datetime) -> list[Fire]:
    out = []
    for job, hours in max_age_hours.items():
        r = last.get(job)
        if r is None:
            continue  # never run yet (not deployed) — not an outage
        age = now - datetime.fromisoformat(r["ts"])
        if not r["ok"]:
            out.append(Fire(f"heartbeat:{job}", "SYSTEM", "notice", f"⚙️ {job} 任务失败",
                            (r["detail"] or "")[:300]))
        elif age > timedelta(hours=hours):
            out.append(Fire(f"heartbeat:{job}", "SYSTEM", "notice", f"⚙️ {job} 已 {age.total_seconds() / 3600:.0f}h 未更新",
                            f"上次成功：{r['ts']}"))
    return out


def heartbeat_job(conn, cfg: Config, now: datetime, push: Push) -> str:
    fires = heartbeat_fires(db.last_runs(conn), cfg.heartbeat, now)
    deliver(conn, cfg, fires, now, push)
    return f"problems={len(fires)}"
