"""SEC EDGAR: 8-K filings and Form 4 insider transactions for tracked tickers."""
from __future__ import annotations

import logging
import sqlite3
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, timedelta

from radar.http import Getter

log = logging.getLogger(__name__)

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{doc}"

ITEM_NAMES = {
    "1.01": "重大协议", "1.02": "终止重大协议", "1.03": "破产", "2.01": "完成收购/处置",
    "2.02": "财报", "2.05": "重组/退出成本", "2.06": "资产减值", "3.01": "退市通知",
    "4.01": "更换审计师", "4.02": "财报不可依赖（重述）", "5.02": "高管/董事变动",
    "5.07": "股东大会投票", "7.01": "Reg FD 披露", "8.01": "其他事件", "9.01": "附件",
}


@dataclass(frozen=True)
class Filing:
    accession: str
    ticker: str
    cik: int
    form: str
    filed: str
    items: str
    url: str


@dataclass(frozen=True)
class InsiderTx:
    accession: str
    idx: int
    ticker: str
    owner: str
    title: str
    tx_date: str
    code: str
    shares: float | None
    price: float | None

    @property
    def value(self) -> float | None:
        if self.shares is None or self.price is None:
            return None
        return self.shares * self.price


def load_cik_map(get: Getter) -> dict[str, int]:
    data = get(TICKERS_URL).json()
    return {row["ticker"].upper(): int(row["cik_str"]) for row in data.values()}


def load_cik_titles(get: Getter) -> dict[str, str]:
    """ticker -> company title (used to match 13F issuer names)."""
    data = get(TICKERS_URL).json()
    return {row["ticker"].upper(): row["title"] for row in data.values()}


def recent_filings(ticker: str, cik: int, get: Getter, forms: set[str], since: date) -> list[Filing]:
    recent = get(SUBMISSIONS_URL.format(cik=cik)).json()["filings"]["recent"]
    out = []
    for i, form in enumerate(recent["form"]):
        filed = recent["filingDate"][i]
        if form not in forms or filed < since.isoformat():
            continue
        acc = recent["accessionNumber"][i]
        doc = recent["primaryDocument"][i]
        if form == "4":
            doc = doc.split("/")[-1]  # strip xsl rendering dir → raw XML
        url = ARCHIVE_URL.format(cik=cik, acc=acc.replace("-", ""), doc=doc)
        out.append(Filing(acc, ticker, cik, form, filed, recent["items"][i] or "", url))
    return out


def _text(el: ET.Element | None, path: str) -> str | None:
    if el is None:
        return None
    node = el.find(path)
    if node is None or node.text is None:
        return None
    return node.text.strip()


def _num(s: str | None) -> float | None:
    try:
        return float(s) if s else None
    except ValueError:
        return None


def parse_form4(xml: str, accession: str, ticker: str, issuer_cik: int | None = None) -> list[InsiderTx]:
    """Transactions in `ticker`'s own stock. A company's filing list also holds Form 4s where the company
    is the *reporting owner* of another issuer (e.g. Berkshire buying OXY) — those are skipped."""
    root = ET.fromstring(xml)
    filed_for = _num(_text(root, "issuer/issuerCik"))
    if issuer_cik is not None and filed_for is not None and int(filed_for) != issuer_cik:
        return []
    owners = root.findall("reportingOwner")
    names = [(_text(o, "reportingOwnerId/rptOwnerName") or "?") for o in owners]
    rel = owners[0].find("reportingOwnerRelationship") if owners else None
    title = _text(rel, "officerTitle") or ("Director" if _text(rel, "isDirector") in ("1", "true") else "")
    out = []
    for i, tx in enumerate(root.findall("nonDerivativeTable/nonDerivativeTransaction")):
        out.append(InsiderTx(
            accession=accession, idx=i, ticker=ticker,
            owner="; ".join(names), title=title,
            tx_date=_text(tx, "transactionDate/value") or "",
            code=_text(tx, "transactionCoding/transactionCode") or "",
            shares=_num(_text(tx, "transactionAmounts/transactionShares/value")),
            price=_num(_text(tx, "transactionAmounts/transactionPricePerShare/value")),
        ))
    return out


# ---- storage ----

def store_filing(conn: sqlite3.Connection, f: Filing) -> bool:
    """Insert; returns True if the filing is new."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO filings VALUES (?,?,?,?,?,?,?)",
        (f.accession, f.ticker, f.cik, f.form, f.filed, f.items, f.url),
    )
    return cur.rowcount == 1


def store_tx(conn: sqlite3.Connection, t: InsiderTx) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO insider_tx VALUES (?,?,?,?,?,?,?,?,?,?)",
        (t.accession, t.idx, t.ticker, t.owner, t.title, t.tx_date, t.code, t.shares, t.price, t.value),
    )


def _purge_foreign_form4(conn: sqlite3.Connection, get: Getter) -> None:
    """One-time cleanup of insider rows stored before parse_form4 checked the issuer."""
    if conn.execute("SELECT 1 FROM meta WHERE key='form4_issuer_v1'").fetchone():
        return
    for f in conn.execute("SELECT accession, cik, url FROM filings WHERE form='4'").fetchall():
        try:
            if not parse_form4(get(f["url"]).text, f["accession"], "", f["cik"]):
                conn.execute("DELETE FROM insider_tx WHERE accession=?", (f["accession"],))
        except Exception as e:
            log.warning("form4 recheck failed %s: %s", f["url"], e)
            return  # retry next run
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('form4_issuer_v1', '1')")
    conn.commit()


def collect(conn: sqlite3.Connection, tickers: list[str], get: Getter, today: date,
            lookback_days: int = 30) -> tuple[list[Filing], list[InsiderTx]]:
    """Fetch recent 8-K / Form 4 for tickers. Returns (new 8-K filings, new insider txs)."""
    ciks = load_cik_map(get)
    _purge_foreign_form4(conn, get)
    since = today - timedelta(days=lookback_days)
    new_8k: list[Filing] = []
    new_tx: list[InsiderTx] = []
    for ticker in tickers:
        cik = ciks.get(ticker.replace(".", "-")) or ciks.get(ticker)
        if cik is None:
            log.info("no CIK for %s (ETF/foreign?) — skipped", ticker)
            continue
        try:
            filings = recent_filings(ticker, cik, get, {"8-K", "4"}, since)
        except Exception as e:
            log.warning("submissions failed for %s: %s", ticker, e)
            continue
        for f in filings:
            if not store_filing(conn, f):
                continue
            if f.form == "8-K":
                new_8k.append(f)
                continue
            try:
                txs = parse_form4(get(f.url).text, f.accession, ticker, cik)
            except Exception as e:
                log.warning("form4 parse failed %s: %s", f.url, e)
                continue
            for t in txs:
                store_tx(conn, t)
            new_tx += txs
    conn.commit()
    return new_8k, new_tx


def item_labels(items: str) -> str:
    codes = [c.strip() for c in items.split(",") if c.strip() and c.strip() != "9.01"]
    return "、".join(f"{c} {ITEM_NAMES.get(c, '')}".strip() for c in codes) or "附件"
