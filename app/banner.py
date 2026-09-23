"""Header banner: landscape pictures of a list of performers, fetched once from StashDB, shrunk, and cached in the data volume (so the browser
only ever loads them from this app). Default source: pictures of scenes you actually own (they are almost always landscape); portraits are the
alternative. Also counts how many videos of each performer the library holds."""
import io
import os
import random
import re
import threading
import time

import requests

import config as libcfg
from app import state

DIR = f"{state.DATA}/banner"
CONF = f"{DIR}/config.json"
INDEX = f"{DIR}/index.json"
BASE = "https://stashdb.org"
PER_PERFORMER = 4
MAX_TRIES = 28          # pictures examined per performer before settling for what was found
MIN_FACE = 0.07         # the largest face must be at least this fraction of the picture's height to count as "something useful to show"
YUNET = "/app/yunet.onnx"  # face detector model baked into the image (OpenCV's Haar cascade is the fallback)
DEFAULT_NAMES = ["Jynx Maze", "Tori Black", "Adriana Chechik", "Kristy Black", "Aidra Fox", "Monique Alexander", "Cindy Shine", "Lana Rhoades",
                 "Amirah Adara", "Megan Rain", "Abella Danger"]
VIDEO_EXT = (".mp4", ".mkv", ".avi", ".wmv", ".mov", ".m4v", ".ts", ".flv", ".mpg", ".mpeg", ".webm", ".vid")
_status = {"running": False, "done": 0, "total": 0, "errors": [], "checked": 0, "kept": 0}
_lock = threading.Lock()
_counts = {"key": None, "val": {}}
norm = lambda s: re.sub(r"[^a-z0-9]", "", (s or "").lower())
slug = lambda s: norm(s) or "x"


def config():
    c = state.read_json(CONF, {})
    return {"enabled": bool(c.get("enabled", True)), "names": c.get("names") or list(DEFAULT_NAMES), "seconds": int(c.get("seconds", 8)),
            "source": c.get("source") if c.get("source") in ("scenes", "portraits") else "scenes"}


def save_config(body):
    names = [n.strip() for n in body.get("names", []) if n.strip()][:40]
    if not names:
        raise ValueError("give at least one name")
    secs = int(body.get("seconds", 8))
    if not 3 <= secs <= 120:
        raise ValueError("seconds must be between 3 and 120")
    if body.get("source", "scenes") not in ("scenes", "portraits"):
        raise ValueError("source must be scenes or portraits")
    os.makedirs(DIR, exist_ok=True)
    state.write_json(CONF, {"enabled": bool(body.get("enabled")), "names": names, "seconds": secs, "source": body.get("source", "scenes")})


def _find(gql, name):
    q = """query($n:String!){ queryPerformers(input:{name:$n, per_page:10, sort:NAME, direction:ASC}){ performers{ id name disambiguation aliases images{ url width height } } } }"""
    ps = gql(q, {"n": name})["data"]["queryPerformers"]["performers"]
    want = norm(name)
    exact = [p for p in ps if norm(p["name"]) == want]
    alias = [p for p in ps if want in {norm(a) for a in (p.get("aliases") or [])}]
    return (exact or alias or ps or [None])[0]


def _shrink(data, max_w, max_h):
    from PIL import Image
    im = Image.open(io.BytesIO(data))
    im.thumbnail((max_w, max_h))
    out = io.BytesIO()
    im.convert("RGB").save(out, "JPEG", quality=82, optimize=True)
    return out.getvalue(), im.size


_det = None


def _detector():
    global _det
    if _det is None:
        import cv2
        if os.path.exists(YUNET) and os.path.getsize(YUNET) > 100_000:
            _det = ("yunet", cv2.FaceDetectorYN.create(YUNET, "", (320, 320), 0.8, 0.3, 200))
        else:
            _det = ("haar", cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_alt2.xml"))
    return _det


def find_face(jpeg):
    """The largest clearly visible face as (centre x, centre y, height) in fractions of the picture, or None. This is how pictures are vetted:
    a banner picture must show a face, and the face position decides how the crop is placed."""
    import cv2
    import numpy as np
    img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return None
    h, w = img.shape[:2]
    kind, det = _detector()
    if kind == "yunet":
        det.setInputSize((w, h))
        _, res = det.detect(img)
        boxes = [(r[0], r[1], r[2], r[3]) for r in (res if res is not None else [])]
    else:
        boxes = det.detectMultiScale(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), 1.1, 6, minSize=(max(24, w // 30),) * 2)
    boxes = [b for b in boxes if b[3] / h >= MIN_FACE]
    if not boxes:
        return None
    x, y, bw, bh = max(boxes, key=lambda b: b[3])
    return round(float((x + bw / 2) / w), 3), round(float((y + bh / 2) / h), 3), round(float(bh / h), 3)


def fits(face, w, h, W=1066, H=280):
    """Would this picture look good in the banner? The same geometry the page uses: the crop is a wide band centred on the face; the face must
    be fully inside it (not too large), and must not collide with the title, the tab buttons, or the performer's name (which goes to the
    side without the face)."""
    fx, fy, fh = face
    sc = max(W / w, H / h)
    sw, sh = w * sc, h * sc
    fpx = fh * sh
    fw = fpx * 0.8
    if fpx > 0.72 * H:
        return False
    cy = min(0, max(H - sh, H * 0.56 - fy * sh)) + fy * sh
    cx = fx * W if sw <= W + 1 else min(0, max(W - sw, W * (0.7 if fx < 0.5 else 0.3) - fx * sw)) + fx * sw
    box = (cx - fw / 2, cy - fpx / 2, cx + fw / 2, cy + fpx / 2)
    if box[1] < 0.04 * H or box[3] > 0.97 * H or box[0] < 0 or box[2] > W:
        return False
    hit = lambda z: box[0] < z[2] and box[2] > z[0] and box[1] < z[3] and box[3] > z[1]
    if hit((0, 0, 200, 72)) or hit((W - 350, 0, W, 74)):  # title, tab buttons
        return False
    return not (hit((0, H - 120, 400, H)) and hit((W - 400, H - 120, W, H)))  # the name can go left or right: one of the two sides must be free


def _pick(images, landscape):
    """The most suitable picture: landscape ones near 1600 px wide (big enough, not huge), or portraits."""
    ok = [i for i in images if (i["width"] >= 1.3 * i["height"]) == landscape]
    return min(ok, key=lambda i: abs(i["width"] - 1600)) if ok else None


def refresh(force=False):
    """Fetch what is missing (or everything with force) in the background."""
    with _lock:
        if _status["running"]:
            return False
        _status.update(running=True, done=0, total=0, errors=[], checked=0, kept=0)
    threading.Thread(target=_refresh, args=(force,), daemon=True).start()
    return True


def _candidates(gql, pid, source, scenes, portraits):
    """Pictures to examine, best sources first: scenes in the library (shuffled), then the newest scenes on StashDB; or portraits."""
    if source == "portraits":
        for img in sorted(portraits, key=lambda i: -(i["width"] * i["height"]))[:MAX_TRIES]:
            yield img, None
        return
    mine = [sc["id"] for sc in scenes.values() if any(pp["performer"]["id"] == pid for pp in sc["performers"])]
    random.shuffle(mine)
    seen = set()
    for sid in mine[:MAX_TRIES]:
        imgs = (gql("query($id:ID!){ findScene(id:$id){ images{ url width height } } }", {"id": sid})["data"]["findScene"] or {}).get("images") or []
        p = _pick(imgs, True)
        if p:
            seen.add(sid)
            yield p, sid
    d = gql("""query($id:ID!){ queryScenes(input:{performers:{value:[$id], modifier:INCLUDES}, sort:DATE, direction:DESC, per_page:20}){ scenes{ id images{ url width height } } } }""", {"id": pid})
    for sc in d["data"]["queryScenes"]["scenes"]:
        p = _pick(sc["images"], True)
        if p and sc["id"] not in seen:
            yield p, sc["id"]


def _refresh(force):
    try:
        cfg = config()
        names, source = cfg["names"], cfg["source"]
        os.makedirs(DIR, exist_ok=True)
        old = {e["slug"]: e for e in state.read_json(INDEX, [])}
        _status["total"] = len(names)
        s = requests.Session()
        s.post(f"{BASE}/login", data={"username": os.environ["STASH_USER"], "password": os.environ["STASH_PASS"]}, timeout=30).raise_for_status()

        def gql(q, v):
            r = s.post(f"{BASE}/graphql", json={"query": q, "variables": v}, timeout=60)
            r.raise_for_status()
            return r.json()

        scenes = state.read_json(f"{state.CACHE}/stash_scenes.json", {}) if source == "scenes" else {}
        index = []
        for n in names:
            e = old.get(slug(n))
            if e and e.get("files") and e.get("source") == source and all("face" in f and os.path.exists(f"{DIR}/{f['file']}") for f in e["files"]) and not force:
                index.append(dict(e, name=n))
            else:
                try:
                    p = _find(gql, n)
                    if not p:
                        raise ValueError("not found on StashDB")
                    size = (1200, 700) if source == "scenes" else (900, 1400)
                    kept, spare, tried = [], [], 0
                    for img, sid in _candidates(gql, p["id"], source, scenes, p["images"]):
                        if len(kept) >= PER_PERFORMER or tried >= MAX_TRIES:
                            break
                        tried += 1
                        data, (w, h) = _shrink(s.get(img["url"], timeout=90).content, *size)
                        face = find_face(data)
                        if face and not fits(face, w, h):
                            face = None  # a face is there, but this picture would not look right in the banner
                        _status["checked"] += 1
                        (kept if face else spare).append((data, w, h, sid, face))
                    if len(kept) < 2:  # too few pictures with a visible face: settle for a couple of the others rather than nothing
                        kept += [(d, w, h, sid, None) for d, w, h, sid, _ in spare[:2 - len(kept)]]
                    if not kept:
                        raise ValueError("no usable pictures found")
                    files = []
                    for k, (data, w, h, sid, face) in enumerate(kept):
                        fn = f"{slug(n)}-{k}.jpg"
                        open(f"{DIR}/{fn}", "wb").write(data)
                        files.append({"file": fn, "w": w, "h": h, "scene": sid, "face": face})
                        _status["kept"] += 1 if face else 0
                    index.append({"name": n, "slug": slug(n), "stash_id": p["id"], "source": source, "files": files})
                except Exception as ex:
                    _status["errors"].append(f"{n}: {str(ex)[:80]}")
                    index.append({"name": n, "slug": slug(n), "files": []})
            _status["done"] += 1
            state.write_json(INDEX, index)
    except Exception as ex:
        _status["errors"].append(str(ex)[:120])
    finally:
        _status["running"] = False


def _video_counts(names):
    p = f"{state.CACHE}/inv_{libcfg.tag(libcfg.get()['dst_share'])}.json"
    if not os.path.exists(p):
        return {}
    key = (os.path.getmtime(p), tuple(names))
    if _counts["key"] != key:
        paths = [norm(e["path"]) for e in state.read_json(p, []) if not e.get("dir") and "error" not in e and e["path"].lower().endswith(VIDEO_EXT)]
        _counts.update(key=key, val={n: sum(1 for x in paths if norm(n) in x) for n in names})
    return _counts["val"]


def overview():
    c = config()
    idx = {e["slug"]: e for e in state.read_json(INDEX, [])}
    counts = _video_counts(c["names"])
    items = []
    for n in c["names"]:
        files = [f for f in idx.get(slug(n), {}).get("files", []) if os.path.exists(f"{DIR}/{f['file']}")]
        items.append({"name": n, "imgs": [{"u": f"/api/banner/img/{f['file']}?v={int(os.path.getmtime(f'{DIR}/{f['file']}'))}", "face": f.get("face")} for f in files], "videos": counts.get(n)})
    return {"config": c, "items": items, "status": dict(_status)}


def image_path(filename):
    if not re.fullmatch(r"[a-z0-9]+-\d+\.jpg", filename):
        return None
    p = f"{DIR}/{filename}"
    return p if os.path.exists(p) else None


def start_up():
    """First start: fetch the pictures in the background (needs the StashDB login)."""
    def go():
        time.sleep(8)
        if config()["enabled"] and not state.read_json(INDEX, []) and os.environ.get("STASH_USER") and os.environ.get("STASH_PASS"):
            refresh()
    threading.Thread(target=go, daemon=True).start()
