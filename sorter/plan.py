"""Dry-run planner: identify every video in the source share, decide destination + new name, resolve duplicates.
Read-only. Writes manifest/manifest.json + manifest.csv. Usage: plan.py"""
import collections
import csv
import difflib
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(__file__))
import analvids
import probe
import smbio
from config import ACTRESS_DIRS, DST_SHARE, DST_TAG, DUPES_DIR, MOVIES_DIR, ORGANIZE_BY_STUDIO, REVIEW_DIR, SRC_SHARE, SRC_TAG
from names import MAX_NAME, append_name, build_name, norm, sanitize
from parse import camel_split, parse
from stash import Stash

ROOT = os.path.join(os.path.dirname(__file__), "..")
C = f"{ROOT}/cache"
OUT = f"{ROOT}/{os.environ.get('MAN_DIR', 'manifest')}"
os.makedirs(OUT, exist_ok=True)
SRC, DST = SRC_SHARE, DST_SHARE
DUPES, TOSORT, MOVIES = DUPES_DIR, REVIEW_DIR, MOVIES_DIR
# top-level src_share folders named after an actress -> her name (files inside get her name appended if not already credited)
# your own convention, if you use folders like that; set from Settings > Library, empty by default. The two special cases just
# below (an "Alexis Tae" megapack folder, a "Dredd" movie-title pattern) are this project's own reference examples of the same
# idea done in code instead of config - harmless for anyone else, since they only match a folder/file name that specific way.
STOP = {"and", "the", "xxx", "of", "in", "to", "with", "for", "her", "his", "a", "an", "on", "at", "is", "s"}
load = lambda n: json.load(open(f"{C}/{n}"))
inv_t, inv_s, h_t, h_s = load(f"inv_{SRC_TAG}.json"), load(f"inv_{DST_TAG}.json"), load(f"hash_{SRC_TAG}.json"), load(f"hash_{DST_TAG}.json")
smbio.connect()
st = Stash()

# ---------------------------------------------------------------- destination folders
vids_s = [e for e in inv_s if not e.get("dir") and "error" not in e and smbio.is_video(e["path"])]
folder_count = collections.Counter(e["path"].split("\\")[0] for e in vids_s if "\\" in e["path"])
all_folders = {e["path"] for e in inv_s if e.get("dir") and "\\" not in e["path"]}
fmap = {}
for f in sorted(all_folders, key=lambda f: -folder_count[f]):
    fmap.setdefault(norm(f), f)  # variant with most files wins


def folder_for(studio):
    """-> (folder, how) where how in exact|parent|fuzzy|NEW|flat"""
    if not ORGANIZE_BY_STUDIO:
        return "", "flat"  # everything lands directly in DST, no per-studio subfolders
    chain = [studio["name"]] + ([studio["parent"]["name"]] if studio.get("parent") else [])
    for depth, n in enumerate(chain):
        for v in (n, re.sub(r"\.(com|net|tv|xxx)$", "", n, flags=re.I), n.replace("&", "and"), re.sub(r"^the ", "", n, flags=re.I)):
            if norm(v) in fmap:
                return fmap[norm(v)], "exact" if depth == 0 else "parent"
    k = norm(studio["name"])
    if len(k) >= 8:
        m = difflib.get_close_matches(k, list(fmap), n=1, cutoff=0.93)
        if m:
            return fmap[m[0]], "fuzzy"
    return sanitize(studio["name"]), "NEW"


# ---------------------------------------------------------------- matching helpers
def toks(s):
    return [t for t in re.findall(r"[a-z0-9]+", norm_ascii(s)) if t not in STOP and len(t) > 1]


def norm_ascii(s):
    import unicodedata
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()


def bag(scene):
    return set(toks(scene["title"] + " " + " ".join(p["performer"]["name"] for p in scene["performers"]) + " " + ((scene["studio"] or {}).get("name") or "") + " " + (scene.get("code") or "")))


def score(query, scene):
    q = toks(query)
    return sum(1 for t in q if t in bag(scene)) / len(q) if q else 0.0


def perf_ids(rest):
    """Find StashDB performers named in free text (segments split on and/&/comma; first 2-3 tokens each)."""
    ids, seen = [], set()
    for seg in re.split(r"\s+(?:and|with)\s+|\s*[&,]\s*| / ", rest, flags=re.I):
        w = seg.split()
        for n in (2, 3):
            name = " ".join(w[:n])
            if len(w) < n or norm(name) in seen:
                continue
            seen.add(norm(name))
            for p in st.find_performers(name):
                if norm(p["name"]) == norm(name) or norm(name) in {norm(a) for a in (p.get("aliases") or [])}:
                    ids.append(p["id"])
    return list(dict.fromkeys(ids))


AV_FAMILY = {"analvids", "legalporno", "pornbox", "pornworld", "giorgiograndi"}


def in_av_family(scene):
    s = scene["studio"] or {}
    return norm(s.get("name")) in AV_FAMILY or norm((s.get("parent") or {}).get("name")) in AV_FAMILY


D = lambda d, m="EQUALS": {"value": d, "modifier": m}
INC = lambda ids: {"value": ids, "modifier": "INCLUDES"}


def text_match(info, actress, dur):
    """Fallback identification. Returns (scene, method, conf) or None."""
    k, cands = info["kind"], []
    rest = re.sub(r"\b(?:1080p|2160p|720p|4K)\b|\s-\s*$", " ", info.get("rest") or "", flags=re.I).strip(" -")
    rest = re.sub(r"\s+", " ", rest)
    if k in ("release", "analvids720") and info.get("date"):
        ids = perf_ids(rest)
        if ids:
            cands += st.query_scenes(f"pd:{info['date']}:{','.join(ids)}", {"date": D(info["date"]), "performers": INC(ids)}, 20)
        if not cands and rest:
            cands += st.query_scenes(f"dt:{info['date']}:{rest[:50]}", {"date": D(info["date"]), "text": rest[:50]}, 20)
        best = max(cands, key=lambda s: score(rest, s), default=None)
        if best and score(rest, best) >= 0.34:
            return best, "date+performer/title", "medium" if score(rest, best) >= 0.5 else "low"
    elif k == "release_yearonly":
        ids = perf_ids(rest)
        if ids:
            c = st.query_scenes(f"py:{info['year']}:{','.join(ids)}", {"performers": INC(ids), "date": D(f"{info['year']}-01-01", "GREATER_THAN")}, 40)
            cands = [s for s in c if (s["release_date"] or "").startswith(info["year"])]
            best = max(cands, key=lambda s: score(rest, s), default=None)
            if best and score(rest, best) >= 0.4:
                return best, "year+performer/title", "medium" if score(rest, best) >= 0.5 else "low"
    elif k == "megapack":
        ids = [i for n in info["performers"][:1] for i in perf_ids(n)]
        if ids and info.get("date"):
            cands = st.query_scenes(f"mp:{info['date']}:{ids[0]}", {"date": D(info["date"]), "performers": INC(ids)}, 20)
        elif ids and info.get("title"):
            cands = st.query_scenes(f"mpt:{ids[0]}:{info['title'][:40]}", {"performers": INC(ids), "title": info["title"][:40]}, 20)
        best = max(cands, key=lambda s: score(info.get("title") or "", s), default=None)
        if best and score(info.get("title") or "", best) >= 0.5:
            return best, "performer+date/title", "medium"
    elif k == "gamma":
        ids = [i for p in info["performers"] for i in perf_ids(p)]
        if ids:
            cands = st.query_scenes(f"g:{','.join(ids)}:{info['title'][:30]}", {"performers": INC(ids), "title": " ".join(toks(info["title"])[:3])}, 20)
        best = max(cands, key=lambda s: score(info["title"], s), default=None)
        if best and score(info["title"], best) >= 0.5:
            return best, "performer+title", "medium"
    elif k == "av4k":
        if info.get("code"):
            cands = st.query_scenes(f"code:{info['code']}", {"code": {"value": info["code"], "modifier": "EQUALS"}}, 10)
        if not cands and len(toks(rest)) >= 6:
            cands = st.query_scenes(f"avt:{rest[:60]}", {"title": " ".join(toks(rest)[:5])}, 10)
            cands = [s for s in cands if score(rest, s) >= 0.6]
        if not cands:
            ids = perf_ids(rest)
            if ids:
                cands = st.query_scenes(f"avp:{','.join(ids)}", {"performers": INC(ids), "date": D("2015-01-01", "GREATER_THAN")}, 40)
                cands = [s for s in cands if dur and s.get("duration") and abs(s["duration"] - dur) <= max(10, 0.01 * dur) and in_av_family(s)]
        if len(cands) == 1:
            return cands[0], "code/title/performer+duration", "medium"
        if cands and dur:
            cands = [s for s in cands if s.get("duration") and abs(s["duration"] - dur) <= max(10, 0.01 * dur)]
            if len(cands) == 1:
                return cands[0], "code/title/performer+duration", "medium"
        am = analvids.match_by_performers(rest, dur) if dur else None
        am = am or (analvids.match(rest, info.get("code"), dur) if dur else None)
        if am:
            sid = re.search(r"/watch/(\d+)/", am["url"])[1]
            return {"id": f"analvids:{sid}", "title": am["title"], "release_date": am["date"], "duration": am["duration"], "code": None, "studio": {"id": "analvids", "name": "AnalVids", "parent": None},
                    "performers": [{"as": None, "performer": {"id": n, "name": n}} for n in am["performers"]]}, f"analvids.com performer+exact duration (delta {am['delta']:.1f}s) {am['url']}", "medium"
    else:  # freeform (actress folders etc.)
        title = re.sub(r"\s+", " ", rest)
        if actress and title:
            ids = perf_ids(actress)
            if ids:
                cands = st.query_scenes(f"f:{ids[0]}:{title[:40]}", {"performers": INC(ids), "title": " ".join(toks(title)[:4])}, 20) if toks(title) else []
                cands = [s for s in cands if score(title, s) >= 0.6 and (not dur or not s.get("duration") or abs(s["duration"] - dur) <= max(30, 0.05 * dur))]
                if len(cands) == 1:
                    return cands[0], "actress+title+duration", "medium"
    return None


# ---------------------------------------------------------------- collect + identify videos from the source share
items = []
for e in inv_t:
    if e.get("dir") or "error" in e or not smbio.is_video(e["path"]):
        continue
    if e["path"].split("\\")[0].lower() == DUPES.lower():
        continue  # already sorted out as a duplicate; reviewed/deleted from the Duplicates page
    p = e["path"]
    parts = p.split("\\")
    it = {"src": p, "size": e["size"], "hash": (h_t.get(p) or [0, None])[1], "info": parse(p), "top": parts[0] if len(parts) > 1 else ""}
    it["actress"] = ACTRESS_DIRS.get(it["top"]) or ("Alexis Tae" if "MegaPACK" in it["top"] else None)
    items.append(it)


ONLY = os.environ.get("PLAN_ONLY")  # json list of source-share paths: restrict the plan to these files
only_tops = set()
if ONLY:
    only_set = set(json.load(open(ONLY)))
    items = [it for it in items if it["src"] in only_set]
    only_tops = {p_.split("\\")[0] for p_ in only_set}


def do_probe(it):
    r = probe.probe(SRC, it["src"], it["size"])
    it["height"], it["dur"] = r.get("height") or it["info"].get("res"), r.get("duration")


with ThreadPoolExecutor(2) as ex:
    list(ex.map(do_probe, items))
probe.save()
print(f"{len(items)} videos probed", flush=True)

for it in items:
    ids = st.fp.get(it["hash"]) or []
    cands = [st.scenes[i] for i in ids]
    site = it["info"].get("site")
    if cands:
        it["scene"] = next((s for s in cands if site and (s["studio"] and norm(s["studio"]["name"]) == norm(site))), cands[0])
        it["method"], it["conf"] = "fingerprint(oshash)", "high"
    else:
        r = text_match(it["info"], it["actress"], it["dur"])
        it["scene"], it["method"], it["conf"] = r if r else (None, "unmatched", "-")
    if it["scene"] and it["conf"] == "low":
        it["low_scene"], it["scene"], it["method"] = it["scene"], None, it["method"] + " (low confidence -> treated as unmatched)"
st.save()
print("probe failures:", sum(1 for it in items if it["dur"] is None), flush=True)
print("identified:", collections.Counter(("matched-" + i["conf"]) if i["scene"] else "unmatched" for i in items), flush=True)

# ---------------------------------------------------------------- duplicates vs the existing library + within the source share
existing = collections.defaultdict(list)  # scene id -> library files
for e in vids_s:
    h = (h_s.get(e["path"]) or [0, None])[1]
    for sid in st.fp.get(h) or []:
        existing[sid].append({"path": e["path"], "size": e["size"], "hash": h})
existing_by_hash = collections.defaultdict(list)
for e in vids_s:
    h = (h_s.get(e["path"]) or [0, None])[1]
    if h:
        existing_by_hash[h].append(e["path"])

by_scene = collections.defaultdict(list)
for it in items:
    if it["scene"]:
        by_scene[it["scene"]["id"]].append(it)


def q_of(height, size):
    return (height or 0, size)


unverified = set()  # files whose duplicate status could not be verified (probe failed)
actions = {}  # src path -> dict(action, reason, ...)
replace_existing = []
part_no = {}
for sid, xs in by_scene.items():
    ex = existing.get(sid, [])
    members = [("X", it) for it in xs] + [("E", e) for e in ex]
    if len(members) < 2:
        continue
    for kind, m in members:  # probe existing members lazily
        if kind == "E" and "dur" not in m:
            r = probe.probe(DST, m["path"], m["size"])
            m["height"], m["dur"] = r.get("height"), r.get("duration")
    parent = list(range(len(members)))
    find = lambda i: i if parent[i] == i else find(parent[i])
    for i in range(len(members)):
        for j in range(i + 1, len(members)):
            a, b = members[i][1], members[j][1]
            same = a.get("hash") and a.get("hash") == b.get("hash")
            if not same and a.get("dur") and b.get("dur"):
                same = abs(a["dur"] - b["dur"]) <= max(5, 0.05 * max(a["dur"], b["dur"]))
            elif not same:
                same = False  # cannot compare -> never assume same content (fail safe: no dupe action)
                unverified.add(a.get("src") or a.get("path")); unverified.add(b.get("src") or b.get("path"))
            if same:
                parent[find(i)] = find(j)
    clusters = collections.defaultdict(list)
    for i in range(len(members)):
        clusters[find(i)].append(members[i])
    for ci, cl in enumerate(sorted(clusters.values(), key=lambda c: min(m[1].get("dur") or 0 for m in c))):
        xs_c = [m for k, m in cl if k == "X"]
        if not xs_c:
            continue
        for m in xs_c:
            part_no[m["src"]] = ci + 1 if len(clusters) > 1 else 0
        if len(cl) < 2:
            continue
        win = max(cl, key=lambda km: (q_of(km[1].get("height"), km[1]["size"]), km[0] == "E", km[1].get("path") or km[1].get("src")))
        for k, m in cl:
            if m is win[1]:
                continue
            if k == "X":
                why = f"dupe of {'existing library file ' + win[1]['path'] if win[0] == 'E' else 'other new file ' + win[1]['src']} (same scene; kept higher/equal quality {win[1].get('height')}p)"
                actions[m["src"]] = {"action": "DUPE", "reason": why, "keep": win[1].get("path") or win[1]["src"], "keep_share": DST if win[0] == "E" else SRC}
            elif win[0] == "X":
                replace_existing.append({"path": m["path"], "height": m.get("height"), "size": m["size"], "replaced_by": win[1]["src"], "winner_height": win[1].get("height"), "winner_size": win[1]["size"]})

# identical-content duplicates for files with no scene id (exact oshash present elsewhere)
first_seen = {}
for it in sorted(items, key=lambda i: (0 if "DVDRip" in i["src"] else 1, i["src"])):
    if it["src"] in actions or not it["hash"]:
        continue
    if it["hash"] in existing_by_hash:
        actions[it["src"]] = {"action": "DUPE", "reason": "identical (oshash+size) to an existing library file", "keep": existing_by_hash[it["hash"]][0], "keep_share": DST}
    elif (it["hash"], it["size"]) in first_seen:
        actions[it["src"]] = {"action": "DUPE", "reason": "identical to another new file", "keep": first_seen[(it["hash"], it["size"])], "keep_share": SRC}
    else:
        first_seen[(it["hash"], it["size"])] = it["src"]

# ---------------------------------------------------------------- destinations + names
STEM_JUNK = re.compile(r"\s*HD Videos & Porn Photos\s*-?\s*Private Porn Sex Videos|Alexis Tae PART \d+of\d+ MegaPACK_", re.I)
taken = {(norm(f), e["path"].split("\\")[-1].lower()) for e in vids_s for f in [e["path"].split("\\")[0]]}
# names already in _dupes count as taken too (original names can repeat), so a move never runs into an existing file
taken |= {(norm(DUPES), e["path"].split("\\")[-1].lower()) for e in inv_t if not e.get("dir") and "error" not in e and e["path"].lower().startswith(DUPES.lower() + "\\")}
used = collections.Counter()
rows = []
new_folders = collections.Counter()
for it in sorted(items, key=lambda i: i["src"]):
    fn = it["src"].split("\\")[-1]
    ext = os.path.splitext(fn)[1].lower()
    info, sc = it["info"], it["scene"]
    row = {"src": it["src"], "size": it["size"], "height": it["height"], "method": it["method"], "conf": it["conf"], "notes": []}
    if it["src"] in actions:
        a = actions[it["src"]]
        # a duplicate keeps its original file name: that is what identifies it when you compare it with the kept copy later
        row.update(action="DUPE", dest_share=SRC, dest_folder=DUPES, dest_name=fn, notes=[a["reason"], "original file name kept"], keep=a["keep"], keep_share=a["keep_share"])
        if sc:
            row["scene"] = sc["id"]
    elif sc:
        studio = sc["studio"] or {"name": info.get("site") or "Unknown", "parent": None}
        performers = [p["as"] or p["performer"]["name"] for p in sc["performers"]]
        if it["actress"] and norm(it["actress"]) not in {norm(p) for p in performers}:
            performers.append(it["actress"])
            row["notes"].append(f"actress folder name '{it['actress']}' appended (not credited in StashDB)")
        folder, how = folder_for(studio)
        if how in ("parent", "fuzzy"):
            row["notes"].append(f"folder chosen via {how} match ({studio['name']} -> {folder})")
        if how == "NEW":
            new_folders[folder] += 1
            row["notes"].append("NEW FOLDER")
        pn = part_no.get(it["src"], 0)
        name = build_name(studio["name"], sc["release_date"], performers, sc["title"], ext, f" - Part {pn}" if pn else "")
        row.update(action="MOVE", dest_share=DST, dest_folder=folder, dest_name=name, scene=sc["id"], studio=studio["name"], date=sc["release_date"], title=sc["title"], performers=performers)
        if info.get("date") and sc["release_date"] and info["date"] != sc["release_date"] and it["conf"] == "high":
            row["notes"].append(f"DATE MISMATCH: filename {info['date']} vs StashDB {sc['release_date']}")
        if info.get("site") and norm(info["site"]) != norm(studio["name"]) and norm(info["site"]) not in norm(studio["name"]) and norm(studio["name"]) not in norm(info["site"]):
            row["notes"].append(f"filename site '{info['site']}' differs from StashDB studio '{studio['name']}' (StashDB used)")
    else:
        is_movie = "DVDRip" in it["src"] or "\\Movies\\" in it["src"] or info["kind"] == "gamma"
        dvd = re.match(r"^(?P<t>.+?)\.DiSC(?P<n>\d)\.", it["top"], re.I) if "DVDRip" in it["src"] else None
        if ".sample." in fn.lower():
            row.update(action="DELETE_SAMPLE", dest_share=SRC, dest_folder="", dest_name=fn, notes=["sample clip of a movie rip - deleted on execute (tick skip to keep it)"])
            rows.append(row)
            continue
        fb = None
        stem0 = os.path.splitext(fn)[0]
        md = re.match(r"^(?P<n>.+?) The Official Dredd XXX$", stem0)
        if md and norm("Dredd") in fmap:
            fb = (fmap[norm("Dredd")], build_name("Dredd", None, re.split(r"\s+and\s+|\s*,\s*", md["n"]), "", ext))
        elif info["kind"] in ("release", "release_yearonly") and info.get("site"):
            site_pretty = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", info["site"]).strip()
            f_ = fmap.get(norm(info["site"])) or (difflib.get_close_matches(norm(info["site"]), list(fmap), n=1, cutoff=0.93) or [None])[0]
            f_ = fmap.get(f_, f_) if f_ and f_ not in fmap.values() else f_
            if f_:
                fb = (f_, build_name(site_pretty, info.get("date"), [], re.sub(r"\b(1080p|2160p|720p)\b|\s-\s*$", "", info["rest"]).strip(), ext))
        elif it["actress"] and info["kind"] == "freeform":
            stem_site = re.sub(r"(\.com)?(_\d+)?$", "", stem0, flags=re.I)
            if norm(stem_site) in fmap:
                fb = (fmap[norm(stem_site)], append_name(stem0, it["actress"], ext))
        if fb and not is_movie:
            row.update(action="MOVE", dest_share=DST, dest_folder=fb[0], dest_name=fb[1], conf="filename", notes=row["notes"] + [f"identified from filename only (site folder '{fb[0]}'); no StashDB/site match"])
        elif info["kind"] == "gamma":
            base = f"{info['title']} - Scene {info['scene_no']} - {', '.join(info['performers'])}"
            name = append_name(base, it["actress"], ext) if it["actress"] else append_name(base, "", ext).replace(" - .", ".")
            dest = MOVIES
        elif info["kind"] == "megapack":
            name = build_name(info.get("site") or "", info.get("date"), info["performers"], info.get("title") or "", ext)
            dest = TOSORT
        elif info["kind"] == "release" and info.get("date"):
            name = build_name(re.sub(r"(?<=[a-z])(?=[A-Z])", " ", info["site"]), info["date"], [], re.sub(r"\b(1080p|2160p|720p)\b|\s-\s*$", "", info["rest"]).strip(), ext)
            dest = TOSORT
        elif dvd:
            name = sanitize(f"{dvd['t'].replace('.', ' ')} - Disc {dvd['n']}") + ext
            dest = MOVIES
        else:
            stem = STEM_JUNK.sub("", os.path.splitext(fn)[0])
            name = append_name(stem, it["actress"], ext) if it["actress"] and norm(it["actress"]) not in norm(stem) else sanitize(stem)[: MAX_NAME - len(ext)] + ext
            dest = MOVIES if is_movie else TOSORT
        if not (fb and not is_movie):
            row.update(action="MOVE", dest_share=DST, dest_folder=dest, dest_name=name, notes=row["notes"] + ["unidentified -> " + dest])
        if it.get("low_scene"):
            row["notes"].append(f"low-confidence candidate: {it['low_scene']['title']} ({(it['low_scene']['studio'] or {}).get('name')}, {it['low_scene']['release_date']})")
    # collision handling (never overwrite)
    key = (norm(row["dest_folder"]), row["dest_name"].lower())
    n = 1
    while key in taken or used[key]:
        n += 1
        stem_, ext_ = os.path.splitext(row["dest_name"])
        base = re.sub(r" \(\d+\)$", "", stem_)
        row["dest_name"] = f"{base} ({n}){ext_}"
        key = (norm(row["dest_folder"]), row["dest_name"].lower())
    if it["src"] in unverified:
        row["notes"].append("UNVERIFIED: could not compare duration with same-scene copy")
    if n > 1:
        row["notes"].append(f"name collision -> numbered ({n})")
    used[key] += 1
    if len(row["dest_folder"]) + len(row["dest_name"]) + 1 > 200:
        row["notes"].append("PATH TOO LONG")
    rows.append(row)

# ---------------------------------------------------------------- relocate existing MOVIES_DIR studio scenes (within the library share)
replaced_paths = {x["path"] for x in replace_existing}
ms_scene = collections.defaultdict(list)
for e in vids_s:
    if e["path"].startswith(MOVIES + "\\"):
        h = (h_s.get(e["path"]) or [0, None])[1]
        for sid in (st.fp.get(h) or [])[:1]:
            ms_scene[sid].append(e)
reloc_left = collections.Counter()
for sid, files in ([] if ONLY else ms_scene.items()):
    sc = st.scenes[sid]
    for e in files:
        ext = os.path.splitext(e["path"])[1].lower()
        row = {"src": e["path"], "src_share": DST, "size": e["size"], "height": None, "method": "fingerprint(oshash)", "conf": "high", "scene": sid, "notes": []}
        others = [x["path"] for x in existing.get(sid, []) if not x["path"].startswith(MOVIES + "\\") and x["path"] != e["path"]]
        if e["path"] in replaced_paths:
            continue  # handled as REPLACE_EXISTING
        if len(files) > 1 or others or not sc["studio"]:
            reloc_left["multiple/duplicate/no studio"] += 1
            continue
        folder, how = folder_for(sc["studio"])
        performers = [p["as"] or p["performer"]["name"] for p in sc["performers"]]
        name = build_name(sc["studio"]["name"], sc["release_date"], performers, sc["title"], ext)
        if how == "NEW":
            new_folders[folder] += 1
            row["notes"].append("NEW FOLDER")
        if how in ("parent", "fuzzy"):
            row["notes"].append(f"folder chosen via {how} match ({sc['studio']['name']} -> {folder})")
        key = (norm(folder), name.lower())
        n = 1
        while key in taken or used[key]:
            n += 1
            stem_, ext_ = os.path.splitext(name)
            name = f"{re.sub(r' \\(\\d+\\)$', '', stem_)} ({n}){ext_}"
            key = (norm(folder), name.lower())
        used[key] += 1
        row.update(action="RELOCATE", dest_share=DST, dest_folder=folder, dest_name=name, studio=sc["studio"]["name"], date=sc["release_date"], title=sc["title"], performers=performers)
        row["notes"].append(f"existing library file moved out of {MOVIES} (StashDB knows it as a studio scene); renamed to standard format")
        if n > 1:
            row["notes"].append(f"name collision -> numbered ({n})")
        rows.append(row)

# ---------------------------------------------------------------- companions + deletions
src_names = {r["src"]: r for r in rows}
funs, deletes, archives = [], [], []
for e in inv_t:
    if e.get("dir") or "error" in e or smbio.is_video(e["path"]):
        continue
    if ONLY and e["path"].split("\\")[0] not in only_tops:
        continue
    if e["path"].split("\\")[0].lower() == DUPES.lower():
        continue
    ext = os.path.splitext(e["path"])[1].lower()
    if ext == ".funscript":
        vid = next((r for r in rows if os.path.splitext(r["src"])[0].lower() == os.path.splitext(e["path"])[0].lower() and r["action"] in ("MOVE", "DUPE")), None)
        funs.append({"src": e["path"], "video": vid["src"] if vid else None, "dest_share": vid["dest_share"] if vid else None, "dest_folder": vid["dest_folder"] if vid else None, "dest_name": os.path.splitext(vid["dest_name"])[0] + ".funscript" if vid else None})
    elif ext in (".rar", ".zip"):
        archives.append(e["path"])
        deletes.append({"path": e["path"], "size": e["size"], "archive": True})
    else:
        deletes.append({"path": e["path"], "size": e["size"]})

summary = {
    "videos": sum(1 for r in rows if r["action"] != "RELOCATE"), "actions": collections.Counter(r["action"] for r in rows),
    "moves_by_dest": collections.Counter(r["dest_folder"] for r in rows if r["action"] == "MOVE").most_common(),
    "new_folders": new_folders.most_common(), "funscripts": funs, "deletes": deletes, "archives_to_inspect": archives,
    "replace_existing": replace_existing, "conf": collections.Counter(r["conf"] for r in rows), "reloc_left": reloc_left,
    "dirs_to_remove": sum(1 for e in inv_t if e.get("dir")),
}
json.dump({"rows": rows, "summary": summary}, open(f"{OUT}/manifest.json", "w"), indent=1, default=list)
with open(f"{OUT}/manifest.csv", "w", newline="", encoding="utf-8-sig") as f:
    w = csv.writer(f)
    w.writerow(["action", "conf", "method", "src share", "src path", "dest share", "dest folder", "dest name", "size MB", "height", "notes"])
    for r in rows:
        w.writerow([r["action"], r["conf"], r["method"], r.get("src_share", SRC), r["src"], r.get("dest_share"), r["dest_folder"], r["dest_name"], r["size"] // 10**6, r["height"], "; ".join(r["notes"])])
print(json.dumps({k: v for k, v in summary.items() if k in ("videos", "actions", "conf")}, default=dict))
print("new folders:", len(new_folders), "| funscripts:", len(funs), "| deletes:", len(deletes), "| archives:", len(archives), "| replace-existing:", len(replace_existing))
