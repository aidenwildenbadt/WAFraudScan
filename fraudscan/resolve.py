"""Cross-source entity resolution.

Clusters entities that resolve to the same real-world *operator* and surfaces those
that are unusual across programs or consolidate several registrations. Matching is
namespaced and explainable:

  org:<name>      child-care business name / DBA  <->  contract vendor name / DBA
  person:<name>   child-care primary contact      <->  health-care credential holder
  addr:<address>  same physical street address (sources with real addresses)
  email:/phone:   shared contact within child care
  + fuzzy business-name matching (typos / abbreviations / spacing) via token blocking

A name shared by more entities than `max_key_members` is treated as generic/chain and
skipped (logged), so national chains and common names don't form giant blobs. Fuzzy
matching is limited to business names and to entities that share a substantial word
(blocking), keeping it tractable and precise.

Every link is a LEAD to verify (names and addresses collide); the operator view shows
all members so a human can confirm.
"""
import datetime
import difflib
import hashlib
import math
import re
from collections import defaultdict

_TODAY_YEAR = datetime.date.today().year

from fraudscan.registry import normalize
from fraudscan.rules.base import norm_email, norm_phone, norm_text
from fraudscan.legitimacy import is_institutional

DEFAULT_MAX_KEY_MEMBERS = 8
DEFAULT_FUZZY_THRESHOLD = 0.9
DEFAULT_FUZZY_MAX_BLOCK = 40
DEFAULT_GEO_RADIUS_M = 50.0
DEFAULT_GEO_MAX_CELL = 15

# Coarse Washington bounding box — reject obviously bad coordinates (incl. 0,0).
_WA_BOUNDS = (45.0, 49.5, -125.0, -116.0)  # lat_min, lat_max, lon_min, lon_max
_M_PER_DEG_LAT = 111_320.0

# Sources that carry a real street address default to child care; callers pass the
# full set (e.g. + CMS facility categories). Sources whose "name" is a person, not an
# organization, are excluded from org-name / fuzzy matching.
DEFAULT_ADDRESS_SOURCES = frozenset({"childcare"})
PERSON_NAME_SOURCES = frozenset({"healthcare"})

# USPS-ish standardization so "123 Main Street NE" == "123 MAIN ST NE".
_ABBR = {
    "STREET": "ST", "AVENUE": "AVE", "ROAD": "RD", "DRIVE": "DR",
    "BOULEVARD": "BLVD", "LANE": "LN", "COURT": "CT", "PLACE": "PL",
    "HIGHWAY": "HWY", "PARKWAY": "PKWY", "TERRACE": "TER", "CIRCLE": "CIR",
    "NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W",
    "NORTHEAST": "NE", "NORTHWEST": "NW", "SOUTHEAST": "SE", "SOUTHWEST": "SW",
    "SUITE": "STE", "APARTMENT": "APT", "BUILDING": "BLDG", "UNIT": "UNIT",
}


def _person_key(name):
    toks = [t for t in norm_text(name).split() if len(t) > 1]
    if len(toks) < 2:
        return None
    return "person:" + " ".join(sorted((toks[0], toks[-1])))


# Generic-word + brand logic lives in fraudscan.brand (shared with the childcare rules):
# a name made up ENTIRELY of generic words ("Early Learning Center") must not form an
# org-merge key, and names sharing one open BRAND root are one chain, not a hidden net.
from fraudscan.brand import (brand_key, open_brand, is_venue, distinctive_tokens,
                             strip_site_suffix,
                             is_generic_name as _is_generic_name)


def _org_keys(entity):
    keys = []
    for nm in (entity.name, entity.dba):
        k = normalize(nm)
        if not (k and len(k) >= 4 and not _is_generic_name(k)):
            continue
        # DBA-sourced keys need ≥2 distinctive tokens: a near-generic trade name like
        # 'Little Blessings Preschool' (1 distinctive token) glued a Vancouver parish
        # school into a Port Angeles cluster purely via its DBA field.
        if nm == entity.dba and nm != entity.name and len(distinctive_tokens(nm)) < 2:
            continue
        keys.append("org:" + k)
    return keys


def _norm_addr(entity):
    toks = [_ABBR.get(t, t) for t in
            norm_text(f"{entity.address} {entity.city} {entity.zip}").split()]
    return " ".join(toks)


def _addr_key(entity, address_sources):
    if entity.source not in address_sources or not entity.address:
        return None
    if not any(c.isdigit() for c in entity.address):   # need a real street number
        return None
    a = _norm_addr(entity)
    return "addr:" + a if len(a) >= 8 else None


def keys_for(entity, address_sources=DEFAULT_ADDRESS_SOURCES):
    """Exact match keys an entity contributes (see module docstring)."""
    src, keys = entity.source, []
    if src not in PERSON_NAME_SOURCES:          # org-named source
        keys += _org_keys(entity)
    if src == "childcare":
        pk = _person_key(entity.raw.get("primarycontactpersonname"))
        if pk:
            keys.append(pk)
        em = norm_email(entity.raw.get("primarycontactemail"))
        ph = norm_phone(entity.raw.get("primarycontactphonenumber"))
        if em:
            keys.append("email:" + em)
        if ph:
            keys.append("phone:" + ph)
    if src in PERSON_NAME_SOURCES:
        pk = _person_key(entity.name)
        if pk:
            keys.append(pk)
    ak = _addr_key(entity, address_sources)
    if ak:
        keys.append(ak)
    return keys


def _similar(a, b):
    if set(a.split()) == set(b.split()):     # same words, any order/spacing
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def _fuzzy_org_pairs(entities, threshold, max_block):
    """Near-duplicate business names, found by blocking on shared substantial words."""
    items = [(e.uid, normalize(e.name)) for e in entities
             if e.source not in PERSON_NAME_SOURCES and len(normalize(e.name)) >= 5
             and not _is_generic_name(normalize(e.name))]   # don't fuzzy-merge generics
    blocks = defaultdict(list)
    for uid, k in items:
        for tok in {t for t in k.split() if len(t) >= 5}:
            blocks[tok].append((uid, k))
    pairs, seen = [], set()
    for members in blocks.values():
        if not (2 <= len(members) <= max_block):
            continue
        for i in range(len(members)):
            u1, k1 = members[i]
            for j in range(i + 1, len(members)):
                u2, k2 = members[j]
                if u1 == u2 or k1 == k2:
                    continue
                pk = (u1, u2) if u1 < u2 else (u2, u1)
                if pk in seen:
                    continue
                seen.add(pk)
                if _similar(k1, k2) >= threshold:
                    pairs.append((u1, u2))
    return pairs


def _haversine_m(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6_371_000.0 * math.asin(math.sqrt(a))


def _geo_pairs(entities, radius_m, max_cell, address_sources):
    """Provider pairs whose geocodes are within radius_m, via grid bucketing so we
    only haversine same/neighboring cells (not every pair)."""
    lo_lat, hi_lat, lo_lon, hi_lon = _WA_BOUNDS
    pts = [(e.uid, e.lat, e.lon) for e in entities
           if e.source in address_sources and e.lat and e.lon
           and lo_lat <= e.lat <= hi_lat and lo_lon <= e.lon <= hi_lon]
    if not pts:
        return []
    cell_lat = radius_m / _M_PER_DEG_LAT
    cell_lon = radius_m / (_M_PER_DEG_LAT * math.cos(math.radians(47.5)))
    grid = defaultdict(list)
    for uid, lat, lon in pts:
        grid[(int(lat / cell_lat), int(lon / cell_lon))].append((uid, lat, lon))

    pairs, seen = [], set()
    for (ci, cj), members in grid.items():
        cand = []
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                cand.extend(grid.get((ci + di, cj + dj), ()))
        if len(cand) > max_cell:          # dense block -> likely legit density, skip
            continue
        for ua, la, loa in members:
            for ub, lb, lob in cand:
                if ua == ub:
                    continue
                pk = (ua, ub) if ua < ub else (ub, ua)
                if pk in seen:
                    continue
                seen.add(pk)
                if _haversine_m(la, loa, lb, lob) <= radius_m:
                    pairs.append(pk)
    return pairs


_ROUTINE_CONTRACT = ("DATA SHAR", "DATA-SHAR", "MEMORANDUM OF UNDERSTANDING",
                     "ORGANIZATIONAL LICENSE", "COMPUTER AND INFORMATION SERVICES",
                     "NON-FINANCIAL", "NURSING FACILITY SERVICES")


def _substantive_contract(m):
    """True when a contract member represents real procurement money — not a $0
    data-sharing agreement, MOU/organizational-license disclosure, or the standard
    DSHS nursing-facility Medicaid payment rail (fleet audit F9)."""
    if (m.amount or 0) <= 0:
        return False
    txt = " ".join(str(m.raw.get(k) or "") for k in
                   ("procurement_type", "purpose_of_the_contract",
                    "purpose_of_the_contract_1")).upper()
    txt += " " + str(m.status or "").upper()
    return not any(t in txt for t in _ROUTINE_CONTRACT)


def _signals_and_bonus(sources, members, colocated_addr, fuzzy_names,
                       geo_proximate, geo_radius, name_counts=None, adverse=False,
                       national_count=None, franchise=None):
    """Structure signals (cross-program, co-location, fuzzy variants, proximity) mostly
    describe HOW records were grouped — they are facts about organization shape, which
    is usually legitimate (chains, nonprofits, hosted programs). They earn their full
    score bonus only when the cluster ALSO carries adverse evidence (a barred/sanctioned
    member); otherwise they are scaled down to context. The healthcare-bridge bonus is
    exempt — its premise IS the adverse credential."""
    s = set(sources)
    name_counts = name_counts or {}
    bridge_bonus = 0
    # F9: cross-program presence only counts when the contract side is SUBSTANTIVE.
    # $0 data-sharing agreements (1,691 orgs hold one), organizational-license MOUs
    # (disclosure of multi-site control), and the standard DSHS nursing-facility
    # Medicaid payment contract are routine rails, not cross-program risk.
    routine_note = ""
    if "contracts" in s:
        cmembers = [m for m in members if m.source.startswith("contracts")]
        if cmembers and not any(_substantive_contract(m) for m in cmembers):
            s = s - {"contracts"}
            routine_note = (f"Holds {len(cmembers)} routine state agreement(s) "
                            f"(data-sharing / MOU / organizational license / standard "
                            f"NF Medicaid contract) — context, no risk weight")
    if len(s) >= 2:
        signals = [f"Present in {len(s)} programs: {', '.join(sorted(s))}"]
        bonus = 10
    else:
        signals = [f"{len(members)} linked registrations in {sorted(s or set(sources))[0]}"]
        bonus = 0
    if routine_note:
        signals.append(routine_note)
    if "childcare" in s and "healthcare" in s:
        hc = [m for m in members if m.source == "healthcare"]
        hc_pk = {_person_key(m.name) for m in hc if _person_key(m.name)}
        names = sorted({m.name for m in hc})
        hc_surnames = {norm_text(m.name).split()[-1] for m in hc
                       if len(norm_text(m.name).split()) >= 2
                       and len(norm_text(m.name).split()[-1]) >= 4}
        # staleness: the most recent credential year among bridged hc members — a
        # credential dead 16 years bridging to an active daycare is near-noise (Askew)
        hc_years = [int(mm.group()) for m in hc
                    for mm in [re.search(r"(19|20)\d\d",
                               str(m.raw.get("expirationdate") or ""))] if mm]
        years_dead = (_TODAY_YEAR - max(hc_years)) if hc_years else 0
        # G6(v): refuting discriminators that already sit in the data —
        #   middle names that DIFFER (Kyong EUN Lee vs Kyong SUK Lee = two people)
        #   credential-holder age vs facility license age (a holder who would have
        #   been a child when the daycare was licensed cannot be its operator)
        hc_mids = {norm_text(m.raw.get("middlename") or "") for m in hc} - {""}
        hc_birth = [int(mm.group()) for m in hc
                    for mm in [re.search(r"(19|20)\d\d",
                               str(m.raw.get("birthyear") or ""))] if mm]
        refuted = []
        # which child-care site carries the matching contact (the actual bridge)?
        bridges, email_mismatch = [], False
        for m in members:
            if m.source == "childcare":
                contact = m.raw.get("primarycontactpersonname")
                if _person_key(contact) in hc_pk:
                    em = (m.raw.get("primarycontactemail") or "")
                    local = norm_text(em.split("@")[0]).replace(" ", "")
                    if local and hc_surnames and not any(sn in local
                                                         for sn in hc_surnames):
                        email_mismatch = True   # rosaward4@… vs surname SANDOVAL
                    ct = norm_text(contact).split()
                    c_mids = {t for t in ct[1:-1] if len(t) > 1}
                    if (hc_mids and c_mids
                            and not (hc_mids & c_mids)
                            and not any(a[0] == b[0] for a in hc_mids
                                        for b in c_mids)):
                        refuted.append(f"middle names differ "
                                       f"({'/'.join(sorted(c_mids))} vs "
                                       f"{'/'.join(sorted(hc_mids))})")
                    lic_start = next((int(mm.group()) for k in
                                      ("licenseeffectivedate", "firstissuedate")
                                      for mm in [re.search(r"(19|20)\d\d",
                                                 str(m.raw.get(k) or ""))] if mm), None)
                    if hc_birth and lic_start and lic_start - max(hc_birth) < 16:
                        refuted.append(
                            f"credential holder (b.{max(hc_birth)}) would have been "
                            f"{max(0, lic_start - max(hc_birth))} when the facility "
                            f"was licensed ({lic_start}) — cannot be its operator")
                    bridges.append(f"{m.name} (contact {contact}"
                                   + (f", {em}" if em else "") + ")")
        via = (" — via " + "; ".join(sorted(set(bridges))[:2])) if bridges else ""
        # commonness measured on the BRIDGE KEY (first+last), not the display name —
        # 'KELLY SHANLEY JOHNSON' bridges as 'KELLY JOHNSON' (415 NPI namesakes), so
        # querying the full name silently under-counted (fleet audit: Johnson).
        def _bq(n):
            return (_person_key(n) or "person:").split(":", 1)[1].strip()
        in_corpus = max((name_counts.get(_person_key(n), 1) for n in names), default=1)
        natl = max(((national_count(_bq(n)) or 0) for n in names if _bq(n)),
                   default=0) if national_count else 0
        partial = sorted({t for n in names for t in norm_text(n).split()[1:-1]
                          if len(t) > 1})
        if in_corpus > 2 or natl > 5:
            bridge_bonus = 10
            why_common = (f"{natl} providers with this first+last name nationally"
                          if natl > 5 else f"{in_corpus}× in WA data")
            signals.append(f"Child-care contact shares a name with a sanctioned health-"
                           f"care credential ({', '.join(names[:3])}){via} — ⚠ COMMON "
                           f"NAME ({why_common}), confirm identity first")
        else:
            bridge_bonus = 25
            signals.append(f"Child-care operator name-linked to a sanctioned health-care "
                           f"credential: {', '.join(names[:3])}{via} — name-only, verify "
                           f"identity")
        if partial:
            signals.append(f"Bridge matched on first+last name only — middle token(s) "
                           f"'{', '.join(partial[:2])}' were not present on the "
                           f"child-care side")
        if email_mismatch:
            bridge_bonus = round(bridge_bonus * 0.5)
            signals.append("⚠ Bridge site's contact email does not contain the "
                           "credential surname — weakens the same-person inference")
        if years_dead > 10:
            bridge_bonus = round(bridge_bonus * 0.25)
            signals.append(f"Bridged credential has been inactive ~{years_dead} years — "
                           f"staleness-discounted (an old lapsed license rarely "
                           f"explains current operations)")
        elif years_dead > 5:
            bridge_bonus = round(bridge_bonus * 0.5)
            signals.append(f"Bridged credential inactive ~{years_dead} years — "
                           f"staleness-discounted")
        if refuted:
            bridge_bonus = 0
            signals.append("⚠ Bridge REFUTED by record discriminators: "
                           + "; ".join(sorted(set(refuted))[:2])
                           + " — treat the adverse member as a namesake")
    # open-brand check: a chain whose sites all advertise one brand root is not hiding
    # common control — the structure signals below exist for the HIDDEN version, so they
    # are discounted when the brand is open (case study: Martha & Mary, Poulsbo).
    # DBAs count as brand candidates (Sullivan Park: the contract names the facility).
    ob = open_brand([n for m in members if m.source != "healthcare"
                     for n in (m.name, m.dba) if n])
    if not ob and franchise:
        # F11c: corpus-level franchise prior — 'KIDDIE ACADEMY OF <city>' sites cluster
        # city-by-city so one cluster may miss the share threshold, but the brand is on
        # 10+ WA sites with 3+ distinct contacts: an open franchise, not a hidden net.
        keys = [k for m in members if m.source != "healthcare"
                for k in [brand_key(m.name) or brand_key(m.dba)] if k]
        if keys:
            top = max(set(keys), key=keys.count)
            if top in franchise and keys.count(top) / len(keys) >= 0.5:
                ob = top
    if ob:
        signals.append(f"Openly-branded multi-site organization ('{ob}') — chain-"
                       f"structure links are expected and discounted; hidden control "
                       f"(different brands, shared IDs/contacts) is what scores high")
    if "childcare" in s and "contracts" in s:
        signals.append("Appears as both a licensed child-care provider and a state "
                       "contractor" + (" — same open brand on both sides, common for "
                                       "established nonprofits" if ob else ""))
        bonus += 4 if ob else 15
    if colocated_addr:
        if ob:
            signals.append(f"Co-located registrations at one address ({colocated_addr})"
                           f" — all under the open '{ob}' brand (HQ + branches)")
            bonus += 5
        else:
            signals.append("Multiple differently-named registrations at one address: "
                           + colocated_addr)
            bonus += 18
    if fuzzy_names:
        signals.append("Name variants linked by fuzzy match: "
                       + ", ".join(sorted(fuzzy_names)[:4])
                       + (" — variants of one open brand" if ob else ""))
        bonus += 2 if ob else 8
    if geo_proximate:
        signals.append(f"Geocode-proximate: registrations within ~{int(geo_radius)}m "
                       f"of each other under "
                       + (f"the open '{ob}' brand" if ob else
                          "different names and addresses"))
        bonus += 4 if ob else 16
    # structure alone isn't risk: without an adverse member, structural bonuses are
    # context, not score — they describe an organization's shape, which is usually legal
    if not adverse and bonus > 0:
        bonus = round(bonus * 0.3)
        signals.append("No barred/sanctioned member — structure links shown as context, "
                       "scored low until adverse evidence appears")
    return signals, bonus + bridge_bonus


def _identity_signals_for(member_uids, id_groups, by_uid):
    """Signals + bonus for hard-identity links among an operator's members: WA-SOS
    business identity (UBI/agent/officer), shared CMS beneficial owner, and shared
    Medicare billing group (reassignment). Each is stronger than a name/address collision.
    'ubi'/'owner'/'reassign' link by an id even under one name; 'agent'/'gov' only count
    across DIFFERENT names (the shell signal)."""
    found = {"ubi": 0, "agent": 0, "gov": 0, "owner": 0}
    for k, uids in id_groups.items():
        shared = member_uids & uids
        if len(shared) < 2:
            continue
        kind = k.split(":", 1)[0]
        if kind not in found:
            continue
        # agent/gov links only count across different BRANDS — one chain's sites all
        # sharing the org's registered agent/officer is disclosure, not a shell network
        brands = {brand_key(by_uid[u].name) or normalize(by_uid[u].name)
                  for u in shared}
        if kind in ("agent", "gov") and len(brands) < 2:
            continue
        found[kind] = max(found[kind], len(shared))
    signals, bonus = [], 0
    if found["ubi"]:
        signals.append(f"Same WA SOS business identity (UBI) across {found['ubi']} "
                       f"registrations")
        bonus += 16
    if found["agent"]:
        signals.append("Shared WA registered agent across differently-named entities "
                       "(possible shell network)")
        bonus += 14
    if found["gov"]:
        signals.append("Shared governing person/officer across differently-named "
                       "entities (common control)")
        bonus += 12
    if found["owner"]:
        signals.append(f"Shared CMS beneficial owner across {found['owner']} facilities "
                       f"(ownership network)")
        bonus += 16
    return signals, bonus


def build_operators(entities, scores_by_uid, registry=None,
                    max_key_members=DEFAULT_MAX_KEY_MEMBERS,
                    fuzzy_threshold=DEFAULT_FUZZY_THRESHOLD,
                    fuzzy_max_block=DEFAULT_FUZZY_MAX_BLOCK,
                    geo_radius_m=DEFAULT_GEO_RADIUS_M,
                    geo_max_cell=DEFAULT_GEO_MAX_CELL,
                    address_sources=DEFAULT_ADDRESS_SOURCES,
                    institutional_suppression=0.5, sos=None, extra_keys=None,
                    member_facts=None, name_counts=None, national_count=None):
    address_sources = frozenset(address_sources)
    # F11c: corpus-level franchise prior — a brand root carried by >=10 WA sites with
    # >=3 distinct contact people is an open franchise/large chain, even when a single
    # cluster's share misses the open-brand threshold (Kiddie Academy city-clusters).
    fr_sites, fr_contacts = {}, {}
    for e in entities:
        if e.source != "childcare":
            continue
        b = brand_key(e.name)
        if not b:
            continue
        fr_sites[b] = fr_sites.get(b, 0) + 1
        c = norm_text(e.raw.get("primarycontactpersonname") or "")
        if c:
            fr_contacts.setdefault(b, set()).add(c)
    franchise = {b for b, n in fr_sites.items()
                 if n >= 10 and len(fr_contacts.get(b, ())) >= 3}
    parent = {}

    def find(x):
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:
            parent[x], x = root, parent[x]
        return root

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    from fraudscan.sos import sos_keys as _sos_keys
    extra_keys = extra_keys or {}
    key_members = defaultdict(set)
    for e in entities:
        parent.setdefault(e.uid, e.uid)
        for k in keys_for(e, address_sources):
            key_members[k].add(e.uid)
        if sos is not None:
            sks, _rec = _sos_keys(e, sos)
            for k in sks:
                key_members[k].add(e.uid)
        for k in extra_keys.get(e.uid, ()):       # owner: links (see note below)
            # reassignment ("reassigns billing to a common group") is intentionally NOT
            # a merge edge: it chains unrelated providers through large hospital billing
            # groups into false mega-clusters. Shared employer != shared fraud operator.
            if k.startswith("reassign:"):
                continue
            key_members[k].add(e.uid)

    _ID_PREFIXES = ("ubi:", "agent:", "gov:", "owner:")
    skipped, addr_groups, id_groups = [], {}, {}
    for k, uids in key_members.items():
        if len(uids) < 2:
            continue
        if len(uids) > max_key_members:
            skipped.append((k, len(uids)))
            continue
        it = iter(uids)
        first = next(it)
        for other in it:
            union(first, other)
        if k.startswith("addr:"):
            addr_groups[k] = set(uids)
        elif k.startswith(_ID_PREFIXES):
            id_groups[k] = set(uids)

    fuzzy_uids = set()
    for a, b in _fuzzy_org_pairs(entities, fuzzy_threshold, fuzzy_max_block):
        union(a, b)
        fuzzy_uids.update((a, b))

    geo_uids = set()
    for a, b in _geo_pairs(entities, geo_radius_m, geo_max_cell, address_sources):
        union(a, b)
        geo_uids.update((a, b))

    groups = defaultdict(list)
    for e in entities:
        groups[find(e.uid)].append(e)

    by_uid = {e.uid: e for e in entities}
    operators = []
    for members in groups.values():
        # F9: the SAME contract re-appears across fiscal-year snapshot datasets, often
        # with different zero-padding ('1300' vs '000001300') — keep only the newest
        # snapshot row per (vendor, contract #) so member counts aren't inflated.
        seen_contracts, deduped = set(), []
        for m in sorted(members, key=lambda m: m.source, reverse=True):
            if m.source.startswith("contracts"):
                ck = (normalize(m.name), str(m.source_id).lstrip("0"))
                if ck in seen_contracts:
                    continue
                seen_contracts.add(ck)
            deduped.append(m)
        members = deduped
        # year-series sources (contracts, contracts_2023, ...) are ONE program — the same
        # vendor across fiscal-year snapshots must not read as "present in 4 programs"
        sources = sorted({("contracts" if m.source.startswith("contracts") else m.source)
                          for m in members})
        member_uids = {m.uid for m in members}
        names = {normalize(m.name) for m in members if m.name}

        # co-located: a shared address key with 2+ distinct BRANDS among these members.
        # Same-brand co-location (HQ + branch of one open chain) is expected, and a
        # provider sharing its HOST venue's address (program at a community center /
        # school / church) is a hosting arrangement — neither is the hidden-network
        # pattern this signal exists for, so venue-named members don't count.
        colocated_addr = None
        for k, uids in addr_groups.items():
            shared = member_uids & uids
            if len(shared) >= 2:
                brands = {brand_key(by_uid[u].name) or normalize(by_uid[u].name)
                          for u in shared
                          if not is_venue(by_uid[u].name, by_uid[u].dba)}
                if len(brands) >= 2:
                    colocated_addr = ", ".join(
                        {by_uid[u].address for u in shared if by_uid[u].address})[:80]
                    break

        fuzzy_members = member_uids & fuzzy_uids
        fuzzy_names = ({by_uid[u].name for u in fuzzy_members}
                       if len(fuzzy_members) >= 2 and len(names) >= 2 else set())

        # geocode-proximate: linked by distance, with distinct names AND addresses
        # (so it adds beyond the exact-address key)
        geo_members = member_uids & geo_uids
        geo_proximate = (
            len(geo_members) >= 2
            and len({normalize(by_uid[u].name) for u in geo_members}) >= 2
            and len({_norm_addr(by_uid[u]) for u in geo_members}) >= 2)

        id_signals, id_bonus = _identity_signals_for(member_uids, id_groups, by_uid)

        # G7: a single facility plus its own $0 routine paperwork is NOT cross-program.
        # Compute the SUBSTANTIVE source set ONCE and use it everywhere (surfacing, the
        # link badge, cross_program) so the card never contradicts its own signals.
        cmembers = [m for m in members if m.source.startswith("contracts")]
        routine_only = bool(cmembers) and not any(_substantive_contract(m)
                                                  for m in cmembers)
        eff_sources = [s for s in sources if not (routine_only and s == "contracts")]

        # surface: cross-program (substantive), OR any single-program consolidation
        if not (len(eff_sources) >= 2 or colocated_addr or fuzzy_names or geo_proximate
                or id_signals):
            continue

        mf = member_facts or {}
        member_rows = sorted(({
            "uid": m.uid, "source": m.source, "name": m.name,
            "entity_type": m.entity_type, "status": m.status,
            "risk_score": scores_by_uid.get(m.uid, 0.0),
            "funds": (mf.get(m.uid) or {}).get("funds", 0.0),
            "barred": (mf.get(m.uid) or {}).get("barred", False),
            "sanctioned": (mf.get(m.uid) or {}).get("sanctioned", False),
            "identity": (mf.get(m.uid) or {}).get("identity", ""),
            "top_flag": (mf.get(m.uid) or {}).get("top_flag", ""),
        } for m in members), key=lambda r: (r["barred"], r["risk_score"], r["funds"]),
            reverse=True)

        # --- structured risk rollup (what makes the operator actionable) ---
        # PERSON-level dedup: one human with two suspended credentials (e.g. pharmacy
        # assistant + pharmacy tech, same name+birthyear) is ONE barred person, not two
        # (Rosa Sandoval audit: "2 barred members" were one woman counted twice).
        def _person(uid):
            m = by_uid[uid]
            pk = _person_key(m.name)
            if m.source == "healthcare" and pk:
                return (pk, str(m.raw.get("birthyear") or ""))
            return uid
        barred_members = len({_person(r["uid"]) for r in member_rows if r["barred"]})
        sanctioned_members = len({_person(r["uid"])
                                  for r in member_rows if r["sanctioned"]})
        # adverse members linked into the cluster ONLY by a person-name key (healthcare
        # credentials carry no address/email in our slice) are UNVERIFIED — they must
        # not unlock structure bonuses or disable institutional suppression, or an
        # unproven namesake supplies its own "adverse evidence" (Patricia Smith audit).
        verified_adverse = len({_person(r["uid"]) for r in member_rows
                                if (r["barred"] or r["sanctioned"])
                                and by_uid[r["uid"]].source != "healthcare"})
        name_bridged_adverse = (barred_members + sanctioned_members) - verified_adverse
        contradiction_amount = sum(
            (mf.get(m.uid) or {}).get("contradiction", 0.0) for m in members)
        dollars_at_stake = round(sum(r["funds"] for r in member_rows), 2)
        sanctioned_dollars = sum(r["funds"] for r in member_rows if r["sanctioned"])
        # G6(iv): name the operator after a VERIFIED barred member or the dominant
        # funded organization — never after an unverified namesake (fleet audit:
        # legitimate daycares were branded with strangers' names).
        top = member_rows[0] if member_rows else None
        org_rows = [r for r in member_rows
                    if by_uid[r["uid"]].source != "healthcare"]
        funded = max(org_rows, key=lambda r: r["funds"], default=None)
        if top and top["barred"] and by_uid[top["uid"]].source != "healthcare":
            canonical = top["name"]
        elif funded and funded["funds"] > 0:
            canonical = funded["name"]
        elif org_rows:
            canonical = max((r["name"] for r in org_rows), key=len)
        else:
            canonical = max((m.name for m in members), key=len, default="")

        # F16: a contract whose DBA field literally names another member is a DOCUMENTED
        # link (the vendor disclosed it) — stronger than fuzzy, no 'confirm same
        # operator' caveat needed (fleet audit: Sullivan Park's 'fuzzy' was exact).
        member_names = {strip_site_suffix(normalize(m.name))
                        for m in members if m.name}
        dba_link = any(
            m.source.startswith("contracts") and m.dba
            and strip_site_suffix(normalize(m.dba)) in member_names
            and normalize(m.dba) != normalize(m.name)
            for m in members)
        # strongest linking evidence (hard id > dba > shell > address > geo > fuzzy)
        if any("UBI" in s or "beneficial owner" in s for s in id_signals):
            strongest_link = "hard"
        elif dba_link:
            strongest_link = "dba"
        elif any("registered agent" in s or "governing person" in s for s in id_signals):
            strongest_link = "shell"
        elif colocated_addr:
            strongest_link = "address"
        elif geo_proximate:
            strongest_link = "geo"
        elif fuzzy_names:
            strongest_link = "fuzzy"
        else:
            strongest_link = "cross-program" if len(eff_sources) >= 2 else "name"

        signals, bonus = _signals_and_bonus(
            sources, members, colocated_addr, fuzzy_names, geo_proximate,
            geo_radius_m, name_counts=name_counts,
            adverse=verified_adverse > 0,
            national_count=national_count, franchise=franchise)
        signals += id_signals
        # G13: hard-id links (UBI/owner) describe corporate structure — without an
        # adverse member they are context like the other structure signals, and were
        # bypassing the no-adverse ×0.3 (EmpRes/Gardens/Horizon double-counts).
        if verified_adverse == 0 and id_bonus > 0:
            id_bonus = round(id_bonus * 0.3)
        bonus += id_bonus
        if dba_link:
            signals.append("Contract DBA field names a member facility — a documented "
                           "vendor disclosure (hard link, no name-collision risk)")
        if name_bridged_adverse > 0 and verified_adverse == 0:
            signals.append("⚠ No identity-VERIFIED barred/sanctioned member — the "
                           "adverse member(s) connect by PERSON-NAME only; verify "
                           "identity before relying on this cluster's score")
        # SCORE by the worst single member + structured bonuses — NOT the sum of members
        # (summing just rewards size and pins every big cluster at 100).
        base = member_rows[0]["risk_score"] if member_rows else 0.0
        # G6(i): an unverified person-name bridge must not import the namesake's full
        # risk score as the cluster's base — cap it until identity verifies (Askew:
        # 30 of 39 points were a dead credential's undiscounted base).
        if (verified_adverse == 0 and name_bridged_adverse > 0 and member_rows
                and by_uid[member_rows[0]["uid"]].source == "healthcare"):
            base = min(base, 15.0)
        risk_bonus = 0
        if contradiction_amount > 0:
            risk_bonus += 30                       # a dated paid-after-barred member
        if barred_members >= 1 and sanctioned_dollars > 0:
            risk_bonus += 15                       # a barred member who drew public $
        if barred_members >= 2:
            risk_bonus += min(12, 4 * (barred_members - 1))
        combined = min(100.0, base + bonus + risk_bonus)
        # suppress obvious institutions; also suppress a cluster that is institutional by
        # any member when it has no VERIFIED barred member (a name-only adverse bridge
        # must not strip a YMCA/parish program of suppression — that's circular)
        # F11: NEVER suppress a cluster carrying verified adverse evidence or after-bar
        # dollars — the audit found a genuine IJ-cited SNF chain halved because its
        # street name ('Gardens on University') matched the institutional regex.
        inst = (verified_adverse == 0 and contradiction_amount == 0 and
                (is_institutional(canonical)
                 or any(is_institutional(
                        m.name, email=m.raw.get("primarycontactemail"))
                        for m in members)))
        if inst:
            combined *= institutional_suppression
            signals.append("Likely a legitimate institution / program (score suppressed)")
        # large clean network (no barred member, no contradiction) → likely a chain /
        # hospital system, not a fraud operator
        if len(members) >= 20 and barred_members == 0 and contradiction_amount == 0:
            combined *= 0.4
            signals.append("Large single-program network with no barred member — likely "
                           "legitimate; review only if a member becomes sanctioned")
        # F12: a name-only adverse bridge with NO public dollars anywhere and NO
        # identity-verified adverse member is a verify-identity task, not a top lead —
        # the audit found $0 clusters at ranks #1/#3/#4 above a $23.3M IJ-cited chain.
        if (dollars_at_stake == 0 and verified_adverse == 0
                and contradiction_amount == 0 and combined > 45):
            combined = 45.0
            signals.append("No public $ attributed and no identity-verified adverse "
                           "member — capped at 45 and routed to identity verification")
        op_id = "op:" + hashlib.md5(
            "|".join(sorted(member_uids)).encode()).hexdigest()[:12]
        operators.append({
            "operator_id": op_id,
            "canonical_name": canonical,
            "sources": sources,
            "source_count": len(sources),
            "member_count": len(members),
            "combined_score": round(combined, 1),
            "signals": signals,
            "registry_status": (registry.best_match(canonical)["status"]
                                if registry is not None else None),
            "barred_members": barred_members,
            "sanctioned_members": sanctioned_members,
            "contradiction_amount": round(contradiction_amount, 2),
            "dollars_at_stake": dollars_at_stake,
            "strongest_link": strongest_link,
            "cross_program": len(eff_sources) >= 2,
            "name_bridged_adverse": name_bridged_adverse,
            "verified_adverse": verified_adverse,
            "members": member_rows,
        })
    operators.sort(key=lambda o: (o["contradiction_amount"] > 0, o["combined_score"],
                                  o["barred_members"], o["dollars_at_stake"]),
                   reverse=True)
    return operators, skipped
