"""SQLite storage. Timestamps are stored as ISO-8601 UTC strings."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from radar.models import Fire, Quote

SCHEMA = """
CREATE TABLE IF NOT EXISTS quotes (
    ticker TEXT NOT NULL,
    ts TEXT NOT NULL,
    price REAL NOT NULL,
    prev_close REAL,
    pe REAL,
    high52 REAL,
    low52 REAL,
    PRIMARY KEY (ticker, ts)
);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    rule_id TEXT NOT NULL,
    ticker TEXT NOT NULL,
    level TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    pushed INTEGER NOT NULL,
    deferred INTEGER NOT NULL DEFAULT 0   -- one-off event held during quiet hours
);
CREATE INDEX IF NOT EXISTS alerts_rule_ticker ON alerts (rule_id, ticker, ts);

-- Phase 2: events & macro
CREATE TABLE IF NOT EXISTS filings (
    accession TEXT PRIMARY KEY,
    ticker TEXT NOT NULL,
    cik INTEGER NOT NULL,
    form TEXT NOT NULL,
    filed TEXT NOT NULL,          -- YYYY-MM-DD
    items TEXT,                   -- 8-K item list, comma separated
    url TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS filings_ticker ON filings (ticker, filed);
CREATE TABLE IF NOT EXISTS insider_tx (
    accession TEXT NOT NULL,
    idx INTEGER NOT NULL,
    ticker TEXT NOT NULL,
    owner TEXT NOT NULL,
    title TEXT,
    tx_date TEXT NOT NULL,
    code TEXT NOT NULL,           -- P purchase, S sale, ...
    shares REAL,
    price REAL,
    value REAL,
    PRIMARY KEY (accession, idx)
);
CREATE INDEX IF NOT EXISTS insider_ticker ON insider_tx (ticker, tx_date);
CREATE TABLE IF NOT EXISTS macro_obs (
    series TEXT NOT NULL,
    date TEXT NOT NULL,
    value REAL NOT NULL,
    PRIMARY KEY (series, date)
);
CREATE TABLE IF NOT EXISTS predictions (
    source TEXT NOT NULL,         -- kalshi | polymarket
    market_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    event_id TEXT NOT NULL,
    event_title TEXT NOT NULL,
    label TEXT NOT NULL,
    close_time TEXT,
    prob REAL NOT NULL,
    PRIMARY KEY (source, market_id, ts)
);
CREATE INDEX IF NOT EXISTS predictions_event ON predictions (event_id, ts);
CREATE TABLE IF NOT EXISTS earnings (
    ticker TEXT NOT NULL,
    date TEXT NOT NULL,           -- YYYY-MM-DD
    eps_est REAL,
    eps_actual REAL,
    surprise_pct REAL,
    PRIMARY KEY (ticker, date)
);
-- Claude write-up per earnings report.
CREATE TABLE IF NOT EXISTS earnings_notes (
    ticker TEXT NOT NULL,
    date TEXT NOT NULL,
    markdown TEXT NOT NULL,
    tags TEXT NOT NULL,           -- JSON list
    level TEXT,                   -- 高 / 中 / 低
    created TEXT NOT NULL,
    PRIMARY KEY (ticker, date)
);
-- DCF engine output per ticker (JSON) + the user's edited assumptions.
CREATE TABLE IF NOT EXISTS valuations (
    ticker TEXT PRIMARY KEY,
    updated TEXT NOT NULL,
    data TEXT,                    -- JSON: hist / proj / quarterly / meta / auto inputs
    error TEXT
);
CREATE TABLE IF NOT EXISTS valuation_inputs (
    ticker TEXT PRIMARY KEY,
    json TEXT NOT NULL,
    updated TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS filing_notes (
    accession TEXT PRIMARY KEY,
    summary TEXT NOT NULL,
    created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS candidate_notes (
    run_date TEXT NOT NULL,
    ticker TEXT NOT NULL,
    why TEXT NOT NULL,
    risk TEXT NOT NULL,
    PRIMARY KEY (run_date, ticker)
);
CREATE TABLE IF NOT EXISTS profiles (
    ticker TEXT PRIMARY KEY,
    name TEXT,
    name_zh TEXT,
    sector TEXT,
    industry TEXT,
    desc_zh TEXT,
    updated TEXT NOT NULL
);
-- Investment theses, editable on the dashboard.
CREATE TABLE IF NOT EXISTS theses (
    ticker TEXT PRIMARY KEY,
    body TEXT NOT NULL,
    updated TEXT NOT NULL,
    is_default INTEGER NOT NULL
);
-- Editable tracked list (dashboard). Seeded once from config/watchlist.yaml.
CREATE TABLE IF NOT EXISTS tracked (
    ticker TEXT PRIMARY KEY,
    tier TEXT NOT NULL CHECK (tier IN ('holding', 'watch')),
    added_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    job TEXT NOT NULL,
    ts TEXT NOT NULL,
    ok INTEGER NOT NULL,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS runs_job ON runs (job, ts);

-- Phase 4: discovery & reports
CREATE TABLE IF NOT EXISTS candidates (
    run_date TEXT NOT NULL,
    ticker TEXT NOT NULL,
    score REAL NOT NULL,
    channels TEXT NOT NULL,       -- JSON list
    detail TEXT NOT NULL,         -- JSON object
    PRIMARY KEY (run_date, ticker)
);
CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY,
    created TEXT NOT NULL,
    title TEXT NOT NULL,
    markdown TEXT NOT NULL,
    ok INTEGER NOT NULL
);
"""


def _ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(reports)")}
    if "kind" not in cols:
        conn.execute("ALTER TABLE reports ADD COLUMN kind TEXT NOT NULL DEFAULT 'weekly'")
        conn.commit()
    return conn


def insert_quote(conn: sqlite3.Connection, q: Quote) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO quotes VALUES (?,?,?,?,?,?,?)",
        (q.ticker, _ts(q.ts), q.price, q.prev_close, q.pe, q.high52, q.low52),
    )


def last_price_before(conn: sqlite3.Connection, ticker: str, ts: datetime) -> float | None:
    row = conn.execute(
        "SELECT price FROM quotes WHERE ticker=? AND ts<? ORDER BY ts DESC LIMIT 1",
        (ticker, _ts(ts)),
    ).fetchone()
    return row["price"] if row else None


def latest_quotes(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT q.* FROM quotes q JOIN (SELECT ticker, MAX(ts) AS ts FROM quotes GROUP BY ticker) m "
        "USING (ticker, ts) ORDER BY q.ticker"
    ).fetchall()


def price_history(conn: sqlite3.Connection, ticker: str, limit: int = 500) -> list[sqlite3.Row]:
    rows = conn.execute(
        "SELECT * FROM quotes WHERE ticker=? ORDER BY ts DESC LIMIT ?", (ticker, limit)
    ).fetchall()
    return rows[::-1]


def record_alert(conn: sqlite3.Connection, f: Fire, ts: datetime, pushed: bool,
                 deferred: bool = False) -> None:
    conn.execute(
        "INSERT INTO alerts (ts, rule_id, ticker, level, title, body, pushed, deferred) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (_ts(ts), f.rule_id, f.ticker, f.level, f.title, f.body, int(pushed), int(deferred)),
    )


def take_deferred(conn: sqlite3.Connection) -> list[tuple[int, Fire]]:
    rows = conn.execute("SELECT * FROM alerts WHERE deferred=1 ORDER BY id").fetchall()
    return [(r["id"], Fire(r["rule_id"], r["ticker"], r["level"], r["title"], r["body"], once=True))
            for r in rows]


def mark_released(conn: sqlite3.Connection, alert_id: int, pushed: bool) -> None:
    conn.execute("UPDATE alerts SET deferred=0, pushed=? WHERE id=?", (int(pushed), alert_id))


def last_fired(conn: sqlite3.Connection, rule_id: str, ticker: str) -> datetime | None:
    row = conn.execute(
        "SELECT MAX(ts) AS ts FROM alerts WHERE rule_id=? AND ticker=?", (rule_id, ticker)
    ).fetchone()
    return datetime.fromisoformat(row["ts"]) if row and row["ts"] else None


def notices_pushed_since(conn: sqlite3.Connection, since: datetime) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM alerts WHERE level='notice' AND pushed=1 AND ts>=?", (_ts(since),)
    ).fetchone()
    return row["n"]


def record_run(conn: sqlite3.Connection, job: str, ts: datetime, ok: bool, detail: str = "") -> None:
    conn.execute("INSERT INTO runs (job, ts, ok, detail) VALUES (?,?,?,?)", (job, _ts(ts), int(ok), detail))
    conn.commit()


def last_runs(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    rows = conn.execute(
        "SELECT r.* FROM runs r JOIN (SELECT job, MAX(id) AS id FROM runs GROUP BY job) m USING (job, id)"
    ).fetchall()
    return {r["job"]: r for r in rows}


def recent_alerts(conn: sqlite3.Connection, limit: int = 50, ticker: str | None = None) -> list[sqlite3.Row]:
    if ticker:
        return conn.execute(
            "SELECT * FROM alerts WHERE ticker=? ORDER BY ts DESC, id DESC LIMIT ?", (ticker, limit)
        ).fetchall()
    return conn.execute("SELECT * FROM alerts ORDER BY ts DESC, id DESC LIMIT ?", (limit,)).fetchall()


# --- tracked list (editable from the dashboard) ---------------------------------

TIERS = ("holding", "watch")


def apply_tracked(conn: sqlite3.Connection, cfg) -> None:
    """Make the DB the source of truth for holdings/watchlist.

    First call seeds the table from the YAML lists; afterwards YAML lists are ignored,
    so dashboard edits survive deploys (which overwrite config/).
    """
    if conn.execute("SELECT 1 FROM meta WHERE key='tracked_seeded'").fetchone() is None:
        now = _ts(datetime.now(timezone.utc))
        for t in cfg.holdings:
            conn.execute("INSERT OR IGNORE INTO tracked VALUES (?, 'holding', ?)", (t, now))
        for t in cfg.watchlist:
            conn.execute("INSERT OR IGNORE INTO tracked VALUES (?, 'watch', ?)", (t, now))
        conn.execute("INSERT INTO meta VALUES ('tracked_seeded', ?)", (now,))
        conn.commit()
    rows = conn.execute("SELECT ticker, tier FROM tracked ORDER BY added_at, ticker").fetchall()
    cfg.holdings = [r["ticker"] for r in rows if r["tier"] == "holding"]
    cfg.watchlist = [r["ticker"] for r in rows if r["tier"] == "watch"]


def set_tracked(conn: sqlite3.Connection, ticker: str, tier: str, now: datetime) -> None:
    if tier not in TIERS:
        raise ValueError(tier)
    conn.execute("INSERT INTO tracked VALUES (?, ?, ?) ON CONFLICT(ticker) DO UPDATE SET tier=excluded.tier",
                 (ticker, tier, _ts(now)))
    conn.commit()


def remove_tracked(conn: sqlite3.Connection, ticker: str) -> None:
    conn.execute("DELETE FROM tracked WHERE ticker=?", (ticker,))
    conn.commit()
