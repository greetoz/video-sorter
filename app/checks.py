"""Read-only connectivity checks used by the status page."""
import time


def timed(fn):
    t = time.time()
    try:
        detail = fn()
        return {"ok": True, "detail": detail, "ms": int((time.time() - t) * 1000)}
    except Exception as ex:  # report, never raise
        return {"ok": False, "detail": f"{type(ex).__name__}: {str(ex)[:160]}", "ms": int((time.time() - t) * 1000)}


def storage_access():
    """Both shares are reachable and listable - works the same for "smb_winrm" and "local"."""
    import config
    import storage
    storage.connect()
    cfg = config.get()
    out = []
    for share in (cfg["src_share"], cfg["dst_share"]):
        n = sum(1 for _ in storage.scandir(storage.locator(share)))
        out.append(f"{share}: {n} top-level entries")
    return "; ".join(out)


def local_write_access():
    """storage_backend "local" only: both mounted roots exist and this process can write to them."""
    import config
    import os
    cfg = config.get()
    out = []
    for label, root in (("source", cfg["src_root"]), ("library", cfg["dst_root"])):
        if not os.path.isdir(root):
            raise RuntimeError(f"{label} path {root!r} does not exist or is not a directory")
        if not os.access(root, os.W_OK):
            raise RuntimeError(f"{label} path {root!r} is not writable by this container")
        out.append(f"{label}: {root} (writable)")
    return "; ".join(out)


def psrp():
    """storage_backend "smb_winrm" only: the PowerShell remoting session the executor uses for moves/deletes."""
    import execute
    c = execute.client()
    out, streams, had = c.execute_ps("$env:COMPUTERNAME + ' / PowerShell ' + $PSVersionTable.PSVersion + ' / ' + [Security.Principal.WindowsIdentity]::GetCurrent().Name")
    return out.strip()


def stashdb():
    from stash import Stash
    s = Stash()
    return f"logged in as {s.gql('{ me { name } }')['me']['name']}"


def test_login(kind, user, password):
    """Try a login with the given credentials without touching the ones in use. Returns (ok, detail)."""
    try:
        if kind == "smb":
            import config
            import smbclient
            import storage
            cfg = config.get()
            if cfg["storage_backend"] == "local":
                return False, "not used with the \"local\" storage backend - there is no file server login"
            smbclient.reset_connection_cache()
            try:
                smbclient.register_session(cfg["smb_host"], username=user, password=password, connection_timeout=15)
                n = sum(1 for _ in storage.scandir(storage.locator(cfg["src_share"])))
            finally:
                smbclient.reset_connection_cache()  # the next real connect() registers the stored credentials
            return True, f"logged in as {user}; {cfg['src_share']} has {n} top-level entries"
        import requests
        from stash import BASE
        s = requests.Session()
        s.post(f"{BASE}/login", data={"username": user, "password": password}, timeout=30).raise_for_status()
        me = s.post(f"{BASE}/graphql", json={"query": "{ me { name } }"}, timeout=30).json().get("data", {}).get("me")
        if not me:
            return False, "StashDB rejected the login"
        return True, f"logged in as {me['name']}"
    except Exception as ex:
        return False, f"{type(ex).__name__}: {str(ex)[:160]}"


def all_checks():
    import config
    if config.get()["storage_backend"] == "local":
        return {"Storage access": timed(storage_access), "Storage write access": timed(local_write_access), "StashDB": timed(stashdb)}
    return {"SMB file access": timed(storage_access), "PowerShell remoting": timed(psrp), "StashDB": timed(stashdb)}
