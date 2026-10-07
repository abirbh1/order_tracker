"""Collect what's needed to understand an alert: endpoint, time window, metrics, logs and traces."""

import json
import os
import re
from datetime import datetime, timedelta, timezone

import httpx


PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://127.0.0.1:9090")
LOKI_URL = os.getenv("LOKI_URL", "http://127.0.0.1:3100")
TEMPO_URL = os.getenv("TEMPO_URL", "http://127.0.0.1:3200")
MAX_LOGS = 50
MAX_TRACES = 5


def parse_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def parse_window(value, default=timedelta(minutes=5)):
    match = re.fullmatch(r"(\d+)([smh])", (value or "").strip())
    if not match:
        return default
    amount, unit = int(match[1]), match[2]
    unit_name = {"s": "seconds", "m": "minutes", "h": "hours"}[unit]
    return timedelta(**{unit_name: amount})


def describe_alert(alert):
    labels, annotations = alert.get("labels", {}), alert.get("annotations", {})
    route = labels.get("http_route")
    window = parse_window(annotations.get("time_window"))
    starts_at = parse_time(alert["startsAt"])
    return {
        "alert_name": labels.get("alertname"),
        "status": alert.get("status"),
        "service": labels.get("service", "order-tracker"),
        "endpoint": annotations.get("endpoint") or route,
        "http_route": route,
        "time_window": annotations.get("time_window", "5m"),
        # From one window before the alert started until now, so the first errors are included.
        "query_start": (starts_at - window).isoformat(),
        "query_end": datetime.now(timezone.utc).isoformat(),
        "starts_at": alert["startsAt"],
        "summary": annotations.get("summary"),
        "description": annotations.get("description"),
        "dashboard_url": annotations.get("dashboard") or alert.get("dashboardURL") or alert.get("panelURL"),
        "alert_url": alert.get("generatorURL"),
        "values": alert.get("values"),
        "labels": labels,
    }


def _ns(iso):
    return str(int(parse_time(iso).timestamp() * 1e9))


def query_status_counts(client, info):
    window = info["time_window"]
    query = (f'sum by (http_route, http_response_status_code) '
             f'(increase(order_lookup_requests_total{{job="{info["service"]}"}}[{window}]))')
    response = client.get(f"{PROMETHEUS_URL}/api/v1/query", params={"query": query})
    response.raise_for_status()
    return {
        "query": query,
        "results": [
            {**r["metric"], "approx_count": round(float(r["value"][1]), 1)}
            for r in response.json()["data"]["result"]
            if float(r["value"][1]) > 0
        ],
    }


def query_logs(client, info):
    query = f'{{service_name="{info["service"]}"}} | severity_text=~"ERROR|WARN"'
    response = client.get(f"{LOKI_URL}/loki/api/v1/query_range", params={
        "query": query, "start": _ns(info["query_start"]), "end": _ns(info["query_end"]),
        "limit": MAX_LOGS, "direction": "backward",
    })
    response.raise_for_status()
    logs = []
    for stream in response.json()["data"]["result"]:
        for timestamp, line in stream["values"]:
            logs.append({
                "time": datetime.fromtimestamp(int(timestamp) / 1e9, timezone.utc).isoformat(),
                "message": line,
                **stream["stream"],
            })
    logs.sort(key=lambda log: log["time"], reverse=True)
    return {"query": query, "logs": logs}


def search_error_traces(client, info):
    conditions = [f'resource.service.name = "{info["service"]}"', "status = error"]
    if info["http_route"]:
        conditions.append(f'span.http.route = "{info["http_route"]}"')
    query = "{ " + " && ".join(conditions) + " }"
    response = client.get(f"{TEMPO_URL}/api/search", params={
        "q": query, "limit": MAX_TRACES,
        "start": int(parse_time(info["query_start"]).timestamp()),
        "end": int(parse_time(info["query_end"]).timestamp()),
    })
    response.raise_for_status()
    return query, [normalize_trace_id(t["traceID"]) for t in response.json().get("traces", [])]


def normalize_trace_id(trace_id):
    # Tempo search drops leading zeros; logs keep all 32 hex digits.
    return trace_id.lower().zfill(32)


def fetch_trace(client, trace_id):
    response = client.get(f"{TEMPO_URL}/api/traces/{trace_id}")
    response.raise_for_status()
    return response.json()


def summarize_trace(trace_id, trace):
    spans = []
    for batch in trace.get("batches", trace.get("resourceSpans", [])):
        for scope in batch.get("scopeSpans", batch.get("instrumentationLibrarySpans", [])):
            for span in scope.get("spans", []):
                attributes = {a["key"]: next(iter(a["value"].values()), None) for a in span.get("attributes", [])}
                events = [
                    {"name": e.get("name"),
                     **{a["key"]: next(iter(a["value"].values()), None) for a in e.get("attributes", [])}}
                    for e in span.get("events", [])
                ]
                spans.append({
                    "name": span.get("name"),
                    "status": span.get("status", {}),
                    "attributes": attributes,
                    "events": events,
                })
    return {"trace_id": trace_id, "spans": spans}


def collect(alert, incident_dir):
    """Write context files into incident_dir and return the collected context. Never raises."""
    info = describe_alert(alert)
    context = {"alert": info, "errors": {}}

    with httpx.Client(timeout=10) as client:
        steps = {
            "metrics": lambda: query_status_counts(client, info),
            "logs": lambda: query_logs(client, info),
        }
        for name, step in steps.items():
            try:
                context[name] = step()
            except Exception as error:
                context["errors"][name] = repr(error)

        # Error traces for the route, plus any traces referenced by error logs.
        trace_ids = []
        try:
            context["trace_query"], trace_ids = search_error_traces(client, info)
        except Exception as error:
            context["errors"]["trace_search"] = repr(error)
        for log in context.get("logs", {}).get("logs", []):
            trace_id = normalize_trace_id(log.get("trace_id") or "")
            if log.get("severity_text") == "ERROR" and log.get("trace_id") and trace_id not in trace_ids:
                trace_ids.append(trace_id)

        traces_dir = incident_dir / "traces"
        traces_dir.mkdir(exist_ok=True)
        context["traces"] = []
        for trace_id in trace_ids[:MAX_TRACES]:
            try:
                trace = fetch_trace(client, trace_id)
            except Exception as error:
                context["errors"][f"trace {trace_id}"] = repr(error)
                continue
            (traces_dir / f"{trace_id}.json").write_text(json.dumps(trace, indent=2))
            context["traces"].append(summarize_trace(trace_id, trace))

    (incident_dir / "context.json").write_text(json.dumps(context, indent=2, default=str))
    (incident_dir / "summary.md").write_text(render_summary(context))
    return context


def render_summary(context):
    info = context["alert"]
    lines = [
        f"# {info['alert_name']}",
        "",
        f"- **Endpoint:** {info['endpoint']}",
        f"- **Service:** {info['service']}",
        f"- **Alert started:** {info['starts_at']}",
        f"- **Time window:** {info['time_window']} (context collected from {info['query_start']} to {info['query_end']})",
        f"- **Alert:** {info['description'] or info['summary']}",
        f"- **Dashboard:** {info['dashboard_url']}",
        f"- **Alert rule:** {info['alert_url']}",
        "",
        "## Requests by status code",
        "",
    ]
    for row in context.get("metrics", {}).get("results", []):
        lines.append(f"- {row.get('http_route')} {row.get('http_response_status_code')}: ~{row['approx_count']}")

    lines += ["", "## Error and warning logs (newest first)", ""]
    for log in context.get("logs", {}).get("logs", [])[:10]:
        lines.append(f"- {log['time']} {log.get('severity_text')} {log['message']} (trace {log.get('trace_id')})")
        if log.get("exception_type"):
            lines.append(f"  - {log['exception_type']}: {log.get('exception_message')}")
    stack = next((log.get("exception_stacktrace") for log in context.get("logs", {}).get("logs", [])
                  if log.get("exception_stacktrace")), None)
    if stack:
        lines += ["", "## Latest stack trace", "", "```", stack.strip(), "```"]

    lines += ["", "## Error traces", ""]
    for trace in context.get("traces", []):
        for span in trace["spans"]:
            exception = next((e for e in span["events"] if e["name"] == "exception"), {})
            lines.append(
                f"- `{trace['trace_id']}` {span['name']} {span['attributes'].get('order.id', '')} "
                f"status={span['attributes'].get('http.response.status_code')} "
                f"{exception.get('exception.type', '')}: {exception.get('exception.message', '')}".rstrip()
            )

    if context["errors"]:
        lines += ["", "## Problems collecting context", ""]
        lines += [f"- {name}: {error}" for name, error in context["errors"].items()]
    return "\n".join(lines) + "\n"
