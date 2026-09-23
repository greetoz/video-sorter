"""Look up oshashes of a share on StashDB. Usage: lookup_fp.py <share>"""
import json, os, sys
sys.path.insert(0, os.path.dirname(__file__))
from stash import Stash, CACHE
tag = sys.argv[1].strip("$")
h = json.load(open(f"{CACHE}/hash_{tag}.json"))
st = Stash()
st.fingerprints([v[1] for v in h.values()])
matched = sum(1 for v in h.values() if v[1] and st.fp.get(v[1]))
print(f"{tag}: {matched}/{len(h)} files matched on StashDB by oshash; {len(st.scenes)} scenes cached")
