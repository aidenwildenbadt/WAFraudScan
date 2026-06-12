"""Generic rules for facility-style sources (CMS Provider Data categories).

Reuses the address-clustering rule (multiple distinct names at one address — a classic
billing-mill pattern) and adds two context signals supported across CMS datasets:
recently certified, and for-profit ownership (an OIG-noted fraud-risk correlate, not
proof). Leads, not conclusions.
"""
from fraudscan.rules.base import Flag, parse_date, today
# address clustering is source-agnostic — reuse it across facility categories
from fraudscan.rules.childcare import address_shared_multiple_providers
from fraudscan.rules.naming import misspelled_word_in_name
from fraudscan.rules.address import address_forensics

_FOR_PROFIT = {"FOR-PROFIT", "PROFIT", "PROPRIETARY", "FOR PROFIT"}


def _is_for_profit(v):
    s = (v or "").strip().upper()
    return s in _FOR_PROFIT or "FOR-PROFIT" in s or "PROPRIETARY" in s


def recently_certified(entities, cfg):
    sev = cfg.get("severity", 8)
    days = cfg.get("days", 365)
    out = []
    for e in entities:
        d = parse_date(e.raw.get("_cert_date"))
        if d:
            age = (today() - d).days
            if 0 <= age <= days:
                out.append(Flag(
                    e.uid, "recently_certified", sev,
                    "Recently certified",
                    f"Medicare-certified {age} days ago ({d.isoformat()}) — newer "
                    f"entrants warrant closer review.",
                    {"certification_date": d.isoformat(), "days_since": age},
                ))
    return out


def for_profit_ownership(entities, cfg):
    sev = cfg.get("severity", 5)
    out = []
    for e in entities:
        if _is_for_profit(e.raw.get("_ownership")):
            out.append(Flag(
                e.uid, "for_profit_ownership", sev,
                "For-profit ownership",
                "For-profit ownership — a fraud-risk correlate in this sector per OIG "
                "studies (context only, not an allegation).",
                {"ownership": e.raw.get("_ownership"), "facility_type": e.entity_type},
            ))
    return out


_STAR_FIELDS = ("quality_of_patient_care_star_rating", "five_star",
                "overall_rating")


def low_quality_rating(entities, cfg):
    """Low CMS Care Compare quality rating — weak care alongside public funding."""
    sev = cfg.get("severity", 8)
    max_stars = cfg.get("max_stars", 2.0)
    out = []
    for e in entities:
        rating, field = None, None
        for f in _STAR_FIELDS:
            v = e.raw.get(f)
            if v in (None, "", "Not Available", "N/A"):
                continue
            try:
                rating, field = float(v), f
                break
            except (TypeError, ValueError):
                continue
        if rating is not None and rating <= max_stars:
            out.append(Flag(
                e.uid, "low_quality_rating", sev, "Low CMS quality rating",
                f"CMS quality rating is {rating:g} of 5 (≤{max_stars:g}) — low "
                f"quality alongside public funding is worth a review.",
                {"rating": rating, "field": field},
            ))
    return out


FACILITY_RULES = [
    ("address_shared_multiple_providers", address_shared_multiple_providers),
    ("recently_certified", recently_certified),
    ("for_profit_ownership", for_profit_ownership),
    ("misspelled_word_in_name", misspelled_word_in_name),
    ("low_quality_rating", low_quality_rating),
    ("address_forensics", address_forensics),
]
