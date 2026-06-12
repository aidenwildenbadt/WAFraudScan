"""Childcare enforcement context from findchildcarewa.org (DCYF "Child Care Check").

Childcare is our anchor domain but, unlike nursing (CMS deficiencies/penalties), WA does
NOT publish per-provider childcare complaint/inspection history as open data — it lives
only on the findchildcarewa provider portal, whose per-provider page id IS our childcare
`source_id` (a WACOMPASS Salesforce id). This scrapes that page for the enforcement
signals we otherwise can't see:

  - valid complaints  (DCYF "Child Care Check" posts investigated/valid complaints; rare,
                       so high-signal when present)
  - inspections       (routine count — context, not a violation by itself)

Each provider page is fetched once, cached + resumable (data/cache/childcare_enforcement
.json). The complaint signal becomes a scored flag; inspections are context. This augments
— never replaces — the authoritative portal (which every childcare dossier links to).

ETHICS GUARDRAIL: the provider page also lists languages spoken, tribal affiliation, and
similar attributes. We deliberately do NOT parse, store, or score on language, ethnicity,
national origin, religion, or tribal status. FraudScan flags on money, oversight gaps, and
verifiable contradictions — never on who a provider's families are. Targeting providers by
demographic profile is discriminatory, unlawful, and produces false leads; it is out of
scope by design.
"""
import json
import os
import re
import ssl
import urllib.request

from fraudscan.config import DATA_DIR

PROVIDER_URL = "https://www.findchildcarewa.org/PSS_Provider?id="
# the portal's cert chain doesn't verify in some sandboxes; this is a read-only public
# page, so fall back to an unverified context rather than failing the whole scrape.
_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE
_DATE = re.compile(r"\d{1,2}/\d{1,2}/\d{4}")


def _cache_path():
    d = os.path.join(DATA_DIR, "cache")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "childcare_enforcement.json")


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "FraudScan/0.1 research"})
    return urllib.request.urlopen(req, timeout=30, context=_CTX).read().decode(
        "utf-8", "replace")


def _pane(html, pid):
    m = re.search(r'id="%s"[^>]*>' % pid, html)
    if not m:
        return ""
    start = m.end()
    nxt = re.search(r'<div class="tab-pane', html[start:])
    return html[start:start + (nxt.start() if nxt else 4000)]


def _field(html, label):
    """Value of a 'Label:' field rendered as <p class="form-control-static">VALUE</p>."""
    m = re.search(re.escape(label) + r":</label>.*?"
                  r'<p class="form-control-static">(.*?)</p>', html, re.S)
    return re.sub(r"<[^>]+>", " ", m.group(1)).strip() if m else ""


def parse_provider(html):
    """Enforcement + public-funding signals from a PSS_Provider page:
      complaints / inspections — MM/DD/YYYY table rows ('No ... available' = zero)
      subsidy / food_program   — WCCC subsidy + USDA CACFP participation (public-fund
                                 streams; CACFP is the program at the center of the
                                 Feeding-Our-Future-style fraud cases)
      facility_type            — Child Care Center vs Family Child Care Home (oversight
                                 context). Deliberately does NOT capture language /
                                 ethnicity / tribal / religious fields — see module note."""
    comp = _pane(html, "complaints")
    insp = _pane(html, "inspections")
    return {
        "complaints": 0 if "No Provider Cases available" in comp else len(_DATE.findall(comp)),
        "inspections": 0 if "No Inspection Data available" in insp else len(_DATE.findall(insp)),
        "subsidy": _field(html, "Subsidy Participation"),
        "food_program": _field(html, "Food Program Participation"),
        "facility_type": _field(html, "Facility Type"),
    }


def build_enforcement(conn, cap=100000, refresh=False, progress=None):
    """Fetch + parse findchildcarewa pages for childcare providers (cached/resumable).
    Returns {entity_uid: {complaints, inspections}} for providers with any data."""
    path = _cache_path()
    cache = {}
    if not refresh and os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            cache = json.load(fh)
    rows = conn.execute(
        "SELECT uid, source_id FROM entities WHERE source='childcare' "
        "AND source_id LIKE '001%'").fetchall()
    n = 0
    for uid, sid in rows:
        if sid in cache:
            continue
        if n >= cap:
            break
        try:
            cache[sid] = parse_provider(_get(PROVIDER_URL + sid))
        except Exception:
            cache[sid] = {}
        n += 1
        if progress and n % 50 == 0:
            progress(n)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(cache, fh)
    # map back onto entity uids
    out = {}
    for uid, sid in rows:
        d = cache.get(sid)
        if d and any(d.get(k) for k in ("complaints", "inspections", "subsidy",
                                        "food_program")):
            out[uid] = d
    return out, n
