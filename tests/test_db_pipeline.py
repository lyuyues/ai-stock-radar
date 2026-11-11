from datetime import datetime, timedelta, timezone

import pytest

from radar import db
from radar.config import Config
from radar.models import Fire, Quote
from radar.pipeline import run_cycle

UTC = timezone.utc
DAY = datetime(2026, 11, 5, 20, 0, tzinfo=UTC)  # 12:00 PST


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    yield c
    c.close()


def test_quotes_roundtrip(conn):
    db.insert_quote(conn, Quote("MSFT", DAY - timedelta(hours=1), 400, 395, 30.1, 470, 350))
    db.insert_quote(conn, Quote("MSFT", DAY, 410, 395))
    db.insert_quote(conn, Quote("NVDA", DAY, 180, 175))
    assert db.last_price_before(conn, "MSFT", DAY) == 400
    assert db.last_price_before(conn, "MSFT", DAY - timedelta(hours=1)) is None
    latest = {r["ticker"]: r["price"] for r in db.latest_quotes(conn)}
    assert latest == {"MSFT": 410, "NVDA": 180}
    assert [r["price"] for r in db.price_history(conn, "MSFT")] == [400, 410]


def test_alerts_roundtrip(conn):
    f = Fire("r", "MSFT", "notice", "t", "b")
    db.record_alert(conn, f, DAY, pushed=True)
    db.record_alert(conn, Fire("r2", "MSFT", "log", "t", "b"), DAY, pushed=False)
    assert db.last_fired(conn, "r", "MSFT") == DAY
    assert db.last_fired(conn, "r", "NVDA") is None
    assert db.notices_pushed_since(conn, DAY - timedelta(hours=1)) == 1
    assert len(db.recent_alerts(conn)) == 2
    assert len(db.recent_alerts(conn, ticker="NVDA")) == 0


def make_cfg():
    return Config(
        holdings=["MSFT"], watchlist=["NVDA"],
        levels={"NVDA": {"below": 150}},
        rules=[
            {"id": "hu", "kind": "pct_change", "tier": "holdings", "threshold": 8, "level": "urgent"},
            {"id": "hn", "kind": "pct_change", "tier": "holdings", "threshold": 5, "level": "notice"},
            {"id": "line", "kind": "level_cross", "tier": "all", "level": "notice"},
        ],
    )


def test_cycle_pushes_once_then_cooldown(conn):
    sent = []
    cfg = make_cfg()
    run_cycle(conn, cfg, [Quote("MSFT", DAY, 90, 100)], DAY, sent.append)
    later = DAY + timedelta(minutes=30)
    run_cycle(conn, cfg, [Quote("MSFT", later, 89, 100)], later, sent.append)
    assert [f.rule_id for f in sent] == ["hu"]


def test_cycle_level_cross_needs_two_points(conn):
    sent = []
    cfg = make_cfg()
    run_cycle(conn, cfg, [Quote("NVDA", DAY, 152, 152)], DAY, sent.append)
    t2 = DAY + timedelta(minutes=30)
    run_cycle(conn, cfg, [Quote("NVDA", t2, 149, 152)], t2, sent.append)
    assert [f.rule_id for f in sent] == ["line"]


def test_cycle_push_failure_recorded_unpushed(conn):
    def boom(f):
        raise RuntimeError("network")

    run_cycle(conn, make_cfg(), [Quote("MSFT", DAY, 90, 100)], DAY, boom)
    rows = db.recent_alerts(conn)
    assert len(rows) == 1 and rows[0]["pushed"] == 0


def test_once_event_deferred_in_quiet_hours_then_released(conn):
    from radar.pipeline import deliver

    night = datetime(2026, 11, 6, 7, 30, tzinfo=UTC)  # 23:30 PST
    morning = datetime(2026, 11, 6, 16, 0, tzinfo=UTC)  # 08:00 PST
    sent = []
    ev = Fire("8k:acc9", "NVDA", "notice", "NVDA 8-K", "b", once=True)
    deliver(conn, make_cfg(), [ev], night, sent.append)
    assert sent == []
    deliver(conn, make_cfg(), [ev], night, sent.append)  # re-detected: deduped, not queued twice
    deliver(conn, make_cfg(), [], morning, sent.append)
    assert [f.rule_id for f in sent] == ["8k:acc9"]
    deliver(conn, make_cfg(), [], morning, sent.append)
    assert len(sent) == 1
