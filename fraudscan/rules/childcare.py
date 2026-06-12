"""Child-care anomaly rules.

Each produces investigative *leads*, not conclusions. Rules favor signals that are
explainable and tied to public-money risk (subsidy/licensing), and every flag
includes the specific evidence that triggered it.
"""
from fraudscan.rules.base import (
    Flag, norm_addr, norm_email, norm_phone, norm_text, parse_date, today,
    mean_std, percentile,
)
from fraudscan.rules.naming import misspelled_word_in_name
from fraudscan.rules.address import address_forensics
from fraudscan.brand import open_brand, brand_key, is_venue


def _active(e):
    return e.status.strip().upper().startswith("ACTIVE")


def _cap(e):
    v = e.raw.get("licensecapacity")
    try:
        return float(v) if v not in (None, "", "NULL") else None
    except (TypeError, ValueError):
        return None


def license_expired_active(entities, cfg):
    sev = cfg.get("severity", 30)
    out = []
    for e in entities:
        if not _active(e):
            continue
        exp = parse_date(e.raw.get("licenseexpirationdate"))
        if exp and exp < today():
            days = (today() - exp).days
            out.append(Flag(
                e.uid, "license_expired_active", sev,
                "License expired but still active",
                f"License expired {exp.isoformat()} ({days} days ago) while operating "
                f"status is '{e.status}'.",
                {"license_expiration_date": exp.isoformat(), "days_expired": days,
                 "status": e.status,
                 "license_type": e.raw.get("licensecertificatetypedesc")},
            ))
    return out


def payment_only_certificate(entities, cfg):
    sev = cfg.get("severity", 5)
    out = []
    for e in entities:
        lt = (e.raw.get("licensecertificatetypedesc") or "").strip().upper()
        if _active(e) and lt == "PAYMENT ONLY":
            out.append(Flag(
                e.uid, "payment_only_certificate", sev,
                "Payment-only certificate",
                "Certificate type is 'PAYMENT ONLY' — receives subsidy payments "
                "without a standard facility license. Low-weight population context.",
                {"license_type": e.raw.get("licensecertificatetypedesc"),
                 "ssps_provider_number": e.raw.get("sspsprovidernumber")},
            ))
    return out


def _brand_discount(names):
    """(severity multiplier, description suffix, brand) for a cluster of facility names.

    These cluster rules exist to catch HIDDEN common control. When every site openly
    carries one brand root (an advertised chain — 'MARTHA & MARY <school>'), the shared
    contact/address is disclosure, not concealment, so the signal is heavily discounted
    and says why. Differently-branded clusters keep full severity."""
    ob = open_brand(names, min_count=2)
    if ob:
        return 0.35, (f" All sites share the open brand '{ob}' — an openly-branded "
                      f"multi-site organization (expected); the high-risk version of "
                      f"this pattern is differently-branded sites sharing "
                      f"contacts/addresses."), ob
    return 1.0, "", None


def address_shared_multiple_providers(entities, cfg):
    sev2 = cfg.get("severity_2", 14)
    sev3 = cfg.get("severity_3plus", 24)
    groups = {}
    for e in entities:
        key = norm_addr(e)
        if not key or not e.address:
            continue
        groups.setdefault(key, []).append(e)
    out = []
    for key, members in groups.items():
        uids = {e.uid for e in members}
        names = sorted({norm_text(e.name) for e in members if e.name})
        if len(uids) < 2 or len(names) < 2:
            continue
        # a provider co-located with its HOST venue (school / community center / church)
        # is a hosting arrangement; the shell pattern needs 2+ NON-venue brands.
        nonvenue = [e.name for e in members
                    if e.name and not is_venue(e.name, e.dba)]
        if len({brand_key(n) or norm_text(n) for n in nonvenue}) < 2:
            continue
        sev = sev3 if len(uids) >= 3 else sev2
        co = sorted({e.name for e in members if e.name})[:10]
        mult, suffix, ob = _brand_discount([e.name for e in members if e.name])
        for e in members:
            out.append(Flag(
                e.uid, "address_shared_multiple_providers", round(sev * mult, 1),
                "Multiple distinct providers at one address",
                f"{len(uids)} distinct providers ({len(names)} different names) share "
                f"the physical address: {e.address}, {e.city}.{suffix}",
                {"address": f"{e.address}, {e.city} {e.zip}".strip(),
                 "provider_count": len(uids), "co_located_providers": co,
                 **({"open_brand": ob} if ob else {})},
            ))
    return out


def shared_contact_multiple_providers(entities, cfg):
    sev = cfg.get("severity", 22)
    # Above this many sites, a shared contact is a known multi-site operator/chain
    # (e.g. a YMCA or a national provider), not a fraud lead — exclude it.
    max_cluster = cfg.get("max_cluster", 8)
    email_map, phone_map = {}, {}
    for e in entities:
        em = norm_email(e.raw.get("primarycontactemail"))
        ph = norm_phone(e.raw.get("primarycontactphonenumber"))
        if em:
            email_map.setdefault(em, []).append(e)
        if ph:
            phone_map.setdefault(ph, []).append(e)
    out = []
    for e in entities:
        em = norm_email(e.raw.get("primarycontactemail"))
        ph = norm_phone(e.raw.get("primarycontactphonenumber"))
        shared_via, others = None, set()
        for key, mp in ((em, email_map), (ph, phone_map)):
            if not key:
                continue
            uids = {m.uid for m in mp[key]}
            if 2 <= len(uids) <= max_cluster:
                shared_via = "email" if mp is email_map else "phone"
                others |= {m.name for m in mp[key] if m.uid != e.uid and m.name}
        if shared_via:
            mult, suffix, ob = _brand_discount(list(others) + ([e.name] if e.name else []))
            out.append(Flag(
                e.uid, "shared_contact_multiple_providers", round(sev * mult, 1),
                "Contact info shared across providers",
                f"Primary {shared_via} is shared with other distinct providers — "
                f"possible common operator behind separate registrations.{suffix}",
                {"shared_via": shared_via,
                 "other_providers": sorted(others)[:10],
                 **({"open_brand": ob} if ob else {})},
            ))
    return out


def concentration_same_contact_person(entities, cfg):
    sev = cfg.get("severity", 12)
    min_fac = cfg.get("min_facilities", 3)
    max_fac = cfg.get("max_facilities", 10)  # above this = institutional operator
    groups = {}
    for e in entities:
        person = norm_text(e.raw.get("primarycontactpersonname"))
        if person:
            groups.setdefault(person, []).append(e)
    out = []
    for person, members in groups.items():
        uids = {e.uid for e in members}
        if not (min_fac <= len(uids) <= max_fac):
            continue
        facs = sorted({e.name for e in members if e.name})[:10]
        mult, suffix, ob = _brand_discount([e.name for e in members if e.name])
        for e in members:
            out.append(Flag(
                e.uid, "concentration_same_contact_person", round(sev * mult, 1),
                "One contact person across many facilities",
                f"Primary contact '{e.raw.get('primarycontactpersonname')}' is listed "
                f"for {len(uids)} facilities.{suffix}",
                {"contact_person": e.raw.get("primarycontactpersonname"),
                 "facility_count": len(uids), "facilities": facs,
                 **({"open_brand": ob} if ob else {})},
            ))
    return out


def capacity_outlier_high(entities, cfg):
    sev = cfg.get("severity", 16)
    z = cfg.get("zscore", 3.0)
    min_sample = cfg.get("min_sample", 20)
    by_type = {}
    for e in entities:
        c = _cap(e)
        if c and c > 0:
            by_type.setdefault(e.entity_type, []).append((e, c))
    out = []
    for etype, pairs in by_type.items():
        caps = [c for _, c in pairs]
        if len(caps) < min_sample:
            continue
        mean, std = mean_std(caps)
        if std <= 0:
            continue
        for e, c in pairs:
            if c > mean + z * std:
                out.append(Flag(
                    e.uid, "capacity_outlier_high", sev,
                    "Unusually high licensed capacity",
                    f"Licensed capacity {int(c)} is far above the average "
                    f"{mean:.0f} for '{etype}' (>{z:g} standard deviations).",
                    {"capacity": c, "facility_type": etype,
                     "type_mean": round(mean, 1), "type_std": round(std, 1)},
                ))
    return out


def recent_license_high_capacity(entities, cfg):
    sev = cfg.get("severity", 12)
    days = cfg.get("days", 365)
    pct = cfg.get("capacity_percentile", 90)
    caps = [_cap(e) for e in entities if _cap(e)]
    threshold = percentile([c for c in caps if c], pct)
    out = []
    for e in entities:
        c = _cap(e)
        initial = parse_date(e.raw.get("initiallicensedate"))
        if not c or not initial:
            continue
        age = (today() - initial).days
        if 0 <= age <= days and c >= threshold:
            out.append(Flag(
                e.uid, "recent_license_high_capacity", sev,
                "Newly licensed with high capacity",
                f"Licensed {age} days ago with capacity {int(c)} "
                f"(top {100 - pct:.0f}% of all providers).",
                {"initial_license_date": initial.isoformat(), "days_since": age,
                 "capacity": c, "capacity_threshold": round(threshold, 1)},
            ))
    return out


def capacity_missing_or_zero(entities, cfg):
    sev = cfg.get("severity", 10)
    out = []
    for e in entities:
        c = _cap(e)
        if _active(e) and (c is None or c == 0):
            out.append(Flag(
                e.uid, "capacity_missing_or_zero", sev,
                "Active but no licensed capacity",
                "Operating status is active but licensed capacity is missing or zero.",
                {"capacity": c, "status": e.status},
            ))
    return out


def ungeocoded_address(entities, cfg):
    sev = cfg.get("severity", 10)
    out = []
    for e in entities:
        if not _active(e):
            continue
        bad_geo = e.lat is None or e.lon is None or (e.lat == 0 and e.lon == 0)
        if bad_geo or not e.address:
            out.append(Flag(
                e.uid, "ungeocoded_address", sev,
                "Missing/ungeocoded address",
                "Active provider with a missing or non-geocoded physical address.",
                {"lat": e.lat, "lon": e.lon, "address": e.address},
            ))
    return out


def missing_contact_info(entities, cfg):
    sev = cfg.get("severity", 8)
    out = []
    for e in entities:
        em = norm_email(e.raw.get("primarycontactemail"))
        ph = norm_phone(e.raw.get("primarycontactphonenumber"))
        if _active(e) and not em and not ph:
            out.append(Flag(
                e.uid, "missing_contact_info", sev,
                "No contact information",
                "Active provider has neither a primary email nor phone on file.",
                {"email": e.raw.get("primarycontactemail"),
                 "phone": e.raw.get("primarycontactphonenumber")},
            ))
    return out


CHILDCARE_RULES = [
    ("license_expired_active", license_expired_active),
    ("shared_contact_multiple_providers", shared_contact_multiple_providers),
    ("address_shared_multiple_providers", address_shared_multiple_providers),
    ("concentration_same_contact_person", concentration_same_contact_person),
    ("capacity_outlier_high", capacity_outlier_high),
    ("recent_license_high_capacity", recent_license_high_capacity),
    ("capacity_missing_or_zero", capacity_missing_or_zero),
    ("ungeocoded_address", ungeocoded_address),
    ("missing_contact_info", missing_contact_info),
    ("payment_only_certificate", payment_only_certificate),
    ("misspelled_word_in_name", misspelled_word_in_name),
    ("address_forensics", address_forensics),
]
