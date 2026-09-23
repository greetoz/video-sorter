"""SMB plumbing for the "smb_winrm" storage backend - session setup and UNC paths. Everything that
actually reads/lists/hashes a file goes through storage.py instead, which is what the rest of the
app calls; this module exists for storage.py (and execute.py, for the PowerShell-remoting client) to
build on."""
import os
import threading

import smbclient

from config import HOST


_connect_lock = threading.Lock()


def connect():
    # serialized: two requests arriving together on a fresh process must not both open the session at once
    with _connect_lock:
        smbclient.register_session(
            HOST, username=os.environ["SMB_USER"], password=os.environ["SMB_PASS"], connection_timeout=30
        )


def unc(share, rel=""):
    return f"\\\\{HOST}\\{share}" + ("\\" + rel if rel else "")
