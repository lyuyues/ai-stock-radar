from datetime import datetime, timezone

import pytest

from radar.config import load_config
from radar.models import Quote

NOW = datetime(2026, 11, 5, 20, 0, tzinfo=timezone.utc)


def test_pct_change():
    assert Quote("A", NOW, 110, prev_close=100).pct_change == pytest.approx(10)
    assert Quote("A", NOW, 95, prev_close=100).pct_change == pytest.approx(-5)
    assert Quote("A", NOW, 95).pct_change is None


def _write_cfg(d):
    (d / "watchlist.yaml").write_text(
        "holdings: [msft]\nwatchlist: [NVDA, MSFT, googl]\nlevels:\n  nvda: {below: 150}\n"
    )
    (d / "rules.yaml").write_text(
        "cooldown_hours: 12\ndaily_cap_notice: 3\n"
        "quiet_hours: {start: '22:00', end: '06:00', tz: America/Los_Angeles}\n"
        "rules:\n  - {id: r1, kind: pct_change, tier: holdings, threshold: 5, level: notice}\n"
    )


def test_load_config(tmp_path):
    _write_cfg(tmp_path)
    cfg = load_config(tmp_path, env={"NTFY_TOPIC": "t", "RADAR_DB": str(tmp_path / "x.db")})
    assert cfg.holdings == ["MSFT"]
    assert cfg.all_tickers == ["MSFT", "NVDA", "GOOGL"]
    assert cfg.tier_of("MSFT") == "holdings"
    assert cfg.tier_of("NVDA") == "watchlist"
    assert cfg.levels["NVDA"] == {"below": 150}
    assert cfg.cooldown_hours == 12 and cfg.daily_cap_notice == 3
    assert cfg.quiet_start == "22:00" and cfg.quiet_end == "06:00"
    assert cfg.rules[0]["id"] == "r1"
    assert cfg.ntfy_topic == "t"
    assert cfg.db_path == tmp_path / "x.db"


def test_config_dir_env_override(tmp_path):
    _write_cfg(tmp_path)
    cfg = load_config(env={"RADAR_CONFIG_DIR": str(tmp_path)})
    assert cfg.holdings == ["MSFT"]
    assert cfg.ntfy_topic is None
