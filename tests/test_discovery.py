from datetime import datetime, timezone

import pytest

from radar import db, discovery
from radar.models import Quote

NOW = datetime(2026, 11, 8, 3, 0, tzinfo=timezone.utc)

OPENINSIDER_HTML = """
<table>
<tr><th>X</th></tr>
<tr><td>D</td><td>2026-11-06 16:30:03</td><td>2026-11-05</td>
<td><b><a href="/TRNS" onmouseover="Tip()">TRNS</a></b></td><td>Transcat Inc</td><td>Instruments</td>
<td>2</td><td>P - Purchase</td><td>$98.96</td><td>+3,325</td><td>10,668</td><td>+45%</td><td>+$829,030</td></tr>
<tr><td>D</td><td>2026-11-06</td><td>2026-11-05</td>
<td><a href="/SELL">SELL</a></td><td>Seller Co</td><td>x</td>
<td>3</td><td>S - Sale</td><td>$1</td><td>-1</td><td>1</td><td>-1%</td><td>-$900,000</td></tr>
</table>"""

INFO_PREV = """<informationTable xmlns="http://www.sec.gov/edgar/document/thirteenf/informationtable">
<infoTable><nameOfIssuer>APPLE INC</nameOfIssuer><cusip>037833100</cusip>
<shrsOrPrnAmt><sshPrnamt>100</sshPrnamt></shrsOrPrnAmt></infoTable>
<infoTable><nameOfIssuer>CHEVRON CORP NEW</nameOfIssuer><cusip>166764100</cusip>
<shrsOrPrnAmt><sshPrnamt>100</sshPrnamt></shrsOrPrnAmt></infoTable>
</informationTable>"""

INFO_CUR = """<?xml version="1.0" encoding="UTF-8"?>
<ns1:informationTable xmlns:ns1="http://www.sec.gov/edgar/document/thirteenf/informationtable"
 xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:schemaLocation="http://x y.xsd">
<ns1:infoTable><ns1:nameOfIssuer>APPLE INC</ns1:nameOfIssuer><ns1:cusip>037833100</ns1:cusip>
<ns1:shrsOrPrnAmt><ns1:sshPrnamt>105</ns1:sshPrnamt></ns1:shrsOrPrnAmt></ns1:infoTable>
<ns1:infoTable><ns1:nameOfIssuer>CHEVRON CORP NEW</ns1:nameOfIssuer><ns1:cusip>166764100</ns1:cusip>
<ns1:shrsOrPrnAmt><ns1:sshPrnamt>150</ns1:sshPrnamt></ns1:shrsOrPrnAmt></ns1:infoTable>
<ns1:infoTable><ns1:nameOfIssuer>ALPHABET INC</ns1:nameOfIssuer><ns1:cusip>02079K305</ns1:cusip>
<ns1:shrsOrPrnAmt><ns1:sshPrnamt>50</ns1:sshPrnamt></ns1:shrsOrPrnAmt></ns1:infoTable>
<ns1:infoTable><ns1:nameOfIssuer>ALPHABET INC</ns1:nameOfIssuer><ns1:cusip>02079K305</ns1:cusip>
<ns1:shrsOrPrnAmt><ns1:sshPrnamt>10</ns1:sshPrnamt></ns1:shrsOrPrnAmt><ns1:putCall>Call</ns1:putCall></ns1:infoTable>
</ns1:informationTable>"""


def test_parse_openinsider_purchases_only():
    rows = discovery.parse_openinsider(OPENINSIDER_HTML)
    assert rows == [{"ticker": "TRNS", "company": "Transcat Inc", "trade_date": "2026-11-05", "filing_date": "2026-11-06",
                     "insiders": 2, "value": 829030.0}]


def test_13f_parse_and_diff():
    prev, cur = discovery.parse_13f_infotable(INFO_PREV), discovery.parse_13f_infotable(INFO_CUR)
    assert cur["02079K305"]["shares"] == 50  # call option skipped
    adds = dict(discovery.diff_13f(prev, cur))
    assert adds == {"CHEVRON CORP NEW": "加仓 +50%", "ALPHABET INC": "新建仓"}


def test_name_matching():
    m = discovery.name_to_ticker_map({"GOOGL": "Alphabet Inc.", "GOOG": "Alphabet Inc.", "CVX": "CHEVRON CORP"})
    assert m[discovery.norm_name("ALPHABET INC")] == "GOOGL"
    assert m[discovery.norm_name("CHEVRON CORP NEW")] == "CVX"


def test_theme_hits():
    quotes = {"NVDA": Quote("NVDA", NOW, 70, high52=100), "AMD": Quote("AMD", NOW, 95, high52=100)}
    hits = discovery.theme_hits({"AI": {"tickers": ["NVDA", "AMD", "MISSING"]}}, quotes)
    assert [h[0] for h in hits] == ["NVDA"] and "-30%" in hits[0][2]


def test_score_orders_by_channel_overlap():
    hits = {"A": discovery.Hit(), "B": discovery.Hit()}
    hits["A"].add("screen", "x")
    hits["B"].add("screen", "x")
    hits["B"].add("smart_money", "y")
    rows = discovery.score(hits)
    assert [r["ticker"] for r in rows] == ["B", "A"]
    assert rows[0]["channels"] == ["screen", "smart_money"]


def test_discover_end_to_end(tmp_path):
    conn = db.connect(tmp_path / "d.db")

    class R:
        def __init__(self, text):
            self.text = text

    def get(url):
        assert "openinsider" in url
        return R(OPENINSIDER_HTML)

    def screener(q, size, sortField, sortAsc):
        return {"quotes": [{"symbol": "TRNS", "shortName": "Transcat", "trailingPE": 20.0,
                            "fiftyTwoWeekHigh": 100, "regularMarketPrice": 90},
                           {"symbol": "XYZ", "shortName": "Xyz", "trailingPE": None}]}

    dcfg = {"screen": [["gt", "intradaymarketcap", 1e10], ["btwn", "peratio.lasttwelvemonths", 0, 30]],
            "themes": {"AI": {"tickers": ["NVDA"]}}}
    rows = discovery.discover(conn, dcfg, NOW, get,
                              fetch_quotes=lambda t, n: [Quote("NVDA", n, 70, high52=100)], screener=screener)
    assert rows[0]["ticker"] == "TRNS" and rows[0]["channels"] == ["screen", "smart_money"]
    d, latest = discovery.latest_candidates(conn)
    assert d == "2026-11-08" and {r["ticker"] for r in latest} == {"TRNS", "XYZ", "NVDA"}


def test_13f_stamp():
    assert discovery._13f_stamp("2026-06-30", "2026-08-14") == \
        "（操作于 2026Q2（04-01~06-30 之间，具体日期不公开），2026-08-14 披露）"
    assert discovery._13f_stamp("2026-09-30", None).startswith("（操作于 2026Q3（07-01~09-30")
    assert discovery._13f_stamp(None, None) == ""


def test_settings_roundtrip():
    from radar import discovery_settings as ds
    old = {"screen": [["gt", "intradaymarketcap", 1e10], ["gt", "quarterlyrevenuegrowth.quarterly", 15],
                      ["btwn", "peratio.lasttwelvemonths", 0, 30], ["gt", "beta", 1]],
           "themes": {"AI": {"tickers": ["NVDA", "AVGO"], "max_from_high": -30}},
           "funds_13f": {"Berkshire Hathaway": 1067983}, "openinsider": True}
    f = ds.to_form(old)
    assert f["mcap_b"] == 10 and f["pe_max"] == 30 and "AI: NVDA, AVGO" in f["themes"]
    form = {k: ("" if v is None else str(v)) for k, v in f.items()}
    form.update(themes="AI: NVDA, avgo\n能源：XOM，CVX", openinsider="", pe_min="", mcap_b="20", min_funds_13f="2")
    new = ds.from_form(form, old)
    assert ["gt", "beta", 1] in new["screen"] and ["gt", "intradaymarketcap", 20e9] in new["screen"]
    assert ["lt", "peratio.lasttwelvemonths", 30.0] in new["screen"]
    assert new["themes"]["AI"] == {"tickers": ["NVDA", "AVGO"], "max_from_high": -30}
    assert new["themes"]["能源"]["tickers"] == ["XOM", "CVX"]
    assert new["openinsider"] is False and new["min_funds_13f"] == 2
    import pytest
    with pytest.raises(ValueError):
        ds.from_form({**form, "themes": "no colon here"}, old)
    with pytest.raises(ValueError):
        ds.from_form({**form, "funds": "Berkshire: abc"}, old)


def test_candidate_review(tmp_path):
    from datetime import datetime, timezone
    from radar import candidate_review as cr, db
    conn = db.connect(tmp_path / "c.db")
    rows = [{"ticker": "MSFT", "score": 3.2, "channels": ["screen", "theme"], "detail": {"screen": ["x"]}},
            {"ticker": "TRNS", "score": 2.1, "channels": ["screen", "smart_money"], "detail": {"smart_money": ["2 名内部人买入"]}},
            {"ticker": "ZZZ", "score": 1.0, "channels": ["screen"], "detail": {}}]
    assert [r["ticker"] for r in cr.pick(rows, {"MSFT"}, 1)] == ["TRNS"]
    import sys
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
    from test_web import _fake_builder
    b, _ = _fake_builder()
    prompts = []
    out = cr.run(conn, "2026-11-10", rows, {"MSFT"}, 5, datetime(2026, 11, 10, tzinfo=timezone.utc),
                 lambda p: prompts.append(p) or "TRNS | 小盘工业，内部人加仓 | 估值不便宜\nZZZ ｜ 理由 ｜ 风险\nnoise",
                 builder=b)
    assert out == {"valued": 2, "reviewed": 2}
    assert "TRNS（渠道：screen+smart_money）" in prompts[0] and "MSFT" not in prompts[0]
    assert conn.execute("SELECT why FROM candidate_notes WHERE ticker='TRNS'").fetchone()[0] == "小盘工业，内部人加仓"


def test_profiles_fill(tmp_path):
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from radar import db, profiles
    conn = db.connect(tmp_path / "p.db")
    infos = {"TRNS": {"longName": "Transcat, Inc.", "sector": "Industrials", "industry": "Scientific Instruments",
                      "longBusinessSummary": "Calibration services."},
             "SMH": {"longName": "VanEck Semiconductor ETF", "quoteType": "ETF"}}
    f = lambda t: SimpleNamespace(info=infos[t])
    seen = []
    n = profiles.fill(conn, ["TRNS", "SMH", "TRNS"], datetime(2026, 11, 10, tzinfo=timezone.utc),
                      lambda p: seen.append(p) or "TRNS | Transcat | 给工业仪器做校准服务\nSMH ｜ VanEck 半导体 ETF ｜ 跟踪美股半导体龙头", f)
    assert n == 2 and "Calibration services" in seen[0]
    p = profiles.lookup(conn)
    assert p["TRNS"]["desc_zh"] == "给工业仪器做校准服务" and p["SMH"]["industry"] == "ETF"
    assert profiles.missing(conn, ["TRNS", "NEW"]) == ["NEW"]
    assert profiles.fill(conn, ["TRNS"], datetime(2026, 11, 10, tzinfo=timezone.utc), lambda p: 1 / 0, f) == 0
