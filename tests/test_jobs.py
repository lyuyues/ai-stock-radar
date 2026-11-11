from datetime import datetime, timedelta, timezone

import pytest

from radar import db, jobs
from radar.config import Config
from radar.models import Quote

NOW = datetime(2026, 11, 6, 20, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "j.db")
    yield c
    c.close()


def test_run_job_records_success_and_failure(conn):
    assert jobs.run_job("a", lambda: "fine", conn, NOW)

    def bad():
        raise ValueError("nope")

    assert not jobs.run_job("b", bad, conn, NOW)
    last = db.last_runs(conn)
    assert last["a"]["ok"] == 1 and last["b"]["ok"] == 0 and "nope" in last["b"]["detail"]


def test_prices_job_raises_when_nothing_fetched(conn):
    cfg = Config(holdings=["MSFT"], watchlist=[])
    with pytest.raises(RuntimeError):
        jobs.prices(conn, cfg, NOW, lambda f: None, fetch=lambda t, n: [])
    out = jobs.prices(conn, cfg, NOW, lambda f: None, fetch=lambda t, n: [Quote("MSFT", n, 1.0, 1.0)])
    assert out.startswith("quotes=1/1")


def test_heartbeat(conn):
    db.record_run(conn, "prices", NOW - timedelta(hours=40), True)
    db.record_run(conn, "edgar", NOW - timedelta(hours=1), False, "HTTPError 403")
    db.record_run(conn, "macro", NOW - timedelta(hours=1), True)
    fires = jobs.heartbeat_fires(db.last_runs(conn), {"prices": 30, "edgar": 30, "macro": 30, "weekly": 192}, NOW)
    titles = sorted(f.title for f in fires)
    assert len(fires) == 2
    assert any("prices" in t and "40h" in t for t in titles)
    assert any("edgar" in t and "失败" in t for t in titles)


def test_cluster_purchases_only_for_new_buys(conn):
    conn.execute("INSERT INTO insider_tx VALUES ('a1',0,'MSFT','A','CEO','2026-11-01','P',10,100,1000)")
    conn.execute("INSERT INTO insider_tx VALUES ('a2',0,'MSFT','B','CFO','2026-11-04','P',10,100,1000)")
    conn.commit()
    cfg = Config(holdings=["MSFT"], watchlist=[],
                 rules=[{"id": "ic", "kind": "insider_cluster", "days": 30, "min_insiders": 2, "level": "urgent"}])

    class T:
        ticker, code = "MSFT", "P"

    assert jobs.cluster_purchases(conn, cfg, NOW.date(), []) == {}
    got = jobs.cluster_purchases(conn, cfg, NOW.date(), [T()])
    assert {r["owner"] for r in got["MSFT"]} == {"A", "B"}
