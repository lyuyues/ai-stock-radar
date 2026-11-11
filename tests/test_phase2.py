import json
from datetime import date, datetime, timedelta, timezone

import pytest

from radar import db
from radar.alerts import events
from radar.collectors import edgar, macro

TODAY = date(2026, 11, 6)
NOW = datetime(2026, 11, 6, 20, 0, tzinfo=timezone.utc)

FORM4 = """<?xml version="1.0"?>
<ownershipDocument>
  <reportingOwner>
    <reportingOwnerId><rptOwnerName>Doe Jane</rptOwnerName></reportingOwnerId>
    <reportingOwnerRelationship><isDirector>0</isDirector><officerTitle>CFO</officerTitle></reportingOwnerRelationship>
  </reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <transactionDate><value>2026-11-04</value></transactionDate>
      <transactionCoding><transactionCode>P</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>5000</value></transactionShares>
        <transactionPricePerShare><value>250.5</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
    </nonDerivativeTransaction>
    <nonDerivativeTransaction>
      <transactionDate><value>2026-11-04</value></transactionDate>
      <transactionCoding><transactionCode>F</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>10</value></transactionShares>
        <transactionPricePerShare><value>250</value></transactionPricePerShare>
      </transactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>"""

SUBMISSIONS = {"filings": {"recent": {
    "form": ["8-K", "4", "10-Q", "8-K"],
    "filingDate": ["2026-11-05", "2026-11-05", "2026-10-30", "2026-08-01"],
    "accessionNumber": ["0001-26-000001", "0001-26-000002", "0001-26-000003", "0001-26-000004"],
    "primaryDocument": ["d8k.htm", "xslF345X06/form4.xml", "q.htm", "old.htm"],
    "items": ["5.02", "", "", "8.01"],
}}}


class Resp:
    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload if not isinstance(self.payload, str) else json.loads(self.payload)

    @property
    def text(self):
        return self.payload if isinstance(self.payload, str) else json.dumps(self.payload)


def fake_get(url):
    if url.endswith("company_tickers.json"):
        return Resp({"0": {"cik_str": 789019, "ticker": "MSFT", "title": "MICROSOFT CORP"}})
    if "submissions" in url:
        return Resp(SUBMISSIONS)
    if url.endswith("form4.xml"):
        return Resp(FORM4)
    raise AssertionError(url)


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "p2.db")
    yield c
    c.close()


# ---- EDGAR ----

def test_parse_form4():
    txs = edgar.parse_form4(FORM4, "acc", "MSFT")
    assert [t.code for t in txs] == ["P", "F"]
    assert txs[0].owner == "Doe Jane" and txs[0].title == "CFO"
    assert txs[0].value == pytest.approx(1_252_500)


def test_collect_dedupes_and_strips_xsl(conn):
    new_8k, new_tx = edgar.collect(conn, ["MSFT", "SPY"], fake_get, TODAY, lookback_days=30)
    assert [f.accession for f in new_8k] == ["0001-26-000001"]
    assert len(new_tx) == 2
    url = conn.execute("SELECT url FROM filings WHERE form='4'").fetchone()["url"]
    assert url.endswith("/000126000002/form4.xml")
    again = edgar.collect(conn, ["MSFT"], fake_get, TODAY)
    assert again == ([], [])


def test_item_labels():
    assert edgar.item_labels("5.02,9.01") == "5.02 高管/董事变动"


# ---- event rules ----

TIER = lambda t: "holdings" if t == "MSFT" else "watchlist"  # noqa: E731


def test_filing_fires_item_filter_and_age():
    rule = {"id": "8k", "kind": "filing_8k", "tier": "holdings", "items": ["5.02"], "level": "urgent"}
    f_new = edgar.Filing("a1", "MSFT", 1, "8-K", "2026-11-05", "5.02", "u")
    f_old = edgar.Filing("a2", "MSFT", 1, "8-K", "2026-10-01", "5.02", "u")
    f_other = edgar.Filing("a3", "MSFT", 1, "8-K", "2026-11-05", "8.01", "u")
    fires = events.filing_fires([rule], [f_new, f_old, f_other], TIER, TODAY, edgar.item_labels)
    assert [f.rule_id for f in fires] == ["8k:a1"]
    assert fires[0].level == "urgent" and "高管" in fires[0].title


def test_insider_buy_only_purchases_over_min():
    rule = {"id": "ib", "kind": "insider_buy", "min_value": 1_000_000, "level": "notice"}
    txs = edgar.parse_form4(FORM4, "acc", "MSFT")
    fires = events.insider_buy_fires([rule], txs, TIER, TODAY)
    assert len(fires) == 1 and "$1.25M" in fires[0].title


def test_insider_cluster():
    rule = {"id": "ic", "kind": "insider_cluster", "min_insiders": 2, "days": 30, "level": "urgent"}
    buys = {"MSFT": [{"owner": "A", "accession": "x1", "value": 1e5},
                     {"owner": "B", "accession": "x2", "value": 2e5}],
            "NVDA": [{"owner": "A", "accession": "y1", "value": 1e5}]}
    fires = events.insider_cluster_fires([rule], buys, TIER)
    assert [(f.ticker, f.rule_id) for f in fires] == [("MSFT", "ic:x2")]


def test_earnings_fires():
    rule = {"id": "er", "kind": "earnings", "tier": "all", "level": "notice"}
    rows = [{"ticker": "MSFT", "date": "2026-11-05", "eps_est": 4.72, "eps_actual": 4.89, "surprise_pct": 3.59},
            {"ticker": "MSFT", "date": "2026-07-29", "eps_est": 4.2, "eps_actual": 4.7, "surprise_pct": 11.8}]
    fires = events.earnings_fires([rule], rows, TIER, TODAY)
    assert len(fires) == 1 and "超预期 +3.6%" in fires[0].title


def test_prob_shift():
    rule = {"id": "ps", "kind": "prob_shift", "threshold_pp": 15, "level": "notice"}
    s = [{"source": "kalshi", "market_id": "m", "event_title": "Fed Dec", "label": "Cut 25bp",
          "prob": 0.70, "prob_before": 0.40},
         {"source": "kalshi", "market_id": "n", "event_title": "Fed Dec", "label": "Hold",
          "prob": 0.30, "prob_before": 0.25},
         {"source": "kalshi", "market_id": "o", "event_title": "x", "label": "y", "prob": 0.9, "prob_before": None}]
    fires = events.prob_shift_fires([rule], s)
    assert [f.rule_id for f in fires] == ["ps:kalshi:m"] and "+30pp" in fires[0].title


def test_macro_cross():
    rule = {"id": "vix", "kind": "macro_cross", "series": "VIXCLS", "above": 30, "label": "VIX", "level": "notice"}
    fires = events.macro_cross_fires([rule], {"VIXCLS": [("d1", 28.0), ("d2", 31.0)]})
    assert len(fires) == 1 and fires[0].rule_id == "vix:d2"
    assert events.macro_cross_fires([rule], {"VIXCLS": [("d1", 31.0), ("d2", 32.0)]}) == []


# ---- macro collectors ----

def test_parse_fred_csv_skips_missing():
    assert macro.parse_fred_csv("observation_date,DGS10\n2026-11-04,4.90\n2026-11-05,.\n") == [("2026-11-04", 4.9)]


def test_parse_kalshi_uses_mid_when_tight():
    data = {"events": [{"event_ticker": "KXFED-26DEC", "title": "Fed funds rate after Dec 2026 meeting?",
                        "sub_title": "On Dec 9, 2026",
                        "markets": [{"ticker": "KXFED-26DEC-T3.50", "yes_sub_title": "Above 3.50%",
                                     "last_price_dollars": "0.6000", "yes_bid_dollars": "0.6400",
                                     "yes_ask_dollars": "0.6600", "close_time": "2026-12-09T18:55:00Z"},
                                    {"ticker": "KXFED-26DEC-T4.00", "yes_sub_title": "Above 4.00%",
                                     "last_price_dollars": "0.1000", "yes_bid_dollars": "0.0000",
                                     "yes_ask_dollars": "0.5000"}]}]}
    ms = macro.parse_kalshi_events(data)
    assert ms[0].prob == pytest.approx(0.65) and ms[1].prob == pytest.approx(0.10)
    assert "Dec 9" in ms[0].event_title


def test_parse_polymarket():
    data = [{"id": "1", "slug": "fed-dec", "title": "Fed decision in December?",
             "markets": [{"id": 11, "groupItemTitle": "25 bps decrease", "outcomePrices": '["0.82","0.18"]'},
                         {"id": 12, "groupItemTitle": "closed one", "outcomePrices": '["1","0"]', "closed": True}]}]
    ms = macro.parse_polymarket_events(data)
    assert len(ms) == 1 and ms[0].prob == pytest.approx(0.82) and ms[0].event_id == "fed-dec"


def test_shifts_uses_snapshot_older_than_window(conn):
    m_old = macro.Market("kalshi", "m", "e", "E", "L", None, 0.40)
    macro.store_markets(conn, [m_old], NOW - timedelta(hours=25))
    m_mid = macro.Market("kalshi", "m", "e", "E", "L", None, 0.55)
    macro.store_markets(conn, [m_mid], NOW - timedelta(hours=2))
    m_now = macro.Market("kalshi", "m", "e", "E", "L", None, 0.70)
    [s] = macro.shifts(conn, [m_now], NOW, hours=24)
    assert s["prob_before"] == pytest.approx(0.40)


def test_implied_rate():
    # P(>3.50)=1, P(>3.75)=0.6, P(>4.00)=0.1 → 3.50 + (1 + 0.6 + 0.1) * 0.25 = 3.925
    assert macro.implied_rate([(3.75, 0.6), (3.50, 1.0), (4.00, 0.1)]) == pytest.approx(3.925)
    # certain hold at 3.75 (P(>3.50)=1, P(>3.75)=0) → 3.75
    assert macro.implied_rate([(3.50, 1.0), (3.75, 0.0), (4.00, 0.0)]) == pytest.approx(3.75)
    assert macro.implied_rate([(3.5, 1.0)]) is None


def test_store_earnings_reports_newly_known(conn):
    r = {"ticker": "MSFT", "date": "2027-01-27", "eps_est": 4.86, "eps_actual": None, "surprise_pct": None}
    assert macro.store_earnings(conn, [r]) == []
    r2 = dict(r, eps_actual=5.0, surprise_pct=2.9)
    assert macro.store_earnings(conn, [r2]) == [r2]
    assert macro.store_earnings(conn, [r2]) == []


def test_parse_form4_skips_other_issuer():
    other = FORM4.replace("<ownershipDocument>", "<ownershipDocument><issuer><issuerCik>0000797468</issuerCik></issuer>", 1)
    assert "issuerCik" in other
    assert edgar.parse_form4(other, "acc", "BRK-B", 1067983) == []
    assert len(edgar.parse_form4(other, "acc", "OXY", 797468)) == 2


def test_filing_summaries(conn):
    from datetime import datetime, timezone
    from radar import filing_notes
    from types import SimpleNamespace
    conn.execute("INSERT INTO filings VALUES ('a1','MSFT',1,'8-K','2026-11-05','5.02','https://sec/d/x/main.htm')")
    conn.commit()
    pages = {"https://sec/d/x/main.htm": "<html><p>Item 5.02 &amp; CFO <b>resigned</b></p><script>x</script></html>",
             "https://sec/d/x/index.json": {"directory": {"item": [{"name": "main.htm"}, {"name": "ex99-1.htm"}, {"name": "googexhibit991q3.htm"}]}},
             "https://sec/d/x/ex99-1.htm": "<p>Press release: Jane Doe named CFO</p>",
             "https://sec/d/x/googexhibit991q3.htm": "<p>Revenue up 16%</p>"}

    def get(u):
        v = pages[u]
        return SimpleNamespace(text=v if isinstance(v, str) else "", json=lambda: v)
    seen = []
    n = filing_notes.run(conn, get, lambda p: seen.append(p) or "CFO 辞职，Jane Doe 接任。",
                         datetime(2026, 11, 10, tzinfo=timezone.utc))
    assert n == 1 and "CFO resigned" in seen[0] and "Jane Doe named CFO" in seen[0] and "Revenue up 16%" in seen[0] and "<script" not in seen[0]
    assert filing_notes.summaries(conn) == {"a1": "CFO 辞职，Jane Doe 接任。"}
    assert filing_notes.run(conn, get, lambda p: "x", datetime(2026, 11, 10, tzinfo=timezone.utc)) == 0


def test_filing_body_skips_cover():
    from radar import filing_notes
    t = "FORM 8-K ... Item 2.02 listed on cover? Emerging growth company ☐ ... Item 8.01 Other Events. Alphabet announced X."
    assert filing_notes.body(t).startswith("Item 8.01")
    assert filing_notes.body("no cover here") == "no cover here"


def test_exhibit_names():
    from radar import filing_notes
    names = ["0001403161-26-000126-index.html", "0001403161-26-000126.txt", "R1.htm", "v-20261027.htm",
             "q42026earningsrelease.htm", "banner.jpg", "livef8k3q26991.htm"]
    assert filing_notes.exhibit_names(names, "v-20261027.htm") == ["livef8k3q26991.htm", "q42026earningsrelease.htm"]
