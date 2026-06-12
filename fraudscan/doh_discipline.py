"""Tier 1 — auto-ingest DOH disciplinary-action narratives.

The DOH Newsroom posts "State disciplines health care providers (MM-DD-YYYY)" releases,
each listing providers as `Name (CREDENTIAL#)` with a plain-English reason
("…agreed order requiring a $5,000 fine and an ethics assessment…"). We crawl the
disciplinary-actions archive, parse those entries, and match them to our flagged
providers by the credential **digit-core** (the same join used for license confirmation).

The result is written to data/context/doh_discipline.csv so the existing curated-context
loader (Tier 2) surfaces it on each provider's dossier — no new rendering path. Raw posts
are cached so re-runs only fetch new releases.

This is HTML parsing of a government newsroom, so it is deliberately lenient and bounded;
it augments, never replaces, the authoritative Provider Credential Search.
"""
import csv
import json
import os
import re
import urllib.request

from fraudscan.config import DATA_DIR

ARCHIVE = "https://doh.wa.gov/newsroom/archive/category/disciplinary-actions"
POST_BASE = "https://doh.wa.gov/newsroom/"
_SLUG = re.compile(r"(state-(?:disciplines|revokes|suspends|charges)[a-z0-9-]*)")
_ENTRY = re.compile(r"([A-Z][A-Za-z.'\-]+(?:\s[A-Z][A-Za-z.'\-]+){1,3})\s*"
                    r"\(([A-Z]{2}\d{8})\)")
_DATE = re.compile(r"(\d{2})-(\d{2})-(\d{4})")


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "FraudScan/0.1 research"})
    return urllib.request.urlopen(req, timeout=60).read().decode("utf-8", "replace")


def _text(html):
    t = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", t.replace("&nbsp;", " ").replace("&amp;", "&")).strip()


def _slug_date(slug):
    m = _DATE.search(slug)
    return f"{m.group(3)}-{m.group(1)}-{m.group(2)}" if m else ""


def _reason(text, start, end):
    """The action sentence containing the credential, plus the following detail sentence."""
    s = text.rfind(". ", 0, start)
    s = s + 2 if s != -1 and start - s < 240 else max(0, start - 140)
    nxt = text.find(". ", end)
    nxt = text.find(". ", nxt + 2) if nxt != -1 else -1
    e = nxt + 1 if nxt != -1 and nxt - end < 320 else min(len(text), end + 200)
    return re.sub(r"^[A-Z][a-z]+ County ", "", text[s:e].strip())[:320]


def _entries_from_text(text, date, url):
    out = []
    for m in _ENTRY.finditer(text):
        out.append({"name": m.group(1).strip(), "credential": m.group(2),
                    "reason": _reason(text, m.start(), m.end()), "date": date,
                    "url": url})
    return out


def _parse_post(url):
    return _entries_from_text(_text(_get(url)), _slug_date(url), url)


def _archive_post_urls(max_pages=24):
    urls = []
    for page in range(max_pages):
        try:
            html = _get(ARCHIVE + (f"?page={page}" if page else ""))
        except Exception:
            break
        slugs = [s for s in dict.fromkeys(_SLUG.findall(html))]
        new = [POST_BASE + s for s in slugs if POST_BASE + s not in urls]
        if not new and page:
            break
        urls.extend(new)
    return urls


def _cache_path(name):
    d = os.path.join(DATA_DIR, "cache")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, name)


def build_discipline(db_credentials=None, refresh=False, progress=None):
    """Crawl + parse DOH disciplinary posts (cached), then write
    data/context/doh_discipline.csv filtered to credentials in db_credentials (digit-core
    set). Returns {posts, entries, matched}."""
    raw_path = _cache_path("doh_discipline_raw.json")
    cache = {}
    if not refresh and os.path.exists(raw_path):
        with open(raw_path, "r", encoding="utf-8") as fh:
            cache = json.load(fh)
    for i, url in enumerate(_archive_post_urls() + _wmc_post_urls()):
        if url not in cache:
            try:
                cache[url] = _parse_post(url)
            except Exception:
                cache[url] = []
            if progress:
                progress(i + 1)
    with open(raw_path, "w", encoding="utf-8") as fh:
        json.dump(cache, fh)

    def _core(c):
        return re.sub(r"\D", "", c or "").lstrip("0")
    # newest first, dedupe by credential (keep the most recent action)
    entries, seen = [], set()
    for url in sorted(cache, key=lambda u: _slug_date(u), reverse=True):
        for e in cache[url]:
            core = _core(e["credential"])
            if not core or core in seen:
                continue
            if db_credentials is not None and core not in db_credentials:
                continue
            seen.add(core)
            entries.append(e)
    out_dir = os.path.join(DATA_DIR, "context")
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "doh_discipline.csv"), "w", newline="",
              encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["credential", "npi", "name", "url", "title", "date", "kind"])
        for e in entries:
            w.writerow([e["credential"], "", e["name"], e["url"],
                        f"DOH disciplinary action: {e['reason']}", e["date"], "order"])
    total = sum(len(v) for v in cache.values())
    return {"posts": len(cache), "entries": total, "matched": len(entries)}


WMC_NEWS = "https://wmc.wa.gov/news"


def _wmc_post_urls(cap=40):
    """G12: the Washington Medical Commission publishes its OWN news releases (MD/PA
    actions never reach the DOH newsroom — Steneker's Oct 2024 restriction was
    invisible). Bounded, lenient crawl of the WMC news index."""
    try:
        html = _get(WMC_NEWS)
    except Exception:
        return []
    hrefs = re.findall(r'href="(/news/[a-z0-9-]+|https://wmc\.wa\.gov/news/'
                       r'[a-z0-9-]+)"', html)
    out = []
    for h in dict.fromkeys(hrefs):
        url = h if h.startswith("http") else "https://wmc.wa.gov" + h
        out.append(url)
        if len(out) >= cap:
            break
    return out


_BAR_WORDS = re.compile(r"revok|suspend|surrender|summary action|practice restrict",
                        re.I)


def load_discipline_bars():
    """{credential digit-core: (date, url)} for dated orders whose reason is a license
    bar — lets a dated DISCIPLINARY ORDER replace the expiry proxy as bar provenance."""
    raw_path = _cache_path("doh_discipline_raw.json")
    if not os.path.exists(raw_path):
        return {}
    with open(raw_path, "r", encoding="utf-8") as fh:
        cache = json.load(fh)
    bars = {}
    for entries in cache.values():
        for en in entries or []:
            if not en.get("date") or not _BAR_WORDS.search(en.get("reason") or ""):
                continue
            core = re.sub(r"\D", "", en.get("credential") or "").lstrip("0")
            if not core:
                continue
            cur = bars.get(core)
            if cur is None or en["date"] < cur[0]:
                bars[core] = (en["date"], en.get("url") or "")
    return bars
