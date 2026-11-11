"""CLI: python -m radar <job>.

Jobs (each records a run for the heartbeat):
  prices    quotes + price alerts            (daily after close; holdings intraday)
  edgar     8-K + Form 4 + insider alerts    (daily)
  macro     FRED actuals + macro thresholds  (daily)
  markets   Kalshi / Polymarket + shifts     (hourly)
  earnings  EPS actual vs estimate           (daily)
  discover  candidate discovery              (weekly)
  weekly    Claude weekly report             (weekly, after discover)
  heartbeat stale/failed job check           (hourly)
  brief     pre-market daily brief (Claude)  (weekdays 09:25 New York; refreshes edgar + markets first)
Other: serve, test-push, daily (= prices edgar macro earnings markets heartbeat)
"""
from __future__ import annotations

import argparse
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from radar import daily, db, discovery, jobs, weekly
from radar.collectors.prices import backfill_history, fetch_quotes
from radar.config import ROOT, Config, load_config
from radar.http import make_getter
from radar.models import Fire
from radar.notify import make_pusher

log = logging.getLogger("radar")

JOBS = ["prices", "edgar", "macro", "markets", "earnings", "discover", "weekly", "heartbeat", "brief", "valuation", "profiles"]
DAILY = ["prices", "edgar", "macro", "earnings", "markets", "heartbeat"]


def load_env_file(path: Path) -> None:
    """Minimal .env loader; real environment variables win."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def _pusher(cfg: Config):
    if cfg.ntfy_topic:
        return make_pusher(cfg.ntfy_server, cfg.ntfy_topic, cfg.public_url)
    return lambda f: log.info("[dry-run push] %s | %s", f.title, f.body)


def _job_fn(name: str, conn, cfg: Config, now: datetime, push):
    get = make_getter()
    if name == "prices":
        return lambda: jobs.prices(conn, cfg, now, push, fetch=fetch_quotes, backfill=backfill_history)
    if name == "edgar":
        return lambda: jobs.edgar_job(conn, cfg, now, push, get, runner=weekly.claude_runner(timeout=180))
    if name == "macro":
        return lambda: jobs.macro_job(conn, cfg, now, push, get)
    if name == "markets":
        return lambda: jobs.markets_job(conn, cfg, now, push, make_getter(0.0))
    if name == "earnings":
        return lambda: jobs.earnings_job(conn, cfg, now, push, runner=weekly.claude_runner(timeout=600))
    if name == "heartbeat":
        return lambda: jobs.heartbeat_job(conn, cfg, now, push)
    if name == "discover":
        def run():
            rows = discovery.discover(conn, cfg.discovery, now, get, fetch_quotes=fetch_quotes)
            from radar import candidate_review
            rv = candidate_review.run(conn, now.date().isoformat(), rows, set(cfg.all_tickers),
                                      cfg.discovery.get("review_n", 15), now, weekly.claude_runner(timeout=900),
                                      fetch_quotes=fetch_quotes)
            from radar import profiles
            profiles.fill(conn, cfg.all_tickers + [r["ticker"] for r in rows[:60]], now, weekly.claude_runner(timeout=600))
            return (f"candidates={len(rows)} multi_channel={sum(len(r['channels']) > 1 for r in rows)} "
                    f"valued={rv['valued']} reviewed={rv['reviewed']}")
        return run
    if name == "weekly":
        def run():
            rid, ok = weekly.generate(conn, cfg, now, weekly.claude_runner())
            link = f"{cfg.public_url}/reports/{rid}" if cfg.public_url else f"/reports/{rid}"
            push(Fire(f"weekly:{rid}", "*", "notice", "📊 本周投资周报已生成" if ok else "📊 周报（Claude 失败，仅数据）", link))
            if not ok:
                raise RuntimeError("claude generation failed; raw bundle saved")
            return f"report={rid}"
        return run
    if name == "profiles":
        def run():
            from radar import discovery, profiles
            _, cands = discovery.latest_candidates(conn, 60)
            n = profiles.fill(conn, cfg.all_tickers + [c["ticker"] for c in cands], now, weekly.claude_runner(timeout=600))
            return f"described={n}"
        return run
    if name == "valuation":
        def run():
            from radar import valuation
            res = valuation.refresh(conn, cfg.all_tickers, now)
            bad = {t: e for t, e in res.items() if e != "ok"}
            return f"ok={len(res) - len(bad)} skipped={len(bad)}"
        return run
    if name == "brief":
        def run():
            pre = daily.fetch_premarket(cfg.all_tickers, now)
            rid, ok = daily.generate(conn, cfg, now, weekly.claude_runner(timeout=900), pre, daily.fetch_gauges(now))
            link = f"{cfg.public_url}/daily/{rid}" if cfg.public_url else f"/daily/{rid}"
            push(Fire(f"daily:{rid}", "*", "notice", "☀️ 开盘前日报" if ok else "☀️ 日报（Claude 失败，仅数据）", link))
            if not ok:
                raise RuntimeError("claude generation failed; raw bundle saved")
            return f"report={rid} premarket={sum(p['pre'] is not None for p in pre.values())}/{len(pre)}"
        return run
    raise ValueError(name)


def run_jobs(names: list[str], cfg: Config) -> int:
    conn = db.connect(cfg.db_path)
    db.apply_tracked(conn, cfg)
    from radar import discovery_settings
    discovery_settings.apply(conn, cfg)
    push = _pusher(cfg)
    failed = 0
    try:
        for name in names:
            now = datetime.now(timezone.utc)
            if not jobs.run_job(name, _job_fn(name, conn, cfg, now, push), conn, now):
                failed += 1
    finally:
        conn.close()
    return 1 if failed else 0




def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="radar", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--env-file", default=str(ROOT / ".env"))
    sub = p.add_subparsers(dest="cmd", required=True)
    for j in JOBS + ["daily"]:
        sub.add_parser(j)
    sub.add_parser("collect", help="alias of prices")
    s = sub.add_parser("serve", help="run web dashboard")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8770)
    sub.add_parser("test-push", help="send a test ntfy notification")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    load_env_file(Path(args.env_file))
    cfg = load_config()

    if args.cmd == "serve":
        import uvicorn

        uvicorn.run("web.app:app", host=args.host, port=args.port)
        return 0
    if args.cmd == "test-push":
        if not cfg.ntfy_topic:
            log.error("NTFY_TOPIC not set (.env)")
            return 1
        _pusher(cfg)(Fire("test", "*", "notice", "Invest Radar 测试", "推送通了 ✅"))
        return 0
    names = {"collect": ["prices"], "daily": DAILY,
             "brief": ["edgar", "markets", "brief"]}.get(args.cmd, [args.cmd])
    return run_jobs(names, cfg)


if __name__ == "__main__":
    raise SystemExit(main())
