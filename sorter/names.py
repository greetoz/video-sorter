"""Windows-safe file naming."""
import os
import re
import unicodedata

MAX_NAME = 150  # filename incl. extension; keeps full paths far below MAX_PATH on the server


def norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", s.lower())


def sanitize(s):
    s = (s or "").replace("’", "'").replace("‘", "'").replace("“", "").replace("”", "")
    s = re.sub(r"\s*:\s*", " - ", s)
    s = re.sub(r"[\\/|]", "-", s)
    s = re.sub(r'[<>"?*\x00-\x1f]', "", s)
    s = re.sub(r"\s+", " ", s).strip(" .")
    s = re.sub(r"( - ){2,}", " - ", s)
    return s


def trim(s, n):
    if len(s) <= n:
        return s
    cut = s[:n].rsplit(" ", 1)[0]
    return (cut if len(cut) > n // 2 else s[:n]).rstrip(" .-,")


def performers_str(names, limit=70):
    names = list(dict.fromkeys(n for n in names if n))
    out = []
    for i, n in enumerate(names):
        cand = ", ".join(out + [n])
        if len(cand) > limit and out:
            return ", ".join(out) + f" +{len(names) - i}"
        out.append(n)
    return ", ".join(out)


def build_name(studio, date, performers, title, ext, suffix=""):
    head = " - ".join(x for x in (sanitize(studio), date or "", sanitize(performers_str(performers))) if x)
    room = MAX_NAME - len(ext) - len(head) - len(suffix) - 3
    t = trim(sanitize(title), max(room, 0)) if title and room >= 12 else ""
    return (head + (" - " + t if t else "") + suffix + ext)[:MAX_NAME]


def append_name(stem_text, name, ext):
    """Original-name fallback: '<cleaned stem> - <name>.ext' trimmed to MAX_NAME."""
    tail = f" - {sanitize(name)}"
    return trim(sanitize(stem_text), MAX_NAME - len(ext) - len(tail)) + tail + ext
