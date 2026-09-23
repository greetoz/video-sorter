"""Executor: turns manifest.json into ordered operations and runs them ON the file server via PowerShell remoting
(pypsrp, non-admin, NTLM). Same-volume renames, never overwrites, verifies every step, resumable via a local JSONL log.

Usage:  execute.py dry            validate every operation on the server, change nothing
        execute.py pilot          execute only the pilot subset (manifest/pilot.json), then verify
        execute.py full           execute everything not yet done
"""
import base64
import collections
import json
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))
from pypsrp.client import Client

ROOT = os.path.join(os.path.dirname(__file__), "..")
MAN = f"{ROOT}/{os.environ.get('MAN_DIR', 'manifest')}"
C = f"{ROOT}/cache"
HOST = os.environ.get("SMB_HOST", "10.10.0.11")
ROOTS = {"xtosort$": r"H:\xToSort$", "xsites$": r"H:\XSites$"}
BATCH = 150

PS = r"""
$ErrorActionPreference = 'Stop'
$roots = @{ 'xtosort$' = 'H:\xToSort$'; 'xsites$' = 'H:\XSites$' }
$payload = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($b64)) | ConvertFrom-Json
$planned = @($payload.planned)
function ToLongPath([string]$share, [string]$rel) { $p = $roots[$share]; if ($rel) { $p = $p + '\' + $rel }; return '\\?\' + $p }
function FileLen([string]$p) { return (New-Object IO.FileInfo($p)).Length }
$out = New-Object System.Collections.ArrayList
foreach ($op in @($payload.ops)) {
  $r = [ordered]@{ id = $op.id; ok = $false; msg = ''; ms = 0 }
  $sw = [Diagnostics.Stopwatch]::StartNew()
  try {
    switch ($op.type) {
      'mkdir' {
        $d = ToLongPath $op.share $op.rel
        if ([IO.Directory]::Exists($d)) { $r.ok = $true; $r.msg = 'exists' }
        elseif ($mode -eq 'exec') { [void][IO.Directory]::CreateDirectory($d); $r.ok = [IO.Directory]::Exists($d); $r.msg = 'created' }
        else { $r.ok = $true; $r.msg = 'would create' }
      }
      'move' {
        $src = ToLongPath $op.sshare $op.spath
        $dst = ToLongPath $op.dshare ($op.dfolder + '\' + $op.dname)
        $parent = ToLongPath $op.dshare $op.dfolder
        if (-not [IO.File]::Exists($src)) { throw 'source missing' }
        $len = FileLen $src
        if ($len -ne [int64]$op.size) { throw "size mismatch: $len vs $($op.size)" }
        if ([IO.File]::Exists($dst) -or [IO.Directory]::Exists($dst)) { throw 'destination exists' }
        if ($op.keep_path) {
          $k = ToLongPath $op.keep_share $op.keep_path
          if (-not [IO.File]::Exists($k)) { throw 'file to keep is missing - refusing to move duplicate' }
        }
        $pok = [IO.Directory]::Exists($parent) -or ($mode -eq 'dry' -and ($planned -contains ($op.dshare + '|' + $op.dfolder)))
        if (-not $pok) { throw 'destination folder missing' }
        if ($mode -eq 'exec') {
          [IO.File]::Move($src, $dst)
          if ([IO.File]::Exists($src) -or -not [IO.File]::Exists($dst)) { throw 'post-check failed' }
          if ((FileLen $dst) -ne $len) { throw 'post-check size differs' }
          $r.msg = 'moved'
        } else { $r.msg = 'ok' }
        $r.ok = $true
      }
      'compare' {
        $a = ToLongPath $op.ashare $op.apath; $b = ToLongPath $op.bshare $op.bpath
        if (-not [IO.File]::Exists($a) -or -not [IO.File]::Exists($b)) { throw 'file missing' }
        $la = FileLen $a; $lb = FileLen $b
        if ($la -ne $lb) { $r.msg = 'size differs' }
        else {
          $n = 48; $b1 = New-Object byte[] 65536; $b2 = New-Object byte[] 65536
          $m1 = [Security.Cryptography.MD5]::Create(); $m2 = [Security.Cryptography.MD5]::Create()
          $fa = [IO.File]::Open($a, 'Open', 'Read', 'ReadWrite'); $fb = [IO.File]::Open($b, 'Open', 'Read', 'ReadWrite')
          try {
            for ($k = 0; $k -lt $n; $k++) {
              $off = 0; if ($la -gt 65536) { $off = [int64][math]::Floor(($la - 65536) * $k / ($n - 1)) }
              $fa.Position = $off; $fb.Position = $off
              $c1 = $fa.Read($b1, 0, 65536); $c2 = $fb.Read($b2, 0, 65536)
              [void]$m1.TransformBlock($b1, 0, $c1, $null, 0); [void]$m2.TransformBlock($b2, 0, $c2, $null, 0)
            }
            [void]$m1.TransformFinalBlock([byte[]]@(), 0, 0); [void]$m2.TransformFinalBlock([byte[]]@(), 0, 0)
          } finally { $fa.Close(); $fb.Close() }
          $r.ok = ([BitConverter]::ToString($m1.Hash) -eq [BitConverter]::ToString($m2.Hash))
          if ($r.ok) { $r.msg = 'identical at 48 sampled points' } else { $r.msg = 'DIFFERENT content' }
        }
      }
      'delete' {
        $f = ToLongPath $op.share $op.path
        if (-not [IO.File]::Exists($f)) { throw 'file missing' }
        if ((FileLen $f) -ne [int64]$op.size) { throw 'size mismatch' }
        if ($op.keep_path) {
          $k = ToLongPath $op.keep_share $op.keep_path
          if (-not [IO.File]::Exists($k)) { throw 'file to keep is missing - refusing to delete' }
          if ($op.keep_size -and (FileLen $k) -ne [int64]$op.keep_size) { throw 'kept file size changed - refusing to delete' }
        }
        if ($mode -eq 'exec') { [IO.File]::Delete($f); if ([IO.File]::Exists($f)) { throw 'still exists' }; $r.msg = 'deleted' } else { $r.msg = 'ok' }
        $r.ok = $true
      }
      'rmdir' {
        $d = ToLongPath $op.share $op.path
        if (-not [IO.Directory]::Exists($d)) { $r.ok = $true; $r.msg = 'gone' }
        else {
          $n = @([IO.Directory]::GetFileSystemEntries($d)).Count
          if ($n -gt 0) { $r.msg = "skip: not empty ($n)" }
          elseif ($mode -eq 'exec') { [IO.Directory]::Delete($d); $r.ok = -not [IO.Directory]::Exists($d); $r.msg = 'removed' }
          else { $r.ok = $true; $r.msg = 'ok (empty)' }
        }
      }
    }
  } catch { $r.ok = $false; $r.msg = $_.Exception.Message }
  $r.ms = $sw.ElapsedMilliseconds
  [void]$out.Add($r)
}
ConvertTo-Json -InputObject @($out) -Compress -Depth 3
"""


def build_ops():
    M = json.load(open(f"{MAN}/manifest.json"))
    rows, S = M["rows"], M["summary"]
    # rows the user skipped in the review screen are left exactly where they are
    excl_path = f"{MAN}/exclude.json"
    excl = set(json.load(open(excl_path))) if os.path.exists(excl_path) else set()
    rows = [r for r in rows if r["src"] not in excl]
    S["deletes"] = [d for d in S["deletes"] if d["path"] not in excl]
    S["funscripts"] = [f for f in S["funscripts"] if f["video"] not in excl]
    S["replace_existing"] = [x for x in S["replace_existing"] if x["replaced_by"] not in excl]
    inv_t = json.load(open(f"{C}/inv_xtosort.json"))
    inv_s = json.load(open(f"{C}/inv_xsites.json"))
    size_t = {e["path"]: e["size"] for e in inv_t if not e.get("dir")}
    xs_top = {e["path"].lower() for e in inv_s if e.get("dir") and "\\" not in e["path"]}
    ops = []

    def add(phase, **kw):
        ops.append(dict(kw, phase=phase, id=f"{len(ops) + 1:05d}"))

    # 1) folders
    need = collections.OrderedDict()
    if any(r["action"] == "DUPE" for r in rows) or S["replace_existing"]:
        need[("xtosort$", "_dupes")] = 1
    for r in rows:
        if r["action"] in ("MOVE", "RELOCATE") and r["dest_folder"] and r["dest_folder"].lower() not in xs_top:
            need[("xsites$", r["dest_folder"])] = 1
    for (share, folder) in need:
        add("1-mkdir", type="mkdir", share=share, rel=folder)
    planned = [f"{s}|{f}" for (s, f) in need]
    used_dupes = {r["dest_name"].lower() for r in rows if r["action"] == "DUPE"}
    used_dupes |= {e["path"].split("\\")[-1].lower() for e in inv_t if not e.get("dir") and "error" not in e and e["path"].lower().startswith("_dupes\\")}

    # 2) replace existing xsites files with better copies -> _dupes
    for x in S["replace_existing"]:
        name = os.path.basename(x["path"].replace("\\", "/"))
        n = 1
        base, ext = os.path.splitext(name)
        while name.lower() in used_dupes:
            n += 1
            name = f"{base} ({n}){ext}"
        used_dupes.add(name.lower())
        add("2-replace", type="move", sshare="xsites$", spath=x["path"], size=x["size"], dshare="xtosort$", dfolder="_dupes", dname=name, note="replaced by better quality " + x["replaced_by"])
    # 3) dupes, 4) moves, 5) relocations
    for r in rows:
        if r["action"] == "DUPE":
            ks = "xsites$" if "existing xsites" in r["notes"][0] else "xtosort$"
            add("3-dupe", type="move", sshare="xtosort$", spath=r["src"], size=r["size"], dshare="xtosort$", dfolder="_dupes", dname=r["dest_name"], keep_share=ks, keep_path=r["keep"])
    for r in rows:
        if r["action"] == "MOVE":
            add("4-move", type="move", sshare="xtosort$", spath=r["src"], size=r["size"], dshare="xsites$", dfolder=r["dest_folder"], dname=r["dest_name"])
    for r in rows:
        if r["action"] == "RELOCATE":
            add("5-relocate", type="move", sshare="xsites$", spath=r["src"], size=r["size"], dshare="xsites$", dfolder=r["dest_folder"], dname=r["dest_name"])
    # 6) funscripts follow their video
    for f in S["funscripts"]:
        if f["video"]:
            add("6-funscript", type="move", sshare="xtosort$", spath=f["src"], size=size_t[f["src"]], dshare=f["dest_share"], dfolder=f["dest_folder"], dname=f["dest_name"], video=f["video"])
    # 7) delete non-video files, 8) remove emptied folders (deepest first)
    for d in S["deletes"]:
        add("7-delete", type="delete", share="xtosort$", path=d["path"], size=d["size"])
    for r in rows:  # sample clips of movie rips are always deleted (a row skipped in the review screen is already filtered out above)
        if r["action"] == "DELETE_SAMPLE":
            add("7-delete", type="delete", share="xtosort$", path=r["src"], size=r["size"])
    srcs = [o["spath"] for o in ops if o["type"] == "move" and o["sshare"] == "xtosort$"] + [o["path"] for o in ops if o["type"] == "delete"]
    dirset = set()
    for sp in srcs:
        parts = sp.split("\\")[:-1]
        for k in range(1, len(parts) + 1):
            dirset.add("\\".join(parts[:k]))
    dirs = sorted((d for d in dirset if d.lower() != "_dupes"), key=lambda p: (-p.count("\\"), p))
    for d in dirs:
        add("8-rmdir", type="rmdir", share="xtosort$", path=d)
    return ops, planned, rows, S


def pick_pilot(ops, rows, S):
    """~20 varied, low-risk operations: no upgrades/samples/archives."""
    rnd = random.Random(42)
    by_phase = collections.defaultdict(list)
    for o in ops:
        by_phase[o["phase"]].append(o)
    rowby = {r["src"]: r for r in rows}
    moves = by_phase["4-move"]
    pick = []
    # moves that live in an obfuscated folder next to a .jpg (tests move -> delete jpg -> rmdir)
    deletes = {d["path"] for d in S["deletes"] if d["path"].lower().endswith(".jpg")}
    lifecycle = [m for m in moves if "\\" in m["spath"] and (m["spath"] + ".jpg") in deletes]
    pick += rnd.sample(lifecycle, 2)
    kinds = {"fp": lambda r: r["conf"] == "high", "analvids": lambda r: "analvids.com" in r["method"], "filename": lambda r: r["conf"] == "filename", "text": lambda r: r["conf"] == "medium" and "analvids" not in r["method"], "tosort": lambda r: r["dest_folder"] == "_To Sort"}
    for k, n in (("fp", 4), ("analvids", 1), ("filename", 1), ("text", 1), ("tosort", 1)):
        cand = [m for m in moves if kinds[k](rowby[m["spath"]]) and m not in pick]
        pick += rnd.sample(cand, n)
    newf = [m for m in moves if any(o["type"] == "mkdir" and o["rel"] == m["dfolder"] and o["share"] == m["dshare"] for o in by_phase["1-mkdir"]) and m["dfolder"] not in ("_dupes",) and m not in pick]
    pick += rnd.sample(newf, 1)
    longest = max(moves, key=lambda m: len(m["spath"]))
    if longest not in pick:
        pick.append(longest)
    pick += rnd.sample(by_phase["3-dupe"], 3) + rnd.sample(by_phase["5-relocate"], 3)
    # a funscript whose video is a MOVE (pick that video too)
    for f in by_phase["6-funscript"]:
        v = next((m for m in moves if m["spath"] == f["video"]), None)
        if v:
            if v not in pick:
                pick.append(v)
            pick.append(f)
            break
    ids = {o["id"] for o in pick}
    # delete the jpgs of the lifecycle videos, then remove their (hopefully empty) folders
    for m in pick[:2]:
        d = next(o for o in by_phase["7-delete"] if o["path"] == m["spath"] + ".jpg")
        ids.add(d["id"])
        ids.add(next(o["id"] for o in by_phase["8-rmdir"] if o["path"] == m["spath"].rsplit("\\", 1)[0]))
    needed = {(o["dshare"], o["dfolder"]) for o in ops if o["id"] in ids and o["type"] == "move"}
    for o in by_phase["1-mkdir"]:
        if (o["share"], o["rel"]) in needed:
            ids.add(o["id"])
    return sorted(ids)


def client():
    return Client(HOST, username=os.environ["SMB_USER"], password=os.environ["SMB_PASS"], ssl=False, port=5985, auth="ntlm", cert_validation=False)


def run_batch(c, mode, ops, planned):
    payload = base64.b64encode(json.dumps({"ops": ops, "planned": planned}).encode()).decode()
    out, streams, had = c.execute_ps(f"$mode='{mode}'\n$b64='{payload}'\n{PS}")
    if streams.error:
        raise RuntimeError(str(streams.error[0])[:300])
    return json.loads(out)


def main():
    mode = sys.argv[1]
    ops, planned, rows, S = build_ops()
    json.dump(ops, open(f"{MAN}/ops.json", "w"))
    log_path = f"{MAN}/exec_log.jsonl"
    done = set()
    if os.path.exists(log_path):
        for line in open(log_path):
            j = json.loads(line)
            if j["mode"] == "exec" and j["ok"]:
                done.add(j["id"])
    # follow files already moved by earlier runs (e.g. a "kept" copy relocated during the pilot)
    moved_map = {}
    if os.path.exists(log_path):
        for line in open(log_path):
            j = json.loads(line)
            if j["mode"] == "exec" and j["ok"] and j["type"] == "move":
                moved_map[(j["sshare"], j["spath"])] = (j["dshare"], j["dfolder"] + "\\" + j["dname"])
    for o in ops:
        k = (o.get("keep_share"), o.get("keep_path"))
        if k in moved_map:
            o["keep_share"], o["keep_path"] = moved_map[k]
    todo = ops
    if mode == "pilot":
        pp = f"{MAN}/pilot.json"
        if not os.path.exists(pp):
            json.dump(pick_pilot(ops, rows, S), open(pp, "w"))
        ids = set(json.load(open(pp)))
        todo = [o for o in ops if o["id"] in ids]
    elif mode == "full":
        todo = [o for o in ops if o["id"] not in done]
    c = client()
    real = "dry" if mode == "dry" else "exec"
    fails, counts, t0 = [], collections.Counter(), time.time()
    lf = open(log_path, "a")
    for i in range(0, len(todo), BATCH):
        batch = todo[i : i + BATCH]
        if real == "exec":  # ordering guard: never run a batch containing ops whose phase precedes an unfinished earlier phase
            pass
        res = run_batch(c, real, batch, planned)
        for op, r in zip(batch, res):
            rec = dict(op, mode=real, ok=r["ok"], msg=r["msg"], ms=r["ms"], t=time.time(), pilot=(mode == "pilot"))
            lf.write(json.dumps(rec) + "\n")
            counts[(op["phase"], r["ok"], r["msg"].split(":")[0][:30])] += 1
            if not r["ok"] and not r["msg"].startswith("skip"):
                fails.append((op, r["msg"]))
        lf.flush()
        print(f"  {min(i + BATCH, len(todo))}/{len(todo)}  {time.time() - t0:.0f}s", flush=True)
        if real == "exec" and fails:
            print("STOPPING: failure in execution", fails[:3])
            break
    print(f"\n{mode}: {len(todo)} ops, {len(fails)} failures, {time.time() - t0:.0f}s")
    for k, v in sorted(counts.items(), key=lambda kv: kv[0][0]):
        print("  ", k, v)
    for op, m in fails[:25]:
        print("FAIL", op["id"], op["phase"], (op.get("spath") or op.get("path") or op.get("rel"))[-70:], "->", m)


if __name__ == "__main__":
    main()
