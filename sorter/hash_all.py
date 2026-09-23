"""Inventory a share (full depth) and oshash every video. Resumable. Usage: hash_all.py <share> [threads]"""
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(__file__))
import smbio

CACHE = os.path.join(os.path.dirname(__file__), "..", "cache")
share = sys.argv[1]
threads = int(sys.argv[2]) if len(sys.argv) > 2 else 8
tag = share.strip("$")

smbio.connect()
t0 = time.time()
inv = smbio.walk(share)
json.dump(inv, open(f"{CACHE}/inv_{tag}.json", "w"))
vids = [e for e in inv if not e.get("dir") and "error" not in e and smbio.is_video(e["path"])]
print(f"{share}: {len(inv)} entries, {len(vids)} videos, walk {time.time()-t0:.0f}s", flush=True)

hpath = f"{CACHE}/hash_{tag}.json"
hashes = json.load(open(hpath)) if os.path.exists(hpath) else {}
todo = [e for e in vids if e["path"] not in hashes or hashes[e["path"]][0] != e["size"]]
print(f"{len(todo)} to hash", flush=True)


def work(e):
    try:
        return e["path"], [e["size"], smbio.oshash(share, e["path"], e["size"])]
    except Exception as ex:
        return e["path"], [e["size"], None, str(ex)[:80]]


done = 0
with ThreadPoolExecutor(threads) as ex:
    for p, v in ex.map(work, todo):
        hashes[p] = v
        done += 1
        if done % 500 == 0:
            json.dump(hashes, open(hpath, "w"))
            print(f"  {done}/{len(todo)}  {time.time()-t0:.0f}s", flush=True)
json.dump(hashes, open(hpath, "w"))
bad = [p for p, v in hashes.items() if v[1] is None]
print(f"done {share}: {len(hashes)} hashed, {len(bad)} failed/small, {time.time()-t0:.0f}s", flush=True)
