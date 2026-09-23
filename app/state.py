"""Where the app keeps its state (the /data volume) and the helpers for sort runs.

Layout (the sorter scripts see it through /app/cache and /app/manifest, which are symlinks into the volume):
  cache/                 inventories, oshashes, StashDB + probe caches (reused between scans)
  manifest/runs/<id>/    one sort run: manifest.json (plan), exclude.json (rows skipped in review), ops.json, exec_log.jsonl, state.json
  manifest/current.txt   id of the run shown on the Sort page
  manifest/dupes/        duplicate review: items.json, verdicts.json, delete_log.jsonl
  jobs/                  one log per background job
"""
import glob
import hashlib
import json
import os
import shutil
import time

DATA = os.environ.get("DATA_DIR", "/data")
ROOT = os.environ.get("APP_ROOT", "/app")
CACHE, MAN, JOBS = f"{DATA}/cache", f"{DATA}/manifest", f"{DATA}/jobs"
RUNS, DUPES = f"{MAN}/runs", f"{MAN}/dupes"


def ensure_seed():
    """First start: copy the caches and history of the manual run (baked into the image at /seed) into the empty volume."""
    for d in (CACHE, RUNS, DUPES, JOBS):
        os.makedirs(d, exist_ok=True)
    marker = f"{DATA}/.seeded"
    if not os.path.exists(marker):
        if os.path.isdir("/seed"):
            shutil.copytree("/seed", DATA, dirs_exist_ok=True)
        open(marker, "w").write(str(int(time.time())))


def read_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


def current_run():
    try:
        return open(f"{MAN}/current.txt").read().strip() or None
    except FileNotFoundError:
        return None


def new_run():
    rid = time.strftime("%Y%m%d-%H%M%S")
    os.makedirs(f"{RUNS}/{rid}", exist_ok=True)
    return rid


def set_current(rid):
    open(f"{MAN}/current.txt", "w").write(rid)


def run_path(rid, name=""):
    return f"{RUNS}/{rid}" + (f"/{name}" if name else "")


def run_state(rid):
    return read_json(run_path(rid, "state.json"), {})


def save_run_state(rid, **kw):
    st = run_state(rid)
    st.update(kw)
    write_json(run_path(rid, "state.json"), st)


def plan_signature(rid):
    """Changes whenever the plan or the skipped rows change, which invalidates an earlier validation."""
    h = hashlib.sha1()
    for name in ("manifest.json", "exclude.json"):
        p = run_path(rid, name)
        h.update(str(os.path.getmtime(p) if os.path.exists(p) else 0).encode())
        if name != "manifest.json" and os.path.exists(p):
            h.update(open(p, "rb").read())
    return h.hexdigest()[:16]


def list_runs():
    return sorted((os.path.basename(d) for d in glob.glob(f"{RUNS}/*") if os.path.isdir(d)), reverse=True)
