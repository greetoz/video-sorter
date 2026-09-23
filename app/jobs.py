"""One background job at a time. A job is a list of steps; each step is a sorter script run as a subprocess whose output goes to a log
that the UI tails. The scripts are resumable, so a job that was interrupted (container restart, cancel) can simply be started again."""
import os
import re
import signal
import subprocess
import sys
import threading
import time

from app import state

_lock = threading.Lock()
_job = None
_proc = None


def _persist():
    state.write_json(f"{state.JOBS}/last.json", {k: v for k, v in _job.items() if k != "proc"})


def load_last():
    """After a restart: whatever was running is over."""
    global _job
    j = state.read_json(f"{state.JOBS}/last.json")
    if j:
        if j["status"] == "running":
            j["status"], j["ended"] = "interrupted", int(time.time())
        _job = j


def _run(job, steps, on_done):
    global _proc
    base_env = dict(os.environ, PYTHONUNBUFFERED="1", **job.get("env", {}))
    with open(job["log"], "a") as log:
        for n, step in enumerate(steps, 1):
            label, argv = step[0], step[1]
            env = dict(base_env, **(step[2] if len(step) > 2 else {}))  # optional per-step environment
            job.update(step=n, label=label)
            _persist()
            log.write(f"\n=== [{n}/{len(steps)}] {label}\n")
            log.flush()
            try:
                _proc = subprocess.Popen([sys.executable, "-u", *argv], cwd=state.ROOT, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                rc = _proc.wait()
            except Exception as ex:
                log.write(f"could not start: {ex}\n")
                rc = -1
            if job["status"] == "cancelling":
                job["status"] = "cancelled"
                break
            if rc != 0:
                job["status"] = "failed"
                log.write(f"=== step failed (exit code {rc})\n")
                break
        else:
            job["status"] = "done"
    job["ended"] = int(time.time())
    _proc = None
    try:
        if on_done:
            on_done(job, open(job["log"]).read())
    finally:
        _persist()


def start(name, steps, env=None, on_done=None, meta=None):
    """steps: [(label, [script, args...])], paths relative to the app root."""
    global _job
    with _lock:
        if _job and _job["status"] in ("running", "cancelling"):
            raise RuntimeError(f"'{_job['name']}' is still running")
        jid = time.strftime("%Y%m%d-%H%M%S") + "-" + name
        _job = {"id": jid, "name": name, "status": "running", "started": int(time.time()), "ended": None, "step": 0, "steps": len(steps),
                "label": "", "log": f"{state.JOBS}/{jid}.log", "env": env or {}, "meta": meta or {}}
        threading.Thread(target=_run, args=(_job, steps, on_done), daemon=True).start()
    return _job


def cancel():
    with _lock:
        if _job and _job["status"] == "running":
            _job["status"] = "cancelling"
            if _proc:
                try:
                    os.killpg(_proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            return True
    return False


def status(tail=60):
    if not _job:
        return {"status": "idle"}
    out = {k: v for k, v in _job.items() if k not in ("log", "env")}
    try:
        lines = open(_job["log"]).read().splitlines()
    except FileNotFoundError:
        lines = []
    out["tail"] = lines[-tail:]
    for l in reversed(lines):  # progress "  123/456" printed by the scripts
        m = re.match(r"\s*(?:tier\d\s+)?(\d+)/(\d+)(?:\s|$)", l)
        if m and int(m[2]):
            out["progress"] = [int(m[1]), int(m[2])]
            break
    out["elapsed"] = (_job["ended"] or int(time.time())) - _job["started"]
    return out
