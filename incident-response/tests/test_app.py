import json

import pytest
from fastapi.testclient import TestClient

from incident_response import app as service
from incident_response import assistant, context


ALERT = {
    "status": "firing",
    "labels": {"alertname": "Order lookup 5xx responses", "http_route": "/api/orders/{order_id}",
               "service": "order-tracker"},
    "annotations": {"endpoint": "GET /api/orders/{order_id}", "time_window": "5m",
                    "dashboard": "http://127.0.0.1:3000/d/order-tracker-requests?viewPanel=6"},
    "startsAt": "2026-10-07T15:20:00Z",
    "fingerprint": "abc123",
    "generatorURL": "http://127.0.0.1:3000/alerting/grafana/order-lookup-5xx/view",
}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "INCIDENTS_DIR", tmp_path)
    started = []
    monkeypatch.setattr(context, "collect", lambda alert, incident_dir: {"alert": context.describe_alert(alert),
                                                                       "errors": {}})
    monkeypatch.setattr(assistant, "start", lambda incident_dir, collected: started.append(incident_dir))
    with TestClient(service.app) as test_client:
        test_client.started = started
        yield test_client


def test_firing_alert_saves_incident_and_starts_assistant(client, tmp_path):
    response = client.post("/alerts", json={"alerts": [ALERT]})
    assert response.status_code == 202
    [name] = response.json()["accepted"]
    assert json.loads((tmp_path / name / "alert.json").read_text())["fingerprint"] == "abc123"
    assert client.started == [tmp_path / name]


def test_repeated_and_resolved_alerts_do_not_start_assistant_again(client):
    client.post("/alerts", json={"alerts": [ALERT]})
    repeat = client.post("/alerts", json={"alerts": [ALERT]}).json()
    resolved = client.post("/alerts", json={"alerts": [{**ALERT, "status": "resolved"}]}).json()
    assert repeat["skipped"][0]["reason"] == "already handled"
    assert resolved["skipped"][0]["reason"] == "resolved"
    assert len(client.started) == 1


def test_describe_alert_extracts_endpoint_and_window():
    info = context.describe_alert(ALERT)
    assert info["endpoint"] == "GET /api/orders/{order_id}"
    assert info["query_start"] == "2026-10-07T15:15:00+00:00"
    assert info["dashboard_url"].endswith("/d/order-tracker-requests?viewPanel=6")


def test_collect_records_unreachable_backends(tmp_path, monkeypatch):
    for name in ("PROMETHEUS_URL", "LOKI_URL", "TEMPO_URL"):
        monkeypatch.setattr(context, name, "http://127.0.0.1:9")
    collected = context.collect(ALERT, tmp_path)
    assert set(collected["errors"]) == {"metrics", "logs", "trace_search"}
    assert "GET /api/orders/{order_id}" in (tmp_path / "summary.md").read_text()


def test_assistant_command_is_headless_with_limited_tools():
    command = assistant.build_command("investigate")
    assert command[:3] == ["claude", "-p", "investigate"]
    tools = command[command.index("--allowedTools") + 1:]
    assert "Edit" in tools and "Bash(uv run --frozen pytest:*)" in tools
    assert "Bash" not in tools and not any(tool.startswith("Bash(git commit") for tool in tools)


def test_investigate_mode_is_read_only():
    import shlex
    tools = shlex.split(assistant.DEFAULT_TOOLS["investigate"])
    assert "Edit" not in tools and "Write" not in tools and "Bash" not in tools


def test_trace_ids_from_tempo_and_loki_match():
    assert context.normalize_trace_id("72ac55f0e70f1adda6bab8bfc6e234c") == "072ac55f0e70f1adda6bab8bfc6e234c"


def test_alert_without_start_time_or_fingerprint_is_accepted(client, tmp_path):
    test_alert = {"status": "firing", "labels": {"alertname": "ResponderTest", "test": "true"},
                  "annotations": {"summary": "Test notification; no incident to fix"}}
    [name] = client.post("/alerts", json={"alerts": [test_alert]}).json()["accepted"]
    saved = json.loads((tmp_path / name / "alert.json").read_text())
    assert saved["startsAt"] and saved["fingerprint"]
    assert context.describe_alert(saved)["endpoint"] is None
