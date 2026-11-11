"""ntfy push. Uses JSON publishing so Chinese titles need no header encoding."""
from __future__ import annotations

from collections.abc import Callable

import httpx

from radar.models import Fire

PRIORITY = {"urgent": 4, "notice": 3, "log": 2}
TAGS = {"urgent": ["rotating_light"], "notice": ["chart_with_upwards_trend"], "log": []}


def make_pusher(server: str, topic: str, public_url: str | None = None,
                post: Callable = httpx.post) -> Callable[[Fire], None]:
    def push(f: Fire) -> None:
        payload = {
            "topic": topic,
            "title": f.title,
            "message": f.body or f.title,
            "priority": PRIORITY[f.level],
            "tags": TAGS[f.level],
        }
        if public_url:
            payload["click"] = f"{public_url}/t/{f.ticker}" if f.ticker != "*" else f"{public_url}/"
        resp = post(server, json=payload, timeout=10)
        resp.raise_for_status()

    return push
