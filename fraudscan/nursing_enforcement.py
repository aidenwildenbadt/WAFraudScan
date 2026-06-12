"""Nursing-home enforcement records (CMS) — verifiable corroboration.

Public, authoritative signals for the nursing facilities we ingest:
  - Health Deficiencies (scope/severity letter; G–L = actual harm, J/K/L = immediate
    jeopardy),
  - Penalties (civil monetary penalty fine amounts).
Aggregated per CCN and cached, so a sanctioned/odd facility can be corroborated with
real fines and harm citations.
"""
import json
import os

from fraudscan import http_util
from fraudscan.config import DATA_DIR

DEFICIENCIES_UUID = "e8563151-b70a-5a9c-9e73-6da406f2b147"
PENALTIES_UUID = "40a9551c-cb31-5869-b745-7f0a613d2174"
_JEOPARDY = {"J", "K", "L"}


def _money(v):
    if v in (None, "", "NULL"):
        return 0.0
    try:
        return float(str(v).replace("$", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return 0.0


def _cache_path():
    d = os.path.join(DATA_DIR, "cache")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "nursing_enforcement.json")


def load_enforcement(state="WA", refresh=False):
    path = _cache_path()
    if not refresh and os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    by_ccn = {}
    for r in http_util.cms_provider_fetch(DEFICIENCIES_UUID, state=state):
        ccn = (r.get("cms_certification_number_ccn") or "").strip()
        if not ccn:
            continue
        d = by_ccn.setdefault(ccn, {"deficiencies": 0, "jeopardy": 0, "worst": "",
                                    "fines": 0.0, "penalties": 0})
        d["deficiencies"] += 1
        ss = (r.get("scope_severity_code") or "").strip().upper()
        if ss in _JEOPARDY:
            d["jeopardy"] += 1
        if ss > d["worst"]:
            d["worst"] = ss
    for r in http_util.cms_provider_fetch(PENALTIES_UUID, state=state):
        ccn = (r.get("cms_certification_number_ccn") or "").strip()
        if not ccn:
            continue
        d = by_ccn.setdefault(ccn, {"deficiencies": 0, "jeopardy": 0, "worst": "",
                                    "fines": 0.0, "penalties": 0})
        ptype = (r.get("penalty_type") or "").upper()
        if "DENIAL" in ptype:        # G15: Denial of Payment for New Admissions —
            d.setdefault("dpna_dates", []).append(   # a dated, testable payment bar
                (r.get("penalty_date") or "")[:10])
        amt = _money(r.get("fine_amount"))
        if amt:
            d["fines"] += amt
            d["penalties"] += 1
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(by_ccn, fh)
    return by_ccn
