"""Federal-court docket context via CourtListener/RECAP (Free Law Project).

Litigation was originally out of scope because PACER is fee-gated; CourtListener's free
public API changes that. The design here is deliberately conservative, per review:

  * lookups run ONLY for leads a human already marked escalated or watched — never the
    whole database;
  * results are filtered to Washington's federal courts (W.D./E.D. Wash. + bankruptcy);
  * matches surface as 'Context & coverage' items labeled as UNVERIFIED party-name
    matches — they are never scored as flags (party names collide constantly).

An API token (free account, https://www.courtlistener.com/profile/) raises rate limits;
set COURTLISTENER_TOKEN or config courtlistener.token. Without one we still try the
anonymous API (low rate) and the dossier always carries a manual search link.
"""
import csv
import json
import os
import ssl
import urllib.parse
import urllib.request

from fraudscan.config import DATA_DIR
from fraudscan.registry import normalize

API = "https://www.courtlistener.com/api/rest/v4/search/"
COURTS = "wawd waed wawb waeb"           # WA federal district + bankruptcy courts
_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE


def _cache_path():
    d = os.path.join(DATA_DIR, "cache")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "courtlistener.json")


def _token(config=None):
    return (os.environ.get("COURTLISTENER_TOKEN")
            or (config or {}).get("courtlistener", {}).get("token") or "")


def search_url(name):
    """Manual search link (no API needed) — prefilled RECAP query in WA courts."""
    q = urllib.parse.urlencode({"q": f'"{name}"', "type": "r", "court": COURTS})
    return "https://www.courtlistener.com/?" + q


def lookup(name, token=""):
    """Top RECAP dockets in WA federal courts naming `name`. [] on any failure."""
    params = urllib.parse.urlencode(
        {"q": f'"{name}"', "type": "r", "court": COURTS, "order_by": "score desc"})
    headers = {"User-Agent": "FraudScan/0.1 research"}
    if token:
        headers["Authorization"] = f"Token {token}"
    req = urllib.request.Request(API + "?" + params, headers=headers)
    try:
        data = json.loads(urllib.request.urlopen(
            req, timeout=45, context=_CTX).read().decode("utf-8"))
    except Exception:
        return None                       # unreachable / throttled — retry next run
    out = []
    for r in (data.get("results") or [])[:5]:
        rel = (r.get("absolute_url") or r.get("docket_absolute_url") or "")
        out.append({"case": r.get("caseName") or "",
                    "court": r.get("court") or r.get("court_id") or "",
                    "filed": (r.get("dateFiled") or "")[:10],
                    "docket": r.get("docketNumber") or "",
                    "url": ("https://www.courtlistener.com" + rel) if rel
                           else search_url(r.get("caseName") or "")})
    return out


def enrich_escalated(conn, config=None, max_lookups=25):
    """Look up escalated/watched leads (long names only) and write hits into
    data/context/courts_auto.csv for the curated-context pipeline."""
    token = _token(config)
    path = _cache_path()
    cache = {}
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            cache = json.load(fh)
    rows = conn.execute(
        "SELECT DISTINCT e.name FROM entities e JOIN triage t ON t.entity_uid=e.uid "
        "WHERE (t.state='escalated' OR t.watched=1) AND e.name<>''").fetchall()
    names = [r[0] for r in rows if len(normalize(r[0])) >= 10]
    looked = wrote = 0
    items = []
    for name in names:
        key = normalize(name)
        if key in cache:
            hits = cache[key]
        elif looked < max_lookups:
            looked += 1
            hits = lookup(name, token)
            if hits is None:              # API failure: don't cache, don't proceed
                break
            cache[key] = hits
        else:
            continue
        for h in hits or []:
            items.append({"name": name, "url": h["url"],
                          "title": (f"UNVERIFIED party-name match — RECAP docket: "
                                    f"{h['case']} ({h['court']}, filed {h['filed']}, "
                                    f"{h['docket']}); confirm identity before use"),
                          "date": h["filed"], "kind": "litigation"})
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(cache, fh)
    d = os.path.join(DATA_DIR, "context")
    os.makedirs(d, exist_ok=True)
    cpath = os.path.join(d, "courts_auto.csv")
    seen = set()
    if os.path.exists(cpath):
        with open(cpath, newline="", encoding="utf-8") as fh:
            seen = {(r.get("name"), r.get("url")) for r in csv.DictReader(fh)}
    new = [r for r in items if (r["name"], r["url"]) not in seen]
    if new:
        header = not os.path.exists(cpath)
        with open(cpath, "a", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=["name", "url", "title", "date", "kind"])
            if header:
                w.writeheader()
            w.writerows(new)
    return {"candidates": len(names), "looked_up": looked, "new_context": len(new),
            "token": bool(token)}
