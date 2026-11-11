from datetime import datetime, timezone

from radar import db
from radar.__main__ import main
from radar.collectors.prices import fetch_quotes
from radar.models import Fire, Quote
from radar.notify import make_pusher

NOW = datetime(2026, 11, 5, 20, 0, tzinfo=timezone.utc)

FAST = {"lastPrice": 410.0, "previousClose": 400.0, "yearHigh": 470.0, "yearLow": 350.0}


class FakeTicker:
    def __init__(self, sym, fail=False, info_fail=False):
        if fail:
            raise RuntimeError("boom")
        self.fast_info = FAST
        self._info_fail = info_fail

    @property
    def info(self):
        if self._info_fail:
            raise RuntimeError("info down")
        return {"trailingPE": 31.5}


def test_fetch_quotes_happy_and_skip_failures():
    def factory(sym):
        return FakeTicker(sym, fail=(sym == "BAD"), info_fail=(sym == "NOPE"))

    quotes = fetch_quotes(["MSFT", "BAD", "NOPE"], NOW, ticker_factory=factory)
    assert [q.ticker for q in quotes] == ["MSFT", "NOPE"]
    assert quotes[0] == Quote("MSFT", NOW, 410.0, 400.0, 31.5, 470.0, 350.0)
    assert quotes[1].pe is None


class FakeResp:
    def raise_for_status(self):
        pass


def test_pusher_payload():
    calls = []

    def post(url, json, timeout):
        calls.append((url, json))
        return FakeResp()

    push = make_pusher("https://ntfy.sh", "topic-x", "https://invest.example", post=post)
    push(Fire("hu", "MSFT", "urgent", "MSFT ▼9%", "body"))
    push(Fire("digest", "*", "notice", "另有 2 条", "b"))
    (url, p1), (_, p2) = calls
    assert url == "https://ntfy.sh"
    assert p1["topic"] == "topic-x" and p1["title"] == "MSFT ▼9%" and p1["priority"] == 4
    assert p1["click"] == "https://invest.example/t/MSFT"
    assert p2["priority"] == 3 and p2["click"] == "https://invest.example/"


def test_cli_collect_dry_run(tmp_path, monkeypatch):
    (tmp_path / "watchlist.yaml").write_text("holdings: [MSFT]\nwatchlist: []\n")
    (tmp_path / "rules.yaml").write_text(
        "rules:\n  - {id: hu, kind: pct_change, tier: holdings, threshold: 1, level: urgent}\n"
    )
    monkeypatch.setenv("RADAR_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("RADAR_DB", str(tmp_path / "x.db"))
    monkeypatch.setenv("NTFY_TOPIC", "")
    monkeypatch.setattr(
        "radar.__main__.fetch_quotes",
        lambda tickers, now: [Quote(t, now, 410.0, 400.0) for t in tickers],
    )
    monkeypatch.setattr("radar.__main__.backfill_history", lambda conn, tickers: 0)
    assert main(["--env-file", str(tmp_path / "none.env"), "collect"]) == 0
    conn = db.connect(tmp_path / "x.db")
    assert len(db.latest_quotes(conn)) == 1
    assert db.recent_alerts(conn)[0]["rule_id"] == "hu"


def test_backfill_history_only_when_missing(tmp_path):
    import pandas as pd

    from radar.collectors.prices import backfill_history

    idx = pd.to_datetime(["2026-01-02", "2026-01-05"]).tz_localize("America/New_York")
    calls = []

    class T:
        def __init__(self, sym):
            calls.append(sym)

        def history(self, **kw):
            return pd.DataFrame({"Close": [100.0, 102.0]}, index=idx)

    conn = db.connect(tmp_path / "b.db")
    assert backfill_history(conn, ["MSFT"], ticker_factory=T) == 2
    rows = db.price_history(conn, "MSFT")
    assert [r["price"] for r in rows] == [100.0, 102.0] and rows[1]["prev_close"] == 100.0
    assert backfill_history(conn, ["MSFT"], ticker_factory=T) == 0 and calls == ["MSFT"]
