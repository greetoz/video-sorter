"""How this deployment's file server is laid out: the SMB host, the two shares (where downloads land and where the
sorted library lives), their local paths on the server (for PowerShell remoting), the special folder names inside
them, and whether the library is split into per-studio subfolders.

Every value has a built-in default (this project's own reference deployment), can be overridden with an environment
variable (set once in docker-compose.yml, for a deployment that is never touched from the UI), and - taking
precedence over both - can be saved from the Settings page, which is kept in the /data volume so it survives
redeploys. Sorter scripts (each a short-lived subprocess) just import the module-level constants below; the app
itself (a long-lived process) calls get() directly wherever a setting might have changed since it started, so a
save from the UI takes effect without a restart.
"""
import json
import os

DATA = os.environ.get("DATA_DIR", "/data")
FILE = os.environ.get("LIBRARY_FILE", f"{DATA}/library.json")

DEFAULTS = {
    "storage_backend": "smb_winrm",    # "smb_winrm": a Windows file server, read over SMB, written to over PowerShell remoting (this
                                       #   project's own reference setup - moves are instant same-volume renames on the server).
                                       # "local": src_root/dst_root are paths bind-mounted straight into this container (any NFS/CIFS/
                                       #   local-disk/... mount your Docker host can see) - no SMB or WinRM at all, just filesystem calls.
    "smb_host": "10.10.0.11",         # storage_backend "smb_winrm" only
    "src_share": "xtosort$",           # where new downloads land, unsorted (an SMB share name, or - "local" backend - just a label)
    "dst_share": "xsites$",            # the sorted library (same)
    "src_root": r"H:\xToSort$",        # "smb_winrm": src_share's path on the file server itself, for PowerShell remoting
                                       # "local": src_share's path inside *this* container, e.g. /incoming
    "dst_root": r"H:\XSites$",         # the same, for dst_share
    "dupes_dir": "_dupes",             # inside src_share: duplicates set aside for you to review/delete on the Duplicates tab
    "review_dir": "_To Sort",          # inside dst_share: unidentified files (only if turned on) and "keep both" picks
    "movies_dir": "_Movie_Scenes",     # inside dst_share: movie rips / scenes with no studio match
    "organize_by_studio": True,        # off: everything lands directly in dst_share, no per-studio subfolders
    "stash_base": "https://stashdb.org",  # a self-hosted stash-box instance works too, if that's what you use
}
ENV = {  # settings key -> environment variable a docker-compose-only deployment can set instead of using the UI
    "storage_backend": "STORAGE_BACKEND",
    "smb_host": "SMB_HOST", "src_share": "SRC_SHARE", "dst_share": "DST_SHARE", "src_root": "SRC_ROOT", "dst_root": "DST_ROOT",
    "dupes_dir": "DUPES_DIR", "review_dir": "REVIEW_DIR", "movies_dir": "MOVIES_DIR",
    "organize_by_studio": "ORGANIZE_BY_STUDIO", "stash_base": "STASH_BASE",
}


def _env_default(k):
    v = os.environ.get(ENV[k])
    if v is None:
        return DEFAULTS[k]
    return (v != "0") if isinstance(DEFAULTS[k], bool) else v


def _stored():
    try:
        with open(FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def get():
    """Effective settings, as a plain dict."""
    out = {k: _env_default(k) for k in DEFAULTS}
    out.update({k: v for k, v in _stored().items() if k in DEFAULTS})
    return out


def actress_dirs():
    """Top-level src_share folder name -> the actress it belongs to (her name is appended to filenames inside it
    that StashDB does not already credit her on). Your own convention, if you use folders like that; empty by default."""
    return _stored().get("actress_dirs", {})


def save(fields, actress_dirs_map=None):
    data = dict(_stored())
    data.update({k: v for k, v in fields.items() if k in DEFAULTS})
    if actress_dirs_map is not None:
        data["actress_dirs"] = actress_dirs_map
    tmp = FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, FILE)


def tag(share):
    """xtosort$ -> xtosort: how hash_all.py/lookup_fp.py name a share's cache files."""
    return share.strip("$")


# module-level constants, for the sorter scripts: each runs once as its own subprocess, so these can never go stale
_cfg = get()
STORAGE_BACKEND = _cfg["storage_backend"]
HOST = _cfg["smb_host"]
SRC_SHARE, DST_SHARE = _cfg["src_share"], _cfg["dst_share"]
SRC_ROOT, DST_ROOT = _cfg["src_root"], _cfg["dst_root"]
DUPES_DIR, REVIEW_DIR, MOVIES_DIR = _cfg["dupes_dir"], _cfg["review_dir"], _cfg["movies_dir"]
ORGANIZE_BY_STUDIO, STASH_BASE = _cfg["organize_by_studio"], _cfg["stash_base"]
SRC_TAG, DST_TAG = tag(SRC_SHARE), tag(DST_SHARE)
ACTRESS_DIRS = actress_dirs()
