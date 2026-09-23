"""analvids.com lookup: search cards -> scene page -> exact duration match. Read-only, cached, polite."""
import html
import json
import os
import re
import time
import urllib.parse

import requests

CACHE = os.path.join(os.path.dirname(__file__), "..", "cache", "analvids.json")
BASE = "https://www.analvids.com"
_c = json.load(open(CACHE)) if os.path.exists(CACHE) else {}
for _k in ("search", "scene", "models", "model_scenes"):
    _c.setdefault(_k, {})
_s = requests.Session()
_s.headers["User-Agent"] = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36"


def save():
    json.dump(_c, open(CACHE, "w"))


def _get(url):
    for i in range(3):
        try:
            r = _s.get(url, timeout=25)
            if r.status_code == 200:
                time.sleep(0.4)
                return r.text
        except requests.RequestException:
            pass
        time.sleep(2 * (i + 1))
    return ""


def search(q, page=1):
    key = f"{q}|{page}"
    if key not in _c["search"]:
        txt = _get(f"{BASE}/search/?q={urllib.parse.quote_plus(q)}" + (f"&page={page}" if page > 1 else ""))
        cards = []
        for chunk in txt.split("card-scene__view")[1:]:
            href = re.search(r'href="(https://www\.analvids\.com/watch/(\d+)/[^"]+)"', chunk)
            mins = re.search(r'label--time">(\d+) min', chunk)
            if href and mins:
                cards.append({"url": href[1], "id": href[2], "minutes": int(mins[1]), "is4k": bool(re.search(r'label--y[^>]*>\s*4k', chunk, re.I))})
        _c["search"][key] = cards
    return _c["search"][key]


def _secs(t):
    p = [int(x) for x in t.split(":")]
    return p[0] * 3600 + p[1] * 60 + p[2] if len(p) == 3 else p[0] * 60 + p[1]


def scene(url):
    if url not in _c["scene"]:
        raw = _get(url)
        body = re.sub(r"<script.*?</script>|<style.*?</style>|data:image[^\"']+", "", raw, flags=re.S)
        lines = [l.strip() for l in html.unescape(re.sub(r"<[^>]+>", "\n", body)).split("\n") if l.strip()]
        title = (re.findall(r"<title>(.*?)</title>", raw, re.S) or [""])[0].replace(" - AnalVids", "").strip()
        i = next((k for k, l in enumerate(lines) if l == "featuring"), 0)
        near = lines[i : i + 14] if i else lines
        dur = next((_secs(l) for l in near if re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?", l)), None) or next((_secs(l) for l in lines if re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?", l)), None)
        date = next((l for l in near if re.fullmatch(r"\d{4}-\d{2}-\d{2}", l)), None)
        perfs, seen = [], set()
        for mid, slug, name in re.findall(r'href="https://www\.analvids\.com/model/(\d+)/([a-z0-9_]+)"[^>]*>([^<]*)<', raw):
            if mid not in seen:
                seen.add(mid)
                perfs.append(html.unescape(name).strip() or slug.replace("_", " ").title())
        studio = (re.findall(r'href="https://www\.analvids\.com/studios/[^"]+"[^>]*>([^<]+)<', raw) or [None])[0]
        _c["scene"][url] = {"title": title, "duration": dur, "date": date, "performers": perfs, "studio": studio}
    return _c["scene"][url]


def match(rest, code, dur, tol=4):
    """Return {url,title,duration,date,performers,studio,delta} for the unique scene whose exact duration is within tol seconds."""
    if not dur:
        return None
    words = rest.split()
    queries = ([code] if code else []) + [" ".join(words[:6])] + ([" ".join(words[:2])] if len(words) > 2 else [])
    cands = {}
    for q in dict.fromkeys(queries):
        for page in (1, 2, 3):
            cards = search(q, page)
            for c in cards:
                cands[c["id"]] = c
            if not cards or any(abs(c["minutes"] - dur / 60) <= 1.6 for c in cards):
                break
    near = sorted((c for c in cands.values() if abs(c["minutes"] - dur / 60) <= 1.6), key=lambda c: (not c["is4k"], abs(c["minutes"] - dur / 60)))
    hits = []
    for c in near[:8]:
        sc = scene(c["url"])
        if sc["duration"] and abs(sc["duration"] - dur) <= tol:
            hits.append(dict(sc, url=c["url"], delta=abs(sc["duration"] - dur)))
    save()
    return _best(hits)


def crawl_models(max_pages=400):
    """Crawl /models/page/N once -> _c['models'] = {slug: id}."""
    if _c["models"].get("_done"):
        return
    for page in range(1, max_pages + 1):
        txt = _get(f"{BASE}/models/page/{page}/")
        found = re.findall(r"/model/(\d+)/([a-z0-9_]+)", txt)
        new = [(i, sl) for i, sl in found if sl not in _c["models"]]
        if not found or not new:
            break
        for i, sl in new:
            _c["models"][sl] = i
        if page % 10 == 0:
            save()
    _c["models"]["_done"] = True
    save()


def model_scenes(mid):
    if mid not in _c["model_scenes"]:
        txt = _get(f"{BASE}/model/{mid}/x")
        cards = []
        for chunk in txt.split("card-scene__view")[1:]:
            href = re.search(r'href="(https://www\.analvids\.com/watch/(\d+)/[^"]+)"', chunk)
            mins = re.search(r'label--time">(\d+) min', chunk)
            if href and mins:
                cards.append({"url": href[1], "id": href[2], "minutes": int(mins[1]), "is4k": bool(re.search(r'label--y[^>]*>\s*4k', chunk, re.I))})
        _c["model_scenes"][mid] = cards
    return _c["model_scenes"][mid]


def match_by_performers(rest, dur, tol=4):
    """rest = performer-ish words (e.g. 'Mia Piper Alice Wu'); find models by consecutive 2-3 word n-grams, then exact duration."""
    if not dur:
        return None
    crawl_models()
    w = re.sub(r"[^a-z0-9 ]", " ", rest.lower()).split()
    mids = []
    for n in (2, 3):
        for i in range(len(w) - n + 1):
            slug = "_".join(w[i : i + n])
            if slug in _c["models"]:
                mids.append(_c["models"][slug])
    mids = list(dict.fromkeys(mids))
    cands = {}
    for mid in mids:
        for c in model_scenes(mid):
            cands[c["id"]] = c
    near = sorted((c for c in cands.values() if abs(c["minutes"] - dur / 60) <= 1.6), key=lambda c: (not c["is4k"], abs(c["minutes"] - dur / 60)))
    hits = []
    for c in near[:10]:
        sc = scene(c["url"])
        if sc["duration"] and abs(sc["duration"] - dur) <= tol:
            hits.append(dict(sc, url=c["url"], delta=abs(sc["duration"] - dur)))
    save()
    return _best(hits)


def _best(hits):
    """Nearest duration wins if it is clearly closer than the runner-up (>=0.6 s) and within 3 s; unique hits within tolerance always win."""
    hits = sorted(hits, key=lambda h: h["delta"])
    if not hits:
        return None
    if len(hits) == 1 or (hits[0]["delta"] <= 3 and hits[1]["delta"] - hits[0]["delta"] >= 0.6):
        return hits[0]
    return None
