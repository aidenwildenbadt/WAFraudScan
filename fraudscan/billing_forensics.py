"""Procedure-level billing forensics from Medicare Part B 'by Provider and Service'.

For the (crosswalked) physicians who actually have Part B payments, pull their HCPCS-line
rows and compute verifiable behavioral signals:
  - single-code dominance: one procedure code = most of the provider's Medicare $ (mill
    pattern),
  - services-per-beneficiary-day outlier: many services billed per patient visit
    (possible unbundling / inflation).

Bounded to providers we already know bill Part B, cached in data/cache/billing.json.
"""
import json
import os

from fraudscan import http_util
from fraudscan.config import DATA_DIR

PARTB_SERVICE_UUID = "92396110-2aed-4d63-a6a2-5d6207d46a29"


def _cache_path():
    d = os.path.join(DATA_DIR, "cache")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "billing.json")


def _provider_metrics(npi):
    rows = http_util.cms_dataapi_fetch(PARTB_SERVICE_UUID,
                                       filters={"Rndrng_NPI": npi}, size=500)
    codes = []
    for r in rows:
        try:
            srv = float(r.get("Tot_Srvcs") or 0)
            pay = float(r.get("Avg_Mdcr_Pymt_Amt") or 0)
            bds = float(r.get("Tot_Bene_Day_Srvcs") or 0)
        except (TypeError, ValueError):
            continue
        codes.append({"code": r.get("HCPCS_Cd"), "desc": r.get("HCPCS_Desc"),
                      "amt": srv * pay, "srv": srv, "bds": bds})
    total = sum(c["amt"] for c in codes)
    if total <= 0:
        return None
    top = max(codes, key=lambda c: c["amt"])
    spbd = max((c["srv"] / c["bds"] for c in codes if c["bds"] >= 20), default=0.0)
    return {"total": round(total), "n_codes": len(codes),
            "top_code": top["code"], "top_desc": top["desc"],
            "top_share": round(top["amt"] / total, 3),
            "max_srv_per_day": round(spbd, 1)}


def build_billing(uid_to_npi, progress=None):
    """uid_to_npi: {entity_uid: npi} for Part-B-billing providers. Returns {uid: metrics}."""
    path = _cache_path()
    cache = {}
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            cache = json.load(fh)
    out = {}
    for i, (uid, npi) in enumerate(uid_to_npi.items()):
        if npi not in cache:
            try:
                cache[npi] = _provider_metrics(npi) or {}
            except Exception:
                cache[npi] = {}
            if progress:
                progress(i + 1)
        if cache[npi]:
            out[uid] = cache[npi]
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(cache, fh)
    return out
