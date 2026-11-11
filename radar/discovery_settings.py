"""Dashboard-editable discovery settings (stored in DB meta, seeded from config/discovery.yaml).

The form exposes the common knobs; other screen criteria in the YAML are kept as-is."""
from __future__ import annotations

import json
import re
import sqlite3

KEY = "discovery_cfg"
FIELDS = {"intradaymarketcap": "mcap", "quarterlyrevenuegrowth.quarterly": "rev_growth",
          "peratio.lasttwelvemonths": "pe"}


def apply(conn: sqlite3.Connection, cfg) -> None:
    r = conn.execute("SELECT value FROM meta WHERE key=?", (KEY,)).fetchone()
    if r:
        cfg.discovery = json.loads(r["value"])


def save(conn: sqlite3.Connection, dcfg: dict) -> None:
    conn.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (KEY, json.dumps(dcfg, ensure_ascii=False)))
    conn.commit()


def to_form(d: dict) -> dict:
    f = {"mcap_b": None, "rev_growth": None, "pe_min": None, "pe_max": None}
    for c in d.get("screen") or []:
        key = FIELDS.get(c[1])
        if key == "mcap":
            f["mcap_b"] = c[2] / 1e9
        elif key == "rev_growth":
            f["rev_growth"] = c[2]
        elif key == "pe":
            f["pe_min"], f["pe_max"] = (c[2], c[3]) if c[0] == "btwn" else (c[2], None)
    f.update({
        "screen_size": d.get("screen_size", 50),
        "theme_max_from_high": d.get("theme_max_from_high", -20),
        "themes": "\n".join(f"{name}: {', '.join(spec.get('tickers', []))}" for name, spec in (d.get("themes") or {}).items()),
        "openinsider": d.get("openinsider", True),
        "insider_min_m": d.get("insider_min_value", 500_000) / 1e6,
        "funds": "\n".join(f"{n}: {c}" for n, c in (d.get("funds_13f") or {}).items()),
        "min_funds_13f": d.get("min_funds_13f", 1),
        "min_increase_pct": d.get("min_increase_13f", 0.2) * 100,
        "review_n": d.get("review_n", 15),
        "top_n": d.get("top_n", 10),
    })
    return f


def _num(form: dict, k: str) -> float | None:
    v = (form.get(k) or "").strip()
    return float(v) if v else None


def from_form(form: dict, old: dict) -> dict:
    """Validate and build a discovery config; raises ValueError with a Chinese message."""
    keep = [c for c in old.get("screen") or [] if c[1] not in FIELDS]
    screen = list(keep)
    if (v := _num(form, "mcap_b")) is not None:
        screen.append(["gt", "intradaymarketcap", v * 1e9])
    if (v := _num(form, "rev_growth")) is not None:
        screen.append(["gt", "quarterlyrevenuegrowth.quarterly", v])
    lo, hi = _num(form, "pe_min"), _num(form, "pe_max")
    if lo is not None and hi is not None:
        if lo >= hi:
            raise ValueError("P/E 下限要小于上限")
        screen.append(["btwn", "peratio.lasttwelvemonths", lo, hi])
    elif lo is not None:
        screen.append(["gt", "peratio.lasttwelvemonths", lo])
    elif hi is not None:
        screen.append(["lt", "peratio.lasttwelvemonths", hi])

    themes = {}
    for line in (form.get("themes") or "").splitlines():
        if not line.strip():
            continue
        if ":" not in line and "：" not in line:
            raise ValueError(f"主题格式应为「名称: 代码, 代码」：{line}")
        name, rest = re.split(r"[:：]", line, maxsplit=1)
        tickers = [t.strip().upper() for t in re.split(r"[,，\s]+", rest) if t.strip()]
        if any(not re.fullmatch(r"[A-Z0-9][A-Z0-9.\-]{0,9}", t) for t in tickers):
            raise ValueError(f"主题「{name.strip()}」里有无效代码")
        old_spec = (old.get("themes") or {}).get(name.strip(), {})
        themes[name.strip()] = {**old_spec, "tickers": tickers}

    funds = {}
    for line in (form.get("funds") or "").splitlines():
        if not line.strip():
            continue
        m = re.fullmatch(r"\s*(.+?)\s*[:：]\s*(\d{1,10})\s*", line)
        if not m:
            raise ValueError(f"基金格式应为「名称: SEC CIK」：{line}")
        funds[m.group(1)] = int(m.group(2))

    def clamp(k, lo, hi, default):
        v = _num(form, k)
        v = default if v is None else v
        if not lo <= v <= hi:
            raise ValueError(f"{k} 超出范围 {lo}–{hi}")
        return v

    return {**old, "screen": screen, "screen_size": int(clamp("screen_size", 5, 250, 50)),
            "themes": themes, "theme_max_from_high": clamp("theme_max_from_high", -90, 0, -20),
            "openinsider": form.get("openinsider") == "on",
            "insider_min_value": clamp("insider_min_m", 0, 1000, 0.5) * 1e6,
            "funds_13f": funds, "min_funds_13f": int(clamp("min_funds_13f", 1, 10, 1)),
            "min_increase_13f": clamp("min_increase_pct", 0, 1000, 20) / 100,
            "review_n": int(clamp("review_n", 0, 40, 15)), "top_n": int(clamp("top_n", 1, 40, 10))}
