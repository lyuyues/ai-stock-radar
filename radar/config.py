"""Load watchlist / rules YAML plus environment settings."""
from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Config:
    holdings: list[str]
    watchlist: list[str]
    levels: dict[str, dict[str, float]] = field(default_factory=dict)
    rules: list[dict] = field(default_factory=list)
    cooldown_hours: float = 24
    daily_cap_notice: int = 10
    quiet_start: str = "23:00"
    quiet_end: str = "07:00"
    tz: str = "America/Los_Angeles"
    ntfy_topic: str | None = None
    ntfy_server: str = "https://ntfy.sh"
    public_url: str | None = None
    db_path: Path = ROOT / "data" / "invest.db"
    macro: dict = field(default_factory=dict)       # config/macro.yaml
    discovery: dict = field(default_factory=dict)   # config/discovery.yaml
    heartbeat: dict = field(default_factory=dict)   # job -> max age hours
    theses_dir: Path | None = None

    def tier_of(self, ticker: str) -> str:
        return "holdings" if ticker in self.holdings else "watchlist"

    @property
    def all_tickers(self) -> list[str]:
        return list(dict.fromkeys(self.holdings + self.watchlist))


def _read_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text()) or {}


def load_config(config_dir: Path | None = None, env: Mapping[str, str] | None = None) -> Config:
    env = os.environ if env is None else env
    config_dir = Path(config_dir or env.get("RADAR_CONFIG_DIR") or ROOT / "config")
    wl = _read_yaml(config_dir / "watchlist.yaml")
    rl = _read_yaml(config_dir / "rules.yaml")
    quiet = rl.get("quiet_hours") or {}
    return Config(
        holdings=[t.upper() for t in wl.get("holdings") or []],
        watchlist=[t.upper() for t in wl.get("watchlist") or []],
        levels={k.upper(): v for k, v in (wl.get("levels") or {}).items()},
        rules=rl.get("rules") or [],
        cooldown_hours=rl.get("cooldown_hours", 24),
        daily_cap_notice=rl.get("daily_cap_notice", 10),
        quiet_start=quiet.get("start", "23:00"),
        quiet_end=quiet.get("end", "07:00"),
        tz=quiet.get("tz", "America/Los_Angeles"),
        ntfy_topic=env.get("NTFY_TOPIC") or None,
        ntfy_server=env.get("NTFY_SERVER") or "https://ntfy.sh",
        public_url=(env.get("RADAR_PUBLIC_URL") or "").rstrip("/") or None,
        db_path=Path(env.get("RADAR_DB") or ROOT / "data" / "invest.db"),
        macro=_read_yaml(config_dir / "macro.yaml"),
        discovery=_read_yaml(config_dir / "discovery.yaml"),
        heartbeat=rl.get("heartbeat") or {},
        theses_dir=Path(env["THESES_DIR"]) if env.get("THESES_DIR") else None,
    )
