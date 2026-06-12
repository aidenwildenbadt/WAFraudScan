"""DOJ U.S. Attorney (W.D. & E.D. Washington) press releases — auto-ingested context.

Healthcare-fraud prosecutions and False Claims Act settlements are announced by the
USAOs with full names in the headline. We pull the offices' RSS feeds, keep fraud-
related items, and match them against OUR flagged entities by full-name containment
(long names only — namesake-guarded). Matches are written to data/context/doj_auto.csv,
which the existing curated-context loader surfaces on the matching dossiers as
'Context & coverage' items. Like all context: a lead to read, never an assertion.
"""
import csv
import os
import re
import ssl
import urllib.request
import xml.etree.ElementTree as ET

from fraudscan.config import DATA_DIR
from fraudscan.registry import normalize

FEEDS = ["https://www.justice.gov/news/rss?type=press_release&component=usao-wdwa",
         "https://www.justice.gov/news/rss?type=press_release&component=usao-edwa"]
_KEYWORDS = ("fraud", "false claims", "medicaid", "medicare", "kickback",
             "embezzle", "health care", "healthcare", "billing")
_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "FraudScan/0.1"})
    return urllib.request.urlopen(req, timeout=60, context=_CTX).read()


def fetch_items():
    """Fraud-related press-release items: {title, url, date, office}."""
    out = []
    for feed in FEEDS:
        try:
            root = ET.fromstring(_get(feed))
        except Exception:
            continue
        office = "USAO-WDWA" if "wdwa" in feed else "USAO-EDWA"
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            date = (item.findtext("pubDate") or "").strip()
            desc = (item.findtext("description") or "")
            hay = (title + " " + desc).lower()
            if link and any(k in hay for k in _KEYWORDS):
                out.append({"title": title, "url": link, "date": date,
                            "office": office})
    return out


def ingest(conn, min_name_len=10):
    """Match feed items to flagged entities; write data/context/doj_auto.csv rows.
    Returns (items_seen, matches_written). Name containment only for LONG names —
    short/common names would manufacture namesake hits."""
    items = fetch_items()
    if not items:
        return 0, 0
    ents = [(r[0], normalize(r[0] or "")) for r in conn.execute(
        "SELECT DISTINCT name FROM entities WHERE uid IN "
        "(SELECT entity_uid FROM scores WHERE risk_score >= 20)")]
    ents = [(n, k) for n, k in ents if len(k) >= min_name_len]
    rows = []
    for it in items:
        tkey = normalize(re.sub(r"<[^>]+>", " ", it["title"]))
        for name, key in ents:
            if key and key in tkey:
                rows.append({"name": name, "url": it["url"],
                             "title": f"{it['office']}: {it['title']}",
                             "date": it["date"][:16], "kind": "news"})
    d = os.path.join(DATA_DIR, "context")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "doj_auto.csv")
    seen = set()
    if os.path.exists(path):
        with open(path, newline="", encoding="utf-8") as fh:
            seen = {(r.get("name"), r.get("url")) for r in csv.DictReader(fh)}
    new = [r for r in rows if (r["name"], r["url"]) not in seen]
    if new:
        write_header = not os.path.exists(path)
        with open(path, "a", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=["name", "url", "title", "date", "kind"])
            if write_header:
                w.writeheader()
            w.writerows(new)
    return len(items), len(new)
