"""Read-only SMB helpers: walking shares and computing OpenSubtitles-style oshash."""
import os
import struct
import threading

import smbclient

HOST = os.environ.get("SMB_HOST", "10.10.0.11")
VIDEO_EXT = {".mp4", ".mkv", ".avi", ".wmv", ".mov", ".m4v", ".ts", ".flv", ".mpg", ".mpeg", ".webm", ".vid"}


_connect_lock = threading.Lock()


def connect():
    # serialized: two requests arriving together on a fresh process must not both open the session at once
    with _connect_lock:
        smbclient.register_session(
            HOST, username=os.environ["SMB_USER"], password=os.environ["SMB_PASS"], connection_timeout=30
        )


def unc(share, rel=""):
    return f"\\\\{HOST}\\{share}" + ("\\" + rel if rel else "")


def is_video(path):
    return os.path.splitext(path)[1].lower() in VIDEO_EXT


def walk(share):
    """Full recursive listing -> list of {path, dir, size, mtime} (paths relative to share, backslash separated)."""
    out, stack = [], [""]
    while stack:
        rel = stack.pop()
        try:
            for e in smbclient.scandir(unc(share, rel)):
                st = e.stat()
                p = (rel + "\\" + e.name) if rel else e.name
                out.append({"path": p, "dir": e.is_dir(), "size": st.st_size, "mtime": int(st.st_mtime)})
                if e.is_dir():
                    stack.append(p)
        except Exception as ex:  # keep walking, record the problem
            out.append({"path": rel, "error": str(ex)[:120]})
    return out


def oshash(share, rel, size):
    if size < 65536:
        return None
    with smbclient.open_file(unc(share, rel), mode="rb") as f:
        head = f.read(65536)
        f.seek(size - 65536)
        tail = f.read(65536)
    h = size
    for chunk in (head, tail):
        for (x,) in struct.iter_unpack("<Q", chunk[: len(chunk) // 8 * 8]):
            h = (h + x) & 0xFFFFFFFFFFFFFFFF
    return "%016x" % h
