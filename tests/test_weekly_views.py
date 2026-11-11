from datetime import datetime, timedelta, timezone

import pytest

from radar import db, views, weekly
from radar.collectors.macro import Market, store_markets
from radar.config import Config
from radar.models import Fire, Quote

NOW = datetime(2026, 11, 8, 3, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "w.db")
    db.insert_quote(c, Quote("MSFT", NOW - timedelta(days=8), 500, 500, 30, 560, 350))
    db.insert_quote(c, Quote("MSFT", NOW, 450, 460, 28, 560, 350))
    db.record_alert(c, Fire("hu", "MSFT", "urgent", "MSFT ▼9%", "body"), NOW - timedelta(days=1), True)
    c.execute("INSERT INTO filings VALUES ('a1','MSFT',1,'8-K','2026-11-05','5.02,9.01','http://x')")
    c.execute("INSERT INTO insider_tx VALUES ('a2',0,'MSFT','Doe','CFO','2026-11-04','P',10,100,1000)")
    c.execute("INSERT INTO earnings VALUES ('MSFT','2026-11-12',4.9,NULL,NULL)")
    c.executemany("INSERT INTO macro_obs VALUES (?,?,?)",
                  [("DFEDTARU", "2026-10-20", 4.0), ("DFEDTARU", "2026-11-05", 3.75), ("DGS10", "2026-10-29", 4.6),
                   ("DGS10", "2026-11-06", 4.84)])
    # Kalshi ladder for a meeting that already happened (closed 11-04) and one upcoming
    old = [Market("kalshi", f"KXFED-26NOV-T{k}", "KXFED-26NOV", "Fed Nov", f"Above {k}%", "2026-11-04T18:55:00Z", p)
           for k, p in [("3.25", 1.0), ("3.50", 0.9), ("3.75", 0.1), ("4.00", 0.0)]]
    store_markets(c, old, datetime(2026, 11, 3, tzinfo=timezone.utc))
    nxt = [Market("kalshi", f"KXFED-26DEC-T{k}", "KXFED-26DEC", "Fed Dec", f"Above {k}%", "2026-12-09T18:55:00Z", p)
           for k, p in [("3.25", 1.0), ("3.50", 0.4), ("3.75", 0.0)]]
    store_markets(c, [Market(m.source, m.market_id, m.event_id, m.event_title, m.label, m.close_time, 0.8 if "3.50" in m.market_id else m.prob) for m in nxt],
                  NOW - timedelta(days=8))
    store_markets(c, nxt, NOW)
    c.commit()
    yield c
    c.close()


def cfg():
    return Config(holdings=["MSFT"], watchlist=[], macro={"fred": {"DGS10": "10年期美债", "DFEDTARU": "利率上限"}})


def test_fed_path_and_gap(conn):
    path = {p["event"]: p for p in views.fed_path(conn, NOW)}
    nov, dec = path["Fed Nov"], path["Fed Dec"]
    # Nov: 3.25 + (1 + 0.9 + 0.1 + 0) * 0.25 = 3.75; actual 3.75 → 0bp
    assert nov["implied"] == pytest.approx(3.75) and nov["actual"] == 3.75 and nov["gap_bp"] == pytest.approx(0)
    # Dec: 3.25 + (1.0 + 0.4 + 0.0) * 0.25 = 3.60, not closed yet
    assert dec["implied"] == pytest.approx(3.60) and dec["actual"] is None


def test_market_changes_week(conn):
    top = views.market_changes(conn, NOW)[0]
    assert top["label"] == "Above 3.50%" and top["delta_pp"] == pytest.approx(-40)


def test_week_moves(conn):
    [m] = views.week_moves(conn, ["MSFT"], NOW)
    assert m["week_pct"] == pytest.approx(-10)


def test_bundle_contains_sections(conn):
    b = weekly.build_bundle(conn, cfg(), NOW)
    for s in ["MSFT", "-10.0%", "高管/董事变动", "买入 1 笔", "即将发布 2026-11-12", "10年期美债",
              "预期差 +0bp", "Above 3.50%", "Thesis 摘要"]:
        assert s in b, s


def test_generate_success_and_fallback(conn):
    rid, ok = weekly.generate(conn, cfg(), NOW, runner=lambda p: "# 周报\n一句话")
    assert ok and conn.execute("SELECT markdown FROM reports WHERE id=?", (rid,)).fetchone()[0].startswith("# 周报")

    def boom(p):
        raise RuntimeError("not logged in")

    rid2, ok2 = weekly.generate(conn, cfg(), NOW, runner=boom)
    md = conn.execute("SELECT markdown FROM reports WHERE id=?", (rid2,)).fetchone()[0]
    assert not ok2 and "not logged in" in md and "## 持仓" in md


def test_theses_matching(tmp_path, conn):
    d = tmp_path / "theses"
    d.mkdir()
    (d / "MSFT-2026-cloud.md").write_text("cloud thesis")
    (d / "NVDA-2024.md").write_text("nvda")
    (d / "_TEMPLATE.md").write_text("tpl")
    c = cfg()
    c.theses_dir = d
    b = weekly.build_bundle(conn, c, NOW)
    assert "cloud thesis" in b and "nvda" not in b and "tpl" not in b


def test_week_moves_none_without_history(tmp_path):
    c = db.connect(tmp_path / "n.db")
    db.insert_quote(c, Quote("NEW", NOW - timedelta(hours=2), 10, 10))
    db.insert_quote(c, Quote("NEW", NOW, 11, 10))
    [m] = views.week_moves(c, ["NEW"], NOW)
    assert m["week_pct"] is None
