"""Attach dollar amounts + time periods to entities, where public data exists.

The honest landscape (see README): per-provider PAYMENT data is mostly NOT public.
  - WA child-care subsidy (WCCC) and Apple Health/Medicaid payments -> not open; a
    public-records request to DCYF/HCA is required.
  - Federal MEDICARE payments ARE public per-provider per-year (data.cms.gov), but only
    cover Medicare-billing providers above a threshold.
  - WA agency CONTRACT amounts + effective dates we already hold.

So this layer surfaces what's genuinely public: contract dollars (full coverage for the
contracts source) and Medicare DME payments joined by NPI (partial — only suppliers that
bill Medicare). Everything else is labeled as a records-request gap in the UI/README.
"""
from fraudscan import http_util


def _money(v):
    if v in (None, "", "NULL"):
        return None
    try:
        return float(str(v).replace("$", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _resolve_key(rec, name):
    """CMS changed field-name casing across years (e.g. PRSCRBR_NPI in 2023 vs
    Prscrbr_NPI in 2021). Resolve the configured field to the record's actual key,
    case-insensitively, so multi-year pulls don't silently drop rows."""
    if name in rec:
        return name
    low = name.lower()
    for k in rec:
        if k.lower() == low:
            return k
    return name


def contract_payments(entities):
    """WA agency contracts: amount + effective date range (already in hand).

    The Agency Contracts series is one snapshot per fiscal year (contracts, contracts_2023,
    ...), and a multi-year contract appears in EVERY snapshot with its full cost — so we
    de-duplicate by (contract no, vendor) and keep only the NEWEST snapshot's row, never
    summing the same contract across years."""
    best = {}
    for e in entities:
        if not e.source.startswith("contracts") or e.amount is None:
            continue
        key = (e.source_id, (e.name or "").upper())
        prev = best.get(key)
        # source keys sort chronologically: contracts < contracts_2023 < ... < contracts_2025
        if prev is None or e.source > prev.source:
            best[key] = e
    rows = []
    for e in best.values():
        start = (e.raw.get("contract_effective_start") or "")[:10]
        end = (e.raw.get("contract_effective_end_date") or "")[:10]
        fy = "FY20" + e.source.rsplit("_", 1)[1][-2:] if "_" in e.source else "FY2022"
        period = f"{start} → {end}".strip(" →") or fy
        rows.append({"entity_uid": e.uid, "program": "WA agency contract",
                     "period": period, "amount": e.amount,
                     "source_url": e.source_url})
    return rows


NPI_SOURCES = {"aba", "nemt", "dme"}     # source_id IS the NPI


def _entity_npi(e, npi_map):
    if e.source in NPI_SOURCES:
        return e.source_id
    return npi_map.get(e.uid)             # crosswalked NPI for healthcare, etc.


def medicare_payments(entities, pcfg, npi_map=None, progress=None):
    """Medicare payments joined by NPI for one configured payment source.

    join_source may be a single source key or a list; NPIs come from source_id for
    NPI-native sources or from the crosswalk (npi_map) for others.
    """
    npi_map = npi_map or {}
    js = pcfg["join_source"]
    join_sources = set(js if isinstance(js, list) else [js])
    npi_field = pcfg["npi_field"]
    amt_field = pcfg["amount_field"]
    state_field = pcfg.get("state_field")
    state = pcfg.get("state", "WA")
    program = pcfg.get("program", "Medicare")
    our_npis = {}
    for e in entities:
        if e.source in join_sources:
            npi = _entity_npi(e, npi_map)
            if npi:
                our_npis[str(npi)] = e
    if not our_npis:
        return []
    rows = []
    for year, uuid in sorted(pcfg.get("years", {}).items(), reverse=True):
        filters = {state_field: state} if state_field else {}
        npi_k = amt_k = None
        for rec in http_util.cms_dataapi_fetch(uuid, filters=filters,
                                               progress=progress):
            if npi_k is None:  # resolve actual key casing once per year
                npi_k, amt_k = _resolve_key(rec, npi_field), _resolve_key(rec, amt_field)
                # the data-api silently IGNORES a wrong-cased filter key and returns
                # the unfiltered nation — verify the filter actually applied before
                # ingesting (a fleet agent hit this and got a stranger's data)
                if state_field:
                    sk = _resolve_key(rec, state_field)
                    got = str(rec.get(sk, "")).upper()
                    if got and got != state.upper():
                        print(f"  WARNING {program} {year}: state filter not applied "
                              f"by API (got {got!r}, wanted {state!r}) — skipping year")
                        break
            npi = str(rec.get(npi_k) or "")
            ent = our_npis.get(npi)
            amt = _money(rec.get(amt_k))
            if ent is not None and amt:
                rows.append({"entity_uid": ent.uid, "program": program,
                             "period": year, "amount": amt,
                             "source_url": ent.source_url})
    return rows


def build_payments(config, all_entities, npi_map=None, progress=None):
    rows = contract_payments(all_entities)
    for name, pcfg in config.get("payments", {}).items():
        if not pcfg.get("enabled", True):
            continue
        rows.extend(medicare_payments(all_entities, pcfg, npi_map=npi_map,
                                      progress=progress))
    # Medicaid (HCPCS, T-MSIS) from the cached HHS extract, if 'medicaid-spending' was run
    try:
        from fraudscan.medicaid_spending import load_cache, medicaid_payments
        agg = load_cache()
        if agg:
            rows.extend(medicaid_payments(all_entities, agg, npi_map=npi_map))
    except Exception:
        pass
    # WA state Open Checkbook (vendor-name match), if 'state-checkbook' was run
    try:
        from fraudscan.state_checkbook import load_cache as _ck, checkbook_payments
        ck = _ck()
        if ck:
            rows.extend(checkbook_payments(all_entities, ck))
    except Exception:
        pass
    return rows
