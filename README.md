# Order Tracker

A small order tracking app for the AI Dev Tools Zoomcamp observability homework. It includes a web page, API, tests, and a Docker Compose setup. You add telemetry, alerts, and an incident responder in Homework 4.

The main user flow is creating an order and checking its status. Three sample orders are created on first startup.

## Run it

You need Docker with Compose. To run the tests, you also need Python 3.11+ and `uv`.

```bash
docker compose up --build -d --wait
```

Open <http://127.0.0.1:8000>. The API is at `/api/orders`, and the health check is at `/healthz`. Data is stored in a Docker volume and survives container recreation.

If port 8000 is occupied, set `ORDER_TRACKER_PORT`, for example:

```bash
ORDER_TRACKER_PORT=18080 docker compose up --build -d --wait
```

Run tests with `uv run --frozen pytest -q`. Stop the app with `docker compose down`. Add `-v` only if you also want to delete the order data.

## Observability

`docker compose up` also starts an observability stack. The app sends its metrics, logs, and traces over OTLP to an OpenTelemetry Collector, which forwards them to the backends:

| Signal | Path | Look at it in |
| --- | --- | --- |
| Metrics | app → Collector → Prometheus (OTLP receiver) | Grafana, or Prometheus at <http://127.0.0.1:9090> |
| Logs | app → Collector → Loki (OTLP endpoint) | Grafana → Explore → Loki |
| Traces | app → Collector → Tempo | Grafana → Explore → Tempo |

Open Grafana at <http://127.0.0.1:3000> (no login; it only listens on localhost). The home dashboard, **Order Tracker — Requests and Errors**, shows order lookup counts, 5xx and 4xx counts, the server error ratio, request rate by route and status code, recent error logs, and failed lookup traces. Log lines link to their trace, and traces link back to their logs.

The lookup metric is `order_lookup_requests_total`, labelled with `http_route` and `http_response_status_code`. Metrics are exported every 10 seconds, so allow a few seconds before new requests appear.

The Grafana alert **Order lookup 5xx responses** (Alerting → Alert rules, folder *Order Tracker*) fires when any lookup returns a 5xx status within the last 5 minutes, after the condition has held for 1 minute. It is evaluated every 30 seconds and has one alert per route. Its annotations give the endpoint, the 5-minute time window, an approximate 5xx count, and a link to the dashboard's 5xx panel. A window with lookups but no 5xx evaluates to 0 (Normal), a window with no lookups at all is also Normal, and a failed Prometheus query shows as Error. Firing alerts are sent to the incident-response service on port 8001, which saves the incident context and starts a coding assistant to investigate; see [`incident-response/README.md`](incident-response/README.md).

All configuration lives in `observability/`: `otel-collector.yaml`, `prometheus.yaml`, `loki.yaml`, `tempo.yaml`, and `grafana/` for data sources and the dashboard. After editing a config file, run `docker compose restart <service>`. Use `GRAFANA_PORT` or `PROMETHEUS_PORT` if 3000 or 9090 is taken.

Without `OTEL_EXPORTER_OTLP_ENDPOINT` (for example when running the app outside Compose), telemetry is printed to stdout instead.

## API

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/` | Web page |
| GET | `/healthz` | Database health check |
| GET | `/api/orders` | List orders |
| POST | `/api/orders` | Create an order |
| GET | `/api/orders/{id}` | Check an order |
| PATCH | `/api/orders/{id}` | Change an order status |

The app uses SQLite to keep setup small. Run one app container at a time. The course exercise is about detecting and handling an incident, not scaling the database.
