"""One collection cycle: store quotes → evaluate rules → apply policy → push → record."""
from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from datetime import datetime
from zoneinfo import ZoneInfo

from radar import db
from radar.alerts.policy import Decision, decide, in_quiet_hours
from radar.alerts.rules import QUOTE_KINDS, evaluate_all
from radar.config import Config
from radar.models import Fire, Quote

log = logging.getLogger(__name__)


def _local_day_start(now: datetime, tz: str) -> datetime:
    local = now.astimezone(ZoneInfo(tz))
    return local.replace(hour=0, minute=0, second=0, microsecond=0)


def _try_push(push_fn: Callable[[Fire], None], f: Fire) -> bool:
    try:
        push_fn(f)
        return True
    except Exception as e:  # network etc. — record and move on
        log.warning("push failed for %s/%s: %s", f.rule_id, f.ticker, e)
        return False


def run_cycle(conn: sqlite3.Connection, cfg: Config, quotes: list[Quote], now: datetime,
              push_fn: Callable[[Fire], None]) -> Decision:
    fires: list[Fire] = []
    quote_rules = [r for r in cfg.rules if r["kind"] in QUOTE_KINDS]
    for q in quotes:
        prev = db.last_price_before(conn, q.ticker, q.ts)
        db.insert_quote(conn, q)
        fires += evaluate_all(quote_rules, q, cfg.tier_of(q.ticker), cfg.levels, prev)
    return deliver(conn, cfg, fires, now, push_fn)


def deliver(conn: sqlite3.Connection, cfg: Config, fires: list[Fire], now: datetime,
            push_fn: Callable[[Fire], None]) -> Decision:
    """Apply policy to fires from any source, push, and record."""
    decision = decide(
        fires, now, cfg,
        last_fired=lambda r, t: db.last_fired(conn, r, t),
        notices_today=db.notices_pushed_since(conn, _local_day_start(now, cfg.tz)),
    )
    for f in decision.push:
        db.record_alert(conn, f, now, pushed=_try_push(push_fn, f))
    for f in decision.logged:
        db.record_alert(conn, f, now, pushed=False)
    for f in decision.deferred:
        if f.once:  # won't be re-detected next run → queue it
            db.record_alert(conn, f, now, pushed=False, deferred=True)
    if decision.digest:
        db.record_alert(conn, decision.digest, now, pushed=_try_push(push_fn, decision.digest))
    if not in_quiet_hours(now, cfg.quiet_start, cfg.quiet_end, cfg.tz):
        for alert_id, f in db.take_deferred(conn):
            db.mark_released(conn, alert_id, pushed=_try_push(push_fn, f))
    conn.commit()
    return decision
