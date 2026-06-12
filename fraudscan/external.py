"""External money + legitimacy enrichment for operators.

Answers "who is receiving what money?" beyond what we already hold, using two reachable
federal APIs, looked up by operator name (+ state) and cached:
  - USAspending.gov — total federal awards (grants/contracts) to the recipient.
  - ProPublica Nonprofit Explorer (IRS Form 990) — EIN, latest total revenue, and the
    fact that an org is an established 501(c) (a legitimacy signal).

Per-name lookups are cached in data/cache/external.json so resolve() doesn't re-hit the
APIs. We only enrich operators (a few hundred), not every entity.
"""
import json
import os
import urllib.parse

from fraudscan import fac, http_util
from fraudscan.config import DATA_DIR
from fraudscan.registry import normalize

USASPENDING_URL = "https://api.usaspending.gov/api/v2/search/spending_by_award/"
PROPUBLICA_SEARCH = "https://projects.propublica.org/nonprofits/api/v2/search.json"
PROPUBLICA_ORG = "https://projects.propublica.org/nonprofits/api/v2/organizations/{}.json"
# USAspending requires award_type_codes from ONE category per query
_GRANT_TYPES = ["02", "03", "04", "05"]
_CONTRACT_TYPES = ["A", "B", "C", "D"]


def _cache_path(name="external.json"):
    d = os.path.join(DATA_DIR, "cache")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, name)


def _load_json(path, default):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    return default


def _awards_for_types(name, types, key):
    """Sum of exact-recipient awards, or None on a FAILED call — a failure must never
    be cached as $0 (it would silently poison the cache)."""
    body = {"filters": {"recipient_search_text": [name], "award_type_codes": types},
            "fields": ["Award Amount", "Recipient Name"], "limit": 100, "page": 1}
    try:
        # short leash: when USAspending drops connections, fail in seconds so the
        # circuit breaker can trip — not 6 minutes of retries per operator
        data = http_util.post_json(USASPENDING_URL, body, retries=2, timeout=20)
    except Exception:
        return None
    return sum((r.get("Award Amount") or 0) for r in data.get("results", [])
               if normalize(r.get("Recipient Name", "")) == key)   # exact recipient


def _federal_awards(name):
    key = normalize(name)
    g = _awards_for_types(name, _GRANT_TYPES, key)
    c = _awards_for_types(name, _CONTRACT_TYPES, key)
    if g is None and c is None:
        return None                      # both calls failed — signal, don't fake a zero
    return round((g or 0) + (c or 0), 2)


def _nonprofit(name, state):
    try:
        url = PROPUBLICA_SEARCH + "?" + urllib.parse.urlencode(
            {"q": name, "state[id]": state})
        data = http_util.get_json(url)
    except Exception:
        return None
    for o in data.get("organizations", []):
        if normalize(o.get("name", "")) == normalize(name):
            ein = o.get("ein")
            revenue = None
            try:
                det = http_util.get_json(PROPUBLICA_ORG.format(ein))
                filings = det.get("filings_with_data") or []
                if filings:
                    revenue = filings[0].get("totrevenue")
            except Exception:
                pass
            return {"ein": ein, "name": o.get("name"), "ntee": o.get("ntee_code"),
                    "revenue": revenue}
    return None


def enrich_operators(operators, state="WA", max_operators=100, progress=None):
    """Attach federal_funds + nonprofit info to each operator (cached by name).

    Only the top `max_operators` (operators arrive sorted by score) trigger live
    lookups per run; the cache persists, so coverage extends across runs.
    """
    path, fac_path = _cache_path(), _cache_path("fac.json")
    cache = _load_json(path, {})
    fac_cache = _load_json(fac_path, {})  # keyed by 9-digit EIN (persists across runs)
    budget = max_operators
    fac_budget = max_operators  # FAC is cheap once cached; cap live lookups per run
    fed_fails = 0                # circuit breaker: USAspending drops/rate-limits us
    looked_up = 0
    for i, o in enumerate(operators):
        name = o["canonical_name"]
        key = normalize(name)
        if key not in cache and budget > 0:
            budget -= 1
            fed = _federal_awards(name) if fed_fails < 3 else None
            if fed is None and fed_fails < 3:
                fed_fails += 1
                if fed_fails == 3:
                    print("  USAspending unreachable/rate-limited — skipping federal "
                          "awards for the rest of this run (will retry next run).")
            else:
                fed_fails = 0
            np_info = _nonprofit(name, state)
            # cache ONLY what succeeded — a failed call must be retried next run,
            # never remembered as $0
            if fed is not None or np_info is not None:
                cache[key] = {"federal_funds": fed, "nonprofit": np_info}
            looked_up += 1
            if looked_up % 10 == 0:      # incremental save — a killed run keeps progress
                with open(path, "w", encoding="utf-8") as fh:
                    json.dump(cache, fh)
            if progress:
                progress(i + 1)
        info = cache.get(key) or {}
        o["federal_funds"] = info.get("federal_funds") or 0.0
        np = info.get("nonprofit") or {}
        o["nonprofit_ein"] = np.get("ein")
        o["nonprofit_revenue"] = np.get("revenue")
        # FAC single-audit findings — only for operators with an EIN (cached by EIN).
        ein = fac.norm_ein(np.get("ein"))
        if len(ein) == 9:
            if ein not in fac_cache and fac_budget > 0:
                fac_budget -= 1
                try:  # cache only real results; let quota/network errors retry next run
                    fac_cache[ein] = fac.lookup_ein(ein)
                except Exception:
                    pass
            finfo = fac_cache.get(ein)
            if finfo and finfo.get("findings"):
                o["audit_findings"] = finfo["findings"]
                o["audit_flagged_amount"] = finfo.get("flagged_amount") or 0.0
                o["audit_json"] = finfo
                o["signals"] = list(o.get("signals", [])) + [fac.summary_line(finfo)]
                # independent audit findings are a strong, decisive signal → nudge rank
                boost = (10 if finfo.get("questioned_costs") else 0) + (
                    6 if finfo.get("material_weakness") else 0) + (
                    4 if finfo.get("repeat_findings") else 0)
                if boost:
                    o["combined_score"] = min(100, round(
                        (o.get("combined_score") or 0) + boost, 1))
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(cache, fh)
    with open(fac_path, "w", encoding="utf-8") as fh:
        json.dump(fac_cache, fh)
    return operators
