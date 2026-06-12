"""Crosswalk sanctioned DOH health-care providers to their NPI via NLM/NPPES.

DOH credential data has no NPI, so a sanctioned *physician/prescriber* can't be matched
to federal data by identity. We resolve the physician/prescriber-type credential holders
to an NPI (name + state, via the reachable NLM NPPES service). With the NPI we can then:
  - confirm a federal exclusion by NPI (not just name+state) — filtering namesakes, and
  - join Medicare Part B / Part D payments — "is this sanctioned provider still paid?"

Scoped to billing-provider credential types, capped per run, and resumable (entities
already attempted are skipped), since each is a live lookup.
"""
import json
import re
import urllib.parse

from fraudscan import http_util, storage

NLM_IDV = "https://clinicaltables.nlm.nih.gov/api/npi_idv/v3/search"

# credential types that bill Medicare / prescribe (worth an NPI lookup)
PHYSICIAN_TYPES = (
    "physician and surgeon", "osteopathic", "podiatric", "dentist", "dental",
    "optometr", "advanced registered nurse", "nurse practitioner",
    "physician assistant", "chiropract", "psychologist", "pharmacist", "naturopath",
)


def _toks(s):
    return {t for t in re.findall(r"[A-Za-z]+", (s or "").upper()) if len(t) > 1}


def _extract_license(raw, state="WA"):
    """Pull the provider's license number from the NLM `licenses` field (a JSON object
    or array), preferring the primary license in `state`. NPPES carries this; it lets us
    confirm the NPI against the DOH credential number — a hard identity check."""
    if not raw:
        return ""
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        # multi-license providers come back as CONCATENATED objects ('{...},{...}'),
        # which is not valid JSON on its own — wrap as a list and retry.
        try:
            data = json.loads("[" + raw + "]")
        except (ValueError, TypeError):
            return ""
    items = data if isinstance(data, list) else [data]
    best = ""
    for it in items:
        if not isinstance(it, dict):
            continue
        num = (it.get("lic_number") or "").strip()
        if not num:
            continue
        st = (it.get("lic_state") or "").upper()
        if st == state.upper() and it.get("is_primary_taxonomy") == "Y":
            return num                       # primary in-state license — best
        if st == state.upper() and not best:
            best = num
        elif not best:
            best = num
    return best


def _extract_all_licenses(raw):
    """EVERY license number on the NPI, ';'-joined. A provider's WA license can hide
    behind an out-of-state primary (Bolling audit: Idaho primary masked the WA license,
    downgrading identity from license-confirmed to dob-confirmed)."""
    if not raw:
        return ""
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        try:
            data = json.loads("[" + raw + "]")
        except (ValueError, TypeError):
            return ""
    items = data if isinstance(data, list) else [data]
    nums = []
    for it in items:
        if isinstance(it, dict):
            num = (it.get("lic_number") or "").strip()
            if num and num not in nums:
                nums.append(num)
    return ";".join(nums)


def _lookup_npi_detail(name, state="WA"):
    """{npi, name, city, taxonomy} for a name+state ONLY if the match is unique. If
    several WA providers share the name we can't confirm identity (namesake risk) →
    return None. Precision over recall: this NPI drives money attribution + exclusion
    confirmation, and the practice city lets us later corroborate it against the DOH
    credential city (catching the wrong-namesake case)."""
    params = [("terms", name), ("q", f"addr_practice.state:{state}"), ("count", 50),
              ("df", "NPI,name.full,addr_practice.state,addr_practice.city,"
                     "provider_type,licenses")]
    try:
        data = http_util.get_json(NLM_IDV + "?" + urllib.parse.urlencode(params))
    except Exception:
        return None
    rows = data[3] if len(data) > 3 and data[3] else []
    want = _toks(name)
    matches, by_npi = [], {}
    for r in rows:
        npi, full = r[0], r[1]
        st = r[2] if len(r) > 2 else ""
        if want and want <= _toks(full) and (st or "").upper() == state.upper():
            matches.append(npi)
            by_npi[npi] = {"npi": npi, "name": full,
                           "city": r[3] if len(r) > 3 else "",
                           "taxonomy": r[4] if len(r) > 4 else "",
                           "license": _extract_license(r[5] if len(r) > 5 else "",
                                                       state),
                           "licenses": _extract_all_licenses(
                               r[5] if len(r) > 5 else "")}
    uniq = list(dict.fromkeys(matches))
    return by_npi[uniq[0]] if len(uniq) == 1 else None


def _lookup_npi(name, state="WA"):
    """Back-compat: just the NPI string (or None)."""
    d = _lookup_npi_detail(name, state)
    return d["npi"] if d else None


def _is_billing_type(etype):
    el = (etype or "").lower()
    return any(k in el for k in PHYSICIAN_TYPES)


def refresh_detail(conn, state="WA", cap=100000, progress=None):
    """Backfill NPPES practice city/taxonomy for already-resolved NPIs that predate the
    detail columns (powers identity corroboration). Re-looks-up by name+state."""
    todo = storage.crosswalk_missing_detail(conn)
    rows, n = [], 0
    for uid, name in todo.items():
        if n >= cap:
            break
        d = _lookup_npi_detail(name, state)
        if d:
            rows.append((uid, d["npi"], d.get("name", ""), d.get("city", ""),
                         d.get("taxonomy", ""), d.get("license", ""),
                         d.get("licenses", "")))
        n += 1
        if progress:
            progress(n)
    if rows:
        storage.upsert_crosswalk(conn, rows)
    return {"attempted": n, "updated": len(rows)}


def build_crosswalk(conn, state="WA", cap=500, progress=None):
    """Resolve NPIs for physician/prescriber-type healthcare entities (resumable)."""
    done = storage.crosswalk_done_uids(conn)
    rows, n = [], 0
    for r in conn.execute(
            "SELECT uid, name, entity_type FROM entities WHERE source='healthcare'"):
        if r["uid"] in done or not _is_billing_type(r["entity_type"]):
            continue
        if n >= cap:
            break
        d = _lookup_npi_detail(r["name"], state)
        rows.append((r["uid"], (d or {}).get("npi", ""), (d or {}).get("name", ""),
                     (d or {}).get("city", ""), (d or {}).get("taxonomy", ""),
                     (d or {}).get("license", ""), (d or {}).get("licenses", "")))
        n += 1
        if progress:
            progress(n)
    storage.upsert_crosswalk(conn, rows)
    found = sum(1 for r in rows if r[1])
    return {"attempted": len(rows), "found": found}
