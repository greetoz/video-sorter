"""Automatic (nightly) run: scan -> plan -> apply the confidence rules -> validate + execute what is confident -> refresh -> plan what is left,
so the Sort tab shows exactly what needs a human in the morning. Settings live in /data/schedule.json, the history in schedule_history.json."""
import collections
import datetime as dt
import os
import re
import sys
import threading
import time
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "sorter"))
import autoselect  # noqa: E402  (DEFAULT_POLICY / merged)
import config  # noqa: E402

from app import jobs, state  # noqa: E402

FILE = f"{state.DATA}/schedule.json"
STATE = f"{state.DATA}/schedule_state.json"
HIST = f"{state.DATA}/schedule_history.json"
SORTER = "sorter"
WINDOW = dt.timedelta(hours=6)  # if the app was down at the scheduled time, it still runs up to this long afterwards


def settings():
    s = state.read_json(FILE, {})
    return {"enabled": bool(s.get("enabled", False)), "time": s.get("time", "03:00"), "tz": s.get("tz", "UTC"), "policy": autoselect.merged(s.get("policy"))}


def save(body):
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", body.get("time", "")):
        raise ValueError("time must look like 03:00")
    try:
        ZoneInfo(body.get("tz", ""))
    except Exception:
        raise ValueError("unknown time zone")
    pol = {}
    for k, default in autoselect.DEFAULT_POLICY.items():
        v = (body.get("policy") or {}).get(k, default)
        if k == "limit":
            v = int(v)
            if not 1 <= v <= 100000:
                raise ValueError("the safety limit must be between 1 and 100000")
        else:
            v = bool(v)
        pol[k] = v
    state.write_json(FILE, {"enabled": bool(body.get("enabled")), "time": body["time"], "tz": body["tz"], "policy": pol})


def next_run(cfg, now=None):
    tz = ZoneInfo(cfg["tz"])
    now = now or dt.datetime.now(tz)
    h, m = map(int, cfg["time"].split(":"))
    cand = now.replace(hour=h, minute=m, second=0, microsecond=0)
    return cand if cand > now else cand + dt.timedelta(days=1)


def overview():
    cfg = settings()
    nxt = next_run(cfg) if cfg["enabled"] else None
    cur = state.current_run()
    summ = state.read_json(state.run_path(cur, "auto_summary.json")) if cur else None
    preview = None
    if summ:
        review = [r for r in summ["rows"] if r["decision"] == "review"] + [dict(src=d["path"], action="LEFTOVER FILE", why=d["why"]) for d in summ["deletes"] if d["decision"] == "review"]
        preview = {"run": cur, "held": summ["held"], "limit": summ["limit"], "counts": summ["counts"], "review": review[:300], "review_total": len(review),
                   "auto_deletes": sum(1 for d in summ["deletes"] if d["decision"] == "auto")}
    return {"settings": cfg, "defaults": autoselect.DEFAULT_POLICY, "next": nxt.strftime("%a %d %b %Y, %H:%M") if nxt else None, "history": state.read_json(HIST, [])[:14], "preview": preview}


def _history_add(entry):
    h = state.read_json(HIST, [])
    h.insert(0, entry)
    state.write_json(HIST, h[:30])


def start(kind="run", trigger="manual"):
    """kind 'run': the full automatic run. kind 'preview': scan, plan and apply the rules, but move nothing."""
    if jobs.status().get("status") in ("running", "cancelling"):
        raise RuntimeError("another job is still running")
    rid1 = state.new_run()
    rid2 = f"{rid1}-left" if kind == "run" else None
    e1 = {"MAN_DIR": f"manifest/runs/{rid1}", "SCHEDULE_FILE": FILE}
    shares = (config.get()["src_share"], config.get()["dst_share"])
    steps = [(f"Inventory + hash {s}", [f"{SORTER}/hash_all.py", s, "8"]) for s in shares]
    steps += [(f"Look up {s} on StashDB", [f"{SORTER}/lookup_fp.py", s]) for s in shares]
    steps += [("Identify and plan", [f"{SORTER}/plan.py"], e1), ("Apply the confidence rules", [f"{SORTER}/autoselect.py"], e1)]
    if kind == "run":
        os.makedirs(state.run_path(rid2), exist_ok=True)
        steps += [("Validate and execute the confident files", [f"{SORTER}/autorun.py"], e1)]
        steps += [(f"Refresh inventory of {s}", [f"{SORTER}/hash_all.py", s, "8"]) for s in shares]
        steps += [("Plan what is left for review", [f"{SORTER}/plan.py"], {"MAN_DIR": f"manifest/runs/{rid2}", "SCHEDULE_FILE": FILE})]

    def done(job, log):
        summ = state.read_json(state.run_path(rid1, "auto_summary.json"), {})
        auto = collections.Counter()
        review = 0
        for k, n in summ.get("counts", {}).items():
            action, decision = k.split(":")
            if decision == "auto":
                auto[action] += n
            else:
                review += n
        full = re.findall(r"^full: (\d+) ops, (\d+) failures", log, re.M)
        fails = [l for l in log.splitlines() if l.startswith("FAIL")]
        executed = job["status"] == "done" and kind == "run" and bool(full) and full[-1][1] == "0"
        left = None
        if kind == "run":
            m2 = state.read_json(state.run_path(rid2, "manifest.json"))
            left = len(m2["rows"]) if m2 else None
        entry = {"t": job["started"], "kind": kind, "trigger": trigger, "status": job["status"], "run": rid1, "held": summ.get("held", False),
                 "moved": auto.get("MOVE", 0) + auto.get("RELOCATE", 0), "dupes": auto.get("DUPE", 0), "deleted": auto.get("DELETE_SAMPLE", 0) + sum(1 for d in summ.get("deletes", []) if d["decision"] == "auto"),
                 "review": review, "left": left, "note": (fails[-1][:160] if fails else "")}
        if kind == "preview":
            entry.update(moved=0, dupes=0, deleted=0)  # a preview does none of it; the counts above were "would"
            entry["would"] = {"moved": auto.get("MOVE", 0) + auto.get("RELOCATE", 0), "dupes": auto.get("DUPE", 0)}
        _history_add(entry)
        now = int(time.time())
        if os.path.exists(state.run_path(rid1, "manifest.json")):
            state.save_run_state(rid1, planned=now)
            if executed:
                state.save_run_state(rid1, executed={"ops": int(full[-1][0]), "failures": 0, "t": now})
            state.set_current(rid1)
        if kind == "run" and os.path.exists(state.run_path(rid2, "manifest.json")):
            m2 = state.read_json(state.run_path(rid2, "manifest.json"))
            if m2 and m2["rows"]:  # something is left: it becomes the plan on the Sort tab
                state.save_run_state(rid2, planned=now)
                state.set_current(rid2)

    return jobs.start("auto-preview" if kind == "preview" else "auto-run", steps, meta={"trigger": trigger}, on_done=done)


def tick():
    cfg = settings()
    if not cfg["enabled"]:
        return
    now = dt.datetime.now(ZoneInfo(cfg["tz"]))
    h, m = map(int, cfg["time"].split(":"))
    sched = now.replace(hour=h, minute=m, second=0, microsecond=0)
    st = state.read_json(STATE, {})
    if not (sched <= now < sched + WINDOW) or st.get("last_date") == now.date().isoformat():
        return
    try:
        start("run", "schedule")
    except RuntimeError:
        return  # a job is running right now; try again on the next tick (the window is long)
    state.write_json(STATE, dict(st, last_date=now.date().isoformat(), last_started=int(time.time())))


def start_loop():
    def loop():
        while True:
            try:
                tick()
            except Exception as ex:
                print(f"scheduler: {ex!r}", file=sys.stderr, flush=True)
            time.sleep(30)

    threading.Thread(target=loop, daemon=True).start()
