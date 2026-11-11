"""One- or two-sentence Chinese summary per 8-K (primary document + EX-99 press releases), by Claude."""
from __future__ import annotations

import html
import logging
import re
import sqlite3
from datetime import datetime, timedelta

from radar.collectors.edgar import item_labels
from radar.db import _ts
from radar.http import Getter
from radar.weekly import Runner

log = logging.getLogger(__name__)

PROMPT = """下面是 {ticker} 在 {filed} 向 SEC 提交的 8-K（事项：{items}）。用中文写 1–2 句简要，不超过 90 字：
说清楚实质内容（发生了什么、关键数字/人名/日期）。如果是例行文件（如只是附上财报新闻稿），就概括新闻稿最重要的一两个数字。
不要客套、不要加标题，只输出简要本身。

{text}"""

_TAG = re.compile(r"<(script|style)[^>]*>.*?</\1>|<[^>]+>", re.S | re.I)


def to_text(raw: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(_TAG.sub(" ", raw))).strip()


def body(text: str) -> str:
    """Drop the 8-K cover page (registrant info, checkboxes) — start at the first Item after it."""
    low = text.lower()
    cover_end = max(low.rfind("emerging growth company"), low.find("securities registered pursuant to section 12(b)"))
    m = re.search(r"item\s+\d\.\d\d", text[cover_end:], re.I) if cover_end > 0 else None
    return text[cover_end + m.start():] if m else text


def exhibit_names(names: list[str], primary: str) -> list[str]:
    """Attached documents (press releases etc.). Names vary by filer (ex99-1.htm, exhibit991.htm,
    q42026earningsrelease.htm, livef8k3q26991.htm), so take every HTML doc except the primary one and
    EDGAR's generated files; ones that look like EX-99 first."""
    docs = [n for n in names if n.lower().endswith((".htm", ".html")) and n != primary
            and not re.match(r"^(R\d+\.htm|\d{10}-\d{2}-\d{6}.*)$", n)]
    return sorted(docs, key=lambda n: not re.search(r"99", n))


def fetch_text(get: Getter, url: str, max_chars: int = 16000) -> str:
    parts = [body(to_text(get(url).text))[:6000]]
    base = url.rsplit("/", 1)[0]
    try:
        items = get(base + "/index.json").json()["directory"]["item"]
        exhibits = exhibit_names([i["name"] for i in items], url.rsplit("/", 1)[1])
        for name in exhibits[:2]:
            parts.append(f"[附件 {name}] " + to_text(get(f"{base}/{name}").text)[:8000])
    except Exception as e:  # exhibits are optional
        log.info("no exhibit index for %s: %s", url, e)
    return "\n\n".join(parts)[:max_chars]


def pending(conn: sqlite3.Connection, today, days: int = 60) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT f.* FROM filings f LEFT JOIN filing_notes n USING (accession) "
        "WHERE f.form='8-K' AND n.accession IS NULL AND f.filed>=? ORDER BY f.filed DESC",
        ((today - timedelta(days=days)).isoformat(),)).fetchall()


def run(conn: sqlite3.Connection, get: Getter, runner: Runner, now: datetime, limit: int = 8) -> int:
    if not conn.execute("SELECT 1 FROM meta WHERE key='filing_notes_v4'").fetchone():
        # earlier versions missed the cover page / "exhibit991"-style names → redo summaries without content
        conn.execute("DELETE FROM filing_notes WHERE summary LIKE '%截断%' OR summary LIKE '%看不到%' "
                     "OR summary LIKE '%没有附具体%' OR summary LIKE '%未附%' OR summary LIKE '%没有具体%' "
                     "OR summary LIKE '%未披露具体%' OR summary LIKE '%没有披露具体%'")
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('filing_notes_v4', '1')")
        conn.commit()
    n = 0
    for f in pending(conn, now.date())[:limit]:
        try:
            text = fetch_text(get, f["url"])
            out = runner(PROMPT.format(ticker=f["ticker"], filed=f["filed"],
                                       items=item_labels(f["items"] or ""), text=text)).strip()
            conn.execute("INSERT OR REPLACE INTO filing_notes VALUES (?,?,?)", (f["accession"], out[:400], _ts(now)))
            conn.commit()
            n += 1
        except Exception as e:
            log.warning("8-K summary failed %s: %s", f["url"], e)
    return n


def summaries(conn: sqlite3.Connection) -> dict[str, str]:
    return {r["accession"]: r["summary"] for r in conn.execute("SELECT accession, summary FROM filing_notes")}
