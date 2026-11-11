"""Weekly candidate discovery: quant screen + themes + smart money, scored by channel overlap."""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from radar.collectors.edgar import SUBMISSIONS_URL, load_cik_titles
from radar.http import Getter

log = logging.getLogger(__name__)

OPENINSIDER_CLUSTER = "http://openinsider.com/latest-cluster-buys"
INDEX_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/index.json"
DOC_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{name}"


@dataclass
class Hit:
    channels: set[str] = field(default_factory=set)
    detail: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))

    def add(self, channel: str, note: str) -> None:
        self.channels.add(channel)
        self.detail[channel].append(note)


# ---- channel 1: quant screen (Yahoo screener via yfinance) ----

def run_screen(criteria: list[list], size: int = 50, screener=None) -> list[dict]:
    """criteria: [[op, field, value...], ...] ANDed, e.g. ["gt", "intradaymarketcap", 1e10]
    or ["btwn", "peratio.lasttwelvemonths", 0, 30]."""
    if screener is None:
        import yfinance as yf

        screener = yf.screen
    from yfinance import EquityQuery as Q

    q = Q("and", [Q("eq", ["region", "us"])] + [Q(c[0], list(c[1:])) for c in criteria])
    res = screener(q, size=size, sortField="intradaymarketcap", sortAsc=False)
    out = []
    for x in res.get("quotes", []):
        hi, px = x.get("fiftyTwoWeekHigh"), x.get("regularMarketPrice")
        out.append({"ticker": x["symbol"], "name": x.get("shortName") or x.get("displayName") or "",
                    "pe": x.get("trailingPE"), "from_high": (px / hi - 1) * 100 if px and hi else None})
    return out


# ---- channel 2: themes ----

def theme_hits(themes: dict, quotes_by_ticker: dict, default_max_from_high: float = -20) -> list[tuple[str, str, str]]:
    """Return (ticker, theme, note) for theme members trading well below their 52w high."""
    out = []
    for name, spec in themes.items():
        limit = spec.get("max_from_high", default_max_from_high)
        for t in spec.get("tickers", []):
            q = quotes_by_ticker.get(t)
            if q is None or not q.high52:
                continue
            fh = (q.price / q.high52 - 1) * 100
            if fh <= limit:
                out.append((t, name, f"{name}：距52周高 {fh:.0f}%"))
    return out


# ---- channel 3a: OpenInsider cluster buys (market-wide) ----

def parse_openinsider(html: str) -> list[dict]:
    out = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        if len(cells) < 13:
            continue
        m = re.search(r'href="/([A-Z.\-]{1,6})"', cells[3])
        text = [re.sub(r"<[^>]+>", "", c).strip() for c in cells]
        if not m or not text[7].startswith("P"):
            continue
        try:
            value = float(re.sub(r"[^\d.]", "", text[12]) or 0)
            n = int(re.sub(r"\D", "", text[6]) or 0)
        except ValueError:
            continue
        out.append({"ticker": m.group(1), "company": text[4], "trade_date": text[2], "filing_date": text[1][:10],
                    "insiders": n, "value": value})
    return out


# ---- channel 3b: 13F adds by tracked funds (SEC EDGAR) ----

_SUFFIX = re.compile(r"\b(INC|CORP|CORPORATION|CO|LTD|PLC|HOLDINGS?|GROUP|COMPANY|THE|NEW|DEL|CL(ASS)? [A-C]|COM|SA|NV|AG|LP)\b")


def norm_name(name: str) -> str:
    s = re.sub(r"[^A-Z0-9 ]", " ", name.upper())
    s = _SUFFIX.sub(" ", s)
    return " ".join(s.split())


def name_to_ticker_map(titles: dict[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for ticker, title in titles.items():  # SEC list is ordered by size; first wins
        out.setdefault(norm_name(title), ticker)
    return out


def parse_13f_infotable(xml: str) -> dict[str, dict]:
    """cusip -> {name, shares}; skips option positions."""
    root = ET.fromstring(xml.encode() if isinstance(xml, str) else xml)
    for el in root.iter():  # use local names regardless of namespace prefixes
        el.tag = el.tag.rsplit("}", 1)[-1]
    out: dict[str, dict] = {}
    for it in root.iter("infoTable"):
        if (it.findtext("putCall") or "").strip():
            continue
        cusip = (it.findtext("cusip") or "").strip()
        shares = float(it.findtext("shrsOrPrnAmt/sshPrnamt") or 0)
        rec = out.setdefault(cusip, {"name": (it.findtext("nameOfIssuer") or "").strip(), "shares": 0.0})
        rec["shares"] += shares
    return out


def diff_13f(prev: dict[str, dict], cur: dict[str, dict], min_increase: float = 0.2) -> list[tuple[str, str]]:
    """Return (issuer name, 'new'|'+x%') for new or materially increased positions."""
    out = []
    for cusip, rec in cur.items():
        before = prev.get(cusip)
        if before is None or before["shares"] == 0:
            out.append((rec["name"], "新建仓"))
        elif rec["shares"] / before["shares"] - 1 >= min_increase:
            out.append((rec["name"], f"加仓 {rec['shares'] / before['shares'] - 1:+.0%}"))
    return out


def fund_13f_adds(cik: int, get: Getter, min_increase: float = 0.2) -> list[tuple[str, str]]:
    """Positions new / added in the latest 13F-HR vs the previous one.

    Each note carries the holdings quarter and filing date: 13F is filed up to 45 days
    after quarter end, so the data is always stale and the reader must see by how much.
    """
    recent = get(SUBMISSIONS_URL.format(cik=cik)).json()["filings"]["recent"]
    idx = [i for i, f in enumerate(recent["form"]) if f == "13F-HR"][:2]
    if len(idx) < 2:
        return []
    accs = [recent["accessionNumber"][i] for i in idx]
    stamp = _13f_stamp(recent.get("reportDate", [None] * (idx[0] + 1))[idx[0]],
                       recent.get("filingDate", [None] * (idx[0] + 1))[idx[0]])
    tables = []
    for acc in accs:
        a = acc.replace("-", "")
        items = get(INDEX_URL.format(cik=cik, acc=a)).json()["directory"]["item"]
        info = next((i["name"] for i in items
                     if i["name"].endswith(".xml") and i["name"] != "primary_doc.xml"), None)
        if info is None:
            return []
        tables.append(parse_13f_infotable(get(DOC_URL.format(cik=cik, acc=a, name=info)).text))
    return [(issuer, f"{how}{stamp}") for issuer, how in diff_13f(tables[1], tables[0], min_increase)]


def _13f_stamp(report_date: str | None, filing_date: str | None) -> str:
    """13F = quarter-end snapshot: the trade happened sometime inside that quarter
    (exact date unknown), and the public learned of it on the filing date."""
    parts = []
    if report_date:
        y, m = int(report_date[:4]), int(report_date[5:7])
        q = (m - 1) // 3 + 1
        start = f"{y}-{3 * q - 2:02d}-01"
        parts.append(f"操作于 {y}Q{q}（{start[5:]}~{report_date[5:]} 之间，具体日期不公开）")
    if filing_date:
        parts.append(f"{filing_date} 披露")
    return f"（{'，'.join(parts)}）" if parts else ""


# ---- combine ----

def score(hits: dict[str, Hit]) -> list[dict]:
    rows = []
    for t, h in hits.items():
        strength = sum(len(v) for v in h.detail.values())
        rows.append({"ticker": t, "score": len(h.channels) + min(strength, 9) / 10,
                     "channels": sorted(h.channels), "detail": {k: v for k, v in h.detail.items()}})
    return sorted(rows, key=lambda r: (-r["score"], r["ticker"]))


def store_candidates(conn: sqlite3.Connection, run_date: str, rows: list[dict]) -> None:
    conn.execute("DELETE FROM candidates WHERE run_date=?", (run_date,))
    conn.executemany("INSERT INTO candidates VALUES (?,?,?,?,?)",
                     [(run_date, r["ticker"], r["score"], json.dumps(r["channels"]),
                       json.dumps(r["detail"], ensure_ascii=False)) for r in rows])
    conn.commit()


def latest_candidates(conn: sqlite3.Connection, limit: int = 30) -> tuple[str | None, list[dict]]:
    row = conn.execute("SELECT MAX(run_date) AS d FROM candidates").fetchone()
    if not row or not row["d"]:
        return None, []
    rows = conn.execute("SELECT * FROM candidates WHERE run_date=? ORDER BY score DESC, ticker LIMIT ?",
                        (row["d"], limit)).fetchall()
    return row["d"], [{"ticker": r["ticker"], "score": r["score"], "channels": json.loads(r["channels"]),
                       "detail": json.loads(r["detail"])} for r in rows]


def discover(conn: sqlite3.Connection, dcfg: dict, now: datetime, get: Getter,
             fetch_quotes=None, screener=None) -> list[dict]:
    hits: dict[str, Hit] = defaultdict(Hit)
    errors = []

    if dcfg.get("screen"):
        try:
            for r in run_screen(dcfg["screen"], dcfg.get("screen_size", 50), screener):
                hits[r["ticker"]].add("screen", f"{r['name']} P/E {r['pe']:.1f}" if r["pe"] else r["name"])
        except Exception as e:
            errors.append(f"screen: {e}")

    themes = dcfg.get("themes") or {}
    if themes and fetch_quotes is not None:
        tickers = sorted({t for spec in themes.values() for t in spec.get("tickers", [])})
        quotes = {q.ticker: q for q in fetch_quotes(tickers, now)}
        for t, _theme, note in theme_hits(themes, quotes, dcfg.get("theme_max_from_high", -20)):
            hits[t].add("theme", note)

    if dcfg.get("openinsider", True):
        try:
            for r in parse_openinsider(get(OPENINSIDER_CLUSTER).text):
                if r["value"] >= dcfg.get("insider_min_value", 500_000):
                    hits[r["ticker"]].add("smart_money", f"{r['insiders']} 名内部人买入 ${r['value'] / 1e6:.1f}M（交易于 {r['trade_date']}，{r['filing_date']} 披露）")
        except Exception as e:
            errors.append(f"openinsider: {e}")

    funds = dcfg.get("funds_13f") or {}
    if funds:
        try:
            names = name_to_ticker_map(load_cik_titles(get))
        except Exception as e:
            names = {}
            errors.append(f"sec titles: {e}")
        by_ticker: dict[str, list[str]] = defaultdict(list)
        for fund, cik in funds.items():
            try:
                for issuer, how in fund_13f_adds(int(cik), get, dcfg.get("min_increase_13f", 0.2)):
                    t = names.get(norm_name(issuer))
                    if t:
                        by_ticker[t].append(f"{fund} {how}")
            except Exception as e:
                errors.append(f"13F {fund}: {e}")
        min_funds = dcfg.get("min_funds_13f", 1)
        for t, notes in by_ticker.items():
            if len(notes) >= min_funds:
                for n in notes:
                    hits[t].add("smart_money", n)

    for e in errors:
        log.warning("discover: %s", e)
    rows = score(hits)
    store_candidates(conn, now.date().isoformat(), rows)
    if not rows and errors:
        raise RuntimeError("; ".join(errors))
    return rows
