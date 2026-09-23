"""Filename parsing: extract site token, date, title/performer hints and resolution from messy release names."""
import os
import re

RES_TAG = re.compile(r"(?<![0-9])(2160|1440|1080|720|576|540|480|360)[pP]?(?![0-9])|\b(4K|UHD)\b", re.I)
JUNK = re.compile(
    r"\b(XXX|MP4|MKV|HEVC|x26[45]|H\.?26[45]|WRB|NBQ|YDC|PRT|VSEX|KTR|P0RNL0V3R|Narcos|HDL|A\.I|Super|Resolution|10Bit|AV1|rq|SD|HD|WEB-?DL|DVDRip)\b",
    re.I,
)


def stem(rel):
    return os.path.splitext(rel.split("\\")[-1])[0]


def camel_split(s):
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", s)
    return re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", s)


def resolution_tag(name):
    """Height in pixels implied by the filename, or None."""
    m = None
    for m in RES_TAG.finditer(name):
        pass
    if not m:
        return None
    if m.group(2):
        return 2160
    return int(m.group(1))


def parse(rel):
    """Return dict(kind, site, date, rest, title, performers[], code) - all optional except kind."""
    name = rel.split("\\")[-1]
    st = stem(rel)
    out = {"kind": "other", "stem": st, "res": resolution_tag(name)}
    flat = re.sub(r"[._]+", " ", st)

    # Gamma style: TitleCamel_s01_Perf1_Perf2_1080p
    m = re.match(r"^(?:BTS-)?(?P<title>.+?)_s(?P<sc>\d{2})_(?P<perfs>.+?)_(?:\d{3,4}p)(?:_.*)?$", st, re.I)
    if m:
        out.update(kind="gamma", title=camel_split(m["title"]).strip(), scene_no=int(m["sc"]), performers=[camel_split(p) for p in m["perfs"].split("_") if p], bts=st.upper().startswith("BTS-"))
        return out

    # MegaPACK compilations: "Alexis Tae PART 02of04 MegaPACK_Alexis Tae, X - Studio - 2020-11-22 - Title - bg - 1080p"
    m = re.match(r"^.*MegaPACK_(?P<perfs>.+?) - (?P<rest>.+)$", st)
    if m:
        parts = [p.strip() for p in m["rest"].split(" - ")]
        date = next((p for p in parts if re.fullmatch(r"\d{4}-\d{2}-\d{2}", p)), None)
        parts = [p for p in parts if p != date and not re.fullmatch(r"(bg|bbg|gg|bgg|solo|bgb|ggg|\d{3,4}p|4K)", p, re.I)]
        out.update(kind="megapack", performers=[p.strip() for p in m["perfs"].split(",")], date=date, site=parts[0] if parts else None, title=" - ".join(parts[1:]) or None)
        return out

    # Scene release: Site.YY.MM.DD.Title... or Site 2025 Title (year only) or "Site - Name [15.09.2025]"
    m = re.match(r"^(?P<site>.+?)[ ](?P<yy>\d{2})[ ](?P<mm>\d{2})[ ](?P<dd>\d{2})[ ](?P<rest>.+)$", flat)
    if m and not re.match(r"^\d", m["site"]):
        yy = int(m["yy"])
        out.update(kind="release", site=m["site"], date=f"{2000 + yy}-{m['mm']}-{m['dd']}", rest=JUNK.sub(" ", m["rest"]))
        out["rest"] = re.sub(r"\s+", " ", out["rest"]).strip()
        return out
    m = re.match(r"^(?P<site>.+?)[ ](?P<y>20\d{2})[ ](?P<rest>.+)$", flat)
    if m and re.search(r"XXX|MP4|1080|2160", flat):
        out.update(kind="release_yearonly", site=m["site"], year=m["y"], rest=re.sub(r"\s+", " ", JUNK.sub(" ", m["rest"])).strip())
        return out
    m = re.match(r"^(?P<site>[^-\[]+?) ?- ?(?P<rest>.+?) ?\[(?P<d>\d{2})\.(?P<m>\d{2})\.(?P<y>\d{4})\]", st.replace(",.", ","))
    if m:
        out.update(kind="release", site=re.sub(r"[.]+", " ", m["site"]).strip(), date=f"{m['y']}-{m['m']}-{m['d']}", rest=re.sub(r"[.]+", " ", m["rest"]).strip())
        return out

    # AnalVids / PornBox 720p style: "Performers - Title CODE (dd-mm-yyyy) 720p"
    m = re.match(r"^(?:PornBox - )?(?P<body>.+?) ?\((?P<a>\d{2,4})-(?P<b>\d{2})-(?P<c>\d{2,4})\) ?\d{3,4}p", st)
    if m:
        a, b, c = m["a"], m["b"], m["c"]
        date = f"{a}-{b}-{c}" if len(a) == 4 else f"{c}-{b}-{a}"
        code = re.search(r"\b([A-Z]{2,4}\d{2,4})\b\s*$", m["body"])
        out.update(kind="analvids720", date=date, rest=m["body"], code=code.group(1) if code else None)
        return out

    # AnalVids/LegalPorno/PornWorld 4K: AV_Name_Name_CODE_4K.HDL / PW.GP1234_Name_Name_4K / LP.Title-CODE-4K
    m = re.match(r"^(?P<pre>AV|LP|PW|AGO|NRX)[._ ](?P<body>.+)$", st)
    if m:
        body = re.sub(r"[._]?(4K|A\.I\.?Super\.Resolution|10Bit|HEVC|2160p?|H\.?265|HDL|AV1)\b", " ", m["body"], flags=re.I)
        code = re.search(r"(?:^|[-_ .])((?:[A-Z]{2,4})\d{2,4}(?:-\d)?)(?=[-_ .]|$)", body)
        body = re.sub(r"[-_.]+", " ", body).strip()
        out.update(kind="av4k", prefix=m["pre"], rest=body, code=code.group(1) if code else None)
        return out

    # Vixen-group numeric IDs
    m = re.match(r"^(?P<site>BLACKED|BLACKEDRAW|TUSHY|TUSHY_RAW|VIXEN|MILFY|DEEPER|SLAYED|WIFEY|[VMWDBTS])_(?:RAW_)?(?P<id>\d{3}_?\d{3})", st, re.I)
    if m:
        out.update(kind="vixenid", site=m["site"], vid=m["id"].replace("_", ""))
        return out

    out.update(kind="freeform", rest=re.sub(r"\s+", " ", JUNK.sub(" ", flat)).strip())
    return out
