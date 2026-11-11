"""FastAPI dashboard: server-rendered, mobile-first."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import asyncio
import json
import re
from urllib.parse import parse_qs

import markdown as md
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from radar import agent, db, views
from radar.collectors.edgar import item_labels
from radar.discovery import latest_candidates
from radar.config import load_config
from web.views import local_time, quote_view, sparkline_points

from contextlib import asynccontextmanager


@asynccontextmanager
async def lifespan(app):
    import os

    from web import scheduler

    task = None if os.environ.get("RADAR_NO_SCHEDULER") else asyncio.create_task(scheduler.loop())
    yield
    if task:
        task.cancel()


app = FastAPI(title="Invest Radar", lifespan=lifespan)
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


LEVEL_ICON = {"urgent": "🔴", "notice": "🟡", "log": "⚪"}
CHANNEL_NAMES = {"screen": "量化筛选", "theme": "主题", "smart_money": "聪明钱"}


def _ctx():
    cfg = load_config()
    conn = db.connect(cfg.db_path)
    db.apply_tracked(conn, cfg)
    from radar import discovery_settings
    discovery_settings.apply(conn, cfg)
    return cfg, conn


def _alerts(rows, tz):
    return [
        {"icon": LEVEL_ICON.get(r["level"], ""), "title": r["title"], "body": r["body"],
         "ticker": r["ticker"], "when": local_time(r["ts"], tz), "pushed": bool(r["pushed"])}
        for r in rows
    ]


def _render(request: Request, name: str, ctx: dict, active: str):
    return templates.TemplateResponse(request, name, {**ctx, "active": active})


_agent_slots = asyncio.Semaphore(2)  # each claude process is ~300 MB


@app.post("/agent")
async def agent_ask(request: Request):
    body = await request.json()
    q = str(body.get("q") or "").strip()[:4000]
    sid = body.get("sid") or None
    if sid is not None and not re.fullmatch(r"[0-9a-f\-]{36}", str(sid)):
        sid = None
    page = str(body.get("page") or "/")[:200]
    if not q:
        raise HTTPException(400, "empty question")
    cfg, conn = _ctx()
    try:
        prompt = agent.build_prompt(conn, cfg, datetime.now(timezone.utc), q, page, first=sid is None)
    finally:
        conn.close()
    argv = agent.agent_argv(sid)
    workdir = cfg.db_path.parent / "agent"

    async def gen():
        if _agent_slots.locked():
            yield _sse({"t": "status", "v": "排队中…"})
        async with _agent_slots:
            async for ev in agent.run(argv, prompt, workdir):
                if await request.is_disconnected():
                    break
                yield _sse(ev)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _sse(ev: dict) -> str:
    return f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    cfg, conn = _ctx()
    try:
        latest = {r["ticker"]: r for r in db.latest_quotes(conn)}
        alerts = _alerts(db.recent_alerts(conn, 20), cfg.tz)
        fed = [f for f in views.fed_path(conn, datetime.now(timezone.utc)) if f["actual"] is None][:3]
        runs = db.last_runs(conn)
    finally:
        conn.close()
    updated = max((r["ts"] for r in latest.values()), default=None)
    problems = [j for j, r in runs.items() if not r["ok"]]
    return _render(request, "home.html", {
        "holdings": [quote_view(t, latest.get(t)) for t in cfg.holdings],
        "watchlist": [quote_view(t, latest.get(t)) for t in cfg.watchlist if t not in cfg.holdings],
        "alerts": alerts,
        "fed": fed,
        "problems": problems,
        "updated": local_time(updated, cfg.tz) if updated else None,
    }, "home")




TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-^=]{0,14}$")


async def _form(request: Request) -> dict[str, str]:
    body = (await request.body()).decode()
    return {k: v[0].strip() for k, v in parse_qs(body, keep_blank_values=True).items()}


def _fetch_new(ticker: str) -> None:
    """Best-effort first quote + history so a newly added ticker shows data immediately."""
    from radar.collectors.prices import backfill_history, fetch_quotes

    cfg = load_config()
    conn = db.connect(cfg.db_path)
    try:
        backfill_history(conn, [ticker])
        for q in fetch_quotes([ticker], datetime.now(timezone.utc)):
            db.insert_quote(conn, q)
        conn.commit()
    except Exception:  # never break the request on data-source errors
        pass
    try:
        from radar.collectors import macro
        macro.store_earnings(conn, macro.fetch_earnings([ticker]))  # so it shows on 财报 right away
        conn.commit()
    except Exception:  # never break the request on data-source errors
        pass
    finally:
        conn.close()
    try:
        _refresh_valuation([ticker])
    except Exception:
        pass


@app.post("/tracked")
async def tracked_add(request: Request, background: BackgroundTasks):
    f = await _form(request)
    ticker = f.get("ticker", "").upper()
    tier = f.get("tier", "watch")
    if not TICKER_RE.match(ticker) or tier not in db.TIERS:
        raise HTTPException(400, "bad ticker or tier")
    cfg, conn = _ctx()
    try:
        db.set_tracked(conn, ticker, tier, datetime.now(timezone.utc))
        has_data = conn.execute("SELECT 1 FROM quotes WHERE ticker=? LIMIT 1", (ticker,)).fetchone()
    finally:
        conn.close()
    if not has_data:
        background.add_task(_fetch_new, ticker)
    back = f.get("back") or "/"
    if not back.startswith("/") or back.startswith("//"):
        back = "/"
    return RedirectResponse(back, status_code=303)


@app.post("/tracked/{ticker}/delete")
async def tracked_delete(request: Request, ticker: str):
    f = await _form(request)
    cfg, conn = _ctx()
    try:
        db.remove_tracked(conn, ticker.upper())
    finally:
        conn.close()
    back = f.get("back") or "/"
    if not back.startswith("/") or back.startswith("//"):
        back = "/"
    return RedirectResponse(back, status_code=303)


@app.get("/t/{ticker}", response_class=HTMLResponse)
def ticker_page(request: Request, ticker: str):
    ticker = ticker.upper()
    cfg, conn = _ctx()
    try:
        hist = db.price_history(conn, ticker)
        alerts = _alerts(db.recent_alerts(conn, 50, ticker=ticker), cfg.tz)
        ev = views.ticker_events(conn, ticker)
        th = conn.execute("SELECT * FROM theses WHERE ticker=?", (ticker,)).fetchone()
        prof = conn.execute("SELECT * FROM profiles WHERE ticker=?", (ticker,)).fetchone()
        from radar.earnings_notes import notes_map
        enotes = {d: n for (t, d), n in notes_map(conn).items() if t == ticker}
    finally:
        conn.close()
    prices = [r["price"] for r in hist]
    return _render(request, "ticker.html", {
        "ticker": ticker,
        "q": quote_view(ticker, hist[-1] if hist else None),
        "tier": cfg.tier_of(ticker) if ticker in cfg.all_tickers else "—",
        "levels": cfg.levels.get(ticker) or {},
        "spark": sparkline_points(prices),
        "lo": min(prices) if prices else None,
        "hi": max(prices) if prices else None,
        "n": len(prices),
        "first": hist[0]["ts"][:10] if hist else None,
        "alerts": alerts,
        "filings": [dict(f, label=item_labels(f["items"] or "")) for f in ev["filings"]],
        "insiders": ev["insiders"],
        "earnings": [dict(e, note={**enotes[e["date"]], "html": _md(enotes[e["date"]]["markdown"])}
                          if e["date"] in enotes else None) for e in ev["earnings"]],
        "thesis": th,
        "prof": prof,
    }, "home")


@app.get("/macro", response_class=HTMLResponse)
def macro_page(request: Request):
    cfg, conn = _ctx()
    now = datetime.now(timezone.utc)
    try:
        table = views.macro_table(conn, cfg.macro.get("fred") or {})
        fed = views.fed_path(conn, now)
        changes = views.market_changes(conn, now, days=1)
        markets = views.latest_markets(conn)
    finally:
        conn.close()
    for m in table:
        m["spark"] = sparkline_points(m.get("history") or [], 120, 28)
    events: dict[str, list] = {}
    for m in markets:
        if m["source"] == "polymarket":
            events.setdefault(m["event_title"], []).append(m)
    poly = [(title, sorted(ms, key=lambda r: -r["prob"])[:6]) for title, ms in events.items()]
    return _render(request, "macro.html", {
        "table": table, "fed": fed, "poly": poly,
        "movers": [c for c in changes if c["delta_pp"] is not None and abs(c["delta_pp"]) >= 3][:12],
    }, "macro")


@app.get("/discover", response_class=HTMLResponse)
def discover_page(request: Request, sort: str = "score"):
    from radar import valuation
    from web import scheduler

    cfg, conn = _ctx()
    try:
        run_date, rows = latest_candidates(conn, 60)
        notes = {r["ticker"]: r for r in conn.execute("SELECT * FROM candidate_notes WHERE run_date=?", (run_date,))}
        vals = {v["ticker"]: v for v in valuation.summary(conn, list(notes))}
        from radar import profiles
        prof = profiles.lookup(conn)
    finally:
        conn.close()
    tracked = set(cfg.all_tickers)
    for r in rows:
        r["names"] = [CHANNEL_NAMES.get(c, c) for c in r["channels"]]
        r["tracked"] = r["ticker"] in tracked
        n = notes.get(r["ticker"])
        r["why"], r["risk"] = (n["why"], n["risk"]) if n else (None, None)
        r["val"] = vals.get(r["ticker"])
        r["prof"] = prof.get(r["ticker"])
    if sort == "upside":
        rows.sort(key=lambda r: -(r["val"]["upside"] if r["val"] and r["val"]["upside"] is not None else -1e9))
    return _render(request, "discover.html", {"run_date": run_date, "rows": rows, "sort": sort,
                                              "running": "discover" in scheduler.state.get("jobs", set())},
                   "discover")


@app.get("/discover/settings", response_class=HTMLResponse)
def discover_settings(request: Request, error: str = ""):
    from radar import discovery_settings

    cfg, conn = _ctx()
    conn.close()
    return _render(request, "discover_settings.html",
                   {"f": discovery_settings.to_form(cfg.discovery), "error": error}, "discover")


@app.post("/discover/settings")
async def discover_settings_save(request: Request):
    from urllib.parse import quote

    from radar import discovery_settings
    from web import scheduler

    form = await _form(request)
    form.setdefault("openinsider", "")
    cfg, conn = _ctx()
    try:
        try:
            new = discovery_settings.from_form(form, cfg.discovery)
        except ValueError as e:
            return RedirectResponse(f"/discover/settings?error={quote(str(e))}", status_code=303)
        discovery_settings.save(conn, new)
    finally:
        conn.close()
    if form.get("rerun"):
        asyncio.create_task(scheduler.spawn("discover"))
    return RedirectResponse("/discover", status_code=303)


@app.post("/discover/run")
async def discover_run():
    from web import scheduler

    asyncio.create_task(scheduler.spawn("discover"))
    return RedirectResponse("/discover", status_code=303)


@app.get("/earnings", response_class=HTMLResponse)
def earnings_page(request: Request):
    cfg, conn = _ctx()
    try:
        board = views.earnings_board(conn, cfg.all_tickers, datetime.now(timezone.utc))
    finally:
        conn.close()
    for r in board["recent"]:
        if r["note"]:
            r["note"] = {**r["note"], "html": _md(r["note"]["markdown"])}
    dated = {e["ticker"] for e in board["upcoming"]}
    return _render(request, "earnings.html", {**board, "holdings": set(cfg.holdings),
                                              "undated": [t for t in cfg.all_tickers if t not in dated]}, "earnings")


@app.get("/theses", response_class=HTMLResponse)
def theses_page(request: Request):
    from radar import theses

    cfg, conn = _ctx()
    try:
        theses.seed(conn, cfg.all_tickers, datetime.now(timezone.utc))
        th = theses.all_theses(conn)
    finally:
        conn.close()
    order = [theses.GENERAL, *cfg.holdings, *[t for t in cfg.watchlist if t not in cfg.holdings]]
    items = [{"ticker": t, "name": "总体原则" if t == theses.GENERAL else t,
              "tier": "" if t == theses.GENERAL else ("持仓" if t in cfg.holdings else "关注"),
              "body": th[t]["body"], "is_default": th[t]["is_default"],
              "updated": local_time(th[t]["updated"], cfg.tz)} for t in order if t in th]
    return _render(request, "theses.html", {"items": items}, "theses")


@app.post("/theses/{ticker}")
async def thesis_save(request: Request, ticker: str):
    from radar import theses

    ticker = ticker.upper()
    if ticker != theses.GENERAL and not TICKER_RE.match(ticker):
        raise HTTPException(400, "bad ticker")
    f = await _form(request)
    cfg, conn = _ctx()
    try:
        theses.save(conn, ticker, f.get("body", ""), datetime.now(timezone.utc))
    finally:
        conn.close()
    back = f.get("back") or "/theses"
    if not back.startswith("/") or back.startswith("//"):
        back = "/theses"
    return RedirectResponse(f"{back}#th-{ticker}" if back == "/theses" else back, status_code=303)


@app.get("/valuation", response_class=HTMLResponse)
def valuation_page(request: Request):
    from radar import valuation

    cfg, conn = _ctx()
    try:
        rows = valuation.summary(conn, cfg.all_tickers)
        from radar import profiles
        prof = profiles.lookup(conn)
    finally:
        conn.close()
    rows.sort(key=lambda r: (r["upside"] is None, -(r["upside"] or 0)))
    return _render(request, "valuation.html", {"rows": rows, "holdings": set(cfg.holdings), "prof": prof}, "valuation")


@app.get("/v/{ticker}", response_class=HTMLResponse)
def valuation_detail(request: Request, ticker: str):
    from radar import valuation

    ticker = ticker.upper()
    cfg, conn = _ctx()
    try:
        v = valuation.load(conn, ticker)
        q = conn.execute("SELECT price FROM quotes WHERE ticker=? ORDER BY ts DESC LIMIT 1", (ticker,)).fetchone()
    finally:
        conn.close()
    return _render(request, "valuation_detail.html", {
        "ticker": ticker, "v": v["data"] if v else None, "mine": v["mine"] if v else None,
        "error": (v["error"] if v else "还没有估值数据，点「重新拉 SEC 数据」"),
        "updated": local_time(v["updated"], cfg.tz) if v else "—", "price": q["price"] if q else None}, "valuation")


def _refresh_valuation(tickers: list[str]) -> None:
    from radar import valuation

    cfg = load_config()
    conn = db.connect(cfg.db_path)
    try:
        valuation.refresh(conn, tickers, datetime.now(timezone.utc))
    finally:
        conn.close()


@app.post("/v/{ticker}/refresh")
async def valuation_refresh(ticker: str):
    ticker = ticker.upper()
    if not TICKER_RE.match(ticker):
        raise HTTPException(400, "bad ticker")
    await asyncio.to_thread(_refresh_valuation, [ticker])
    return RedirectResponse(f"/v/{ticker}", status_code=303)


@app.post("/v/{ticker}/inputs")
async def valuation_inputs(request: Request, ticker: str):
    from radar import valuation

    ticker = ticker.upper()
    body = await request.json()
    raw = body.get("inputs")
    inputs = None
    if raw is not None:
        try:
            inputs = {k: float(raw[k]) for k in ("rr", "terminal", "fcf_margin", "shares", "g_start")}
            inputs["analyst"] = [float(a) for a in (raw.get("analyst") or [])[:2] if a]
        except (KeyError, TypeError, ValueError):
            raise HTTPException(400, "bad inputs")
        if not (0 < inputs["shares"] and -0.5 < inputs["terminal"] < inputs["rr"] < 1):
            raise HTTPException(400, "out of range")
    cfg, conn = _ctx()
    try:
        valuation.save_inputs(conn, ticker, inputs, datetime.now(timezone.utc))
    finally:
        conn.close()
    return {"ok": True}


@app.get("/reports", response_class=HTMLResponse)
def reports_page(request: Request):
    cfg, conn = _ctx()
    try:
        rows = conn.execute("SELECT id, created, title, ok FROM reports WHERE kind='weekly' "
                            "ORDER BY id DESC LIMIT 100").fetchall()
    finally:
        conn.close()
    return _render(request, "reports.html", {
        "rows": [{"id": r["id"], "title": r["title"], "ok": r["ok"], "when": local_time(r["created"], cfg.tz)}
                 for r in rows]}, "reports")


def _md(text: str) -> Markup:
    # Report text includes web-sourced content: neutralise raw HTML before Markdown rendering.
    return Markup(md.markdown(text.replace("<", "&lt;"), extensions=["tables", "fenced_code", "sane_lists"]))


@app.get("/reports/{rid}", response_class=HTMLResponse)
def report_page(request: Request, rid: int):
    cfg, conn = _ctx()
    try:
        r = conn.execute("SELECT * FROM reports WHERE id=?", (rid,)).fetchone()
    finally:
        conn.close()
    if r is None:
        raise HTTPException(404)
    return _render(request, "report.html", {"title": r["title"], "html": _md(r["markdown"]),
                                            "when": local_time(r["created"], cfg.tz)},
                   "daily" if r["kind"] == "daily" else "reports")


@app.post("/daily/run")
async def daily_run():
    from web import scheduler

    if not scheduler.state["running"]:
        asyncio.create_task(scheduler.run_brief())
    return RedirectResponse("/daily", status_code=303)


@app.get("/daily", response_class=HTMLResponse)
@app.get("/daily/{rid}", response_class=HTMLResponse)
def daily_page(request: Request, rid: int | None = None):
    """Latest (or chosen) daily brief in full, with the archive below."""
    cfg, conn = _ctx()
    try:
        rows = conn.execute("SELECT id, created, title, ok FROM reports WHERE kind='daily' "
                            "ORDER BY id DESC LIMIT 60").fetchall()
        cur = conn.execute("SELECT * FROM reports WHERE kind='daily' AND id=?",
                           (rid if rid is not None else (rows[0]["id"] if rows else -1),)).fetchone()
    finally:
        conn.close()
    if rid is not None and cur is None:
        raise HTTPException(404)
    return _render(request, "daily.html", {
        "running": __import__("web.scheduler", fromlist=["state"]).state["running"],
        "cur": {"id": cur["id"], "title": cur["title"], "html": _md(cur["markdown"]),
                "when": local_time(cur["created"], cfg.tz)} if cur else None,
        "rows": [{"id": r["id"], "title": r["title"], "ok": r["ok"], "when": local_time(r["created"], cfg.tz)}
                 for r in rows]}, "daily")
