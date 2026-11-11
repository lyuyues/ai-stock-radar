from datetime import datetime, timedelta, timezone

import pytest

from radar.alerts.policy import decide, in_quiet_hours
from radar.alerts.rules import evaluate, evaluate_all
from radar.config import Config
from radar.models import Fire, Quote

UTC = timezone.utc
# 2026-11-05 12:00 PST == 20:00 UTC (daytime, not quiet)
DAY = datetime(2026, 11, 5, 20, 0, tzinfo=UTC)
# 2026-11-05 23:30 PST == 2026-11-06 07:30 UTC (quiet)
NIGHT = datetime(2026, 11, 6, 7, 30, tzinfo=UTC)

R_URGENT = {"id": "hu", "kind": "pct_change", "tier": "holdings", "threshold": 8, "level": "urgent"}
R_NOTICE = {"id": "hn", "kind": "pct_change", "tier": "holdings", "threshold": 5, "level": "notice"}
R_LINE = {"id": "line", "kind": "level_cross", "tier": "all", "level": "notice"}


def q(price, prev=100.0, t="MSFT"):
    return Quote(t, DAY, price, prev_close=prev)


# ---- rules ----

def test_pct_below_threshold_no_fire():
    assert evaluate(R_NOTICE, q(104), "holdings", {}, None) is None


def test_pct_fires_both_directions():
    up = evaluate(R_NOTICE, q(106), "holdings", {}, None)
    down = evaluate(R_NOTICE, q(94), "holdings", {}, None)
    assert up.level == "notice" and "▲" in up.title
    assert "▼" in down.title


def test_tier_mismatch():
    assert evaluate(R_NOTICE, q(120), "watchlist", {}, None) is None


def test_level_cross_below_only_on_transition():
    levels = {"MSFT": {"below": 400}}
    assert evaluate(R_LINE, q(399, t="MSFT"), "holdings", levels, prev_price=401) is not None
    assert evaluate(R_LINE, q(398, t="MSFT"), "holdings", levels, prev_price=399) is None
    assert evaluate(R_LINE, q(399, t="MSFT"), "holdings", levels, prev_price=None) is None


def test_level_cross_above():
    levels = {"MSFT": {"above": 500}}
    f = evaluate(R_LINE, q(501), "holdings", levels, prev_price=499)
    assert f is not None and "500" in f.title


def test_unknown_kind():
    with pytest.raises(ValueError):
        evaluate({"id": "x", "kind": "nope", "level": "log"}, q(100), "holdings", {}, None)


def test_evaluate_all_keeps_most_severe_per_kind():
    fires = evaluate_all([R_NOTICE, R_URGENT], q(90), "holdings", {}, None)
    assert [f.rule_id for f in fires] == ["hu"]


# ---- policy ----

def cfg(**kw):
    return Config(holdings=["MSFT"], watchlist=[], **kw)


def F(level, rule="r", t="MSFT"):
    return Fire(rule, t, level, f"{t} {level}", "")


def test_quiet_hours_across_midnight():
    tz = "America/Los_Angeles"
    assert in_quiet_hours(NIGHT, "23:00", "07:00", tz)
    assert not in_quiet_hours(DAY, "23:00", "07:00", tz)
    assert in_quiet_hours(DAY, "11:00", "13:00", tz)


def test_cooldown_drops():
    d = decide([F("urgent")], DAY, cfg(), lambda r, t: DAY - timedelta(hours=2), 0)
    assert d.push == [] and d.logged == []


def test_cooldown_expired_pushes():
    d = decide([F("urgent")], DAY, cfg(), lambda r, t: DAY - timedelta(hours=25), 0)
    assert len(d.push) == 1


def test_urgent_pushes_in_quiet_notice_deferred():
    d = decide([F("urgent", "a"), F("notice", "b")], NIGHT, cfg(), lambda r, t: None, 0)
    assert [f.rule_id for f in d.push] == ["a"]
    assert [f.rule_id for f in d.deferred] == ["b"]


def test_log_level_is_logged_not_pushed():
    d = decide([F("log")], DAY, cfg(), lambda r, t: None, 0)
    assert d.push == [] and len(d.logged) == 1


def test_cap_overflow_becomes_digest():
    fires = [F("notice", f"r{i}") for i in range(4)]
    d = decide(fires, DAY, cfg(daily_cap_notice=3), lambda r, t: None, notices_today=1)
    assert len(d.push) == 2
    assert len(d.logged) == 2
    assert d.digest is not None and "2" in d.digest.title


def test_once_fire_never_repeats_even_after_cooldown():
    f = Fire("8k:acc1", "MSFT", "urgent", "t", "", once=True)
    d = decide([f], DAY, cfg(), lambda r, t: DAY - timedelta(days=30), 0)
    assert d.push == []
