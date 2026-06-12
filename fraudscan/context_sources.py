"""Curated context & coverage for flagged providers (Tier 2).

Investigators find the real story behind a flag — the disciplinary order, a news
investigation, an agency update. Drop a CSV in data/context/ and those links surface on
the matching provider's dossier, keyed by credential number, NPI, or name. This is the
same file-drop pattern as SAM/SOS: no scraping, no namesake guessing, fully curated.

CSV columns (auto-detected, case-insensitive); need a url plus at least one match key:
  credential / credentialnumber / license / lic   — matched on its DIGIT CORE (so
                                                     'RN00120098' == 'RN.RN.00120098')
  npi                                              — exact NPI
  name / provider                                  — normalized name (within our WA data)
  url   (required)   title / summary   date   kind (order | news | update)

Name-only matches can hit same-named providers; add a credential # or NPI to pin one.
"""
import csv
import glob
import os
import re

from fraudscan.registry import normalize

_COLS = {
    "credential": ["credential", "credentialnumber", "credential_number",
                   "credential #", "license", "license_number", "lic", "license #"],
    "npi": ["npi", "npi number"],
    "name": ["name", "provider", "provider_name", "entity", "licensee"],
    "url": ["url", "link", "source", "source_url"],
    "title": ["title", "summary", "description", "headline", "note"],
    "date": ["date", "action_date", "published", "action date"],
    "kind": ["kind", "type", "category"],
}


def _digits(s):
    return re.sub(r"\D", "", str(s or "")).lstrip("0")


def _pick(hl, cands):
    for c in cands:
        if c in hl:
            return hl[c]
    return None


class Context:
    def __init__(self):
        self.by_cred = {}   # credential digit-core -> [item]
        self.by_npi = {}    # NPI -> [item]
        self.by_name = {}   # normalized name -> [item]
        self.count = 0

    def add(self, item, cred="", npi="", name=""):
        dc = _digits(cred)
        if dc and len(dc) >= 3:
            self.by_cred.setdefault(dc, []).append(item)
        npi = (npi or "").strip()
        if npi:
            self.by_npi.setdefault(npi, []).append(item)
        if name:
            self.by_name.setdefault(normalize(name), []).append(item)
        self.count += 1

    def lookup(self, credentialnumber=None, npi=None, name=None):
        """Context items matching this provider (deduped by url), best key first."""
        out, seen = [], set()

        def push(items, via):
            for it in items:
                if it["url"] in seen:
                    continue
                seen.add(it["url"])
                out.append(dict(it, matched_via=via))
        dc = _digits(credentialnumber)
        if dc and dc in self.by_cred:
            push(self.by_cred[dc], "credential")
        if npi and str(npi).strip() in self.by_npi:
            push(self.by_npi[str(npi).strip()], "NPI")
        if name and normalize(name) in self.by_name:
            push(self.by_name[normalize(name)], "name")
        return out


def load_context(data_dir):
    """Load every CSV under data_dir/context/ into a Context index (empty if none)."""
    ctx = Context()
    d = os.path.join(data_dir, "context")
    if not os.path.isdir(d):
        return ctx
    paths = glob.glob(os.path.join(d, "*.csv")) + glob.glob(os.path.join(d, "*.CSV"))
    for path in sorted(set(os.path.realpath(p) for p in paths)):
        try:
            with open(path, newline="", encoding="utf-8-sig", errors="replace") as fh:
                header = next(csv.reader(fh))
        except (StopIteration, OSError):
            continue
        hl = {h.strip().lower(): h for h in header}
        cols = {k: _pick(hl, v) for k, v in _COLS.items()}
        if not cols["url"]:
            continue
        with open(path, newline="", encoding="utf-8-sig", errors="replace") as fh:
            for r in csv.DictReader(fh):
                def g(field):
                    col = cols[field]
                    return (r.get(col) or "").strip() if col else ""
                url = g("url")
                if not url:
                    continue
                item = {"url": url, "title": g("title"), "date": g("date"),
                        "kind": g("kind").lower()}
                ctx.add(item, cred=g("credential"), npi=g("npi"), name=g("name"))
    return ctx
