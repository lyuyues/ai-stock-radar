"""Delivery policy: cooldown, quiet hours, daily cap. Pure functions."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from radar.config import Config
from radar.models import Fire


@dataclass
class Decision:
    push: list[Fire] = field(default_factory=list)      # send now + record
    logged: list[Fire] = field(default_factory=list)    # record only
    deferred: list[Fire] = field(default_factory=list)  # neither; re-evaluated next run
    digest: Fire | None = None                          # summary push for cap overflow


def _parse(hhmm: str) -> time:
    h, m = map(int, hhmm.split(":"))
    return time(h, m)


def in_quiet_hours(now: datetime, start: str, end: str, tz: str) -> bool:
    local = now.astimezone(ZoneInfo(tz)).time()
    s, e = _parse(start), _parse(end)
    if s > e:  # window crosses midnight
        return local >= s or local < e
    return s <= local < e


def decide(fires: list[Fire], now: datetime, cfg: Config,
           last_fired: Callable[[str, str], datetime | None], notices_today: int) -> Decision:
    d = Decision()
    quiet = in_quiet_hours(now, cfg.quiet_start, cfg.quiet_end, cfg.tz)
    budget = max(cfg.daily_cap_notice - notices_today, 0)
    overflow: list[Fire] = []
    for f in fires:
        last = last_fired(f.rule_id, f.ticker)
        if last is not None and (f.once or now - last < timedelta(hours=cfg.cooldown_hours)):
            continue
        if f.level == "urgent":
            d.push.append(f)
        elif f.level == "notice":
            if quiet:
                d.deferred.append(f)
            elif budget > 0:
                d.push.append(f)
                budget -= 1
            else:
                overflow.append(f)
        else:
            d.logged.append(f)
    if overflow:
        d.logged.extend(overflow)
        d.digest = Fire("digest", "*", "notice", f"另有 {len(overflow)} 条提醒",
                        "\n".join(f.title for f in overflow))
    return d
