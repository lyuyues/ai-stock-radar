"""Shared HTTP getter. Injected everywhere so tests never touch the network."""
from __future__ import annotations

import os
import time
from collections.abc import Callable

import httpx

Getter = Callable[[str], httpx.Response]

DEFAULT_UA = "InvestRadar/0.1 personal research"


def user_agent() -> str:
    # SEC asks for a descriptive UA with contact info; set SEC_USER_AGENT in .env.
    return os.environ.get("SEC_USER_AGENT") or DEFAULT_UA


def make_getter(min_interval: float = 0.15) -> Getter:
    """GET with an honest UA and a minimum spacing between requests (SEC limit: 10 req/s)."""
    last = [0.0]

    def get(url: str, **kw) -> httpx.Response:
        wait = min_interval - (time.monotonic() - last[0])
        if wait > 0:
            time.sleep(wait)
        last[0] = time.monotonic()
        resp = httpx.get(url, headers={"User-Agent": user_agent()}, timeout=30,
                         follow_redirects=True, **kw)
        resp.raise_for_status()
        return resp

    return get
