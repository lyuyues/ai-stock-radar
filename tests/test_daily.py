from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from radar import daily, db
from radar.models import Quote

NOW = datetime(2026, 11, 10, 14, 25, tzinfo=timezone.utc)  # 09:25 New York


def cfg():
    return SimpleNamespace(all_tickers=["MSFT", "AMAT"], tier_of=lambda t: "holdings" if t == "MSFT" else "watchlist",
                           tz="America/Los_Angeles")


def fake_factory(infos):
    import pandas as pd

    # Bars for Fri 11-06 and Mon 11-09; at 09:25 Tue the last close is Monday's, whatever Yahoo's fields say.
    hist = pd.DataFrame({"Close": [90.0, 100.0]}, index=pd.to_datetime(["2026-11-06", "2026-11-09"]).tz_localize(daily.NY))
    return lambda s: SimpleNamespace(info=infos.get(s, {}), fast_info={"lastPrice": 101.0},
                                     history=lambda **kw: hist)


def test_fetch_premarket_uses_last_completed_close():
    f = fake_factory({"A": {"preMarketPrice": 105, "regularMarketPreviousClose": 90}, "B": {}})
    pre = daily.fetch_premarket(["A", "B"], NOW, f)
    assert pre == {"A": {"pre": 105.0, "prev": 100.0}, "B": {"pre": None, "prev": 100.0}}
    assert daily.fetch_gauges(NOW, f)["ES=F"] == {"last": 101.0, "prev": 100.0}


def test_bundle_and_generate(tmp_path):
    conn = db.connect(tmp_path / "d.db")
    conn.execute("INSERT INTO earnings VALUES ('AMAT','2026-11-12',4.06,NULL,NULL)")
    pre = {"MSFT": {"pre": 510.0, "prev": 500.0}, "AMAT": {"pre": None, "prev": 200.0}}
    gauges = {"ES=F": {"last": 6000.0, "prev": 6060.0}}
    b = daily.build_bundle(conn, cfg(), NOW, pre, gauges)
    for s in ["| MSFT | 持仓 | 500.00 | 510.00 | +2.0% |", "无盘前成交", "标普500期货", "-0.99%",
              "即将发布 2026-11-12", "09:25 纽约时间"]:
        assert s in b, s
    rid, ok = daily.generate(conn, cfg(), NOW, lambda p: "# 日报\n一句话", pre, gauges)
    row = conn.execute("SELECT kind, title FROM reports WHERE id=?", (rid,)).fetchone()
    assert ok and row["kind"] == "daily" and row["title"] == "日报 2026-11-10"


def test_kind_migration(tmp_path):
    import sqlite3
    p = tmp_path / "old.db"
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE reports (id INTEGER PRIMARY KEY, created TEXT NOT NULL, title TEXT NOT NULL, "
              "markdown TEXT NOT NULL, ok INTEGER NOT NULL)")
    c.execute("INSERT INTO reports (created, title, markdown, ok) VALUES ('t','周报','x',1)")
    c.commit()
    c.close()
    assert db.connect(p).execute("SELECT kind FROM reports").fetchone()[0] == "weekly"


@pytest.fixture
def web(tmp_path, monkeypatch):
    (tmp_path / "watchlist.yaml").write_text("holdings: [MSFT]\nwatchlist: []\n")
    (tmp_path / "rules.yaml").write_text("rules: []\n")
    (tmp_path / "macro.yaml").write_text("fred: {}\n")
    monkeypatch.setenv("RADAR_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("RADAR_DB", str(tmp_path / "r.db"))
    conn = db.connect(tmp_path / "r.db")
    conn.execute("INSERT INTO reports (created,title,markdown,ok,kind) VALUES ('2026-11-08T03:00:00+00:00','周报 W','# w',1,'weekly')")
    conn.execute("INSERT INTO reports (created,title,markdown,ok,kind) VALUES ('2026-11-10T14:28:00+00:00','日报 2026-11-10','# 今日一句话',1,'daily')")
    conn.commit()
    conn.close()
    from web.app import app
    return TestClient(app)


def test_daily_pages(web):
    r = web.get("/daily")
    assert r.status_code == 200 and "今日一句话" in r.text
    assert "日报 2026-11-10" not in web.get("/reports").text and "周报 W" in web.get("/reports").text
    assert web.get("/daily/2").status_code == 200
    assert web.get("/daily/1").status_code == 404  # weekly id is not a daily brief


def test_daily_empty(tmp_path, monkeypatch, web):
    assert web.get("/daily/999").status_code == 404


def test_scheduler_due():
    from web import scheduler
    scheduler.state.update(running=False, attempted=None)
    tue = lambda h, m: datetime(2026, 11, 10, h, m, tzinfo=daily.NY)
    assert scheduler.due(tue(9, 25), False)
    assert not scheduler.due(tue(9, 24), False)
    assert not scheduler.due(tue(9, 50), False)
    assert not scheduler.due(tue(9, 30), True)  # already have today's brief
    assert not scheduler.due(datetime(2026, 11, 8, 9, 30, tzinfo=daily.NY), False)  # Sunday
    scheduler.state["attempted"] = tue(9, 25).date()
    assert not scheduler.due(tue(9, 26), False)  # tried once today already
    scheduler.state["attempted"] = None
