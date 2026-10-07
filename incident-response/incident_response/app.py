"""Receive Grafana webhook alerts, save incident context, and start the coding assistant."""

import hashlib
import json
import logging
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI

from incident_response import assistant, context


INCIDENTS_DIR = Path(os.getenv("INCIDENTS_DIR", Path(__file__).resolve().parents[1] / "incidents"))
ASSISTANT_ENABLED = os.getenv("INCIDENT_ASSISTANT_ENABLED", "true").lower() not in {"0", "false", "no"}

log = logging.getLogger("uvicorn.error")
app = FastAPI(title="Incident Response")
_claim_lock = threading.Lock()


def normalize(alert):
    # Grafana always sends these, but hand-written or test alerts may not.
    alert = dict(alert)
    alert.setdefault("labels", {})
    alert.setdefault("annotations", {})
    if not alert.get("startsAt"):
        alert["startsAt"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    if not alert.get("fingerprint"):
        alert["fingerprint"] = hashlib.sha256(json.dumps(alert["labels"], sort_keys=True).encode()).hexdigest()[:16]
    return alert


def incident_id(alert):
    # Grafana re-sends a firing alert on every repeat interval; the same fingerprint and
    # start time mean it is the same incident, so it is only handled once.
    key = f"{alert.get('fingerprint')}|{alert.get('startsAt')}"
    name = re.sub(r"[^a-z0-9]+", "-", alert.get("labels", {}).get("alertname", "alert").lower()).strip("-")
    started = re.sub(r"[^0-9T]", "", alert.get("startsAt", ""))[:15]
    return f"{started}-{name}-{hashlib.sha256(key.encode()).hexdigest()[:8]}"


def claim(alert):
    """Create the incident directory, or return None if this incident was already handled."""
    incident_dir = INCIDENTS_DIR / incident_id(alert)
    with _claim_lock:
        try:
            incident_dir.mkdir(parents=True)
        except FileExistsError:
            return None
    (incident_dir / "alert.json").write_text(json.dumps(alert, indent=2))
    return incident_dir


def handle(alert, incident_dir):
    try:
        collected = context.collect(alert, incident_dir)
        assistant.write_status(incident_dir, context="collected", context_errors=collected["errors"])
    except Exception as error:
        log.exception("Collecting context failed for %s", incident_dir.name)
        assistant.write_status(incident_dir, context="failed", error=repr(error))
        return
    if ASSISTANT_ENABLED:
        assistant.start(incident_dir, collected)
        log.info("Started coding assistant for %s", incident_dir.name)
    else:
        assistant.write_status(incident_dir, assistant="disabled")


@app.get("/healthz")
def health():
    return {"status": "ok"}


@app.post("/alerts", status_code=202)
def receive_alerts(payload: dict, background: BackgroundTasks):
    accepted, skipped = [], []
    for alert in payload.get("alerts", []):
        alert = normalize(alert)
        if alert.get("status") != "firing":
            skipped.append({"fingerprint": alert.get("fingerprint"), "reason": alert.get("status")})
            continue
        incident_dir = claim(alert)
        if incident_dir is None:
            skipped.append({"fingerprint": alert.get("fingerprint"), "reason": "already handled"})
            continue
        log.info("Incident %s: %s", incident_dir.name, alert.get("annotations", {}).get("summary"))
        background.add_task(handle, alert, incident_dir)
        accepted.append(incident_dir.name)
    return {"accepted": accepted, "skipped": skipped}
