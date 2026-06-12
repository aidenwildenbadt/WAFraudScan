"""WA Secretary of State business-identity linking (UBI / registered agent / officers).

WA SOS retired its free bulk "Corporations Data Extract", and the live CCFS search API is
bot-gated, so this loads a SOS export you drop in data/sos/ (from the CCFS Advanced
Business Search — the green CSV icon — or any CSV carrying these columns). With it we link
our entities that share a hard business identity, far stronger than fuzzy name + geo:

  ubi:<UBI>      same registered legal entity across our programs (strongest)
  agent:<agent>  same registered agent across differently-named entities (shell signal)
  gov:<officer>  same governing person/officer across entities (common control)

Columns are auto-detected case-insensitively. Governing persons may be one ';'/'|'-
separated column or repeated rows per business. Absent directory/file => no-op.
"""
import csv
import glob
import os

from fraudscan.registry import normalize

# candidate header names (lower-cased) -> canonical field
_FIELDS = {
    "name": ["business name", "businessname", "entity name", "entityname", "name",
             "title"],
    "ubi": ["ubi number", "ubinumber", "ubi", "ubi #", "ubi no", "ubi_number"],
    "status": ["status", "business status", "businessstatus", "entity status"],
    "agent": ["registered agent name", "registered agent", "agent name", "agentname",
              "agent", "ra name"],
    "address": ["principal office", "principal office address", "address",
                "principal office street address", "principal address"],
    "governors": ["governors", "governing persons", "governing person", "governor",
                  "officers", "governing people", "governing_persons"],
}


def _pick(header_lower_map, candidates):
    for c in candidates:
        if c in header_lower_map:
            return header_lower_map[c]
    return None


def _split_govs(v):
    if not v:
        return []
    for sep in (";", "|", "/"):
        if sep in v:
            return [g.strip() for g in v.split(sep) if g.strip()]
    return [v.strip()] if v.strip() else []


class Sos:
    def __init__(self):
        self.by_name = {}   # normalized name -> record
        self.by_ubi = {}    # UBI -> record
        self.count = 0

    def add(self, rec):
        key = normalize(rec.get("name"))
        if key:
            self.by_name.setdefault(key, rec)
        ubi = rec.get("ubi")
        if ubi:
            self.by_ubi.setdefault(ubi, rec)
        self.count += 1

    def match(self, name, dba=None):
        for nm in (name, dba):
            k = normalize(nm)
            if k and k in self.by_name:
                return self.by_name[k]
        return None


def load_sos(data_dir):
    """Load every CSV under data_dir/sos/ into an Sos index (empty if none)."""
    scr = Sos()
    d = os.path.join(data_dir, "sos")
    if not os.path.isdir(d):
        return scr
    paths = glob.glob(os.path.join(d, "*.csv")) + glob.glob(os.path.join(d, "*.CSV"))
    for path in sorted(set(os.path.realpath(p) for p in paths)):
        try:
            with open(path, newline="", encoding="utf-8-sig", errors="replace") as fh:
                header = next(csv.reader(fh))
        except (StopIteration, OSError):
            continue
        hl = {h.strip().lower(): h for h in header}
        cols = {f: _pick(hl, cands) for f, cands in _FIELDS.items()}
        if not cols["name"]:
            continue
        with open(path, newline="", encoding="utf-8-sig", errors="replace") as fh:
            for r in csv.DictReader(fh):
                def g(field):
                    col = cols[field]
                    return (r.get(col) or "").strip() if col else ""
                rec = {"name": g("name"), "ubi": g("ubi"), "status": g("status"),
                       "agent": g("agent"), "address": g("address"),
                       "governors": _split_govs(g("governors"))}
                if rec["name"]:
                    scr.add(rec)
    return scr


def sos_keys(entity, sos):
    """Linking keys (and the matched SOS record) for an entity, or ([], None)."""
    rec = sos.match(entity.name, entity.dba) if sos else None
    if not rec:
        return [], None
    keys = []
    if rec.get("ubi"):
        keys.append("ubi:" + normalize(rec["ubi"]))
    ag = normalize(rec.get("agent"))
    if ag and len(ag) >= 4:
        keys.append("agent:" + ag)
    for gname in rec.get("governors", []):
        gk = normalize(gname)
        if gk and len(gk) >= 4:
            keys.append("gov:" + gk)
    return keys, rec
