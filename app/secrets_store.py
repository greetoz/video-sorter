"""Credentials the app uses for the file server and StashDB.

They live in the /data volume (mode 600), so they are neither baked into the image nor stored in the Portainer stack,
and they can be replaced from the web UI. The sorter modules read them from the SMB_USER/SMB_PASS/STASH_USER/STASH_PASS
environment variables, so apply_env() exports the stored values into the process environment.
"""
import json
import os
import tempfile
import threading
import time

PATH = os.environ.get("SECRETS_FILE", "/data/secrets.json")
ENV = {"smb": ("SMB_USER", "SMB_PASS"), "stash": ("STASH_USER", "STASH_PASS")}
_lock = threading.Lock()
_env_defaults = {k: os.environ.get(u, "") for k, (u, _) in ENV.items()}  # non-secret default usernames from the stack


def _read():
    try:
        with open(PATH) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def apply_env():
    stored = _read()
    for kind, (u, p) in ENV.items():
        if kind in stored:
            os.environ[u], os.environ[p] = stored[kind]["username"], stored[kind]["password"]


def username(kind):
    return _read().get(kind, {}).get("username") or _env_defaults.get(kind, "")


def save(kind, user, password):
    with _lock:
        data = _read()
        data[kind] = {"username": user, "password": password, "updated": int(time.time())}
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(PATH), prefix=".secrets-")
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, PATH)


def summary():
    """Usernames and whether a password is stored - never the passwords themselves."""
    stored = _read()
    return {k: {"username": username(k), "configured": k in stored or bool(os.environ.get(ENV[k][1])), "updated": stored.get(k, {}).get("updated")} for k in ENV}
