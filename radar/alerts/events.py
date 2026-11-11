"""Event rules: turn new filings / transactions / observations into Fires. Pure functions.

Event fires embed the event id in rule_id (e.g. "8k_material:0001193125-26-448357") so the
24h cooldown is per event, not per rule.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import date, timedelta

from radar.models import Fire


def _rules(rules: list[dict], kind: str) -> list[dict]:
    return [r for r in rules if r["kind"] == kind]


def _tier_ok(rule: dict, tier: str) -> bool:
    return rule.get("tier", "all") in ("all", tier)


def filing_fires(rules, filings, tier_of: Callable[[str], str], today: date,
                 labels: Callable[[str], str]) -> list[Fire]:
    out = []
    for r in _rules(rules, "filing_8k"):
        wanted = set(r.get("items") or [])
        max_age = timedelta(days=r.get("max_age_days", 3))
        for f in filings:
            items = {c.strip() for c in f.items.split(",") if c.strip()}
            if not _tier_ok(r, tier_of(f.ticker)) or not (items & wanted):
                continue
            if date.fromisoformat(f.filed) < today - max_age:
                continue
            out.append(Fire(f"{r['id']}:{f.accession}", f.ticker, r["level"],
                            f"{f.ticker} 8-K：{labels(f.items)}", f"{f.filed} · {f.url}", once=True))
    return out


def insider_buy_fires(rules, txs, tier_of, today: date) -> list[Fire]:
    out = []
    for r in _rules(rules, "insider_buy"):
        max_age = timedelta(days=r.get("max_age_days", 14))
        for t in txs:
            if t.code != "P" or t.value is None or t.value < r["min_value"]:
                continue
            if not _tier_ok(r, tier_of(t.ticker)) or not t.tx_date:
                continue
            if date.fromisoformat(t.tx_date) < today - max_age:
                continue
            who = f"{t.owner}（{t.title}）" if t.title else t.owner
            out.append(Fire(f"{r['id']}:{t.accession}:{t.idx}", t.ticker, r["level"],
                            f"{t.ticker} 内部人买入 ${t.value / 1e6:.2f}M",
                            f"{who} {t.tx_date} 买入 {t.shares:,.0f} 股 @ {t.price:.2f}", once=True))
    return out


def insider_cluster_fires(rules, purchases_by_ticker: dict[str, list[dict]], tier_of) -> list[Fire]:
    """purchases_by_ticker: ticker -> open-market purchases within the rule window,
    each {"owner", "accession", "value"}; newest accession last."""
    out = []
    for r in _rules(rules, "insider_cluster"):
        for ticker, buys in purchases_by_ticker.items():
            owners = {b["owner"] for b in buys}
            if len(owners) < r.get("min_insiders", 2) or not _tier_ok(r, tier_of(ticker)):
                continue
            total = sum(b["value"] or 0 for b in buys)
            latest = max(b["accession"] for b in buys)
            out.append(Fire(f"{r['id']}:{latest}", ticker, r["level"],
                            f"{ticker} 内部人集体买入：{len(owners)} 人",
                            f"近 {r.get('days', 30)} 天共 ${total / 1e6:.2f}M：" + "、".join(sorted(owners)), once=True))
    return out


def earnings_fires(rules, rows, tier_of, today: date) -> list[Fire]:
    """rows: newly reported earnings dicts {ticker, date, eps_est, eps_actual, surprise_pct}."""
    out = []
    for r in _rules(rules, "earnings"):
        max_age = timedelta(days=r.get("max_age_days", 3))
        for e in rows:
            if e["eps_actual"] is None or not _tier_ok(r, tier_of(e["ticker"])):
                continue
            if date.fromisoformat(e["date"]) < today - max_age:
                continue
            s = e["surprise_pct"]
            verdict = "超预期" if s is not None and s > 0 else ("不及预期" if s is not None and s < 0 else "")
            est = f"{e['eps_est']:.2f}" if e["eps_est"] is not None else "—"
            out.append(Fire(f"{r['id']}:{e['date']}", e["ticker"], r["level"],
                            f"{e['ticker']} 财报{verdict} {s:+.1f}%" if s is not None else f"{e['ticker']} 发布财报",
                            f"EPS {e['eps_actual']:.2f} vs 预期 {est}（{e['date']}）", once=True))
    return out


def prob_shift_fires(rules, shifts: list[dict]) -> list[Fire]:
    """shifts: {source, market_id, event_title, label, prob, prob_before}."""
    out = []
    for r in _rules(rules, "prob_shift"):
        for s in shifts:
            if s["prob_before"] is None:
                continue
            delta = (s["prob"] - s["prob_before"]) * 100
            if abs(delta) < r["threshold_pp"]:
                continue
            out.append(Fire(f"{r['id']}:{s['source']}:{s['market_id']}", "MACRO", r["level"],
                            f"预期变化 {delta:+.0f}pp：{s['label']}",
                            f"{s['event_title']} · {s['label']}：{s['prob_before']:.0%} → {s['prob']:.0%}"
                            f"（{r.get('hours', 24)}h，{s['source']}）"))
    return out


def macro_cross_fires(rules, latest: dict[str, list[tuple[str, float]]]) -> list[Fire]:
    """latest: series -> last two observations [(date, value), ...] oldest first."""
    out = []
    for r in _rules(rules, "macro_cross"):
        obs = latest.get(r["series"]) or []
        if len(obs) < 2:
            continue
        (_, prev), (d, cur) = obs[-2], obs[-1]
        label = r.get("label", r["series"])
        if "above" in r and prev <= r["above"] < cur:
            out.append(Fire(f"{r['id']}:{d}", "MACRO", r["level"],
                            f"{label} 升破 {r['above']}", f"{label} {prev} → {cur}（{d}）", once=True))
        if "below" in r and prev >= r["below"] > cur:
            out.append(Fire(f"{r['id']}:{d}", "MACRO", r["level"],
                            f"{label} 跌破 {r['below']}", f"{label} {prev} → {cur}（{d}）", once=True))
    return out
