"""Read resolution/duration from a remote video's headers with PyAV (no full download). Successful probes are cached."""
import json
import os
import threading
import time

import av
import smbclient

import smbio

CACHE = os.path.join(os.path.dirname(__file__), "..", "cache", "probe.json")
_cache = json.load(open(CACHE)) if os.path.exists(CACHE) else {}
_gate = threading.Semaphore(2)  # PyAV holds many reads open; more than ~2 concurrent exhausts SMB credits


def save():
    json.dump(_cache, open(CACHE, "w"))


def _probe_once(share, rel):
    res = {"height": None, "width": None, "duration": None, "codec": None}
    try:
        with _gate:
            with smbclient.open_file(smbio.unc(share, rel), mode="rb", buffering=1 << 20) as f:
                with av.open(f, mode="r", options={"analyzeduration": "0", "probesize": "1000000"}) as c:
                    v = next((s for s in c.streams if s.type == "video"), None)
                    if v:
                        res.update(height=v.codec_context.height, width=v.codec_context.width, codec=v.codec_context.name)
                    if c.duration:
                        res["duration"] = round(c.duration / 1_000_000, 1)
    except Exception as ex:
        res["error"] = str(ex)[:80]
    return res


def probe(share, rel, size):
    key = f"{share}|{rel}|{size}"
    if key in _cache:
        return _cache[key]
    for attempt in range(4):
        res = _probe_once(share, rel)
        if not res.get("error"):
            _cache[key] = res
            return res
        time.sleep(1 + attempt)
    return res  # failure is returned but never cached
