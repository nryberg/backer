#!/usr/bin/env python3
"""
Backer Agent — HTTP-triggered backups for the machine being backed up.

Exposes three endpoints so a scheduler (Clocktower) can drive backups over
HTTP instead of owning a crontab entry:

  POST /backup   kick off a backup; returns 202 Accepted immediately
  GET  /status   JSON state of the current/last run (for a job's status_url)
  GET  /healthz  liveness check

Clocktower contract
-------------------
Clocktower POSTs {job_id, job_name, run_id, callback_url} to /backup. A 202
response tells it the work was handed off, so it leaves the run open. When the
backup finishes this agent POSTs {run_id, exit_code, output, stderr} back to
callback_url; a non-zero exit_code makes Clocktower fire the job's failure
webhook. A 409 (backup already running) is a >=400 response, so Clocktower
records that run as failed and alerts — overlapping backups are worth knowing
about rather than silently skipping.

Configuration (environment)
---------------------------
  BACKER_SQLITE      colon-separated SQLite files to snapshot consistently
  BACKER_PATHS       colon-separated directories to back up as-is
  BACKER_STAGING     where SQLite snapshots are written
                     default: /var/backups/backer-agent
  PUSH_SCRIPT        path to push.sh        default: search $HOME/bin, /usr/local/bin
  PUSH_TARGET        rsync destination host, "user@host" on root-only boxes
                     default: framboise
  BACKER_AGENT_PORT  listen port            default: 8770
  BACKER_AGENT_ADDR  bind address           default: 0.0.0.0
  BACKER_TOKEN       if set, /backup requires ?token= or X-Backer-Token
  BACKER_STATE_FILE  status persisted here  default: BACKER_STAGING/.agent-state.json
  PUSH_TIMEOUT       seconds per push       default: 3600
"""

import json
import os
import shutil
import sqlite3
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


def _split(value: str) -> list[str]:
    return [p for p in (piece.strip() for piece in value.split(":")) if p]


SQLITE_TARGETS = _split(os.environ.get("BACKER_SQLITE", ""))
PATH_TARGETS = _split(os.environ.get("BACKER_PATHS", ""))
STAGING = Path(os.environ.get("BACKER_STAGING", "/var/backups/backer-agent"))
PUSH_TARGET = os.environ.get("PUSH_TARGET", "framboise")
PORT = int(os.environ.get("BACKER_AGENT_PORT", "8770"))
ADDR = os.environ.get("BACKER_AGENT_ADDR", "0.0.0.0")
TOKEN = os.environ.get("BACKER_TOKEN", "")
PUSH_TIMEOUT = int(os.environ.get("PUSH_TIMEOUT", "3600"))
STATE_FILE = Path(
    os.environ.get("BACKER_STATE_FILE", str(STAGING / ".agent-state.json"))
)
MAX_CALLBACK_FIELD = 8000  # keep callback payloads small


def find_push_script() -> str:
    explicit = os.environ.get("PUSH_SCRIPT", "")
    if explicit:
        return explicit
    host = PUSH_TARGET.split("@")[-1]
    candidates = [
        Path.home() / "bin" / "push.sh",
        Path.home() / "bin" / f"push-to-{host}",
        Path("/usr/local/bin/push.sh"),
        Path("/usr/local/bin") / f"push-to-{host}",
    ]
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return ""


PUSH_SCRIPT = find_push_script()


def log(message: str) -> None:
    # flush so journald sees each line as it happens, not when the pipe buffer fills
    print(f"[backer-agent] {message}", flush=True)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Run state
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_running = False
_state: dict = {
    "state": "idle",
    "started_at": None,
    "finished_at": None,
    "duration_seconds": None,
    "exit_code": None,
    "run_id": None,
    "job_name": None,
    "targets": SQLITE_TARGETS + PATH_TARGETS,
    "last_success_at": None,
    "message": "no run recorded yet",
    "output_tail": "",
}


def load_state() -> None:
    """Restore the last run's outcome so /status survives an agent restart."""
    global _state
    try:
        saved = json.loads(STATE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return
    if isinstance(saved, dict):
        saved["targets"] = SQLITE_TARGETS + PATH_TARGETS
        if saved.get("state") == "running":
            # We were killed mid-run; that run's outcome is unknowable.
            saved["state"] = "interrupted"
            saved["message"] = "agent restarted while a backup was running"
        _state = {**_state, **saved}


def save_state() -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(_state, indent=2))
        tmp.replace(STATE_FILE)
    except OSError as exc:
        log(f"could not persist state: {exc}")


# ---------------------------------------------------------------------------
# The backup itself
# ---------------------------------------------------------------------------

def snapshot_sqlite(src: Path, dest_dir: Path) -> str:
    """
    Copy a live SQLite database consistently using the online backup API.
    A plain file copy of a database being written (WAL and all) can capture a
    torn snapshot; this cannot.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    source = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    try:
        target = sqlite3.connect(str(dest))
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()
    size = dest.stat().st_size
    return f"snapshot {src} -> {dest} ({size:,} bytes)"


def run_push(path: str) -> tuple[int, str, str]:
    cmd = [PUSH_SCRIPT, path, PUSH_TARGET]
    log(f"running: {' '.join(cmd)}")
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=PUSH_TIMEOUT
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return 1, "", f"push of {path} timed out after {PUSH_TIMEOUT}s"
    except OSError as exc:
        return 1, "", f"could not run {PUSH_SCRIPT}: {exc}"


def do_backup(run_id, job_name, callback_url) -> None:
    """Run every configured target, then report the result to the scheduler."""
    global _running
    started = time.monotonic()
    out_parts: list[str] = []
    err_parts: list[str] = []
    exit_code = 0

    with _lock:
        _state.update(
            state="running",
            started_at=now_iso(),
            finished_at=None,
            duration_seconds=None,
            exit_code=None,
            run_id=run_id,
            job_name=job_name,
            message="backup in progress",
        )
        save_state()

    try:
        if not PUSH_SCRIPT:
            raise RuntimeError(
                "no push script found — set PUSH_SCRIPT to your push.sh path"
            )
        if not SQLITE_TARGETS and not PATH_TARGETS:
            raise RuntimeError(
                "nothing configured to back up — set BACKER_SQLITE and/or BACKER_PATHS"
            )

        # 1. Consistent snapshots of live databases into the staging directory
        for db in SQLITE_TARGETS:
            src = Path(db)
            if not src.is_file():
                err_parts.append(f"missing SQLite target: {db}")
                exit_code = 1
                continue
            try:
                out_parts.append(snapshot_sqlite(src, STAGING))
            except (sqlite3.Error, OSError) as exc:
                err_parts.append(f"snapshot of {db} failed: {exc}")
                exit_code = 1

        # 2. Push the staging directory, then any plain paths
        to_push = ([str(STAGING)] if SQLITE_TARGETS else []) + PATH_TARGETS
        for path in to_push:
            code, out, err = run_push(path)
            out_parts.append(out)
            if err.strip():
                err_parts.append(err)
            if code != 0:
                err_parts.append(f"push of {path} exited {code}")
                exit_code = code or 1

    except Exception as exc:  # report, never crash the agent
        err_parts.append(str(exc))
        exit_code = 1

    duration = round(time.monotonic() - started, 1)
    output = "\n".join(p for p in out_parts if p and p.strip())
    errors = "\n".join(p for p in err_parts if p and p.strip())

    with _lock:
        _state.update(
            state="ok" if exit_code == 0 else "failed",
            finished_at=now_iso(),
            duration_seconds=duration,
            exit_code=exit_code,
            message=(
                f"backup completed in {duration}s"
                if exit_code == 0
                else f"backup failed (exit {exit_code})"
            ),
            output_tail=(errors or output)[-2000:],
        )
        if exit_code == 0:
            _state["last_success_at"] = _state["finished_at"]
        save_state()
        _running = False

    log(f"run {run_id} finished exit={exit_code} in {duration}s")
    if callback_url:
        send_callback(callback_url, run_id, exit_code, output, errors)


def send_callback(url, run_id, exit_code, output, errors) -> None:
    """Close the scheduler's open run. Failure here is logged, never fatal."""
    payload = json.dumps(
        {
            "run_id": run_id,
            "exit_code": exit_code,
            "output": output[-MAX_CALLBACK_FIELD:],
            "stderr": errors[-MAX_CALLBACK_FIELD:],
        }
    ).encode()
    request = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as resp:
            log(f"callback {url} -> {resp.status}")
    except (urllib.error.URLError, OSError) as exc:
        log(f"callback to {url} failed: {exc}")


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class AgentHandler(BaseHTTPRequestHandler):
    server_version = "backer-agent/1.0"

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, indent=2).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self, query: dict) -> bool:
        if not TOKEN:
            return True
        supplied = self.headers.get("X-Backer-Token") or query.get("token", [""])[0]
        return supplied == TOKEN

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/status":
            with _lock:
                self._json(200, dict(_state))
        elif parsed.path == "/healthz":
            self._json(200, {"ok": True, "push_script": PUSH_SCRIPT or None})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        global _running
        parsed = urlparse(self.path)
        if parsed.path != "/backup":
            self._json(404, {"error": "not found"})
            return

        query = parse_qs(parsed.query)
        if not self._authorized(query):
            self._json(403, {"error": "bad or missing token"})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        raw = self.rfile.read(length).decode("utf-8", "replace") if 0 < length <= 65536 else ""
        try:
            trigger = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            trigger = {}

        run_id = trigger.get("run_id")
        job_name = trigger.get("job_name")
        callback_url = trigger.get("callback_url") or ""

        with _lock:
            if _running:
                started = _state.get("started_at")
                self._json(
                    409,
                    {
                        "error": "a backup is already running",
                        "started_at": started,
                        "run_id": _state.get("run_id"),
                    },
                )
                log(f"rejected overlapping trigger (running since {started})")
                return
            _running = True

        threading.Thread(
            target=do_backup,
            args=(run_id, job_name, callback_url),
            daemon=True,
        ).start()

        log(f"accepted run_id={run_id} job={job_name!r} callback={callback_url or 'none'}")
        self._json(
            202,
            {
                "status": "accepted",
                "run_id": run_id,
                "targets": SQLITE_TARGETS + PATH_TARGETS,
                "status_url": f"http://{self.headers.get('Host', ADDR)}/status",
            },
        )

    def log_message(self, fmt, *args):
        pass  # our own log() lines are the useful ones


if __name__ == "__main__":
    load_state()
    if not PUSH_SCRIPT:
        log("WARNING: no push script found — triggers will fail until PUSH_SCRIPT is set")
    if not shutil.which("rsync"):
        log("WARNING: rsync not found on PATH")
    log(f"listening on {ADDR}:{PORT}")
    log(f"push script: {PUSH_SCRIPT or '(none)'} -> {PUSH_TARGET}")
    log(f"sqlite targets: {SQLITE_TARGETS or '(none)'}")
    log(f"path targets: {PATH_TARGETS or '(none)'}")
    ThreadingHTTPServer((ADDR, PORT), AgentHandler).serve_forever()
