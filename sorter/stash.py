"""Minimal StashDB GraphQL client (session-cookie login) with on-disk caching."""
import json
import os
import time

import requests

BASE = "https://stashdb.org"
CACHE = os.path.join(os.path.dirname(__file__), "..", "cache")
SCENE_FIELDS = """id title code release_date duration
  studio { id name parent { id name } }
  performers { as performer { id name disambiguation } }"""


class Stash:
    def __init__(self):
        self.s = requests.Session()
        r = self.s.post(f"{BASE}/login", data={"username": os.environ["STASH_USER"], "password": os.environ["STASH_PASS"]}, timeout=30)
        r.raise_for_status()
        me = self.gql("{ me { name } }")["me"]
        assert me and me["name"], "StashDB login failed"
        self.scenes = self._load("stash_scenes.json")  # id -> scene
        self.fp = self._load("stash_fp.json")  # hash -> [scene ids]
        self.q = self._load("stash_queries.json")  # query-key -> [scene ids]
        self.studios = self._load("stash_studios.json")  # name -> [studio dicts]
        self.performers = self._load("stash_performers.json")  # name -> [performer dicts]

    def _load(self, name):
        p = f"{CACHE}/{name}"
        return json.load(open(p)) if os.path.exists(p) else {}

    def save(self):
        for name, obj in (("stash_scenes.json", self.scenes), ("stash_fp.json", self.fp), ("stash_queries.json", self.q), ("stash_studios.json", self.studios), ("stash_performers.json", self.performers)):
            json.dump(obj, open(f"{CACHE}/{name}", "w"))

    def gql(self, query, variables=None, retries=5):
        for i in range(retries):
            try:
                r = self.s.post(f"{BASE}/graphql", json={"query": query, "variables": variables or {}}, timeout=60)
                if r.status_code in (429, 502, 503, 504):
                    time.sleep(2 * (i + 1))
                    continue
                j = r.json()
                if j.get("errors") and not j.get("data"):
                    raise RuntimeError(j["errors"][0]["message"])
                return j["data"]
            except (requests.RequestException, ValueError):
                time.sleep(2 * (i + 1))
        raise RuntimeError("StashDB request failed repeatedly")

    def fingerprints(self, hashes, batch=40):
        """oshash list -> fills self.fp; returns nothing."""
        todo = [h for h in dict.fromkeys(hashes) if h and h not in self.fp]
        q = "query($f:[[FingerprintQueryInput!]!]!){ findScenesBySceneFingerprints(fingerprints:$f){ %s } }" % SCENE_FIELDS
        for i in range(0, len(todo), batch):
            chunk = todo[i : i + batch]
            res = self.gql(q, {"f": [[{"hash": h, "algorithm": "OSHASH"}] for h in chunk]})["findScenesBySceneFingerprints"]
            for h, scenes in zip(chunk, res):
                self.fp[h] = [sc["id"] for sc in scenes]
                for sc in scenes:
                    self.scenes[sc["id"]] = sc
            time.sleep(0.15)
            if (i // batch) % 25 == 0:
                self.save()
        self.save()

    def query_scenes(self, key, inp, per_page=8):
        if key in self.q:
            return [self.scenes[i] for i in self.q[key]]
        q = "query($i:SceneQueryInput!){ queryScenes(input:$i){ scenes { %s } } }" % SCENE_FIELDS
        inp = dict(inp, per_page=per_page)
        scenes = self.gql(q, {"i": inp})["queryScenes"]["scenes"]
        for sc in scenes:
            self.scenes[sc["id"]] = sc
        self.q[key] = [sc["id"] for sc in scenes]
        time.sleep(0.15)
        return scenes

    def find_studios(self, name):
        if name not in self.studios:
            q = "query($n:String!){ queryStudios(input:{name:$n, per_page:10}){ studios { id name parent { id name } } } }"
            self.studios[name] = self.gql(q, {"n": name})["queryStudios"]["studios"]
            time.sleep(0.15)
        return self.studios[name]

    def find_performers(self, name):
        if name not in self.performers:
            q = "query($n:String!){ queryPerformers(input:{name:$n, per_page:10}){ performers { id name disambiguation aliases } } }"
            self.performers[name] = self.gql(q, {"n": name})["queryPerformers"]["performers"]
            time.sleep(0.15)
        return self.performers[name]
