"""The sorting workflow: scan -> plan -> review -> validate -> execute, and the duplicate review with delete."""
import collections
import os
import re
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from app import banner, jobs, media, scheduler, state
from app.security import auth, same_origin

router = APIRouter(prefix="/api", dependencies=[Depends(auth)])
post = dict(dependencies=[Depends(same_origin)])
SORTER = "sorter"


def _start(name, steps, **kw):
    try:
        return jobs.start(name, steps, **kw)
    except RuntimeError as ex:
        raise HTTPException(status_code=409, detail=str(ex))


def _need_run():
    rid = state.current_run()
    if not rid or not os.path.exists(state.run_path(rid, "manifest.json")):
        raise HTTPException(status_code=409, detail="there is no plan yet - build one first")
    return rid


# ------------------------------------------------------------------ jobs
@router.get("/job")
def job():
    return jobs.status()


@router.post("/job/cancel", **post)
def cancel():
    return {"cancelled": jobs.cancel()}


# ------------------------------------------------------------------ sort
def _inbox():
    inv = state.read_json(f"{state.CACHE}/inv_xtosort.json", [])
    vids = [e for e in inv if not e.get("dir") and "error" not in e and os.path.splitext(e["path"])[1].lower() in (".mp4", ".mkv", ".avi", ".wmv", ".mov", ".m4v", ".ts", ".flv", ".mpg", ".mpeg", ".webm", ".vid")
            and e["path"].split("\\")[0].lower() != "_dupes"]
    p = f"{state.CACHE}/inv_xtosort.json"
    return {"videos": len(vids), "bytes": sum(e["size"] for e in vids), "scanned": int(os.path.getmtime(p)) if os.path.exists(p) else None}


@router.get("/sort")
def sort_overview():
    rid = state.current_run()
    out = {"inbox": _inbox(), "run": rid, "plan": None}
    if rid and os.path.exists(state.run_path(rid, "manifest.json")):
        m = state.read_json(state.run_path(rid, "manifest.json"))
        S, rows = m["summary"], m["rows"]
        st = state.run_state(rid)
        excl = state.read_json(state.run_path(rid, "exclude.json"), [])
        dry = st.get("dry")
        out["plan"] = {
            "planned": st.get("planned"), "actions": dict(collections.Counter(r["action"] for r in rows)),
            "conf": dict(collections.Counter(r["conf"] for r in rows)), "new_folders": len(S.get("new_folders", [])),
            "funscripts": len(S.get("funscripts", [])), "deletes": len(S.get("deletes", [])), "replace_existing": len(S.get("replace_existing", [])),
            "unidentified": sum(1 for r in rows if r["action"] == "MOVE" and r["dest_folder"] in ("_To Sort", "_Movie_Scenes")),
            "excluded": len(excl), "dry": dry, "dry_current": bool(dry and dry.get("sig") == state.plan_signature(rid)), "executed": st.get("executed"), "exec_failed": st.get("exec_failed"),
        }
    return out


@router.get("/sort/rows")
def sort_rows():
    rid = _need_run()
    m = state.read_json(state.run_path(rid, "manifest.json"))
    excl = set(state.read_json(state.run_path(rid, "exclude.json"), []))
    keys = ("action", "conf", "method", "src", "dest_folder", "dest_name", "size", "height", "notes")
    return {"run": rid, "rows": [dict({k: r.get(k) for k in keys}, skip=r["src"] in excl) for r in m["rows"]]}


@router.post("/sort/scan", **post)
def scan():
    """Refresh both inventories and the oshash / StashDB fingerprint caches (incremental, read-only)."""
    steps = [(f"Inventory + hash {s}", [f"{SORTER}/hash_all.py", s, "8"]) for s in ("xtosort$", "xsites$")]
    steps += [(f"Look up {s} on StashDB", [f"{SORTER}/lookup_fp.py", s]) for s in ("xtosort$", "xsites$")]
    return _start("scan", steps)


@router.post("/sort/plan", **post)
def plan():
    """Identify every video in xtosort$ and decide where it goes. Read-only; starts a new run."""
    if not all(os.path.exists(f"{state.CACHE}/{f}") for f in ("inv_xtosort.json", "inv_xsites.json", "hash_xtosort.json", "hash_xsites.json")):
        raise HTTPException(status_code=409, detail="scan first - there is no inventory yet")
    if jobs.status().get("status") in ("running", "cancelling"):
        raise HTTPException(status_code=409, detail="another job is still running")
    rid = state.new_run()

    def done(job, log):  # the new plan replaces the one on screen only when it was built successfully
        if job["status"] == "done" and os.path.exists(state.run_path(rid, "manifest.json")):
            state.save_run_state(rid, planned=int(time.time()))
            state.set_current(rid)

    return _start("plan", [("Identify and plan", [f"{SORTER}/plan.py"])], env={"MAN_DIR": f"manifest/runs/{rid}"}, on_done=done)


class Exclude(BaseModel):
    srcs: list[str]


@router.post("/sort/exclude", **post)
def exclude(body: Exclude):
    rid = _need_run()
    state.write_json(state.run_path(rid, "exclude.json"), sorted(set(body.srcs)))
    return {"excluded": len(set(body.srcs))}


def _finish(kind, rid):
    def cb(job, log):
        m = re.findall(rf"^{kind}: (\d+) ops, (\d+) failures", log, re.M)
        if job["status"] == "done" and m:
            res = {"ops": int(m[-1][0]), "failures": int(m[-1][1]), "t": int(time.time())}
            by_phase = collections.Counter()  # the script prints "('7-delete', True, 'ok') 2" per phase/result
            for phase, n in re.findall(r"^\s+\('\d-(\w+)', (?:True|False), '[^']*'\)\s+(\d+)", log, re.M):
                by_phase[phase] += int(n)
            res["by_phase"] = dict(by_phase)
            if kind == "dry":
                res["sig"] = state.plan_signature(rid)
                state.save_run_state(rid, dry=res)
            elif res["failures"] == 0:
                state.save_run_state(rid, executed=res, exec_failed=None)
            else:  # execute.py stops at the first failure and is resumable: keep the plan on screen
                state.save_run_state(rid, exec_failed=res)
    return cb


@router.post("/sort/validate", **post)
def validate():
    """Dry run: every operation is checked on the file server (source, size, destination, folders); nothing changes."""
    rid = _need_run()
    return _start("validate", [("Validate all operations on the file server", [f"{SORTER}/execute.py", "dry"])], env={"MAN_DIR": f"manifest/runs/{rid}"}, on_done=_finish("dry", rid))


@router.post("/sort/execute", **post)
def execute():
    rid = _need_run()
    dry = state.run_state(rid).get("dry")
    if not dry or dry.get("sig") != state.plan_signature(rid):
        raise HTTPException(status_code=409, detail="validate the current plan first (the plan or the skipped rows changed since the last validation)")
    if dry["failures"]:
        raise HTTPException(status_code=409, detail=f"the validation had {dry['failures']} failures - fix or skip those rows first")
    steps = [("Move, rename and clean up", [f"{SORTER}/execute.py", "full"])]
    steps += [(f"Refresh inventory of {s}", [f"{SORTER}/hash_all.py", s, "8"]) for s in ("xtosort$", "xsites$")]
    return _start("execute", steps, env={"MAN_DIR": f"manifest/runs/{rid}"}, on_done=_finish("full", rid))


# ------------------------------------------------------------------ duplicates
PURGE = {"PURGE_DIR": "manifest/dupes"}


@router.post("/dupes/verify", **post)
def dupes_verify():
    """Re-check every file in _dupes against the copy that was kept (byte compare on the server, then frame comparison)."""
    steps = [(f"Refresh inventory of {s}", [f"{SORTER}/hash_all.py", s, "8"]) for s in ("xtosort$", "xsites$")]
    steps += [("List duplicates", [f"{SORTER}/purge.py", "analyze"]), ("Verify duplicates", [f"{SORTER}/purge.py", "verify"])]
    return _start("verify-dupes", steps, env=PURGE)


@router.get("/dupes")
def dupes():
    items = state.read_json(f"{state.DUPES}/items.json", [])
    verdicts = state.read_json(f"{state.DUPES}/verdicts.json", {})
    rows, count, size = [], collections.Counter(), collections.Counter()
    for i in items:
        if i["tier"] == "MISSING_DUPE":
            count["gone"] += 1  # already deleted or moved away
            continue
        v = verdicts.get(i["id"])
        verdict = v[0] if v else "UNVERIFIED"
        count[verdict] += 1
        size[verdict] += i["dupe_size"] or 0
        rows.append({"id": i["id"], "verdict": verdict, "reason": v[1] if v else "not verified yet", "dupe": i["dupe"][1].replace("_dupes\\", "", 1), "keep": i["keep"][1], "keep_share": i["keep"][0],
                     "dupe_size": i["dupe_size"], "keep_size": i["keep_size"]})
    tracked = {i["dupe"][1].lower() for i in items}
    for e in state.read_json(f"{state.CACHE}/inv_xtosort.json", []):
        p = e["path"]
        if e.get("dir") or "error" in e or not p.lower().startswith("_dupes\\") or p.lower() in tracked:
            continue
        count["OTHER"] += 1
        rows.append({"id": None, "verdict": "OTHER", "reason": "not one of the tracked duplicates (for example a funscript that followed its video) - nothing is done with it", "dupe": p.split("\\", 1)[1],
                     "keep": "", "keep_share": "", "dupe_size": e["size"], "keep_size": None, "other": True})
    log = f"{state.DUPES}/delete_log.jsonl"
    deleted = sum(1 for l in open(log) if '"mode": "exec"' in l and '"ok": true' in l) if os.path.exists(log) else 0
    return {"rows": rows, "count": dict(count), "bytes": dict(size), "deleted_ever": deleted, "verified_at": int(os.path.getmtime(f"{state.DUPES}/verdicts.json")) if os.path.exists(f"{state.DUPES}/verdicts.json") else None}


def _item(id):
    it = next((i for i in state.read_json(f"{state.DUPES}/items.json", []) if i["id"] == id), None)
    if not it:
        raise HTTPException(status_code=404, detail="unknown duplicate")
    return it


@router.get("/dupes/info")
def dupes_info(id: str):
    """Everything the compare player shows about a duplicate and its kept copy (from the probe cache, no file access)."""
    it = _item(id)
    probe = state.read_json(f"{state.CACHE}/probe.json", {})
    v = state.read_json(f"{state.DUPES}/verdicts.json", {}).get(id, ["UNVERIFIED", "not verified yet"])
    out = {"id": id, "verdict": v[0], "reason": v[1]}
    for which in ("dupe", "keep"):
        share, path = it[which]
        size = it[which + "_size"]
        pr = probe.get(f"{share}|{path}|{size}") or {}
        out[which] = {"name": path.rsplit("\\", 1)[-1], "where": f"{share}\\{path.rsplit(chr(92), 1)[0] if chr(92) in path else ''}".rstrip("\\"), "size": size,
                      "width": pr.get("width"), "height": pr.get("height"), "codec": pr.get("codec"), "duration": pr.get("duration"),
                      "native": media.is_native(path, pr.get("codec")), "missing": it["tier"] == ("MISSING_DUPE" if which == "dupe" else "MISSING_KEEP")}
    return out


@router.get("/dupes/media")
def dupes_media(request: Request, id: str, which: str, convert: int = 0, start: float = 0.0):
    """Streams one side of a duplicate to the browser. The path comes from the server-side list, never from the request.
    convert=1 converts on the fly (for WMV/MPEG/AVI...) starting `start` seconds in; otherwise the file is served as it is, with range support."""
    if which not in ("dupe", "keep"):
        raise HTTPException(status_code=404)
    import smbclient
    import smbio
    smbio.connect()
    share, path = _item(id)[which]
    unc = smbio.unc(share, path)
    if convert:
        media.release([unc])  # a reseek/drift-correction on this same side abandons its old stream client-side without telling the server;
        # drop it first so its conversion slot is freed before we try to take one, instead of leaking until the 15 min idle reaper gets to it
        gen = media.convert_stream(unc, max(0.0, min(start, 86400.0)))
        if gen is None:
            raise HTTPException(status_code=503, detail="two conversions are already running - close another comparison first")
        return StreamingResponse(gen, media_type="video/mp4", headers={"Cache-Control": "no-store", "Accept-Ranges": "none"})
    try:
        size = smbclient.stat(unc).st_size
    except Exception:
        raise HTTPException(status_code=404, detail="that file no longer exists")
    try:
        status, first, last = media.parse_range(request.headers.get("range"), size)
    except ValueError:
        raise HTTPException(status_code=416, headers={"Content-Range": f"bytes */{size}"})
    headers = {"Accept-Ranges": "bytes", "Content-Length": str(last - first + 1), "Cache-Control": "private, max-age=600"}
    if status == 206:
        headers["Content-Range"] = f"bytes {first}-{last}/{size}"
    return StreamingResponse(media.range_stream(unc, first, last), status_code=status, headers=headers,
                             media_type=media.MIME.get(os.path.splitext(path)[1].lower(), "application/octet-stream"))


@router.post("/dupes/restore-names", **post)
def dupes_restore_names():
    """Duplicates used to be renamed on their way into _dupes; give the files that are still there their original names back."""
    media.release_all()
    steps = [("Refresh inventory of xtosort$", [f"{SORTER}/hash_all.py", "xtosort$", "8"]), ("Restore original file names", [f"{SORTER}/purge.py", "restore-names"]),
             ("Refresh inventory of xtosort$", [f"{SORTER}/hash_all.py", "xtosort$", "8"]), ("Rebuild the duplicate list", [f"{SORTER}/purge.py", "analyze"])]
    return _start("restore-dupe-names", steps, env=PURGE)


class ReleaseReq(BaseModel):
    id: str


@router.post("/dupes/release", **post)
def dupes_release(body: ReleaseReq):
    """The player left this pair: close the app's open streams of both files so nothing keeps them locked on the share."""
    import smbio
    it = _item(body.id)
    return {"closed": media.release([smbio.unc(*it["dupe"]), smbio.unc(*it["keep"])])}


# ------------------------------------------------------------------ header banner
class BannerSettings(BaseModel):
    enabled: bool
    names: list[str]
    seconds: int = 8
    source: str = "scenes"


@router.get("/banner")
def banner_get():
    return banner.overview()


@router.get("/banner/img/{filename}")
def banner_img(filename: str):
    p = banner.image_path(filename)
    if not p:
        raise HTTPException(status_code=404)
    return FileResponse(p, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


@router.post("/banner/settings", **post)
def banner_save(body: BannerSettings):
    """Save the list and fetch what is missing in the background."""
    try:
        banner.save_config(body.model_dump())
    except (ValueError, TypeError) as ex:
        raise HTTPException(status_code=400, detail=str(ex))
    banner.refresh()
    return banner.overview()


@router.post("/banner/refresh", **post)
def banner_refresh():
    banner.refresh(force=True)
    return banner.overview()


# ------------------------------------------------------------------ automatic (nightly) run
class AutoSettings(BaseModel):
    enabled: bool
    time: str
    tz: str
    policy: dict


class AutoRun(BaseModel):
    preview: bool = False


@router.get("/auto")
def auto_get():
    return scheduler.overview()


@router.post("/auto/settings", **post)
def auto_save(body: AutoSettings):
    try:
        scheduler.save(body.model_dump())
    except (ValueError, TypeError) as ex:
        raise HTTPException(status_code=400, detail=str(ex))
    return scheduler.overview()


@router.post("/auto/run", **post)
def auto_run(body: AutoRun):
    """Run the automatic run right now (or only its preview: nothing is moved)."""
    try:
        return scheduler.start("preview" if body.preview else "run", "manual")
    except RuntimeError as ex:
        raise HTTPException(status_code=409, detail=str(ex))


class Decide(BaseModel):
    id: str
    keep: str  # "dupe" = keep the duplicate (delete the kept copy), "keep" = keep the kept copy (delete the duplicate), "both" = not a duplicate


@router.post("/dupes/decide", **post)
def dupes_decide(body: Decide):
    """From the compare player: the user picked the file to keep; the other one is deleted."""
    if body.keep not in ("dupe", "keep", "both"):
        raise HTTPException(status_code=400, detail="keep must be 'dupe', 'keep' or 'both'")
    it = _item(body.id)
    if it["tier"].startswith("MISSING"):
        raise HTTPException(status_code=409, detail="one of the two files no longer exists")
    import smbio
    media.release([smbio.unc(*it["dupe"]), smbio.unc(*it["keep"])])  # close the app's own streams of these files before moving/deleting them
    label = {"keep": "Delete the duplicate, keep the kept copy", "dupe": "Keep the duplicate, delete the kept copy", "both": "Not a duplicate: keep both"}[body.keep]
    return _start("decide-dupe", [(label, [f"{SORTER}/purge.py", "decide", body.id, body.keep])], env=PURGE)


class Delete(BaseModel):
    ids: list[str]
    include_review: bool = False
    dry_run: bool = False
    confirm: str = ""


@router.post("/dupes/delete", **post)
def dupes_delete(body: Delete):
    """Delete the selected duplicates. Needs the typed confirmation `DELETE <count>`; dry_run only runs the checks."""
    if not body.ids:
        raise HTTPException(status_code=400, detail="nothing selected")
    if not body.dry_run and body.confirm != f"DELETE {len(body.ids)}":
        raise HTTPException(status_code=400, detail=f"confirmation must be exactly 'DELETE {len(body.ids)}'")
    state.write_json(f"{state.DUPES}/delete_request.json", {"ids": body.ids, "include_review": body.include_review, "t": int(time.time())})
    argv = [f"{SORTER}/purge.py", "delete", f"{state.DUPES}/delete_request.json"] + (["dry"] if body.dry_run else [])
    if body.dry_run:
        return _start("delete-dupes-check", [("Check selected duplicates (nothing is deleted)", argv)], env=PURGE)
    steps = [(f"Delete {len(body.ids)} duplicates", argv), ("Refresh inventory of xtosort$", [f"{SORTER}/hash_all.py", "xtosort$", "8"]), ("Rebuild the duplicate list", [f"{SORTER}/purge.py", "analyze"])]
    return _start("delete-dupes", steps, env=PURGE)
