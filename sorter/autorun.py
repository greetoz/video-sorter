"""Unattended run of a plan: validate on the file server first, and only if that is clean, execute. Any problem stops everything before a
single file is touched. Prints the same "dry:"/"full:" summary lines execute.py does."""
import os
import re
import subprocess
import sys

HERE = os.path.dirname(__file__)


def run(mode):
    p = subprocess.run([sys.executable, "-u", f"{HERE}/execute.py", mode], capture_output=True, text=True)
    print(p.stdout, end="")
    print(p.stderr, end="", file=sys.stderr)
    m = re.findall(rf"^{mode}: (\d+) ops, (\d+) failures", p.stdout, re.M)
    return p.returncode, (int(m[-1][0]), int(m[-1][1])) if m else None


rc, res = run("dry")
if rc or res is None:
    sys.exit("FAIL: the validation could not run - nothing was executed")
if res[1]:
    sys.exit(f"FAIL: validation found {res[1]} problem(s) - nothing was executed")
if res[0] == 0:
    print("full: 0 ops, 0 failures")
    sys.exit(0)
rc, res = run("full")
if rc or res is None or res[1]:
    sys.exit("FAIL: the run stopped with problems - see above; it can be resumed")
