"""Backend-agnostic file access. Two backends, chosen once for the whole app (config.STORAGE_BACKEND
/ Settings > Library):

  "smb_winrm"  a Windows file server: read/listed over SMB, moves/deletes done over PowerShell
               remoting by execute.py (this project's own reference setup - a move is an instant
               same-volume rename on the server, never a copy through the app).
  "local"      src_root/dst_root are paths bind-mounted straight into *this* container - any NFS or
               CIFS mount, a Synology/TrueNAS/Unraid share mounted at the OS level, local disk,
               whatever your Docker host can see. No SMB, no WinRM: plain filesystem calls, and
               execute.py's moves/deletes/etc. run directly in this process instead of remotely.

Everything else in sorter/ and app/ goes through the functions here (never smbclient directly),
keyed by a "locator" - an opaque, backend-specific handle for one file or directory, built by
locator(share, path). A relative path is always backslash-joined (matching how the SMB side has
always represented it, and how it's stored in every cache/manifest file already on disk); only
locator() knows how to turn that into something the active backend can actually use.
"""
import os
import struct

import config

VIDEO_EXT = {".mp4", ".mkv", ".avi", ".wmv", ".mov", ".m4v", ".ts", ".flv", ".mpg", ".mpeg", ".webm", ".vid"}


def is_local():
    return config.get()["storage_backend"] == "local"


def connect():
    """Establish an SMB session if this backend needs one; a no-op for "local"."""
    if is_local():
        return
    import smbio
    smbio.connect()


def _local_root(share):
    cfg = config.get()
    if share == cfg["src_share"]:
        return cfg["src_root"]
    if share == cfg["dst_share"]:
        return cfg["dst_root"]
    raise ValueError(f"unknown share {share!r} (expected {cfg['src_share']!r} or {cfg['dst_share']!r})")


def locator(share, rel=""):
    """share + a backslash-joined relative path -> something open_file/stat/scandir can use."""
    if is_local():
        root = _local_root(share)
        return os.path.join(root, rel.replace("\\", "/")) if rel else root
    import smbio
    return smbio.unc(share, rel)


def is_video(path):
    return os.path.splitext(path)[1].lower() in VIDEO_EXT


def stat(loc):
    if is_local():
        return os.stat(loc)
    import smbclient
    return smbclient.stat(loc)


def scandir(loc):
    if is_local():
        return os.scandir(loc)
    import smbclient
    return smbclient.scandir(loc)


def open_file(loc, mode="rb", buffering=1 << 20, writable=False):
    """writable=True: a second reader (this app's own stream, a viewer) may already have the file open."""
    if is_local():
        return open(loc, mode, buffering=buffering)
    import smbclient
    return smbclient.open_file(loc, mode=mode, buffering=buffering, share_access="rw" if writable else "r")


def walk(share):
    """Full recursive listing -> list of {path, dir, size, mtime} (paths relative to share, backslash separated)."""
    out, stack = [], [""]
    while stack:
        rel = stack.pop()
        try:
            for e in scandir(locator(share, rel)):
                st = e.stat()
                p = (rel + "\\" + e.name) if rel else e.name
                out.append({"path": p, "dir": e.is_dir(), "size": st.st_size, "mtime": int(st.st_mtime)})
                if e.is_dir():
                    stack.append(p)
        except Exception as ex:  # keep walking, record the problem
            out.append({"path": rel, "error": str(ex)[:120]})
    return out


def oshash(share, rel, size):
    """OpenSubtitles-style hash: first+last 64 KiB folded into a 64-bit sum. Same algorithm regardless of backend."""
    if size < 65536:
        return None
    with open_file(locator(share, rel), mode="rb") as f:
        head = f.read(65536)
        f.seek(size - 65536)
        tail = f.read(65536)
    h = size
    for chunk in (head, tail):
        for (x,) in struct.iter_unpack("<Q", chunk[: len(chunk) // 8 * 8]):
            h = (h + x) & 0xFFFFFFFFFFFFFFFF
    return "%016x" % h
