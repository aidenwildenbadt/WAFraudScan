"""OIG Corporate Integrity Agreements (CIAs) — entities that settled federal fraud cases.

A CIA is the compliance agreement an entity signs with HHS-OIG, almost always as part of
settling a False Claims Act / fraud investigation (the alternative being exclusion).
Being under (or recently under) a CIA is one of the strongest "history of fraud
allegations" signals that exists in public data — and prior settled conduct is the best
predictor of repeat conduct. OIG's site lists all agreements (~330) as paginated cards;
we scrape name / location / type / effective date, cached.

Matching is by normalized name (+ state corroboration when present) — surfaced as
CONTEXT and an operator chip, sized as a signal, never as proof of anything current.
"""
import json
import os
import re
import ssl
import urllib.request

from fraudscan.config import DATA_DIR

LIST_URL = ("https://oig.hhs.gov/compliance/corporate-integrity-agreements/"
            "cia-documents.asp")
_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE
_MON = {m: i for i, m in enumerate(
    "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split(), 1)}


def _cache_path():
    d = os.path.join(DATA_DIR, "cache")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "oig_cia.json")


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "FraudScan/0.1"})
    return urllib.request.urlopen(req, timeout=60, context=_CTX).read().decode(
        "utf-8", "replace")


def _iso(s):
    m = re.search(r"([A-Z][a-z]{2})\s+(\d{1,2}),\s+(\d{4})", s or "")
    if not m or m.group(1) not in _MON:
        return ""
    return f"{m.group(3)}-{_MON[m.group(1)]:02d}-{int(m.group(2)):02d}"


def _parse_cards(html):
    out = []
    # cards are <li class="usa-card …"> blocks; metadata lives in <span>s
    for card in re.split(r'<li class="usa-card', html)[1:]:
        a = re.search(r"<a[^>]*>(.*?)</a>", card, re.S)
        if not a:
            continue
        name = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", a.group(1))).strip()
        spans = [re.sub(r"\s+", " ", s).strip()
                 for s in re.findall(r"<span[^>]*>([^<]*)</span>", card)]
        city = state = kind = eff = ""
        for s in spans:
            m = re.match(r"^([A-Za-z .'-]+),\s*([A-Z]{2})$", s)
            if m:
                city, state = m.group(1).strip(), m.group(2)
            elif "Agreement" in s:
                kind = s
            elif s.startswith("Effective"):
                eff = _iso(s)
        if name:
            out.append({"name": name, "city": city, "state": state,
                        "kind": kind or "CIA", "effective": eff})
    return out


def load_cia(refresh=False):
    """Cached {normname: rec} of OIG agreements.

    COVERAGE IS PARTIAL BY DESIGN: the list endpoint ignores pagination params, so only
    the newest page (~20 agreements) is machine-readable. Those are the operationally
    relevant ones (new CIAs); the full archive is reachable via the on-demand search
    link surfaced in dossiers. `partial: True` is recorded so the UI never overclaims."""
    path = _cache_path()
    if not refresh and os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    from fraudscan.registry import normalize
    rows = _parse_cards(_get(LIST_URL))
    rows.sort(key=lambda r: r.get("effective") or "")   # latest wins the dict below
    by_name = {}
    for r in rows:
        k = normalize(r["name"])
        if k:
            by_name[k] = r
    data = {"by_name": by_name, "count": len(rows), "source": LIST_URL,
            "partial": True}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    return data
