"""CMS Revalidation Clinic Group Practice Reassignment.

Maps an individual provider's NPI to the group practice it reassigns Medicare billing to.
Two providers who reassign to the SAME group share a documented billing relationship — an
operator edge stronger than a name collision. Filtered to WA and cached.
"""
import json
import os

from fraudscan import http_util
from fraudscan.config import DATA_DIR

REASSIGN_UUID = "e1f1fa9a-d6b4-417e-948a-c72dead8a41c"


def _cache_path():
    d = os.path.join(DATA_DIR, "cache")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "reassignment.json")


def _build(state):
    npi_to_pacs, group_names = {}, {}
    for r in http_util.cms_dataapi_fetch(
            REASSIGN_UUID, filters={"Individual State Code": state}):
        npi = (r.get("Individual NPI") or "").strip()
        pac = (r.get("Group PAC ID") or "").strip()
        if not (npi and pac):
            continue
        npi_to_pacs.setdefault(npi, [])
        if pac not in npi_to_pacs[npi]:
            npi_to_pacs[npi].append(pac)
        group_names.setdefault(pac, (r.get("Group Legal Business Name") or "").strip())
    return {"npi_to_pacs": npi_to_pacs, "group_names": group_names}


def load_reassignment(state="WA", refresh=False):
    path = _cache_path()
    if not refresh and os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    data = _build(state)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    return data
