# Incident Response

A small service that receives Grafana alerts at `POST /alerts` on port 8001. For each new firing alert it:

1. Saves the alert and the context needed to understand it in `incidents/<start time>-<alert>-<id>/`.
2. Starts Claude Code in headless mode (`claude -p`) to investigate, and saves its report.

It runs on the host rather than in Docker, so it can start your local `claude` CLI with your login. Grafana (in Docker) sends alerts to `http://host.docker.internal:8001/alerts`; this is set up by `observability/grafana/provisioning/alerting/`.

## Run it

Start the order tracker stack first (`docker compose up -d` in the repository root), then:

```bash
cd incident-response
uv run --frozen uvicorn incident_response.app:app --host 127.0.0.1 --port 8001
```

On Docker Desktop, Grafana can reach a service bound to `127.0.0.1`. On Linux, use `--host 0.0.0.0` (and keep port 8001 firewalled from other machines, since an alert starts the assistant).

Run tests with `uv run --frozen pytest -q`.

## What is saved

| File | Contents |
| --- | --- |
| `alert.json` | The alert as Grafana sent it |
| `summary.md` | Endpoint, time window, request counts by status code, error logs, latest stack trace, error traces, dashboard and alert links |
| `context.json` | Everything in `summary.md`, in full, plus the queries used |
| `traces/<id>.json` | Full error traces from Tempo for the endpoint, and traces referenced by error logs |
| `prompt.md` | The prompt given to the assistant |
| `assistant-output.json`, `assistant-stderr.log` | Raw assistant output |
| `report.md` | The assistant's incident report |
| `status.json` | Progress: context collected, assistant running/finished/failed, exit code, session id, cost |

Context is read from one alert time window before the alert started until it was received. Logs come from Loki, traces from Tempo and counts from Prometheus. If a backend can't be reached, the rest is still saved and the problem is listed in `summary.md`.

## How alerts are handled

- Only `firing` alerts start an investigation. `resolved` notifications are acknowledged and ignored.
- Grafana re-sends a firing alert every repeat interval. The same fingerprint and start time count as one incident, so the assistant starts once per incident.
- The webhook replies `202` immediately; collecting context and running the assistant happen in the background.
- If the service is down when an alert fires, Grafana does not deliver it again until the repeat interval (4 hours).

## The assistant

By default the assistant is **read-only**: it can use `Read`, `Grep`, `Glob`, and `git log/show/diff`. It is asked to find the root cause and propose a fix as a diff, not to change files. It runs from the repository root.

| Variable | Default | Purpose |
| --- | --- | --- |
| `INCIDENT_ASSISTANT_ENABLED` | `true` | Set to `false` to only save context |
| `INCIDENT_ASSISTANT_BIN` | `claude` | Path to the Claude Code CLI |
| `INCIDENT_ASSISTANT_ALLOWED_TOOLS` | read-only list above | Tools passed to `--allowedTools` |
| `INCIDENT_ASSISTANT_MODEL` | CLI default | Passed to `--model` |
| `INCIDENT_REPO_DIR` | repository root | Where the assistant runs |
| `INCIDENTS_DIR` | `incident-response/incidents` | Where incidents are saved |
| `PROMETHEUS_URL`, `LOKI_URL`, `TEMPO_URL` | `http://127.0.0.1:9090`, `:3100`, `:3200` | Backends to read context from |

## Try it

Look up the order that fails, then wait about 2 minutes for the alert to fire and be delivered:

```bash
for i in 1 2 3; do curl -s -o /dev/null http://localhost:8000/api/orders/express-1002; done
ls incidents/
```
