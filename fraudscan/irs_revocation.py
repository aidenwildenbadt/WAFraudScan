"""IRS auto-revocation list — nonprofits whose tax-exempt status was REVOKED.

The IRS publishes (monthly, by statute) every organization whose exemption was
automatically revoked for failing to file Form 990 for three consecutive years. For an
operator we matched to an EIN via ProPublica/990, a revocation is a real integrity
signal: the org presents as a nonprofit (and may keep receiving public funds on that
basis) while the IRS has revoked the status — and our "established nonprofit" score
suppression must not apply to it.

File: ~46MB zip of pipe-delimited text (EIN|NAME|DBA|ADDR|CITY|STATE|ZIP|COUNTRY|
EXEMPTION-TYPE|REVOCATION-DATE|POSTING-DATE|). We stream it once and keep only the EINs
we track (cached).
"""
import io
import json
import os
import re
import ssl
import urllib.request
import zipfile

from fraudscan.config import DATA_DIR

URL = "https://apps.irs.gov/pub/epostcard/data-download-revocation.zip"
_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE
_MON = {m: i for i, m in enumerate(
    "JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split(), 1)}


def _cache_path():
    d = os.path.join(DATA_DIR, "cache")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "irs_revoked.json")


def _iso(d):
    m = re.match(r"(\d{2})-([A-Z]{3})-(\d{4})", str(d or "").strip().upper())
    if not m or m.group(2) not in _MON:
        return ""
    return f"{m.group(3)}-{_MON[m.group(2)]:02d}-{m.group(1)}"


def norm_ein(e):
    return re.sub(r"\D", "", str(e or ""))[:9]


def load_revoked(eins, refresh=False):
    """{ein: {name, revoked, posted}} for the EINs we track (cached by tracked set)."""
    want = {norm_ein(e) for e in eins if norm_ein(e)}
    if not want:
        return {}
    path = _cache_path()
    if not refresh and os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            cached = json.load(fh)
        if want <= set(cached.get("tracked", [])):
            return cached.get("revoked", {})
    req = urllib.request.Request(URL, headers={"User-Agent": "FraudScan/0.1"})
    data = urllib.request.urlopen(req, timeout=300, context=_CTX).read()
    z = zipfile.ZipFile(io.BytesIO(data))
    out = {}
    with z.open(z.namelist()[0]) as fh:
        for line in io.TextIOWrapper(fh, encoding="latin-1"):
            p = line.split("|")
            if len(p) < 11:
                continue
            ein = norm_ein(p[0])
            if ein in want:
                out[ein] = {"name": p[1].strip(), "revoked": _iso(p[9]),
                            "posted": _iso(p[10])}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"tracked": sorted(want), "revoked": out, "source": URL}, fh)
    return out
