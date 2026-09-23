"""Re-verify every file in xtosort$\\_dupes against its kept copy, then (only when certain) delete it.
Stages:  purge.py analyze   -> tiers + counts (read-only)
         purge.py verify    -> server-side byte compare (tier 1) + probe/frame compare (tier 2), writes manifest_r3/verdicts.json (read-only)
         purge.py delete    -> delete files with verdict CERTAIN (keeps a log)"""
import collections
import glob
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))
ROOT = os.path.join(os.path.dirname(__file__), "..")
C = f"{ROOT}/cache"
OUT = f"{ROOT}/{os.environ.get('PURGE_DIR', 'manifest_r3')}"
RUNS = os.environ.get("RUN_GLOB", f"{ROOT}/manifest/runs/*")
HIST = os.path.join(os.path.dirname(__file__), "history", "*.jsonl")  # one-off maintenance scripts outside the app (folder merges/renames
# done by a purpose-built copy of execute.py, never through the app itself) log here so tracked dupes still resolve to their real location
os.makedirs(OUT, exist_ok=True)
low = lambda p: p.lower()


def run_dirs():
    """Every sort run that was executed (has ops + an exec log), oldest first, imported legacy runs before all others."""
    ds = [d for d in glob.glob(RUNS) if os.path.exists(f"{d}/ops.json") and os.path.exists(f"{d}/exec_log.jsonl")]
    return sorted(ds, key=lambda d: (not os.path.basename(d).startswith("legacy"), os.path.basename(d)))


def _absorb_log(path, moved, dir_renamed):
    for line in open(path):
        j = json.loads(line)
        if j.get("mode") != "exec" or not j.get("ok"):
            continue
        if j["type"] == "move":
            moved[(j["sshare"], j["spath"])] = (j["dshare"], j["dfolder"] + "\\" + j["dname"])
        elif j["type"] == "renamedir":
            dir_renamed.setdefault(j["share"], {})[j["old"].lower()] = j["new"]


def load_state():
    ops, moved, dir_renamed = [], {}, {}
    for d in run_dirs():
        rid = os.path.basename(d)
        for o in json.load(open(f"{d}/ops.json")):
            ops.append(dict(o, id=f"{rid}:{o['id']}"))  # ids are only unique within one run
        _absorb_log(f"{d}/exec_log.jsonl", moved, dir_renamed)
    for h in sorted(glob.glob(HIST)):
        _absorb_log(h, moved, dir_renamed)
    inv = {"xtosort$": {low(e["path"]): e for e in json.load(open(f"{C}/inv_xtosort.json")) if not e.get("dir") and "error" not in e},
           "xsites$": {low(e["path"]): e for e in json.load(open(f"{C}/inv_xsites.json")) if not e.get("dir") and "error" not in e}}
    hashes = {"xtosort$": json.load(open(f"{C}/hash_xtosort.json")), "xsites$": json.load(open(f"{C}/hash_xsites.json"))}
    fp = json.load(open(f"{C}/stash_fp.json"))
    return ops, moved, dir_renamed, inv, hashes, fp


def final_loc(moved, dir_renamed, share, path):
    loc = (share, path)
    for _ in range(8):
        if loc in moved:
            loc = moved[loc]
            continue
        sh, p = loc
        if "\\" in p:
            folder, rest = p.split("\\", 1)
            newf = dir_renamed.get(sh, {}).get(folder.lower())
            if newf and newf != folder:
                loc = (sh, newf + "\\" + rest)
                continue
        break
    return loc


def build():
    ops, moved, dir_renamed, inv, hashes, fp = load_state()
    items = []
    for o in ops:
        if o["phase"] == "3-dupe":
            keep0 = (o["keep_share"], o["keep_path"])
        elif o["phase"] == "2-replace":
            keep0 = ("xtosort$", o["note"].split("replaced by better quality ", 1)[1])
        else:
            continue
        dupe = final_loc(moved, dir_renamed, "xtosort$", "_dupes\\" + o["dname"])  # follows later renames (restore-names)
        keep = final_loc(moved, dir_renamed, *keep0)
        rec = {"id": o["id"], "phase": o["phase"], "dupe": dupe, "keep": keep, "orig": (o["sshare"], o["spath"])}
        de, ke = inv[dupe[0]].get(low(dupe[1])), inv[keep[0]].get(low(keep[1]))
        rec["dupe_size"], rec["keep_size"] = (de or {}).get("size"), (ke or {}).get("size")
        rec["dupe_hash"] = (hashes[dupe[0]].get(dupe[1]) or [0, None])[1]
        rec["keep_hash"] = (hashes[keep[0]].get(keep[1]) or [0, None])[1]
        d_sc, k_sc = set(fp.get(rec["dupe_hash"] or "", [])), set(fp.get(rec["keep_hash"] or "", []))
        rec["shared_scene"] = sorted(d_sc & k_sc)[:1]
        if de is None:
            rec["tier"] = "MISSING_DUPE"
        elif ke is None:
            rec["tier"] = "MISSING_KEEP"
        elif rec["dupe_size"] == rec["keep_size"] and rec["dupe_hash"] and rec["dupe_hash"] == rec["keep_hash"]:
            rec["tier"] = "1-identical"
        elif rec["shared_scene"]:
            rec["tier"] = "2-same-scene"
        else:
            rec["tier"] = "3-review"
        items.append(rec)
    return items


if __name__ == "__main__":
    if sys.argv[1] == "analyze":
        items = build()
        c = collections.Counter(i["tier"] for i in items)
        print(len(items), dict(c))
        for t in sorted(c):
            sz = sum(i["dupe_size"] or 0 for i in items if i["tier"] == t)
            print(f"  {t}: {c[t]} files, {sz/1e12:.2f} TB")
        json.dump(items, open(f"{OUT}/items.json", "w"))
        for t in ("3-review", "MISSING_KEEP", "MISSING_DUPE"):
            for i in [x for x in items if x["tier"] == t][:6]:
                print("  ", t, i["dupe"][1][:60], "| keep:", i["keep"][1][:60], "| hashes", bool(i["dupe_hash"]), bool(i["keep_hash"]))


# ---------------------------------------------------------------- verification
def frame_sigs(share, rel, dur, fracs=(0.15, 0.5, 0.85)):
    """dHash (64 bit) of the frame at ~fraction*duration for each fraction."""
    import av
    import smbclient
    import smbio
    sigs = []
    with smbclient.open_file(smbio.unc(share, rel), mode="rb", buffering=1 << 20) as f:
        with av.open(f) as c:
            v = c.streams.video[0]
            v.thread_type = "AUTO"
            for fr in fracs:
                t = dur * fr
                c.seek(int(t * 1_000_000))
                sig = None
                for n, frame in enumerate(c.decode(v)):
                    if (frame.time or 0) >= t - 0.001 or n > 600:
                        img = frame.reformat(width=36, height=32, format="gray", interpolation="AREA").to_ndarray()
                        blocks = img.reshape(8, 4, 9, 4).mean(axis=(1, 3))
                        sig = (blocks[:, 1:] > blocks[:, :-1]).flatten()
                        break
                sigs.append(sig)
    return sigs


def verify_tier2(rec):
    import probe
    import smbio
    d = probe.probe(rec["dupe"][0], rec["dupe"][1], rec["dupe_size"])
    k = probe.probe(rec["keep"][0], rec["keep"][1], rec["keep_size"])
    if d.get("error") or k.get("error") or not d.get("duration") or not k.get("duration"):
        return "REVIEW", "probe failed"
    if abs(d["duration"] - k["duration"]) > 0.03 * max(d["duration"], k["duration"]):
        return "REVIEW", f"durations differ {d['duration']:.0f}s vs {k['duration']:.0f}s"
    dh, kh = d.get("height") or 0, k.get("height") or 0
    if not (kh > dh or (kh == dh and rec["keep_size"] >= rec["dupe_size"])):
        return "REVIEW", f"kept copy is not better ({kh}p {rec['keep_size']//10**6}MB vs {dh}p {rec['dupe_size']//10**6}MB)"
    try:
        a = frame_sigs(rec["dupe"][0], rec["dupe"][1], d["duration"])
        b = frame_sigs(rec["keep"][0], rec["keep"][1], k["duration"])
    except Exception as ex:
        return "REVIEW", f"frame decode failed: {str(ex)[:60]}"
    dist = [int((x != y).sum()) if x is not None and y is not None else 64 for x, y in zip(a, b)]
    if sum(1 for x in dist if x <= 12) >= 2:
        return "CERTAIN", f"same scene id, duration {d['duration']:.0f}s~{k['duration']:.0f}s, {dh}p<={kh}p, frame distances {dist}"
    return "REVIEW", f"frames differ (hamming {dist})"


def run_verify(only_tier2=False):
    import execute
    import smbio
    smbio.connect()  # tier 2 probes read frames over SMB and need a registered session
    items = json.load(open(f"{OUT}/items.json"))
    verdicts = json.load(open(f"{OUT}/verdicts.json")) if os.path.exists(f"{OUT}/verdicts.json") else {}
    c = execute.client()
    t1 = [i for i in items if i["tier"] == "1-identical" and i["id"] not in verdicts]
    if not only_tier2:
        for n in range(0, len(t1), 100):
            batch = t1[n : n + 100]
            ops = [{"id": i["id"], "type": "compare", "ashare": i["dupe"][0], "apath": i["dupe"][1], "bshare": i["keep"][0], "bpath": i["keep"][1]} for i in batch]
            res = execute.run_batch(c, "dry", ops, [])
            for i, r in zip(batch, res):
                keep_in_dupes = i["keep"][1].lower().startswith("_dupes\\")
                verdicts[i["id"]] = ("CERTAIN", r["msg"]) if (r["ok"] and not keep_in_dupes) else ("REVIEW", r["msg"] + (" (kept copy is itself in _dupes)" if keep_in_dupes else ""))
            json.dump(verdicts, open(f"{OUT}/verdicts.json", "w"))
            print(f"  tier1 {min(n + 100, len(t1))}/{len(t1)}", flush=True)
    t2 = [i for i in items if i["tier"] in ("2-same-scene", "3-review") and i["id"] not in verdicts]
    import probe
    for n, i in enumerate(t2):
        verdicts[i["id"]] = verify_tier2(i) if i["tier"] == "2-same-scene" else ("REVIEW", "no shared StashDB scene between the two files")
        if n % 10 == 0:
            json.dump(verdicts, open(f"{OUT}/verdicts.json", "w")); probe.save()
            print(f"  tier2 {n + 1}/{len(t2)}", flush=True)
    for i in items:
        if i["tier"].startswith("MISSING"):
            verdicts[i["id"]] = ("REVIEW", i["tier"])
    json.dump(verdicts, open(f"{OUT}/verdicts.json", "w")); probe.save()
    cnt = collections.Counter(v[0] for v in verdicts.values())
    print("verdicts:", dict(cnt))
    for i in items:
        v = verdicts.get(i["id"])
        if v and v[0] == "REVIEW":
            print("  REVIEW:", i["dupe"][1][:55], "|", v[1][:110])


if __name__ == "__main__" and sys.argv[1] == "verify":
    run_verify()


# ---------------------------------------------------------------- delete (only ever started by the user from the Duplicates page)
def run_delete(request_path, dry=False):
    """Delete the _dupes files the user selected. Request: {"ids": [...], "include_review": bool}.
    Guards, on top of the file server's own checks (source exists + size, kept copy exists + size):
      - only verdict CERTAIN unless the user explicitly included REVIEW items
      - never when the kept copy is itself in _dupes or is also being deleted
    Every deletion is appended to delete_log.jsonl. dry=True runs all checks on the server and deletes nothing."""
    import execute
    req = json.load(open(request_path))
    items = {i["id"]: i for i in json.load(open(f"{OUT}/items.json"))}
    verdicts = json.load(open(f"{OUT}/verdicts.json"))
    selected = [items[i] for i in req["ids"] if i in items]
    doomed = {(i["dupe"][0], low(i["dupe"][1])) for i in selected}
    ops, skipped = [], []
    for i in selected:
        v = verdicts.get(i["id"], ["NONE", "not verified"])
        kshare, kpath = i["keep"]
        if i["tier"].startswith("MISSING"):
            why = i["tier"].lower() + " (nothing to delete or nothing to keep)"
        elif v[0] not in ("CERTAIN", "DECIDED") and not req.get("include_review"):  # DECIDED = the user picked the loser in the compare player
            why = f"verdict {v[0]}: {v[1][:80]}"
        elif low(kpath).startswith("_dupes\\") or (kshare, low(kpath)) in doomed:
            why = "the kept copy is itself in _dupes or also selected for deletion"
        else:
            ops.append({"id": i["id"], "type": "delete", "share": i["dupe"][0], "path": i["dupe"][1], "size": i["dupe_size"],
                        "keep_share": kshare, "keep_path": kpath, "keep_size": i["keep_size"], "verdict": v[0]})
            continue
        skipped.append((i["dupe"][1], why))
    for path, why in skipped:
        print("SKIP", path[:70], "|", why, flush=True)
    mode = "dry" if dry else "exec"
    c = execute.client()
    ok = failed = freed = 0
    with open(f"{OUT}/delete_log.jsonl", "a") as log:
        for n in range(0, len(ops), 100):
            batch = ops[n : n + 100]
            results = execute.run_batch(c, mode, batch, [])
            for _ in range(5):  # a viewer (the compare player, another program) may still have a file open for a moment: retry those
                again = [k for k, r in enumerate(results) if not r["ok"] and "another process" in r["msg"]]
                if not again or dry:
                    break
                time.sleep(2)
                for k, r2 in zip(again, execute.run_batch(c, mode, [batch[k] for k in again], [])):
                    results[k] = r2
            for op, r in zip(batch, results):
                log.write(json.dumps({"mode": mode, "ok": r["ok"], "msg": r["msg"], "dupe": [op["share"], op["path"]], "keep": [op["keep_share"], op["keep_path"]],
                                      "size": op["size"], "verdict": op["verdict"], "t": int(time.time())}) + "\n")
                if r["ok"]:
                    ok += 1
                    freed += op["size"]
                else:
                    failed += 1
                    print("FAIL", op["path"][:70], "|", r["msg"], flush=True)
            log.flush()
            print(f"  {min(n + 100, len(ops))}/{len(ops)}", flush=True)
    verb = "checked (nothing deleted)" if dry else "deleted"
    print(f"{mode}: {ok} {verb}, {freed / 1e12:.3f} TB, {failed} failed, {len(skipped)} skipped")
    return ok, failed


if __name__ == "__main__" and sys.argv[1] == "delete":
    run_delete(sys.argv[2], dry=len(sys.argv) > 3 and sys.argv[3] == "dry")


# ---------------------------------------------------------------- decide (a pair compared in the player; the user picked the file to keep)
def _patch_inventory(gone=None, renamed=None):
    """Keep the cached inventories in step with what was just done, so the lists are right without a 60 s rescan."""
    if gone:
        p = f"{C}/inv_xtosort.json"
        json.dump([e for e in json.load(open(p)) if e["path"].lower() != gone.lower()], open(p, "w"))
    if renamed:
        old, new, size = renamed
        p = f"{C}/inv_xsites.json"
        inv = json.load(open(p))
        for e in inv:
            if e["path"].lower() == old.lower():
                e["path"], e["size"] = new, size
        json.dump(inv, open(p, "w"))


def run_decide(item_id, keep_which):
    """keep_which 'keep': keep the kept copy, delete the duplicate.
    keep_which 'dupe': keep the duplicate. It takes the kept copy's place in the library (same folder and name, its own extension) and the
    old kept copy moves to _dupes, where it is then deleted. All moves are same-volume renames. The library is never without a copy:
    the duplicate goes in under a temporary name first, and step 2 is rolled back if step 3 cannot complete."""
    import execute
    items = json.load(open(f"{OUT}/items.json"))
    verdicts = json.load(open(f"{OUT}/verdicts.json"))
    it = next((i for i in items if i["id"] == item_id), None)
    if it is None or it["tier"].startswith("MISSING"):
        sys.exit("FAIL: nothing to decide - one of the two files is already gone")
    (dshare, dpath), (kshare, kpath) = it["dupe"], it["keep"]
    if low(kpath).startswith("_dupes\\"):
        sys.exit("FAIL: the kept copy is itself in _dupes")
    c = execute.client()

    def mv(sshare, spath, size, dshare_, dfolder, dname):  # one server-side move (size-checked, never overwrites), retried while a viewer still holds the file
        op = {"id": item_id, "type": "move", "sshare": sshare, "spath": spath, "size": size, "dshare": dshare_, "dfolder": dfolder, "dname": dname}
        for _ in range(5):
            r = execute.run_batch(c, "exec", [op], [])[0]
            if r["ok"] or "another process" not in r["msg"]:
                break
            time.sleep(2)
        return r

    old_verdict = verdicts.get(item_id, ["UNVERIFIED", ""])
    dsize, ksize = it["dupe_size"], it["keep_size"]
    if keep_which == "both":
        # not a duplicate after all: keep both. The file leaves _dupes for the library's holding folder, where unidentified files go.
        execute.run_batch(c, "exec", [{"id": item_id, "type": "mkdir", "share": "xsites$", "rel": "_To Sort"}], [])
        name = dpath.rsplit("\\", 1)[-1]
        final = name
        for n in range(2, 9):
            r = mv(dshare, dpath, dsize, "xsites$", "_To Sort", final)
            if r["ok"] or "destination exists" not in r["msg"]:
                break
            b, e = os.path.splitext(name)
            final = f"{b} ({n}){e}"
        if not r["ok"]:
            sys.exit(f"FAIL: {r['msg']} - nothing changed")
        verdicts[item_id] = ["NOT_DUPE", f"you marked it as a different video (was {old_verdict[0]}: {old_verdict[1][:60]}); moved to _To Sort"]
        it["tier"] = "MISSING_DUPE"
        json.dump(items, open(f"{OUT}/items.json", "w"))
        json.dump(verdicts, open(f"{OUT}/verdicts.json", "w"))
        _patch_inventory(gone=dpath)
        with open(f"{OUT}/decide_log.jsonl", "a") as log:
            log.write(json.dumps({"id": item_id, "action": "not-a-duplicate", "from": [dshare, dpath], "to": ["xsites$", f"_To Sort\\{final}"], "t": int(time.time())}) + "\n")
        print(f"kept both: {name} moved to xsites$\\_To Sort\\{final}", flush=True)
        return
    if keep_which == "dupe":
        if "\\" not in kpath:
            sys.exit("FAIL: the kept copy sits at the share root, which is not supported")
        kdir, kname = kpath.rsplit("\\", 1)
        stem, dext = os.path.splitext(kname)[0], os.path.splitext(dpath)[1]
        tmp, final, dname = f"{stem}.incoming{dext}", f"{stem}{dext}", dpath.rsplit("\\", 1)[-1]
        r = mv(dshare, dpath, dsize, kshare, kdir, tmp)
        if not r["ok"]:
            sys.exit(f"FAIL step 1/3 (duplicate into the library): {r['msg']} - nothing changed")
        oname, r = kname, None
        for n in range(2, 9):  # a free name in _dupes for the old kept copy
            r = mv(kshare, kpath, ksize, dshare, "_dupes", oname)
            if r["ok"] or "destination exists" not in r["msg"]:
                break
            b, e = os.path.splitext(kname)
            oname = f"{b} ({n}){e}"
        if not r["ok"]:
            rb = mv(kshare, f"{kdir}\\{tmp}", dsize, dshare, "_dupes", dname)
            sys.exit(f"FAIL step 2/3 (old kept copy to _dupes): {r['msg']} - " + ("rolled back, nothing changed" if rb["ok"] else f"ROLLBACK FAILED ({rb['msg']}); the duplicate is at {kshare}\\{kdir}\\{tmp}"))
        r = mv(kshare, f"{kdir}\\{tmp}", dsize, kshare, kdir, final)
        if not r["ok"]:
            sys.exit(f"FAIL step 3/3 (final name): {r['msg']} - the file you kept is in the library as {kshare}\\{kdir}\\{tmp}; the old copy is in _dupes\\{oname}")
        it["dupe"], it["dupe_size"], it["keep"], it["keep_size"] = [dshare, f"_dupes\\{oname}"], ksize, [kshare, f"{kdir}\\{final}"], dsize
        _patch_inventory(renamed=(kpath, f"{kdir}\\{final}", dsize))
        print(f"swapped: {final} is now in the library at {kdir}; the old copy is in _dupes\\{oname}", flush=True)
    verdicts[item_id] = ["DECIDED", old_verdict[1] if old_verdict[0] == "DECIDED" else f"you chose which file to keep (was {old_verdict[0]}: {old_verdict[1][:60]})"]
    json.dump(items, open(f"{OUT}/items.json", "w"))
    json.dump(verdicts, open(f"{OUT}/verdicts.json", "w"))
    req = f"{OUT}/decide_request.json"
    json.dump({"ids": [item_id], "include_review": True}, open(req, "w"))
    ok, failed = run_delete(req)
    if ok != 1 or failed:
        sys.exit("FAIL: the file could not be deleted (see above)")
    it["tier"] = "MISSING_DUPE"  # gone
    json.dump(items, open(f"{OUT}/items.json", "w"))
    _patch_inventory(gone=it["dupe"][1])
    print("decided: deleted", it["dupe"][1].rsplit("\\", 1)[-1], flush=True)


if __name__ == "__main__" and sys.argv[1] == "decide":
    run_decide(sys.argv[2], sys.argv[3])


# ---------------------------------------------------------------- restore-names (duplicates used to be renamed on their way into _dupes)
def run_restore_names():
    """Give the files in _dupes their original file names back (taken from the move logs of every run). Same-folder renames, never overwrite;
    the renames are written to a run of their own so that the duplicate list keeps following the files."""
    import execute
    import smbclient
    import smbio
    smbio.connect()
    present = {e.name.lower(): e for e in smbclient.scandir(smbio.unc("xtosort$", "_dupes")) if not e.is_dir()}
    wanted = []  # (current name, original name)
    for d in run_dirs():
        for line in open(f"{d}/exec_log.jsonl"):
            j = json.loads(line)
            if j["mode"] == "exec" and j["ok"] and j["type"] == "move" and j["dshare"] == "xtosort$" and j["dfolder"] == "_dupes" and j["sshare"] == "xtosort$":
                orig = j["spath"].rsplit("\\", 1)[-1]
                if orig != j["dname"]:
                    wanted.append((j["dname"], orig))
    c = execute.client()
    rid = time.strftime("%Y%m%d-%H%M%S") + "-restore-names"
    rdir = f"{ROOT}/manifest/runs/{rid}"
    os.makedirs(rdir, exist_ok=True)
    json.dump([], open(f"{rdir}/ops.json", "w"))
    done = failed = 0
    with open(f"{rdir}/exec_log.jsonl", "a") as log:
        for cur, orig in wanted:
            e = present.get(cur.lower())
            if e is None:
                continue  # already gone or already renamed
            target, n = orig, 1
            while target.lower() in present and target.lower() != cur.lower():
                n += 1
                b, x = os.path.splitext(orig)
                target = f"{b} ({n}){x}"
            op = {"id": "r", "type": "move", "sshare": "xtosort$", "spath": f"_dupes\\{cur}", "size": e.stat().st_size, "dshare": "xtosort$", "dfolder": "_dupes", "dname": target}
            for _ in range(5):
                r = execute.run_batch(c, "exec", [op], [])[0]
                if r["ok"] or "another process" not in r["msg"]:
                    break
                time.sleep(2)
            if r["ok"]:
                done += 1
                del present[cur.lower()]
                present[target.lower()] = e
                log.write(json.dumps(dict(op, mode="exec", ok=True, msg="renamed back", t=int(time.time()))) + "\n")
                log.flush()
                print(f"  {cur[:60]}  ->  {target[:60]}", flush=True)
            else:
                failed += 1
                print("FAIL", cur[:60], "|", r["msg"][:100], flush=True)
    print(f"restore-names: {done} renamed back to their original names, {failed} failed")
    if failed:
        sys.exit(1)


if __name__ == "__main__" and sys.argv[1] == "restore-names":
    run_restore_names()
