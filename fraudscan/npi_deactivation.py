"""NPPES deactivated NPIs (CMS monthly file) — payments dated after an NPI was
deactivated are a hard contradiction.

An NPI is deactivated when the provider dies, retires, disbands, or CMS acts — a
deactivated NPI cannot lawfully bill. CMS publishes the full deactivation list monthly
(~350k rows: NPI + deactivation date, free). We keep only the NPIs we track, cached, and
the `paid_after_npi_deactivated` rule compares payment years against the date.

Year-granularity caveat is honored: our payments carry years, so the rule only fires
when a payment year is STRICTLY after the deactivation year — never the same year.
"""
import io
import json
import os
import re
import ssl
import urllib.request
import zipfile

from fraudscan.config import DATA_DIR

FILES_PAGE = "https://download.cms.gov/nppes/NPI_Files.html"
_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE


def _cache_path():
    d = os.path.join(DATA_DIR, "cache")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "npi_deactivated.json")


def _get(url, timeout=120):
    req = urllib.request.Request(url, headers={"User-Agent": "FraudScan/0.1"})
    return urllib.request.urlopen(req, timeout=timeout, context=_CTX).read()


def discover_url():
    """The deactivation zip's name changes monthly — find it on the files page."""
    html = _get(FILES_PAGE, timeout=45).decode("utf-8", "replace")
    m = re.search(r"(NPPES_Deactivated_NPI_Report_\d+(?:_V\d+)?\.zip)", html)
    if not m:
        raise RuntimeError("deactivation file not found on NPI_Files.html")
    return "https://download.cms.gov/nppes/" + m.group(1)


def _iso(mdy):
    m = re.match(r"(\d{2})/(\d{2})/(\d{4})", str(mdy or "").strip())
    return f"{m.group(3)}-{m.group(1)}-{m.group(2)}" if m else ""


def build_deactivated(npis, refresh=False):
    """{npi: deactivation ISO date} for the NPIs we track (cached)."""
    path = _cache_path()
    if not refresh and os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            cached = json.load(fh)
        # cache is keyed to a tracked-NPI set; rebuild if our set grew meaningfully
        if set(map(str, npis)) <= set(cached.get("tracked", [])):
            return cached.get("deactivated", {})
    from fraudscan.xlsx_util import xlsx_rows
    url = discover_url()
    z = zipfile.ZipFile(io.BytesIO(_get(url)))
    inner = [n for n in z.namelist() if n.lower().endswith(".xlsx")][0]
    want = {str(n) for n in npis if n}
    out = {}
    for r in xlsx_rows(z.read(inner)):
        if r and str(r[0]).strip() in want:
            iso = _iso(r[1] if len(r) > 1 else "")
            if iso:
                out[str(r[0]).strip()] = iso
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"tracked": sorted(want), "deactivated": out, "source": url}, fh)
    return out


def load_cached():
    path = _cache_path()
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh).get("deactivated", {})
