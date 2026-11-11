"""In-process scheduler for the pre-market brief (runs inside the always-on web service, as user `invest`,
so adding/changing this schedule needs no root/systemd change). The brief itself runs as a separate
`python -m radar brief` process, same as a systemd job would."""
from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from radar import db
from radar.config import ROOT, load_config

log = logging.getLogger(__name__)
NY = ZoneInfo("America/New_York")
START, END = time(9, 25), time(9, 50)  # fire at 09:25; catch up until 09:50 if the service was restarting

state = {"running": False, "attempted": None}


def due(now: datetime, has_today: bool) -> bool:
    ny = now.astimezone(NY)
    return (ny.weekday() < 5 and START <= ny.time() < END and not has_today
            and state["attempted"] != ny.date() and not state["running"])


def _has_today(now: datetime) -> bool:
    cfg = load_config()
    conn = db.connect(cfg.db_path)
    try:
        rows = conn.execute("SELECT created FROM reports WHERE kind='daily' ORDER BY id DESC LIMIT 3").fetchall()
    finally:
        conn.close()
    today = now.astimezone(NY).date()
    return any(datetime.fromisoformat(r["created"]).astimezone(NY).date() == today for r in rows)


async def run_brief() -> int:
    """Run one brief now (separate process). Returns exit code; no-op if one is already running."""
    if state["running"]:
        return -1
    state["running"] = True
    state["attempted"] = datetime.now(timezone.utc).astimezone(NY).date()
    try:
        proc = await asyncio.create_subprocess_exec(sys.executable, "-m", "radar", "brief", cwd=str(ROOT),
                                                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        _, err = await proc.communicate()
        if proc.returncode:
            log.warning("brief exited %s: %s", proc.returncode, err.decode(errors="ignore")[-500:])
        return proc.returncode
    finally:
        state["running"] = False


async def spawn(job: str) -> None:
    """Run `python -m radar <job>` in the background once at a time (e.g. discover after a settings change)."""
    running = state.setdefault("jobs", set())
    if job in running:
        return
    running.add(job)
    try:
        proc = await asyncio.create_subprocess_exec(sys.executable, "-m", "radar", job, cwd=str(ROOT),
                                                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await proc.wait()
    finally:
        running.discard(job)


def valuation_due(now: datetime, last: str | None) -> bool:
    """Saturday 10:00 New York, or whenever the last full refresh is over 8 days old."""
    ny = now.astimezone(NY)
    age = (now - datetime.fromisoformat(last)).total_seconds() / 86400 if last else 99
    return (ny.weekday() == 5 and ny.time() >= time(10, 0) and age > 1) or age > 8


async def run_valuation() -> None:
    if state.get("valuing"):
        return
    state["valuing"] = True
    try:
        proc = await asyncio.create_subprocess_exec(sys.executable, "-m", "radar", "valuation", cwd=str(ROOT),
                                                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await proc.wait()
    finally:
        state["valuing"] = False


def _last_valuation_run() -> str | None:
    cfg = load_config()
    conn = db.connect(cfg.db_path)
    try:
        r = conn.execute("SELECT ts FROM runs WHERE job='valuation' ORDER BY ts DESC LIMIT 1").fetchone()
    finally:
        conn.close()
    return r["ts"] if r else None


def _profiles_missing() -> bool:
    from radar import discovery, profiles

    cfg = load_config()
    conn = db.connect(cfg.db_path)
    try:
        db.apply_tracked(conn, cfg)
        _, cands = discovery.latest_candidates(conn, 60)
        return bool(profiles.missing(conn, cfg.all_tickers + [c["ticker"] for c in cands]))
    finally:
        conn.close()




async def spawn_args(*args: str) -> None:
    proc = await asyncio.create_subprocess_exec(sys.executable, "-m", "radar", *args, cwd=str(ROOT),
                                                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    await proc.wait()


async def loop() -> None:
    while True:
        try:
            now = datetime.now(timezone.utc)
            if due(now, _has_today(now)):
                log.info("starting pre-market brief")
                asyncio.create_task(run_brief())
            if _profiles_missing() and (now - state.get("profiles_at", now - timedelta(hours=1))).total_seconds() > 600:
                state["profiles_at"] = now
                asyncio.create_task(spawn("profiles"))
            if not state.get("valuing") and valuation_due(now, _last_valuation_run()):
                log.info("starting weekly valuation refresh")
                asyncio.create_task(run_valuation())
        except Exception:
            log.exception("scheduler tick failed")
        await asyncio.sleep(30)
