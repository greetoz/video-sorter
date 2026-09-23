"""Applies the automatic-run policy to a plan: which rows are confident enough to run unattended, which stay in the source share for review.
Reads $MAN_DIR/manifest.json and the policy from $SCHEDULE_FILE, writes $MAN_DIR/exclude.json (what to leave alone) and auto_summary.json.
Nothing is moved here; execute.py honours exclude.json."""
import collections
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from config import MOVIES_DIR, REVIEW_DIR

ROOT = os.path.join(os.path.dirname(__file__), "..")

DEFAULT_POLICY = {
    "text_matches": False,  # also move files identified only by performer/title/date matching (not by their content fingerprint)
    "exact_dupes": True,    # a byte-identical copy of a file already in the library goes to _dupes (never deleted)
    "samples": True,        # sample clips of movie rips are deleted
    "junk": True,           # leftover images/nfo/txt files are deleted, once the videos in their folder are all handled
    "archives": False,      # .rar/.zip files are deleted (off: they may still contain videos)
    "unidentified": False,  # unidentified files are moved to review_dir (off: they stay in the source share for you)
    "limit": 200,           # safety valve: if a run would handle more files than this, it does nothing and asks for a look
}
BAD_NOTES = ("DATE MISMATCH", "UNVERIFIED", "PATH TOO LONG", "name collision", "fuzzy match")
JUNK_EXT = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".nfo", ".txt", ".url", ".sfv", ".md5", ".sha1", ".htm", ".html", ".db", ".ini", ".ds_store", ".torrent", ".website"}


def merged(policy):
    return dict(DEFAULT_POLICY, **{k: v for k, v in (policy or {}).items() if k in DEFAULT_POLICY})


def decide_rows(rows, replace_existing, policy):
    """-> {src: (auto: bool, why: str)}"""
    replacing = {x["replaced_by"] for x in replace_existing}
    out = {}
    for r in rows:
        a, notes = r["action"], " | ".join(r.get("notes") or [])
        bad = next((b for b in BAD_NOTES if b.lower() in notes.lower()), None)
        if a == "DELETE_SAMPLE":
            ok, why = policy["samples"], "sample clip of a movie rip" if policy["samples"] else "sample clips are set to review"
        elif a == "DUPE":
            exact = "identical" in notes.lower()
            ok = policy["exact_dupes"] and exact
            why = "identical copy of a file you already have" if ok else "same scene as another file, but not proven identical" if not exact else "duplicates are set to review"
        elif a == "RELOCATE":
            ok, why = bad is None, "fingerprint match, moves within the library" if bad is None else f"held: {bad}"
        elif a == "MOVE":
            folder, conf = r["dest_folder"], r["conf"]
            if r["src"] in replacing:
                ok, why = False, "would replace a file already in the library"
            elif folder == MOVIES_DIR:
                ok, why = False, "a movie scene"
            elif folder == REVIEW_DIR:
                ok, why = policy["unidentified"], "unidentified" + ("" if policy["unidentified"] else " - not identified by content, stays for review")
            elif conf == "high":
                ok, why = True, "matched by content fingerprint"
            elif conf == "medium" and policy["text_matches"]:
                ok, why = True, "matched by performer/title/date"
            else:
                ok, why = False, "identified by " + ("name/text only" if conf in ("medium", "filename") else "nothing")
            if ok and bad:
                ok, why = False, f"held: {bad}"
        else:
            ok, why = False, f"{a}: needs a decision"
        out[r["src"]] = (ok, why)
    return out


def decide_deletes(deletes, decisions, rows, policy):
    """Non-video leftovers: only whitelisted junk, and only when every video in the same folder is handled."""
    held = {r["src"].split("\\")[0] for r in rows if "\\" in r["src"] and not decisions[r["src"]][0]}
    out = {}
    for d in deletes:
        p = d["path"]
        top = p.split("\\")[0] if "\\" in p else None
        ext = os.path.splitext(p)[1].lower()
        if top in held:
            out[p] = (False, "videos in this folder are held back")
        elif d.get("archive"):
            out[p] = (policy["archives"], "archive" if policy["archives"] else "archives are set to review (they may contain videos)")
        elif ext in JUNK_EXT:
            out[p] = (policy["junk"], "leftover junk file" if policy["junk"] else "junk files are set to review")
        else:
            out[p] = (False, f"unknown file type {ext or '(none)'}")
    return out


def main():
    man = f"{ROOT}/{os.environ['MAN_DIR']}"
    sched = os.environ.get("SCHEDULE_FILE", "/data/schedule.json")
    policy = merged((json.load(open(sched)) if os.path.exists(sched) else {}).get("policy"))
    M = json.load(open(f"{man}/manifest.json"))
    rows, S = M["rows"], M["summary"]
    dec = decide_rows(rows, S.get("replace_existing", []), policy)
    handled = sum(1 for r in rows if dec[r["src"]][0] and r["action"] != "DELETE_SAMPLE")
    held = handled > policy["limit"]
    if held:  # something looks off (or the inbox is unusually big): do nothing tonight
        dec = {k: (False, f"held: {handled} files would be handled, more than the safety limit of {policy['limit']}") for k in dec}
    dels = decide_deletes(S.get("deletes", []), dec, rows, policy) if not held else {d["path"]: (False, "held by the safety limit") for d in S.get("deletes", [])}
    exclude = [k for k, (ok, _) in dec.items() if not ok] + [k for k, (ok, _) in dels.items() if not ok]
    json.dump(sorted(exclude), open(f"{man}/exclude.json", "w"))
    counts = collections.Counter((r["action"], "auto" if dec[r["src"]][0] else "review") for r in rows)
    summary = {
        "policy": policy, "held": held, "limit": policy["limit"],
        "rows": [{"src": r["src"], "action": r["action"], "decision": "auto" if dec[r["src"]][0] else "review", "why": dec[r["src"]][1]} for r in rows],
        "deletes": [{"path": p, "decision": "auto" if ok else "review", "why": why} for p, (ok, why) in dels.items()],
        "counts": {f"{a}:{d}": n for (a, d), n in counts.items()},
    }
    json.dump(summary, open(f"{man}/auto_summary.json", "w"))
    auto = sum(1 for v in dec.values() if v[0]); rev = len(dec) - auto
    print(f"automatic: {auto} files, left for review: {rev}, junk to delete: {sum(1 for v in dels.values() if v[0])}, junk kept: {sum(1 for v in dels.values() if not v[0])}"
          + (f" | HELD by the safety limit ({handled} > {policy['limit']})" if held else ""))


if __name__ == "__main__":
    main()
