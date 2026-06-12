"""National name-frequency prior for person-name bridges.

The fleet audit showed in-corpus commonness is a bad prior: our healthcare table is an
enforcement-only slice, so "Patricia Smith" counted as unique-ish in-corpus while NPPES
lists 175 providers with that name (Richard Harris: 47). A name-only bridge to a common
name is weak evidence regardless of how many copies OUR slice happens to hold.

national_count(name) returns the number of NPI-holding providers with that name
nationwide (NLM Clinical Tables total-count field), cached in data/cache/name_freq.json.
It undercounts people without NPIs (aides, techs, childcare staff), so it's a FLOOR on
commonness — fine for the purpose (count > threshold ⇒ definitely common).
"""
import json
import os
import urllib.parse
import urllib.request

from fraudscan.config import DATA_DIR
from fraudscan.registry import normalize

NLM = "https://clinicaltables.nlm.nih.gov/api/npi_idv/v3/search"


def _cache_path():
    d = os.path.join(DATA_DIR, "cache")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "name_freq.json")


def load_cache():
    p = _cache_path()
    if os.path.exists(p):
        with open(p, "r", encoding="utf-8") as fh:
            return json.load(fh)
    return {}


def make_lookup():
    """Returns national_count(name) -> int|None with a persistent cache. Network
    failures return None (caller falls back to in-corpus counts only)."""
    cache = load_cache()

    def national_count(name):
        key = normalize(name)
        if not key:
            return None
        if key in cache:
            return cache[key]
        try:
            q = urllib.parse.urlencode([("terms", key), ("count", 1)])
            data = json.loads(urllib.request.urlopen(
                NLM + "?" + q, timeout=15).read())
            n = int(data[0])
        except Exception:
            return None
        cache[key] = n
        with open(_cache_path(), "w", encoding="utf-8") as fh:
            json.dump(cache, fh)
        return n

    return national_count
