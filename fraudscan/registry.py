"""Business-registry reference data for the registration cross-check.

Washington's authoritative registries are NOT available as a bulk API:
  - DOR Business Lookup (data.wa.gov 4wur-kfnr) is just an href to a web app.
  - SoS Corporations Search (f9jk-mm39) is an href to CCFS, whose API is bot-
    protected — we deliberately do not circumvent that.
Both portals DO support exporting search results to CSV/Excel. So the registry is
loaded from CSV files the user drops in data/registry/ (or any path in config). The
matching, normalization, and rule logic are fully built and tested regardless of how
the CSV was produced.

Drop one or more CSVs in data/registry/ and the cross-check turns on automatically.
"""
import csv
import glob
import os
import re

from fraudscan.config import ROOT

# Header substrings we treat as a business/trade name or a status column.
NAME_HINTS = ("businessname", "business name", "entityname", "entity name",
              "tradename", "trade name", "legalname", "dba", "name")
STATUS_HINTS = ("businessstatus", "entitystatus", "status", "statusdescription")

# Corporate suffixes / noise stripped so "Keller Child Care LLC" == "Keller Child Care".
_SUFFIXES = (r"\bL\.?L\.?C\.?\b", r"\bINC(ORPORATED)?\b", r"\bCORP(ORATION)?\b",
             r"\bCO\b", r"\bCOMPANY\b", r"\bL\.?L\.?P\.?\b", r"\bL\.?P\.?\b",
             r"\bP\.?L\.?L\.?C\.?\b", r"\bP\.?S\.?\b", r"\bP\.?C\.?\b",
             r"\bLTD\b", r"\bDBA\b", r"\bENTERPRISES?\b")
_SUFFIX_RE = re.compile("|".join(_SUFFIXES))
_NONALNUM = re.compile(r"[^A-Z0-9 ]")


def normalize(name):
    if not name:
        return ""
    # Drop periods first so "P.L.L.C." -> "PLLC" (not space-separated letters).
    s = str(name).upper().replace(".", "")
    s = " " + _NONALNUM.sub(" ", s) + " "
    s = _SUFFIX_RE.sub(" ", s)
    if s.strip().startswith("THE "):
        s = s.strip()[4:]
    return " ".join(s.split())


class BusinessRegistry:
    def __init__(self, active_values):
        self.active_values = {v.strip().upper() for v in active_values}
        self._index = {}      # canonical name -> {"active": bool, "status": str}
        self.row_count = 0
        self.files = []

    def add(self, name, status):
        key = normalize(name)
        if not key:
            return
        active = (status or "").strip().upper() in self.active_values if status \
            else True
        prev = self._index.get(key)
        # Keep the "most active" record for a name.
        if prev is None or (active and not prev["active"]):
            self._index[key] = {"active": active, "status": status or ""}

    def lookup(self, name):
        hit = self._index.get(normalize(name))
        if hit is None:
            return {"found": False, "active": False, "status": None}
        return {"found": True, "active": hit["active"], "status": hit["status"]}

    def best_match(self, *names):
        """True/active if ANY of the supplied names (e.g. legal + dba) matches."""
        results = [self.lookup(n) for n in names if n]
        if any(r["found"] and r["active"] for r in results):
            return {"found": True, "active": True,
                    "status": next(r["status"] for r in results
                                   if r["found"] and r["active"])}
        for r in results:
            if r["found"]:
                return r
        return {"found": False, "active": False, "status": None}


def _detect(headers, hints):
    cols = []
    for h in headers:
        hl = (h or "").strip().lower()
        if any(hint in hl for hint in hints):
            cols.append(h)
    return cols


def load_registry(config):
    """Return a populated BusinessRegistry, or None if no CSVs are present."""
    rcfg = config.get("registry", {})
    pattern = rcfg.get("csv_glob", "data/registry/*.csv")
    if not os.path.isabs(pattern):
        pattern = os.path.join(ROOT, pattern)
    paths = sorted(glob.glob(pattern))
    if not paths:
        return None

    reg = BusinessRegistry(rcfg.get("active_values", ["ACTIVE", "OPEN"]))
    name_override = rcfg.get("name_columns")
    status_override = rcfg.get("status_column")
    for path in paths:
        with open(path, newline="", encoding="utf-8-sig", errors="replace") as fh:
            reader = csv.DictReader(fh)
            headers = reader.fieldnames or []
            name_cols = name_override or _detect(headers, NAME_HINTS)
            status_cols = ([status_override] if status_override
                           else _detect(headers, STATUS_HINTS))
            status_col = status_cols[0] if status_cols else None
            if not name_cols:
                continue
            for row in reader:
                status = row.get(status_col) if status_col else None
                for nc in name_cols:
                    reg.add(row.get(nc), status)
                reg.row_count += 1
        reg.files.append(os.path.relpath(path, ROOT))
    return reg
