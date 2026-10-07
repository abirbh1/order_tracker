"""Start Claude Code in headless mode to investigate an incident."""

import json
import os
import shlex
import subprocess
import threading
from pathlib import Path


REPO_DIR = Path(os.getenv("INCIDENT_REPO_DIR", Path(__file__).resolve().parents[2]))
CLAUDE_BIN = os.getenv("INCIDENT_ASSISTANT_BIN", "claude")
# "fix": edit files in the repository and run the tests. "investigate": read-only, propose a fix as a diff.
MODE = os.getenv("INCIDENT_ASSISTANT_MODE", "fix")
READ_ONLY_TOOLS = 'Read Grep Glob "Bash(git log:*)" "Bash(git show:*)" "Bash(git diff:*)" "Bash(git status:*)"'
DEFAULT_TOOLS = {
    "investigate": READ_ONLY_TOOLS,
    # Claude Code only allows edits inside the working directory (the repository); no commits, no other commands.
    "fix": READ_ONLY_TOOLS + ' Edit Write "Bash(uv run --frozen pytest:*)"',
}
ALLOWED_TOOLS = shlex.split(os.getenv("INCIDENT_ASSISTANT_ALLOWED_TOOLS", DEFAULT_TOOLS[MODE]))
MODEL = os.getenv("INCIDENT_ASSISTANT_MODEL")

PROMPT = """\
You are the on-call engineer for the order-tracker service in this repository.
Grafana fired the alert "{alert_name}" for the endpoint {endpoint}.
Alert summary: {summary}

The incident context is saved in {incident_dir}:
- summary.md: endpoint, time window, request counts by status code, error logs, stack trace and error traces
- context.json: the same data in full
- alert.json: the raw Grafana alert
- traces/: full traces from Tempo

Investigate the incident:
1. Read summary.md first, then the relevant application code.
2. Find the root cause. Point to the exact file and line, and explain which requests are affected and why.
{fix_steps}
Reply with a Markdown incident report with these sections:
Summary, Impact, Evidence, Root cause, {fix_sections}, Follow-ups.

If the alert and its context show no actual problem (for example a test notification, or no errors in the
time window), do not invent one: reply with a short report that says so and what you checked.
"""


FIX_STEPS = {
    "investigate": """3. Propose a minimal fix as a unified diff, and a test that would have caught it.
   Do not modify any files; only describe the fix.
""",
    "fix": """3. Fix the root cause with a minimal change to the application code (app/), and add a regression test
   in tests/ that fails without the fix.
4. Run the tests from the repository root with `uv run --frozen pytest -q` and make sure they pass.
   Do not commit, and do not change observability/ or incident-response/. The operator restarts the app.
""",
}
FIX_SECTIONS = {"investigate": "Proposed fix", "fix": "Fix applied (with the diff), Tests run"}


def build_command(prompt):
    command = [CLAUDE_BIN, "-p", prompt, "--output-format", "json", "--allowedTools", *ALLOWED_TOOLS]
    if MODEL:
        command += ["--model", MODEL]
    return command


def write_status(incident_dir, **status):
    path = incident_dir / "status.json"
    current = json.loads(path.read_text()) if path.exists() else {}
    path.write_text(json.dumps({**current, **status}, indent=2))


def start(incident_dir, context):
    """Launch the assistant in the background and return immediately."""
    info = context["alert"]
    prompt = PROMPT.format(
        alert_name=info["alert_name"],
        endpoint=info["endpoint"] or "(none given)",
        summary=info["description"] or info["summary"] or "(none)",
        fix_steps=FIX_STEPS[MODE],
        fix_sections=FIX_SECTIONS[MODE],
        incident_dir=incident_dir.relative_to(REPO_DIR) if incident_dir.is_relative_to(REPO_DIR) else incident_dir,
    )
    (incident_dir / "prompt.md").write_text(prompt)
    command = build_command(prompt)

    output = open(incident_dir / "assistant-output.json", "w")
    errors = open(incident_dir / "assistant-stderr.log", "w")
    try:
        process = subprocess.Popen(command, cwd=REPO_DIR, stdout=output, stderr=errors,
                                   stdin=subprocess.DEVNULL, start_new_session=True)
    except OSError as error:
        output.close()
        errors.close()
        write_status(incident_dir, assistant="failed to start", error=repr(error))
        return None

    write_status(incident_dir, assistant="running", pid=process.pid, command=command[:2] + ["<prompt>"] + command[3:])
    threading.Thread(target=_wait, args=(process, incident_dir, output, errors), daemon=True).start()
    return process


def _wait(process, incident_dir, output, errors):
    returncode = process.wait()
    output.close()
    errors.close()
    try:
        result = json.loads((incident_dir / "assistant-output.json").read_text())
    except (OSError, ValueError):
        result = {}
    if result.get("result"):
        (incident_dir / "report.md").write_text(result["result"])
    write_status(
        incident_dir,
        assistant="finished" if returncode == 0 and not result.get("is_error") else "failed",
        exit_code=returncode,
        session_id=result.get("session_id"),
        cost_usd=result.get("total_cost_usd"),
    )
