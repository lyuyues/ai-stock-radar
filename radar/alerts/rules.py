"""Alert rule evaluation. Pure functions: no I/O, no clock."""
from __future__ import annotations

from radar.models import LEVELS, Fire, Quote

# Rule kinds evaluated against price quotes; other kinds are event rules (alerts/events.py).
QUOTE_KINDS = {"pct_change", "level_cross"}


def _pct_change(rule: dict, quote: Quote) -> Fire | None:
    pct = quote.pct_change
    if pct is None or abs(pct) < rule["threshold"]:
        return None
    arrow = "▲" if pct > 0 else "▼"
    return Fire(
        rule["id"],
        quote.ticker,
        rule["level"],
        f"{quote.ticker} {arrow}{abs(pct):.1f}%",
        f"{quote.ticker} {quote.price:.2f}（较昨收 {quote.prev_close:.2f}，{pct:+.1f}%）",
    )


def _level_cross(rule: dict, quote: Quote, levels: dict, prev_price: float | None) -> Fire | None:
    if prev_price is None:
        return None
    lv = levels.get(quote.ticker) or {}
    below, above = lv.get("below"), lv.get("above")
    if below is not None and prev_price >= below > quote.price:
        return Fire(rule["id"], quote.ticker, rule["level"],
                    f"{quote.ticker} 跌破 {below}",
                    f"{quote.ticker} {quote.price:.2f}（上次 {prev_price:.2f}）")
    if above is not None and prev_price <= above < quote.price:
        return Fire(rule["id"], quote.ticker, rule["level"],
                    f"{quote.ticker} 突破 {above}",
                    f"{quote.ticker} {quote.price:.2f}（上次 {prev_price:.2f}）")
    return None


def evaluate(rule: dict, quote: Quote, tier: str, levels: dict, prev_price: float | None) -> Fire | None:
    """Evaluate one rule against one quote. Returns a Fire or None."""
    if rule.get("tier", "all") not in ("all", tier):
        return None
    kind = rule["kind"]
    if kind == "pct_change":
        return _pct_change(rule, quote)
    if kind == "level_cross":
        return _level_cross(rule, quote, levels, prev_price)
    raise ValueError(f"unknown rule kind: {kind}")


def evaluate_all(rules: list[dict], quote: Quote, tier: str, levels: dict,
                 prev_price: float | None) -> list[Fire]:
    """Evaluate all rules; for each rule kind keep only the most severe fire."""
    best: dict[str, Fire] = {}
    for rule in rules:
        fire = evaluate(rule, quote, tier, levels, prev_price)
        if fire is None:
            continue
        kind = rule["kind"]
        if kind not in best or LEVELS.index(fire.level) > LEVELS.index(best[kind].level):
            best[kind] = fire
    return list(best.values())
