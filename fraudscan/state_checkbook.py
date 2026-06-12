"""Washington state vendor payments from fiscal.wa.gov "Open Checkbook" (OFM/LEAP).

Unlike the federal Medicaid/Medicare data (NPI-keyed), this is WA's own statewide
expenditure file: one row per agency -> NAMED vendor -> amount, monthly, with actual
disbursements (not contract ceilings). Columns: Bien, FY, FMonth, Agy, Agency, Object,
Category, Subobj, SubCategory, Vendor, Amount. It is the only public route to WA-state
dollars (DSHS in-home care, DCYF, HCA, DOH, etc.) paid to providers we hold.

Join key is the VENDOR NAME (no NPI/SSPS#/UBI in the file), so matches are by normalized
name — strong for org/LLC names, weaker for individuals; namesake risk applies, so these
surface as leads to verify, not proof. We stream the .xlsx (a zip of XML) with stdlib
(zipfile + ElementTree iterparse) and keep only vendors whose normalized name matches an
entity we track.
"""
import io
import os
import re
import ssl
import urllib.request
import xml.etree.ElementTree as ET
import zipfile

from fraudscan.config import DATA_DIR
from fraudscan.registry import normalize

# both current biennia: 2023-25 (FY2024+FY2025) and 2025-27 (FY2026, monthly through the
# previous month) — the FY2026 file is the freshest public money data available anywhere.
URLS = ["https://fiscal.wa.gov/Spending/VendorPayments2325.xlsx",
        "https://fiscal.wa.gov/Spending/VendorPayments2527.xlsx"]
URL = URLS[0]                                          # back-compat
_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE
# HHS-relevant paying agencies (others are dropped to cut noise)
AGENCIES = {"107": "HCA", "300": "DSHS", "307": "DCYF", "303": "DOH"}


def _cache_path():
    d = os.path.join(DATA_DIR, "cache")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "state_checkbook.json")


def _col(ref):
    return "".join(ch for ch in ref if ch.isalpha()) if ref else ""


def _scan_sheet(z, sheet, ss, want, agg):
    # column letters: D=Agy, B=FY, J=Vendor, K=Amount
    with z.open(sheet) as fh:
        row = {}
        for ev, el in ET.iterparse(fh, events=("end",)):
            if el.tag == f"{_NS}c":
                col = _col(el.get("r"))
                v = el.find(f"{_NS}v")
                if v is not None and v.text is not None:
                    row[col] = (ss[int(v.text)] if el.get("t") == "s" else v.text)
                el.clear()
            elif el.tag == f"{_NS}row":
                agy = (row.get("D") or "").strip()
                if agy in AGENCIES:
                    raw = (row.get("J") or "").strip()
                    key = normalize(raw)
                    if key and key in want:
                        try:
                            amt = float(row.get("K") or 0)
                        except ValueError:
                            amt = 0.0
                        if amt:
                            a = agg.get(key)
                            if a is None:
                                a = agg[key] = {"total": 0.0, "by_fy": {},
                                                "agencies": set(), "raw_name": raw}
                            a["total"] += amt
                            fy = (row.get("B") or "").strip()
                            a["by_fy"][fy] = a["by_fy"].get(fy, 0.0) + amt
                            a["agencies"].add(AGENCIES[agy])
                row = {}
                el.clear()


def build_checkbook(want_names, urls=None, progress=None):
    """Stream every sheet of every Open Checkbook biennium file (each fiscal year lives
    on its OWN worksheet); keep only rows whose vendor normalizes to a name we track AND
    whose paying agency is HHS-relevant. Returns
    {normalized_vendor: {total, by_fy:{fy:amt}, agencies:[...], raw_name}}."""
    want = set(want_names or [])
    if not want:
        return {}
    agg = {}
    for url in (urls or URLS):
        data = urllib.request.urlopen(
            urllib.request.Request(url, headers={"User-Agent": "FraudScan/0.1 research"}),
            timeout=180, context=_CTX).read()
        if progress:
            progress(len(data))
        z = zipfile.ZipFile(io.BytesIO(data))
        ss = []
        if "xl/sharedStrings.xml" in z.namelist():
            root = ET.fromstring(z.read("xl/sharedStrings.xml"))
            ss = ["".join(t.text or "" for t in si.iter(f"{_NS}t"))
                  for si in root.findall(f"{_NS}si")]
        sheets = sorted(n for n in z.namelist()
                        if re.match(r"xl/worksheets/sheet\d+\.xml$", n))
        for sheet in sheets:
            _scan_sheet(z, sheet, ss, want, agg)
    for a in agg.values():
        a["total"] = round(a["total"], 2)
        a["by_fy"] = {k: round(v, 2) for k, v in a["by_fy"].items()}
        a["agencies"] = sorted(a["agencies"])
    return agg


def save_cache(agg):
    import json
    with open(_cache_path(), "w", encoding="utf-8") as fh:
        json.dump(agg, fh)


def load_cache():
    import json
    p = _cache_path()
    if not os.path.exists(p):
        return {}
    with open(p, "r", encoding="utf-8") as fh:
        return json.load(fh)


def checkbook_payments(entities, agg):
    """Payment rows from the cached vendor aggregates, matched by normalized name.

    A vendor's dollars are attributed ONLY when its normalized name maps to exactly one
    of our PROVIDER entities — if several share the name it's ambiguous (namesake/generic
    name), so we skip rather than multiply the money across all of them. Contract entities
    are excluded on purpose: a contract's disbursements ARE checkbook payments, so putting
    checkbook $ on contract records would count the same money twice (ceiling + outlay),
    and procurement vendors would drown the provider signal this tool exists to surface."""
    by_name = {}
    for e in entities:
        if e.source.startswith("contracts"):
            continue
        by_name.setdefault(normalize(e.name), []).append(e)
    rows = []
    for key, a in agg.items():
        ents = by_name.get(key)
        if not ents or len(ents) != 1:        # unmatched or ambiguous -> don't attribute
            continue
        e = ents[0]
        label = "WA state (" + "/".join(a.get("agencies", [])) + ")"
        for fy, amt in a.get("by_fy", {}).items():
            if amt:
                rows.append({"entity_uid": e.uid, "program": label,
                             "period": "FY" + str(fy), "amount": amt,
                             "source_url": e.source_url})
    return rows
