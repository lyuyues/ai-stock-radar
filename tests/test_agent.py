import json
import sys
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from radar import agent, db
from radar.models import Quote

NOW = datetime(2026, 11, 9, 20, 0, tzinfo=timezone.utc)

FAKE = r'''
import json, sys
prompt = sys.stdin.read()
resumed = "--resume" in sys.argv
def out(o): print(json.dumps(o), flush=True)
out({"type": "system", "subtype": "init", "session_id": "11111111-2222-3333-4444-555555555555"})
out({"type": "stream_event", "event": {"type": "content_block_start", "content_block": {"type": "server_tool_use", "name": "WebSearch"}}})
for piece in ["看到", "了 MSFT" if "MSFT" in prompt else "?", " resumed" if resumed else "", " first" if "仪表盘数据快照" in prompt else ""]:
    out({"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": piece}}})
out({"type": "result", "subtype": "success", "is_error": False, "session_id": "11111111-2222-3333-4444-555555555555", "result": "x"})
'''


@pytest.fixture
def client(tmp_path, monkeypatch):
    (tmp_path / "watchlist.yaml").write_text("holdings: [MSFT]\nwatchlist: [NVDA]\n")
    (tmp_path / "rules.yaml").write_text("rules: []\n")
    fake = tmp_path / "fake_claude.py"
    fake.write_text(FAKE)
    monkeypatch.setenv("RADAR_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("RADAR_DB", str(tmp_path / "a.db"))
    monkeypatch.setenv("AGENT_CMD", f"{sys.executable} {fake}")
    conn = db.connect(tmp_path / "a.db")
    db.insert_quote(conn, Quote("MSFT", NOW, 500, 490, 30.0, 560, 380))
    conn.commit()
    conn.close()
    from web.app import app
    return TestClient(app)


def _events(text):
    return [json.loads(c[6:]) for c in text.split("\n\n") if c.startswith("data: ")]


def test_agent_stream_first_turn_and_resume(client):
    r = client.post("/agent", json={"q": "MSFT 怎么样", "page": "/t/MSFT"})
    evs = _events(r.text)
    assert evs[0] == {"t": "session", "v": "11111111-2222-3333-4444-555555555555"}
    assert {"t": "tool", "v": "WebSearch"} in evs
    text = "".join(e["v"] for e in evs if e["t"] == "text")
    assert text == "看到了 MSFT first"
    assert evs[-1]["t"] == "done"

    r = client.post("/agent", json={"q": "再说说", "sid": "11111111-2222-3333-4444-555555555555", "page": "/"})
    text = "".join(e["v"] for e in _events(r.text) if e["t"] == "text")
    assert "resumed" in text and "first" not in text   # follow-up: no full bundle again


def test_agent_rejects_empty_and_bad_sid(client):
    assert client.post("/agent", json={"q": "  "}).status_code == 400
    r = client.post("/agent", json={"q": "hi", "sid": "../../etc"})
    assert "resumed" not in "".join(e["v"] for e in _events(r.text) if e["t"] == "text")


def test_agent_reports_crash(client, monkeypatch):
    monkeypatch.setenv("AGENT_CMD", f"{sys.executable} -c \"import sys; sys.stderr.write('Not logged in'); sys.exit(1)\"")
    evs = _events(client.post("/agent", json={"q": "hi"}).text)
    assert evs[-1]["t"] == "error" and "Not logged in" in evs[-1]["v"]


def test_parse_event_ignores_noise():
    assert agent.parse_event("not json") is None
    assert agent.parse_event('{"type": "rate_limit_event"}') is None
    assert agent.parse_event('{"type": "result", "subtype": "error_max_turns", "is_error": true}')["t"] == "error"


def test_page_context_ticker(tmp_path):
    conn = db.connect(tmp_path / "p.db")
    db.insert_quote(conn, Quote("NVDA", NOW, 180, 175, 40.0, 210, 120))
    assert "NVDA 的个股页" in agent.page_context(conn, "/t/nvda") and "180.00" in agent.page_context(conn, "/t/nvda")
    assert "宏观" in agent.page_context(conn, "/macro")
