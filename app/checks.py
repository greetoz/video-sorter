"""Read-only connectivity checks used by the status page."""
import os
import sys
import time

sys.path.insert(0, "/app/sorter")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "sorter"))


def timed(fn):
    t = time.time()
    try:
        detail = fn()
        return {"ok": True, "detail": detail, "ms": int((time.time() - t) * 1000)}
    except Exception as ex:  # report, never raise
        return {"ok": False, "detail": f"{type(ex).__name__}: {str(ex)[:160]}", "ms": int((time.time() - t) * 1000)}


def smb():
    import smbclient
    import smbio
    smbio.connect()
    out = []
    for share in ("xtosort$", "xsites$"):
        n = sum(1 for _ in smbclient.scandir(smbio.unc(share)))
        out.append(f"{share}: {n} top-level entries")
    return "; ".join(out)


def psrp():
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
            import smbclient
            import smbio
            smbclient.reset_connection_cache()
            try:
                smbclient.register_session(smbio.HOST, username=user, password=password, connection_timeout=15)
                n = sum(1 for _ in smbclient.scandir(smbio.unc("xtosort$")))
            finally:
                smbclient.reset_connection_cache()  # the next real connect() registers the stored credentials
            return True, f"logged in as {user}; xtosort$ has {n} top-level entries"
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
    return {"SMB file access": timed(smb), "PowerShell remoting": timed(psrp), "StashDB": timed(stashdb)}
