import pytest
from fastapi.testclient import TestClient

from app import main


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "orders.db")
    with TestClient(main.app) as test_client:
        yield test_client


def test_health_and_seeded_orders(client):
    assert client.get("/healthz").json() == {"status": "ok"}
    orders = client.get("/api/orders").json()
    assert len(orders) == 3
    assert {order["priority"] for order in orders} == {"standard", "express"}


def test_create_and_update_order(client):
    response = client.post(
        "/api/orders",
        json={"customer": "Taylor", "item": "Mug", "priority": "standard"},
    )
    assert response.status_code == 201
    order_id = response.json()["id"]
    assert client.get(f"/api/orders/{order_id}").json()["status"] == "received"
    updated = client.patch(f"/api/orders/{order_id}", json={"status": "shipped"})
    assert updated.status_code == 200
    assert updated.json()["status"] == "shipped"


def test_missing_order(client):
    assert client.get("/api/orders/missing").status_code == 404


@pytest.mark.parametrize(
    ("created_at", "expected"),
    [
        ("2026-09-30T12:00:00+00:00", "2026-10-02"),
        ("2026-09-29T12:00:00+00:00", "2026-10-01"),
        ("2026-12-31T12:00:00+00:00", "2027-01-02"),
        ("2026-10-07T12:00:00+00:00", "2026-10-09"),
    ],
)
def test_express_lookup_near_month_end(client, created_at, expected):
    with main.connect() as db:
        db.execute("UPDATE orders SET created_at = ? WHERE id = 'express-1002'", (created_at,))
    response = client.get("/api/orders/express-1002")
    assert response.status_code == 200
    assert response.json()["estimated_delivery"] == expected


def test_order_lookup_telemetry(client, monkeypatch):
    from opentelemetry import trace
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    recorded = []
    monkeypatch.setattr(main.order_lookup_requests, "add", lambda value, attributes: recorded.append(attributes))
    spans = InMemorySpanExporter()
    trace.get_tracer_provider().add_span_processor(SimpleSpanProcessor(spans))

    client.get("/api/orders/standard-1001")
    client.get("/api/orders/missing")

    assert [(a["http.route"], a["http.response.status_code"]) for a in recorded] == [
        ("/api/orders/{order_id}", 200),
        ("/api/orders/{order_id}", 404),
    ]
    lookups = [s for s in spans.get_finished_spans() if s.name == "order.lookup"]
    assert [s.attributes["http.response.status_code"] for s in lookups] == [200, 404]
