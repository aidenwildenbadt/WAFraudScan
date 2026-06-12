"""Ownership signals for nursing homes (the only category with open CMS ownership data).

CMS publishes ownership only for SNFs and hospitals (PECOS change-of-ownership +
owner disclosure). We use the SNF files:
  - Change of Ownership (CofO): facilities that changed hands (CCN + effective date) —
    frequent ownership churn is a known fraud-risk signal.
  - Owner Information: owner -> facilities, to flag one owner controlling several homes.

Coverage notes (logged, not hidden): CofO is bounded to WA buyers; Owner Info is
bounded to WA-located owners, so out-of-state owners of WA homes are not captured.
Results are cached under data/cache/ so scoring doesn't re-fetch.
"""
import json
import os
import re

from fraudscan import http_util
from fraudscan.config import DATA_DIR
from fraudscan.registry import normalize

SNF_COFO_UUID = "f557a6ed-95b3-4a22-8433-4175db2dec1c"
# CMS "All Owners" (PECOS) files share one schema; we link owners across facility types.
OWNER_UUIDS = {
    "SNF": "a4358712-e910-4eaf-8f24-5e90ba3cf8d0",
    "Hospice": "e983965e-1603-4cb8-82b5-c40090e380d1",
    "HHA": "fc009b2d-7846-44b1-b4a1-692f0c143879",
}


def _cache_path():
    d = os.path.join(DATA_DIR, "cache")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "ownership_v2.json")  # v2: + hospice/HHA owners


def _owner_key(rec):
    name = (rec.get("ORGANIZATION NAME - OWNER") or " ".join(
        p for p in (rec.get("FIRST NAME - OWNER"), rec.get("LAST NAME - OWNER")) if p))
    return normalize(name), (name or "").strip()


def _build(state):
    churn, churn_n, churn_dates = {}, {}, {}
    date_ccns = {}
    for r in http_util.cms_dataapi_fetch(
            SNF_COFO_UUID, filters={"ENROLLMENT STATE - BUYER": state}):
        date = r.get("EFFECTIVE DATE") or ""
        for ccn in (r.get("CCN - BUYER"), r.get("CCN - SELLER")):
            ccn = (ccn or "").strip()
            if not ccn:
                continue
            churn_n[ccn] = churn_n.get(ccn, 0) + 1
            churn_dates.setdefault(ccn, set()).add(date)
            date_ccns.setdefault(date, set()).add(ccn)
            if date > churn.get(ccn, ""):
                churn[ccn] = date
    # G14: a CHOW date shared by 4+ facilities is a CHAIN-WIDE transfer (one corporate
    # sale), not per-facility "flips" — license-laundering language must not apply.
    chain_dates = sorted(d for d, cc in date_ccns.items() if len(cc) >= 4)

    owner_to_orgs, org_to_owners = {}, {}
    for label, uuid in OWNER_UUIDS.items():
        try:
            rows = http_util.cms_dataapi_fetch(uuid, filters={"STATE - OWNER": state})
        except Exception:
            continue  # one owner file failing shouldn't drop the others
        for r in rows:
            org = normalize(r.get("ORGANIZATION NAME"))
            # prefer the owner's PECOS associate id (a hard identifier) as the key,
            # falling back to the normalized owner name.
            assoc = (r.get("ASSOCIATE ID - OWNER") or "").strip()
            okey, oname = _owner_key(r)
            okey = ("aid:" + assoc) if assoc else okey
            if not org or not okey:
                continue
            # G13: passive institutional stakes are not operational control — the WA
            # State Investment Board's national LP portfolio falsely "hard-linked" a
            # Spokane hospice to a Mississippi namesake facility (~32 of 37 points).
            up = (oname or "").upper()
            if re.search(r"INVESTMENT BOARD|PENSION|RETIREMENT SYSTEM|"
                         r"INVESTMENT FUND|CAPITAL PARTNERS", up):
                continue
            role = (r.get("ROLE PLAYED BY OWNER OR MANAGING EMPLOYEE IN FACILITY")
                    or r.get("ROLE") or "")
            pct_raw = re.sub(r"[^\d.]", "", str(r.get("PERCENTAGE OWNERSHIP") or ""))
            try:
                if "INDIRECT" in role.upper() and pct_raw and float(pct_raw) < 25:
                    continue
            except ValueError:
                pass
            owner_to_orgs.setdefault(
                okey, {"name": oname, "orgs": set()})["orgs"].add(org)
            org_to_owners.setdefault(org, set()).add(okey)
    # serialize sets
    o2o = {k: {"name": v["name"], "orgs": sorted(v["orgs"])}
           for k, v in owner_to_orgs.items()}
    org2o = {k: sorted(v) for k, v in org_to_owners.items()}
    return {"churn": churn, "churn_n": churn_n,
            "churn_dates": {k: sorted(v) for k, v in churn_dates.items()},
            "chain_dates": chain_dates,
            "owner_to_orgs": o2o, "org_to_owners": org2o}


def load_ownership(state="WA", refresh=False):
    path = _cache_path()
    if not refresh and os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    data = _build(state)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    return data
