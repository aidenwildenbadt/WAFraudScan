"""Cross-source rules — these need a shared context (e.g. the business registry),
so they take (entities, cfg, context) instead of the per-source (entities, cfg).
"""
import re

from fraudscan.rules.base import Flag
from fraudscan.registry import normalize


def _year(s):
    """Pull a 4-digit year from any date string (20220320, 11/10/2017, 2023…)."""
    m = re.search(r"(19|20)\d\d", str(s or ""))
    return int(m.group()) if m else None


def _city_key(s):
    return re.sub(r"[^A-Z]", "", (s or "").upper())


def _lic_core(s):
    """Comparable core of a license / credential number: digits with leading zeros
    stripped (DOH 'MD.MD.00028366' and NPPES '00028366' both -> '28366')."""
    return re.sub(r"\D", "", str(s or "")).lstrip("0")


# identity grades that mean the resolved NPI is almost certainly a DIFFERENT person —
# any flag derived from that NPI (money, NPI-exclusion, billing) is mis-attributed and
# must be heavily down-weighted so namesakes don't sit at score 100.
_MISMATCH = {"license-mismatch", "dob-mismatch", "taxonomy-mismatch"}

# profession family of a credential type / NPI taxonomy string. A Dentist credential
# whose "unique" NPI turns out to be an Emergency-Medicine physician is almost certainly
# a namesake (case study: Robert Parks — retired dentist vs working ER doc). Order
# matters: more specific keywords first (PHYSICIAN ASSISTANT before PHYSICIAN).
_FAMILY_KEYWORDS = [
    # trainees first: "STUDENT" contains the substring "DENT", so these must rank
    # above the dental keywords (which are spelled out precisely for the same reason —
    # "Independent Clinical Social Worker" also contains "DENT")
    ("STUDENT", "STUDENT"), ("RESIDEN", "STUDENT"),
    ("PHYSICIAN ASSISTANT", "PA"),
    ("DENTIST", "DENTAL"), ("DENTAL", "DENTAL"), ("DENTUR", "DENTAL"),
    ("ORAL & MAXILLO", "DENTAL"), ("ORAL AND MAXILLO", "DENTAL"),
    ("CHIROPRAC", "CHIRO"), ("OPTOMETR", "OPTOM"), ("PODIATR", "PODIATRY"),
    ("NATUROPATH", "NATUROPATH"), ("MIDWIFE", "MIDWIFE"), ("PHARMAC", "PHARMACY"),
    ("MASSAGE", "MASSAGE"), ("NURS", "NURSING"), ("PSYCHOL", "BEHAVIORAL"),
    ("COUNSEL", "BEHAVIORAL"), ("SOCIAL WORK", "BEHAVIORAL"),
    ("BEHAVIOR ANALYST", "BEHAVIORAL"), ("MARRIAGE", "BEHAVIORAL"),
    ("PHYSICAL THERAP", "PT"), ("OCCUPATIONAL THERAP", "OT"),
    ("SPEECH", "SLP"), ("DIETIT", "DIET"),
    ("OSTEOPATH", "PHYSICIAN"), ("PHYSICIAN", "PHYSICIAN"),
    ("EMERGENCY MEDICINE", "PHYSICIAN"), ("SURG", "PHYSICIAN"),
]
# family pairs that can legitimately be the SAME person (trainee licenses; nurse-midwife)
_FAMILY_COMPAT = {frozenset({"PHYSICIAN", "STUDENT"}), frozenset({"NURSING", "STUDENT"}),
                  frozenset({"PA", "STUDENT"}), frozenset({"NURSING", "MIDWIFE"})}


def _type_family(s):
    s = (s or "").upper()
    for kw, fam in _FAMILY_KEYWORDS:
        if kw in s:
            return fam
    return None


def _families_conflict(cred_type, taxonomy):
    """True when the DOH credential profession and the NPI's specialty are different
    professions entirely (and not a known same-person pairing)."""
    f1, f2 = _type_family(cred_type), _type_family(taxonomy)
    if not f1 or not f2 or f1 == f2:
        return False
    return frozenset({f1, f2}) not in _FAMILY_COMPAT


def _xwalk_identity(e, detail, m=None):
    """Confidence that the DOH credential-holder is the same person as the NPI that
    received the money (the DOH→NPI crosswalk hop). Strongest first:
      license-confirmed  DOH credential # == NPPES license #            (hard id)
      dob-confirmed      NPI on an exclusion list (by NPI), DOB == DOH birth year
      city-corroborated  NPPES practice city == the exclusion-record city
      exclusion-npi      NPI on an exclusion list under the same name (no DOB to check)
      unique-name        only the unique name+state crosswalk
      *-mismatch         conflicting license # or DOB year -> likely a namesake
    Returns None if the entity wasn't crosswalked."""
    d = (detail or {}).get(e.uid)
    if not d or not d.get("npi"):
        return None
    conf, notes = "unique-name", []
    # 1) license number — the hardest identifier both sides carry. Compare against
    # EVERY license on the NPI, not just the primary: a WA license can hide behind an
    # out-of-state primary (Bolling: Idaho primary masked the matching WA license).
    cred = _lic_core(e.raw.get("credentialnumber"))
    all_lics = [s for s in (d.get("licenses") or "").split(";") if s]
    if d.get("license") and d["license"] not in all_lics:
        all_lics.insert(0, d["license"])
    cores = [(_lic_core(x), x) for x in all_lics]
    cores = [(c, x) for c, x in cores if c and len(c) >= 3]
    if cores and cred:
        hit = next(((c, x) for c, x in cores if c == cred), None)
        if hit:
            conf, notes = "license-confirmed", [
                f"DOH credential # matches NPPES license # ({hit[1]})"]
        else:
            conf, notes = "license-mismatch", [
                f"DOH credential # {e.raw.get('credentialnumber')} ≠ NPPES "
                f"license #(s) {', '.join(x for _, x in cores[:3])} — verify"]
    # 2) DOB via the exclusion record (only meaningful when matched by NPI)
    if conf == "unique-name" and m and m.get("matched_via") == "NPI":
        ly, by = _year(m.get("dob")), _year(e.raw.get("birthyear"))
        if ly and by and ly == by:
            conf, notes = "dob-confirmed", [
                f"NPI is on {m.get('list')} with DOB matching DOH birth year ({by})"]
        elif ly and by:
            conf, notes = "dob-mismatch", [
                f"exclusion DOB year {ly} ≠ DOH birth year {by} — possible namesake"]
        else:
            conf, notes = "exclusion-npi", [
                f"NPI is listed on {m.get('list')} under the same name"]
    # 3) city corroboration (NPPES vs the exclusion record)
    if conf in ("unique-name", "exclusion-npi") and m:
        nc, mc = _city_key(d.get("city")), _city_key(m.get("city"))
        if nc and mc and nc == mc:
            notes.append(f"NPPES & exclusion-record city agree ({d.get('city')})")
            if conf == "unique-name":
                conf = "city-corroborated"
    # 4) profession consistency — only when nothing HARD confirmed identity. A license or
    # DOB match proves the (rare but real) dual-credentialed person, so it wins; but a
    # soft name-level match where the credential says Dentist and the NPI's specialty
    # says Emergency Medicine is almost certainly two different people.
    if conf in ("unique-name", "city-corroborated", "exclusion-npi"):
        ct, tx = e.raw.get("credentialtype"), d.get("taxonomy")
        if _families_conflict(ct, tx):
            conf = "taxonomy-mismatch"
            notes.insert(0, (f"credential is '{ct}' but the NPI's specialty is "
                             f"'{tx}' — different professions, likely a namesake"))
    if not notes:
        notes = ["NPI resolved by unique name+state match"]
    return {"npi": d.get("npi"), "npi_city": d.get("city"),
            "npi_name": d.get("name"), "npi_license": d.get("license"),
            "confidence": conf, "note": "; ".join(notes)}


def no_active_business_registration(entities, cfg, context):
    """Flag a provider/vendor with no active match in the business registry.

    Distinguishes 'no match found' (could be a name mismatch — softer) from 'match
    found but inactive/closed' (a stronger signal). Only runs when a registry CSV is
    loaded; otherwise the engine skips it.
    """
    registry = (context or {}).get("registry")
    if registry is None:
        return []
    sev_missing = cfg.get("severity_not_found", 12)
    sev_inactive = cfg.get("severity_inactive", 20)
    out = []
    for e in entities:
        m = registry.best_match(e.name, e.dba)
        if m["found"] and m["active"]:
            continue
        if m["found"]:  # found but closed/revoked/expired
            out.append(Flag(
                e.uid, "no_active_business_registration", sev_inactive,
                "Business registration not active",
                f"A matching business registration exists but its status is "
                f"'{m['status']}', not active.",
                {"searched": [n for n in (e.name, e.dba) if n],
                 "registry_status": m["status"]},
            ))
        else:
            out.append(Flag(
                e.uid, "no_active_business_registration", sev_missing,
                "No business registration found",
                "No active business registration matched this name in the loaded "
                "registry. Verify the legal name / DBA before drawing conclusions.",
                {"searched": [n for n in (e.name, e.dba) if n]},
            ))
    return out


# what each list actually bars — never claim federal-wide exclusion for a state or
# program-specific action (fleet audit: boilerplate overstated HCA/SAM matches).
_LIST_SCOPE = [
    ("LEIE", "Excluded parties may not be paid by any federal health program "
             "(Medicare, Medicaid, CHIP)."),
    ("CMS Revoked", "Revoked from MEDICARE billing; state Medicaid status is separate."),
    ("SAM", "Debarred from federal procurement/awards; clinical program billing is "
            "governed separately by OIG/CMS status."),
    ("HCA terminated", "Terminated from WA MEDICAID (Apple Health) only — this does "
                       "not by itself bar Medicare or other federal programs."),
]


def _scope_text(list_name):
    for key, txt in _LIST_SCOPE:
        if key.upper() in (list_name or "").upper():
            return txt
    return "Verify which programs this listing actually bars before attributing dollars."


def _list_title(list_name):
    """G5: never call a state list 'federal' in the headline — 15/40 fleet-audit
    subjects carried 'On a federal exclusion/sanction list (WA HCA ...)' while being
    verified NOT on any federal list."""
    u = (list_name or "").upper()
    if "HCA" in u:
        return f"On Washington's Medicaid termination list ({list_name})"
    if "LEIE" in u:
        return f"On the federal OIG exclusion list ({list_name})"
    if "SAM" in u:
        return f"On the federal SAM.gov exclusion list ({list_name})"
    if "CMS REVOKED" in u:
        return f"Medicare billing privileges revoked ({list_name})"
    return f"On an exclusion/sanction list ({list_name})"


def _license_derived(excltype):
    """1128(b)(4)/(b)(5): exclusion DERIVED from a state license action — it restates
    the license event rather than adding independent fraud evidence."""
    t = (excltype or "").lower().replace(" ", "").replace("(", "").replace(")", "")
    return t.startswith("1128b4") or t.startswith("1128b5")


def excluded_or_sanctioned(entities, cfg, context):
    """Flag an entity that matches a federal exclusion/sanction list (OIG LEIE, CMS
    Revoked, …). Severity is tiered by (a) identity confidence — NPI-definitive >
    city/type-corroborated > name-only — and (b) exclusion authority — a MANDATORY
    conviction-based exclusion outranks a permissive license action; a license-DERIVED
    1128(b)(4)/(b)(5) exclusion on a provider whose DOH sanction we already hold is
    emitted as `excluded_license_derived` (same correlation group as the DOH sanction,
    so one license event can no longer stack itself to 100 via its federal echo)."""
    scr = (context or {}).get("screening")
    if scr is None:
        return []
    sev_npi = cfg.get("severity_npi", 40)
    sev_npi_mand = cfg.get("severity_npi_mandatory", 48)
    sev_corrob = cfg.get("severity_corroborated", 30)
    sev_name = cfg.get("severity_name", 20)
    npi_sources = set(cfg.get("npi_sources", ["aba", "nemt", "dme"]))
    default_state = cfg.get("default_state", "WA")  # our data is WA-scoped
    xwalk = (context or {}).get("crosswalk") or {}
    xdetail = (context or {}).get("crosswalk_detail") or {}
    out = []
    for e in entities:
        npi = e.source_id if e.source in npi_sources else xwalk.get(e.uid)
        m = scr.match(npi=npi, name=e.name, state=e.state or default_state,
                      city=e.city, entity_type=e.entity_type,
                      birth_year=e.raw.get("birthyear"),
                      license_no=e.raw.get("credentialnumber"))
        if not m:
            continue
        conf, mand = m.get("confidence", "name-only"), m.get("mandatory")
        if conf == "definitive":
            sev = sev_npi_mand if mand else sev_npi
        elif conf == "corroborated":
            sev = sev_corrob
        else:
            sev = sev_name
        tier = ("MANDATORY exclusion (conviction-based)" if mand else
                "permissive exclusion" if mand is False else "exclusion")
        confirm = {
            "definitive": "Identity confirmed by NPI.",
            "corroborated": f"Identity corroborated ({m.get('corroboration')}).",
            "name-only": f"Name+state match only ({m.get('corroboration')}) — "
                         f"confirm it is the same individual before acting.",
        }[conf]
        # if we matched via a CROSSWALKED NPI whose license # doesn't match this
        # provider, the NPI (and thus the exclusion hit) is a different person — strip it
        # to name-only weight so a namesake doesn't sit at 100.
        if conf == "definitive" and e.source not in npi_sources:
            idc = _xwalk_identity(e, xdetail)
            if idc and idc["confidence"] in _MISMATCH:
                sev = min(sev, 10)
                conf = "npi-mismatch"
                confirm = ("⚠ The resolved NPI's license # does NOT match this provider "
                           "— the exclusion hit is likely a different person.")
        # F15: an out-of-state PO-box address on the exclusion record often means the
        # subject is incarcerated (BOP facilities receive mail at PO boxes) — i.e. the
        # case is ALREADY adjudicated and this is monitoring, not discovery.
        adjudicated_note = ""
        maddr = (m.get("address") or "").upper()
        if ("PO BOX" in maddr.replace(".", "") or "P O BOX" in maddr) and \
                (m.get("state") or "").upper() not in ("", "WA"):
            adjudicated_note = (" ⚖ The exclusion record's address is an out-of-state "
                                "PO box — subject may be incarcerated (case likely "
                                "already prosecuted); check DOJ press before treating "
                                "as an undiscovered lead.")
        # F2/G3: a license-derived listing on an entity whose DOH sanction we already
        # score is the SAME event echoed — separate rule_id, capped severity, correlated
        # group (taxonomy maps it to doh_sanction). This covers BOTH the federal echo
        # (1128(b)(4)/(b)(5)) and the state one: an HCA termination whose stated reason
        # is the license action itself ('License Suspended/Revoked/Surrendered') was
        # scoring a full standalone 40 on top of the DOH sanction — one license event
        # supplied 60-70 of ~95 points on eleven of the fleet-audit subjects.
        hca_license_echo = bool(
            "HCA" in (m.get("list") or "").upper()
            and re.search(r"licen[cs]e\s+(suspend|revok|surrender)",
                          (m.get("reason") or "") + " "
                          + (m.get("excltype_desc") or ""), re.I))
        rule_id = "excluded_or_sanctioned"
        if ((_license_derived(m.get("excltype")) or hca_license_echo)
                and e.source == "healthcare"
                and (e.status or "").strip().upper() in _ADVERSE):
            rule_id = "excluded_license_derived"
            sev = min(sev, cfg.get("severity_derived", 22))
            tier = ("license-derived listing — it restates the state license action "
                    "already counted"
                    if hca_license_echo else
                    "license-derived exclusion — 1128(b)(4)/(b)(5) restates the state "
                    "license action already counted")
        pay_ctx = (context or {}).get("payments") or {}
        pd_ = (pay_ctx.get("by_entity") or {}).get(e.uid)
        ceased_note = ""
        if pd_ and m.get("date"):
            lp = max((_year(p) for p in pd_.get("periods", {}) if _year(p)),
                     default=None)
            horizon = pay_ctx.get("max_year")
            ey_ = _year(m.get("date"))
            if lp and ey_ and lp <= ey_ and horizon and (horizon - lp) >= 5:
                # all observed money predates the listing AND is years stale —
                # adjudicated history, not a live lead
                sev = max(10, round(sev * (0.3 if horizon - lp >= 8 else 0.6)))
                ceased_note = (f" All observed billing predates this listing and ended "
                               f"{lp} ({horizon - lp} years before the data horizon) — "
                               f"an adjudicated/closed case, decayed accordingly.")
        out.append(Flag(
            e.uid, rule_id, sev,
            _list_title(m["list"]),
            f"Matched {m['list']} as '{m['matched_name']}' — {tier}: "
            f"{m.get('excltype_desc') or m['reason']} (dated {m['date'] or '—'}). "
            f"{_scope_text(m['list'])} {confirm}{adjudicated_note}{ceased_note}",
            {"list": m["list"], "exclusion_type": m.get("excltype"),
             "exclusion_reason": m.get("excltype_desc") or m["reason"],
             "mandatory": mand, "date": m["date"], "matched_via": m["matched_via"],
             "confidence": conf, "corroboration": m.get("corroboration"),
             "matched_dob": m.get("dob"), "matched_city": m.get("city"),
             "list_url": m["url"]},
        ))
    return out


def payment_anomaly(entities, cfg, context):
    """Year-over-year payment spikes and top-of-peer-group payment outliers, from the
    public payment data already attached to entities."""
    pay = (context or {}).get("payments")
    if not pay:
        return []
    by_entity, p95 = pay.get("by_entity", {}), pay.get("p95", {})
    spike_ratio = cfg.get("spike_ratio", 2.0)
    spike_sev = cfg.get("spike_severity", 14)
    out_sev = cfg.get("outlier_severity", 12)
    out_min = cfg.get("outlier_min", 100000)
    out = []
    for e in entities:
        d = by_entity.get(e.uid)
        if not d:
            continue
        periods = sorted((p, a) for p, a in d["periods"].items() if p)
        if len(periods) >= 2 and periods[-2][1] > 0:
            prev, cur = periods[-2][1], periods[-1][1]
            # G10: an absolute floor — $256→$795 is noise, not a billing spike; and a
            # rise at the END of a multi-year decline is wind-down variance.
            descending = (len(periods) >= 4
                          and all(periods[i][1] >= periods[i + 1][1]
                                  for i in range(len(periods) - 3)))
            if (cur / prev >= spike_ratio
                    and cur >= cfg.get("spike_min_cur", 25000)
                    and (cur - prev) >= cfg.get("spike_min_delta", 10000)
                    and not descending):
                out.append(Flag(
                    e.uid, "billing_spike", spike_sev,
                    "Sharp year-over-year payment increase",
                    f"{d['program']} payments rose from ${prev:,.0f} "
                    f"({periods[-2][0]}) to ${cur:,.0f} ({periods[-1][0]}) — "
                    f"{cur / prev:.1f}× in one year.",
                    {"program": d["program"], "prev": prev, "cur": cur,
                     "ratio": round(cur / prev, 2)}))
        thr = p95.get(d["program"])
        if thr and d["total"] >= thr and d["total"] >= out_min:
            out.append(Flag(
                e.uid, "payment_outlier", out_sev, "Payments far above peers",
                f"Total {d['program']} payments ${d['total']:,.0f} are in the top 5% "
                f"for this program.",
                {"program": d["program"], "total": d["total"], "p95": round(thr)}))
    return out


def ownership_churn(entities, cfg, context):
    """Nursing home that changed ownership (CHOW) — frequent churn is a fraud-risk
    signal (asset-stripping, license laundering)."""
    own = (context or {}).get("ownership")
    if not own:
        return []
    churn = own.get("churn", {})
    churn_n = own.get("churn_n", {})
    churn_dates = own.get("churn_dates", {})
    chain_dates = set(own.get("chain_dates", []))
    sev = cfg.get("severity", 14)
    min_chows = cfg.get("min_chows", 2)
    out = []
    for e in entities:
        d = churn.get(e.source_id)
        if not d:
            continue
        dates = set(churn_dates.get(e.source_id, [d]))
        # G14: a CHOW date shared chain-wide (4+ facilities, one corporate sale) is
        # NOT a per-facility flip — count only facility-specific dates toward churn.
        own_dates = dates - chain_dates
        n = churn_n.get(e.source_id, 1)
        if len(own_dates) >= min_chows:
            out.append(Flag(
                e.uid, "ownership_churn", sev, "Repeated ownership changes (CHOWs)",
                f"This facility has {len(own_dates)} facility-specific Medicare "
                f"change-of-ownership dates (latest {d}; {n} records). Repeated "
                f"flips warrant review (asset-stripping / license-laundering pattern).",
                {"effective_date": d, "chow_count": n,
                 "distinct_dates": sorted(own_dates), "ccn": e.source_id}))
        elif dates & chain_dates:
            out.append(Flag(
                e.uid, "ownership_churn", cfg.get("chain_severity", 6),
                "Chain-wide ownership transfer (CHOW)",
                f"This facility changed hands as part of a multi-facility corporate "
                f"sale ({(sorted(dates & chain_dates))[-1]}) — context: a recently "
                f"acquired chain's post-acquisition record is unproven, but a single "
                f"corporate sale is not license-laundering.",
                {"effective_date": d, "chain_dates": sorted(dates & chain_dates),
                 "ccn": e.source_id}))
    return out


def shared_owner(entities, cfg, context):
    """One owner controlling multiple facilities (ownership-network concentration)."""
    own = (context or {}).get("ownership")
    if not own:
        return []
    org_to_owners = own.get("org_to_owners", {})
    owner_to_orgs = own.get("owner_to_orgs", {})
    sev = cfg.get("severity", 16)
    min_fac = cfg.get("min_facilities", 2)
    out = []
    for e in entities:
        owners = org_to_owners.get(normalize(e.name), [])
        for okey in owners:
            info = owner_to_orgs.get(okey, {})
            facs = info.get("orgs", [])
            if len(facs) >= min_fac:
                out.append(Flag(
                    e.uid, "shared_owner", sev,
                    "Owner controls multiple facilities",
                    f"Owner '{info.get('name')}' is linked to {len(facs)} facilities — "
                    f"an ownership network worth mapping.",
                    {"owner": info.get("name"), "facility_count": len(facs)}))
                break
    return out


_ADVERSE = {"REVOKED", "SUSPENDED", "EXPIRED", "TERMINATED", "SURRENDER",
            "VOLUNTARY SURRENDER", "DENIED", "INACTIVE"}
# genuine bars on practice — only these justify dating a "paid AFTER barred" claim from
# the credential record (an Expired credential is a lapse, not necessarily a sanction).
_BARRED = {"REVOKED", "SUSPENDED", "TERMINATED", "SURRENDER", "VOLUNTARY SURRENDER"}


def paid_while_sanctioned(entities, cfg, context):
    """Provider whose credential is revoked/suspended/expired but still drawing public
    money. When we can date the sanction (credential expiry or exclusion date) AND a
    payment falls in a LATER year, it becomes `paid_after_barred` — a near-provable,
    date-stamped contradiction (the strongest lead). Otherwise the softer
    `paid_while_sanctioned`."""
    pay = (context or {}).get("payments")
    if not pay:
        return []
    by_entity = pay.get("by_entity", {})
    scr = (context or {}).get("screening")
    xwalk = (context or {}).get("crosswalk") or {}
    xdetail = (context or {}).get("crosswalk_detail") or {}
    npi_sources = {"aba", "nemt", "dme"}
    sev = cfg.get("severity", 35)
    sev_after = cfg.get("severity_after", 50)
    out = []
    for e in entities:
        if e.source != "healthcare":
            continue
        if (e.status or "").strip().upper() not in _ADVERSE:
            continue
        d = by_entity.get(e.uid)
        if not d or d.get("total", 0) <= 0:
            continue
        # When was this provider genuinely barred? FORMAL sanction dates (exclusion /
        # termination orders) are authoritative; a credential expiration is only a
        # proxy (it lags or LEADS the real order — Aflatooni's license lapsed 14 months
        # before his actual surrender, which made pre-sanction runoff look 'after-bar').
        # Use formal dates when any exist; fall back to expiration with provenance.
        status_u = (e.status or "").strip().upper()
        ey = _year(e.raw.get("expirationdate"))
        npi = e.source_id if e.source in npi_sources else xwalk.get(e.uid)
        m = (scr.match(npi=npi, name=e.name, state=e.state or "WA",
                       city=e.city, entity_type=e.entity_type,
                       birth_year=e.raw.get("birthyear"),
                       license_no=e.raw.get("credentialnumber")) if scr else None)
        formal = []
        if m and _year(m.get("date")):
            formal.append((_year(m["date"]), m["list"]))
        # G12: a dated disciplinary ORDER (DOH/WMC newsroom) is formal bar provenance —
        # it replaces the 'credential expiration (no dated order held)' proxy.
        dbar = ((context or {}).get("discipline_bars") or {}).get(
            _lic_core(e.raw.get("credentialnumber")))
        if dbar and _year(dbar[0]) and status_u in _BARRED:
            formal.append((_year(dbar[0]),
                           f"disciplinary order dated {dbar[0]}"))
        # F7: reinstatement detection — a license (re)issued AFTER the expiry-proxy bar
        # means the proxy bar was not in force (Earl: re-licensed 2017 after the 2014
        # action, practiced lawfully until the terminal 2018 surrender).
        reissue = _year(e.raw.get("lastissuedate"))
        expiry_valid = bool(ey and status_u in _BARRED
                            and not (reissue and ey and reissue > ey))
        # bar-provenance inheritance: when a formal record's stated REASON is the
        # license action itself, the license-loss proxy is evidenced by that dated
        # record — don't print 'no dated order held' under a timeline showing the date.
        proxy_label = "credential expiration (no dated order held)"
        if (m and re.search(r"licen[cs]e", (m.get("reason") or ""), re.I)
                and m.get("date")):
            proxy_label = (f"license action evidenced by {m['list']} record "
                           f"dated {m['date']}")
        proxy = (ey, proxy_label)
        bars = list(formal) + ([proxy] if expiry_valid else [])
        if formal:        # T2: a formal dated order always beats the expiry proxy
            barred_year, bar_source = min(formal, key=lambda t: t[0])
        elif expiry_valid:
            barred_year, bar_source = proxy
        else:
            barred_year, bar_source = None, None
        # F8: a bar only covers the programs its issuer controls — HCA termination bars
        # MEDICAID, a CMS revocation bars MEDICARE; LEIE/license-loss bar everything.
        # After-bar dollars are counted per (bar, program) pairing, never across scopes.
        def _covers(src, prog):
            s, p = (src or "").upper(), (prog or "").upper()
            if "HCA" in s:
                return "MEDICAID" in p
            if "CMS REVOKED" in s:
                return "MEDICARE" in p
            return True   # LEIE / SAM / license loss: all programs
        # identity confidence for the money attribution (DOH credential → NPI → $).
        ident = _xwalk_identity(e, xdetail, m)
        ident_ev = {}
        if ident:
            ident_ev = {"npi": ident["npi"], "npi_practice_city": ident["npi_city"],
                        "identity_confidence": ident["confidence"],
                        "identity_note": ident["note"]}
        if m:  # exclusion-list corroboration of the sanction
            ident_ev.update({"exclusion_list": m["list"],
                             "exclusion_type": m.get("excltype"),
                             "exclusion_reason": m.get("excltype_desc") or m["reason"],
                             "exclusion_match": m.get("confidence"),
                             "matched_dob": m.get("dob")})
        # wrong-person NPI -> the money is mis-attributed; emit a low-severity
        # "unconfirmed" flag instead of the strong paid-while/after-barred lead, so a
        # namesake doesn't ride to score 100 on someone else's Medicare billing.
        if ident and ident["confidence"] in _MISMATCH:
            out.append(Flag(
                e.uid, "paid_attribution_unconfirmed", 8,
                "Payment attribution unconfirmed — likely a namesake",
                f"The NPI linked to this provider has a license #, DOB, or profession "
                f"that does NOT match the credential record, so the ${d['total']:,.0f} "
                f"in {d['program']} likely belongs to a different person. Resolve "
                f"identity before attributing.",
                {"status": e.status, "program": d["program"], "total": d["total"],
                 **ident_ev}))
            continue
        # recency guard: a payment decades after a bar is a different person / stale
        # record. Compute AFTER-bar money PER PROGRAM (the old merged version labeled
        # Part D drug cost as whatever program happened to be stored first).
        max_gap = cfg.get("max_gap_years", 6)
        min_year = cfg.get("min_barred_year", 2006)
        by_prog = d.get("by_program") or {
            d.get("program", "public funds"): {"total": d.get("total", 0),
                                               "periods": d.get("periods", {})}}
        # G11 (Spencer class): WA-checkbook dollars attach by VENDOR NAME, not NPI.
        # For a person-named credential that is a namesake trap — an active same-named
        # vendor (e.g. a licensed midwife) may be the true payee. Never present
        # name-matched state money as "this provider's" without identity verification.
        if (e.source == "healthcare"
                and by_prog and all(p.startswith("WA state") for p in by_prog)):
            out.append(Flag(
                e.uid, "paid_attribution_unconfirmed", 8,
                "Payment attribution unconfirmed — vendor-name match only",
                f"${d['total']:,.0f} in WA-state payments matched this provider by "
                f"VENDOR NAME (no NPI linkage). A same-named active vendor may be the "
                f"true payee — verify identity before attributing.",
                {"status": e.status, "program": d["program"], "total": d["total"],
                 "match_basis": "vendor-name", **ident_ev}))
            continue
        after_by_prog, same_year_amt, same_years = {}, 0.0, set()
        prog_bar = {}      # program -> (bar_year, bar_source) actually applied
        gap_dropped = {}   # program -> {year: amt} beyond the recency window (visible!)
        lic_ok = bool(ident and ident["confidence"] == "license-confirmed")
        if bars:
            for prog, pd in by_prog.items():
                # earliest FORMAL bar whose issuer's scope covers THIS program (F8);
                # the expiry proxy is a fallback only where no formal bar covers it
                # (T2 hierarchy, kept per-program: Dees' Medicaid-scoped HCA bar must
                # not erase his license-loss bar over Medicare dollars).
                pbars = [(y, s) for y, s in formal
                         if y >= min_year and _covers(s, prog)]
                if not pbars and expiry_valid and ey >= min_year:
                    pbars = [proxy]
                if not pbars:
                    continue
                by_, bs_ = min(pbars, key=lambda t: t[0])
                prog_bar[prog] = (by_, bs_)
                cand = {}
                for per, pamt in pd["periods"].items():
                    y = _year(per)        # G11: handles '2015' AND 'FY2026'
                    if not y or not pamt:
                        continue
                    if y > by_:
                        cand[y] = cand.get(y, 0) + pamt
                    elif y == by_:
                        same_year_amt += pamt
                        same_years.add(y)
                # F14: the recency guard exists for NAMESAKE risk — when identity is
                # license-confirmed and the post-bar stream is CONTIGUOUS, truncating
                # mid-series understates the lead (Dees: $472K cut to $130K). Years
                # dropped by the guard are recorded, never silently discarded.
                ys = sorted(cand)
                contiguous = bool(ys) and (ys[0] - by_) <= max_gap and all(
                    b - a == 1 for a, b in zip(ys, ys[1:]))
                for y, v in cand.items():
                    if (y - by_) <= max_gap or (lic_ok and contiguous):
                        after_by_prog.setdefault(prog, {})[y] = v
                    else:
                        gap_dropped.setdefault(prog, {})[y] = v
        amt_after = sum(v for ys in after_by_prog.values() for v in ys.values())
        total_all = d.get("total", 0) or 0
        # G2: a trivial after-tail (<$1K or <2% of all money, and a minority of it) is
        # residue, not conduct — route it to the ceased disposition instead of
        # manufacturing a 90+ "paid after barred" from $591 (Lush / Brooks / Pan).
        trivial_tail = (bool(after_by_prog)
                        and amt_after < max(1000.0, 0.02 * total_all)
                        and amt_after <= 0.5 * total_all)
        if after_by_prog and not trivial_tail:
            amt = amt_after
            yrs = sorted({y for ys in after_by_prog.values() for y in ys})
            prog_txt = "; ".join(
                p + " " + ", ".join(f"{y}: ${v:,.0f}" for y, v in sorted(ys.items()))
                for p, ys in sorted(after_by_prog.items()))
            # Part D semantics: drug cost is attributed to the prescriber at FILL time
            # and paid to pharmacies — refills of pre-bar prescriptions land after the
            # bar with a steep decline and end within ~a year ("runoff").
            partd = [p for p in after_by_prog if "Part D" in p]
            runoff = False
            runoff_baseline = "held"
            if partd and set(after_by_prog) == set(partd):
                # G4 (Earl bug): evaluate against the bar actually APPLIED to these
                # programs — the global earliest bar can be a different program's
                # (2014 Medicaid bar zeroed `prior` for a 2018 license bar's runoff).
                pby = max(prog_bar[p][0] for p in partd if p in prog_bar)
                prior = sum(by_prog[p]["periods"].get(str(pby), 0)
                            + by_prog[p]["periods"].get(str(pby - 1), 0)
                            for p in partd)
                held_years = [int(per) for p in partd
                              for per in by_prog[p]["periods"]
                              if str(per).isdigit()]
                if prior == 0 and held_years and pby - 1 < min(held_years):
                    runoff_baseline = "pre-ingestion (unobservable)"
                last_after = max(y for ys in after_by_prog.values() for y in ys)
                runoff = (last_after <= pby + 1 and prior > 0
                          and amt < 0.35 * prior)
            # dollar-scaled severity: $27 of residue must not rank like $472K
            if amt < 1000:
                sev_amt = 22
            elif amt < 25000:
                sev_amt = 35
            elif amt < 100000:
                sev_amt = 45
            else:
                sev_amt = sev_after
            sev_final = min(sev_amt, 20) if runoff else sev_amt
            # G1: money-age decay vs the data horizon — a contradiction whose last
            # dollar is nearly a decade old is a recovery/referral lead, not an active
            # one; it must not outrank live money.
            horizon = pay.get("max_year")
            money_age = (horizon - max(yrs)) if horizon else 0
            big = amt >= 100000     # a $472K contradiction is a recovery-window lead,
            if money_age >= 8 and not big:        # not noise — staleness must not sink
                sev_final = max(8, round(sev_final * 0.3))
            elif money_age >= 5 and not big:
                sev_final = max(10, round(sev_final * 0.6))
            age_note = (f" Last payment is {money_age} years before the data horizon "
                        f"(CY{horizon}) — a stale, recovery-window lead." if
                        money_age >= 5 else "")
            drug_note = (" Part D figures are drug COST of prescriptions attributed at "
                         "fill time (paid to pharmacies, not to the provider)."
                         if partd else "")
            runoff_note = (" Pattern is consistent with refill runoff of pre-bar "
                           "prescriptions (steep decline, ends within a year of the "
                           "bar) — verify prescription-written dates before treating "
                           "as post-bar conduct." if runoff else "")
            # cite the bar actually APPLIED to the after-bar programs (F8) — the global
            # earliest bar may be a different program's scope
            used = {prog_bar[p] for p in after_by_prog if p in prog_bar}
            if len(used) == 1:
                uy, us = next(iter(used))
                bar_txt = f"Barred as of {uy} ({us})"
                barred_year, bar_source = uy, us
            else:
                bar_txt = "Barred — " + "; ".join(
                    f"{p}: {prog_bar[p][0]} ({prog_bar[p][1]})"
                    for p in sorted(after_by_prog) if p in prog_bar)
            reinstate_note = (f" Note: license re-issued {reissue}, so the credential "
                              f"expiry was not used as a bar." if reissue and ey
                              and reissue > ey else "")
            gap_amt = sum(v for ys_ in gap_dropped.values() for v in ys_.values())
            gap_note = (f" A further ${gap_amt:,.0f} lies beyond the {max_gap}-year "
                        f"recency window and is EXCLUDED from the after-bar figure "
                        f"(identity not license-confirmed or stream not contiguous)."
                        if gap_amt else "")
            out.append(Flag(
                e.uid, "paid_after_barred", sev_final,
                "Paid public money AFTER being barred",
                f"{bar_txt}; ${amt:,.0f} is dated "
                f"after the bar — {prog_txt}.{drug_note}{runoff_note}{reinstate_note}"
                f"{gap_note}{age_note} Verifiable against the linked records.",
                {"status": e.status, "barred_year": barred_year,
                 "money_age_years": money_age,
                 "runoff_baseline": runoff_baseline,
                 "bar_source": bar_source,
                 "bars_applied": {p: f"{prog_bar[p][0]} ({prog_bar[p][1]})"
                                  for p in after_by_prog if p in prog_bar},
                 "reinstated_year": (reissue if reissue and ey and reissue > ey
                                     else None),
                 "excluded_after_gap": {p: {str(y): round(v, 2)
                                            for y, v in ys_.items()}
                                        for p, ys_ in gap_dropped.items()} or None,
                 "amount_after": round(amt, 2),
                 "after_by_program": {p: {str(y): round(v, 2) for y, v in ys.items()}
                                      for p, ys in after_by_prog.items()},
                 "payment_years": ", ".join(str(y) for y in yrs),
                 "runoff_consistent": runoff,
                 "same_year_amount": round(same_year_amt, 2),
                 "program": max(after_by_prog,
                                key=lambda p: sum(after_by_prog[p].values())),
                 **ident_ev}))
        else:
            # F5: per-program subtotals — never glue the merged total to one program
            per_prog_txt = "; ".join(
                f"{p} ${pd.get('total', 0):,.0f}" for p, pd in sorted(by_prog.items()))
            pay_years = sorted({_year(per) for pd in by_prog.values()
                                for per in pd.get("periods", {})
                                if _year(per)})
            last_pay = pay_years[-1] if pay_years else None
            horizon = pay.get("max_year")
            ev = {"status": e.status, "program": d["program"], "total": d["total"],
                  "programs": per_prog_txt, "last_payment_year": last_pay,
                  "coverage_through": horizon, **ident_ev}
            if barred_year:
                ev.update({"barred_year": barred_year, "bar_source": bar_source})
            note = ""
            if same_year_amt:
                ev["same_year_amount"] = round(same_year_amt, 2)
                sy = ", ".join(str(y) for y in sorted(same_years)) or str(barred_year)
                note = (f" ${same_year_amt:,.0f} falls in the bar year itself "
                        f"({sy}) — ambiguous at year granularity (month "
                        f"unknown), so it is NOT counted as after-bar.")
            # F1/G2: when the bar is dated and payments PREDATE it (or stop IN the bar
            # year, or leave only a trivial residue tail) with coverage running past
            # the bar and $0 after — the enforcement system WORKED. Informational
            # disposition, not a present-tense "still paid" lead.
            ceased = bool(barred_year and last_pay) and (
                last_pay < barred_year
                or (horizon and horizon > barred_year
                    and (last_pay == barred_year or trivial_tail)))
            if ceased:
                if horizon and horizon > barred_year:
                    tail = (f" Data coverage runs through CY{horizon} with $0 "
                            f"{'(beyond a trivial residue) ' if trivial_tail else ''}"
                            f"observed after the bar — affirmative evidence billing "
                            f"CEASED.")
                elif horizon:
                    tail = (f" CMS data is published only through CY{horizon} (~18-month "
                            f"lag), so post-bar billing is NOT yet observable — "
                            f"re-check when CY{barred_year} data lands.")
                    ev["after_unobservable"] = True
                else:
                    tail = ""
                if trivial_tail:
                    pct = 100.0 * amt_after / total_all if total_all else 0
                    tail += (f" A trivial after-tail of ${amt_after:,.0f} "
                             f"({pct:.1f}% of the total) is treated as residue, not "
                             f"post-bar conduct.")
                    ev["trivial_after_tail"] = round(amt_after, 2)
                phrasing = ("is dated on or before" if (last_pay or 0) >= barred_year
                            else "is dated BEFORE")
                out.append(Flag(
                    e.uid, "paid_before_bar", cfg.get("severity_prebar", 10),
                    "Payments predate the sanction",
                    f"All ${d['total']:,.0f} ({per_prog_txt}) {phrasing} "
                    f"the {barred_year} bar "
                    f"({bar_source}).{tail}{note}",
                    ev))
                continue
            if (barred_year and horizon and barred_year > horizon):
                # bar is newer than any published data — nothing observable either way
                ev["after_unobservable"] = True
                note += (f" The bar ({barred_year}) postdates the CMS data horizon "
                         f"(CY{horizon}) — post-bar billing is not yet observable; "
                         f"flagged for watch, not for review.")
                sev_use = cfg.get("severity_unobservable", 14)
            else:
                sev_use = sev
            # F8: payments past the bar but in programs OUTSIDE its scope (Medicare
            # after a Medicaid-only termination) are lawful on their face — say so and
            # de-weight rather than implying a contradiction that isn't there.
            if (barred_year and last_pay and last_pay > barred_year
                    and not ev.get("after_unobservable")):
                ev["out_of_scope_continuation"] = True
                note += (f" Payments after {barred_year} are in programs the bar does "
                         f"not cover ({bar_source}) — lawful on its face; verify no "
                         f"broader bar (OIG/license) applies.")
                sev_use = min(sev_use, cfg.get("severity_out_of_scope", 18))
            # G10: materiality + money-age tiers on the soft branch (mirror after-bar):
            # $166 of decade-old drug cost must not carry the same 35 as live $100K.
            if total_all < 1000:
                sev_use = min(sev_use, 15)
            elif total_all < 25000:
                sev_use = min(sev_use, 28)
            money_age = (horizon - last_pay) if (horizon and last_pay) else 0
            if money_age >= 8:
                sev_use = max(8, round(sev_use * 0.3))
            elif money_age >= 5:
                sev_use = max(10, round(sev_use * 0.6))
            ev["money_age_years"] = money_age
            # G5: tense-honesty — present tense only when the money is current
            if money_age >= 2:
                title = ("Received public funds while credential was sanctioned")
                verb = "was associated with"
            else:
                title = "Receiving public funds while credential is sanctioned"
                verb = "is associated with"
            stale_note = (f" Last payment {last_pay} — {money_age} years before the "
                          f"data horizon (CY{horizon})." if money_age >= 5 else "")
            out.append(Flag(
                e.uid, "paid_while_sanctioned", sev_use,
                title,
                f"Credential status is '{e.status}', yet ${d['total']:,.0f} "
                f"({per_prog_txt}) {verb} this provider's NPI.{note}{stale_note}",
                ev))
    return out


def billing_forensics(entities, cfg, context):
    """Procedure-level Part B signals: one code dominating a provider's Medicare $
    (mill), or many services billed per patient-visit (unbundling)."""
    bill = (context or {}).get("billing")
    if not bill:
        return []
    dom_share = cfg.get("dominance_share", 0.85)
    dom_min = cfg.get("dominance_min", 50000)
    dom_sev = cfg.get("dominance_severity", 16)
    spd = cfg.get("services_per_day", 10)
    spd_sev = cfg.get("services_per_day_severity", 14)
    xdetail = (context or {}).get("crosswalk_detail") or {}
    out = []
    for e in entities:
        m = bill.get(e.uid)
        if not m:
            continue
        idc = _xwalk_identity(e, xdetail)  # billing belongs to the wrong person on mismatch
        if idc and idc["confidence"] in _MISMATCH:
            continue
        if m.get("top_share", 0) >= dom_share and m.get("total", 0) >= dom_min:
            out.append(Flag(
                e.uid, "single_code_dominance", dom_sev,
                "One procedure = most of the Medicare billing",
                f"{m['top_share'] * 100:.0f}% of ${m['total']:,.0f} Medicare Part B is a "
                f"single code ({m['top_code']} {(m.get('top_desc') or '')[:36]}).",
                {"top_code": m["top_code"], "top_share": m["top_share"],
                 "total": m["total"]}))
        if m.get("max_srv_per_day", 0) >= spd:
            out.append(Flag(
                e.uid, "services_per_visit_outlier", spd_sev,
                "Unusually many services per patient-visit",
                f"Up to {m['max_srv_per_day']:.0f} services billed per beneficiary-day "
                f"— possible unbundling or inflated service counts.",
                {"max_srv_per_day": m["max_srv_per_day"]}))
    return out


def _pctile(value, dist):
    """Percentile rank of value within dist (0..1); 0 when dist is empty."""
    if not dist:
        return 0.0
    return sum(1 for v in dist if v <= value) / len(dist)


def nursing_enforcement(entities, cfg, context):
    """CMS enforcement on nursing homes: immediate-jeopardy citations, civil monetary
    penalties, and high deficiency counts.

    F10: fines and deficiency counts fire on 57-84% of WA SNFs, so flat severities made
    a routine $13K fine read like worst-in-state $378K. Severity now scales with the
    facility's PERCENTILE within the in-state enforcement distribution, and the
    deficiency flag fires only for genuine outliers (>= P90)."""
    enf = (context or {}).get("nursing_enforcement")
    if not enf:
        return []
    fine_min = cfg.get("fine_min", 10000)
    fine_dist = [d.get("fines", 0) for d in enf.values() if d.get("fines", 0) > 0]
    def_dist = [d.get("deficiencies", 0) for d in enf.values()
                if d.get("deficiencies", 0) > 0]
    out = []
    for e in entities:
        d = enf.get(e.source_id)
        if not d:
            continue
        if d.get("jeopardy", 0) > 0:
            out.append(Flag(
                e.uid, "immediate_jeopardy_citation", cfg.get("jeopardy_severity", 22),
                "Immediate-jeopardy / actual-harm citation",
                f"{d['jeopardy']} immediate-jeopardy citation(s) on record (worst "
                f"scope/severity '{d.get('worst')}'). Care-quality enforcement — "
                f"context for MFCU-style review, not a billing bar.",
                {"jeopardy_citations": d["jeopardy"], "worst_scope_severity": d.get("worst")}))
        if d.get("fines", 0) >= fine_min:
            pct = _pctile(d["fines"], fine_dist)
            sev = round(6 + 14 * pct)              # P50 ≈ 13, P99 ≈ 20
            out.append(Flag(
                e.uid, "civil_monetary_penalty", sev,
                "Civil monetary penalties imposed",
                f"${d['fines']:,.0f} in CMS fines across {d['penalties']} penalties — "
                f"P{int(pct * 100)} of fined WA nursing facilities. Care-quality "
                f"enforcement — context, not a billing bar.",
                {"total_fines": round(d["fines"]), "penalties": d["penalties"],
                 "state_percentile": round(pct * 100)}))
        # G15: a Denial of Payment for New Admissions is a DATED payment bar — billing
        # for new admissions after it is a concrete, testable contradiction (the fleet
        # audit found these rows silently dropped at Heartwood / North Bend).
        if d.get("dpna_dates"):
            dts = sorted(x for x in d["dpna_dates"] if x)
            out.append(Flag(
                e.uid, "payment_denial_imposed", cfg.get("dpna_severity", 16),
                "CMS Denial of Payment for New Admissions imposed",
                f"CMS imposed a Denial of Payment for New Admissions "
                f"(effective {', '.join(dts[-2:])}). Payments for NEW admissions "
                f"after this date are barred — a dated cross-check against state "
                f"Medicaid payment records.",
                {"dpna_dates": dts, "ccn": e.source_id}))
        # deficiency counts: only a true outlier is signal (>= P90 in-state)
        if def_dist and d.get("deficiencies", 0) > 0:
            pct = _pctile(d["deficiencies"], def_dist)
            if pct >= cfg.get("deficiency_pctile", 0.90):
                out.append(Flag(
                    e.uid, "many_health_deficiencies", cfg.get("deficiency_severity", 8),
                    "High number of health deficiencies",
                    f"{d['deficiencies']} health deficiencies cited — "
                    f"P{int(pct * 100)} of cited WA nursing facilities.",
                    {"deficiencies": d["deficiencies"],
                     "state_percentile": round(pct * 100)}))
    return out


def childcare_enforcement(entities, cfg, context):
    """DCYF 'Child Care Check' enforcement (scraped from findchildcarewa, keyed by the
    provider's WACOMPASS id): valid/investigated complaints (rare → high-signal) and the
    routine inspection count (context)."""
    enf = (context or {}).get("childcare_enforcement")
    if not enf:
        return []
    sev_complaint = cfg.get("severity_complaint", 18)
    per_extra = cfg.get("severity_per_extra", 6)
    sev_max = cfg.get("severity_max", 34)
    insp_min = cfg.get("inspection_min", 6)
    insp_sev = cfg.get("inspection_severity", 6)
    out = []
    for e in entities:
        d = enf.get(e.uid)
        if not d:
            continue
        comp = d.get("complaints", 0)
        if comp >= 1:
            out.append(Flag(
                e.uid, "childcare_valid_complaint",
                min(sev_max, sev_complaint + per_extra * (comp - 1)),
                "Valid complaint(s) on DCYF Child Care Check",
                f"{comp} valid/investigated complaint(s) posted on findchildcarewa "
                f"(DCYF Child Care Check) for this provider — substantiated complaints "
                f"are uncommon, so verify the case detail on the linked portal.",
                {"complaints": comp, "inspections": d.get("inspections", 0)}))
        if d.get("inspections", 0) >= insp_min:
            out.append(Flag(
                e.uid, "childcare_many_inspections", insp_sev,
                "High number of licensing inspections",
                f"{d['inspections']} licensing inspections on record — context (frequent "
                f"inspections can indicate follow-up monitoring), not a finding itself.",
                {"inspections": d["inspections"]}))
    return out


CROSS_RULES_BY_SOURCE = {
    "childcare": [("no_active_business_registration", no_active_business_registration),
                  ("childcare_enforcement", childcare_enforcement)],
    "contracts": [("no_active_business_registration", no_active_business_registration)],
    "nursing": [("ownership_churn", ownership_churn),
                ("shared_owner", shared_owner),
                ("nursing_enforcement", nursing_enforcement)],
    "hospice": [("shared_owner", shared_owner)],
    "home_health": [("shared_owner", shared_owner)],
}

# Cross rules that run for EVERY source (need the shared context).
def paid_after_npi_deactivated(entities, cfg, context):
    """Public payments dated strictly AFTER the provider's NPI was deactivated in NPPES.

    A deactivated NPI (death, retirement, dissolution, CMS action) cannot lawfully bill —
    so payment years after the deactivation year are a dated, checkable contradiction in
    the same class as paid-after-barred. Payments are year-granular, so same-year is
    never counted; identity caveats from the crosswalk apply and are carried along."""
    deact = (context or {}).get("npi_deactivated")
    pay = (context or {}).get("payments")
    if not deact or not pay:
        return []
    by_entity = pay.get("by_entity", {})
    xwalk = (context or {}).get("crosswalk") or {}
    xdetail = (context or {}).get("crosswalk_detail") or {}
    npi_sources = {"aba", "nemt", "dme"}
    sev = cfg.get("severity", 45)
    out = []
    for e in entities:
        npi = e.source_id if e.source in npi_sources else xwalk.get(e.uid)
        iso = deact.get(str(npi or ""))
        if not iso:
            continue
        d = by_entity.get(e.uid)
        if not d:
            continue
        dy = _year(iso)
        after = {}
        for period, amt in (d.get("periods") or {}).items():
            y = _year(period)
            if y and dy and y > dy and amt:
                after[y] = after.get(y, 0) + amt
        if not after:
            continue
        amount_after = round(sum(after.values()), 2)
        ident = _xwalk_identity(e, xdetail)
        ev = {"npi": npi, "deactivation_date": iso,
              "amount_after": amount_after,
              "years_after": sorted(after),
              "source": "NPPES monthly deactivation file (CMS)"}
        if ident:
            ev["identity_confidence"] = ident["confidence"]
            ev["identity_note"] = ident["note"]
            if ident["confidence"] in _MISMATCH:
                continue          # NPI likely belongs to a namesake — don't assert
        out.append(Flag(
            e.uid, "paid_after_npi_deactivated", sev,
            "Paid public money AFTER the NPI was deactivated",
            f"NPI {npi} was deactivated in NPPES on {iso}, yet "
            f"${amount_after:,.0f} in public payments are dated in later year(s) "
            f"({', '.join(map(str, sorted(after)))}). A deactivated NPI cannot "
            f"lawfully bill — verify the payment dates and identity against NPPES.",
            ev))
    return out


def debarred_contractor(entities, cfg, context):
    """Entity matches WA L&I's debarred-contractors (or strike) list.

    Debarment bars bidding on/working public contracts (RCW 39.12/18.27/51.48). The
    decisive form: an agency contract whose effective dates OVERLAP the debarment window.
    Matching is by normalized name (L&I also gives UBI + principals, carried as
    evidence); short/generic names are skipped to avoid collisions."""
    lni = (context or {}).get("lni")
    if not lni:
        return []
    by_name = lni.get("by_name", {})
    sev = cfg.get("severity", 22)
    sev_during = cfg.get("severity_contract_during", 40)
    strike_sev = cfg.get("strike_severity", 10)
    min_len = cfg.get("min_name_len", 7)
    out = []
    for e in entities:
        key = normalize(e.name)
        if len(key) < min_len:
            continue
        rec = by_name.get(key)
        if not rec:
            continue
        ev = {"lni_name": rec["name"], "ubi": rec["ubi"], "license": rec["license"],
              "principals": rec["principals"], "rcw": rec["rcw"],
              "debar_begin_date": rec["begin"], "debar_end_date": rec["end"],
              "source": "L&I debar/strike list (secure.lni.wa.gov)"}
        if rec["kind"] == "strike":
            out.append(Flag(
                e.uid, "lni_strike", strike_sev,
                "On L&I contractor strike list",
                f"Name matches L&I strike list ({rec['rcw']}) — a violation strike "
                f"short of debarment (two strikes in 3 years → debarment). Verify the "
                f"UBI ({rec['ubi']}) matches this entity.", ev))
            continue
        # debarred: does a public contract overlap the debarment window?
        start = (e.raw.get("contract_effective_start") or "")[:10]
        end = (e.raw.get("contract_effective_end_date") or "")[:10]
        overlap = (e.source.startswith("contracts") and rec["begin"] and start
                   and (not rec["end"] or start <= rec["end"])
                   and (not end or end >= rec["begin"]))
        if overlap:
            out.append(Flag(
                e.uid, "contract_during_debarment", sev_during,
                "Public contract in force DURING debarment",
                f"Debarred from public works {rec['begin']} → {rec['end'] or 'open'} "
                f"({rec['rcw']}), yet this agency contract runs {start} → "
                f"{end or '?'} — overlapping the debarment. Verify the UBI "
                f"({rec['ubi']}) identifies the same company, then this is a dated "
                f"contradiction.", ev))
        else:
            out.append(Flag(
                e.uid, "lni_debarred", sev,
                "On L&I debarred contractors list",
                f"Name matches L&I's debarred list ({rec['rcw']}, "
                f"{rec['begin']} → {rec['end'] or 'open'}) — barred from public-works "
                f"contracts. Verify the UBI ({rec['ubi']}) matches this entity.", ev))
    return out


GLOBAL_CROSS_RULES = [
    ("excluded_or_sanctioned", excluded_or_sanctioned),
    ("debarred_contractor", debarred_contractor),
    ("payment_anomaly", payment_anomaly),
    ("paid_while_sanctioned", paid_while_sanctioned),
    ("paid_after_npi_deactivated", paid_after_npi_deactivated),
    ("billing_forensics", billing_forensics),
]
