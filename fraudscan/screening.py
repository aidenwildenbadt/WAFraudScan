"""Exclusion / sanction screening — the #1 program-integrity check.

Loads authoritative federal lists of parties barred from public health programs and
indexes them by NPI and by (normalized name, state). A provider/operator that matches
while still operating or being paid is the strongest single lead the tool produces
(billing federal programs while excluded is per-se unlawful).

Live, reachable sources:
  - OIG LEIE  (List of Excluded Individuals/Entities) — oig.hhs.gov CSV, ~83k records.
  - CMS Revoked Medicare Providers — data.cms.gov data-api.
SAM.gov debarment needs a (free) API key; drop a CSV in data/screening/ and it loads.

The LEIE file is cached under data/cache/ so scoring doesn't re-download it.
"""
import csv
import glob
import io
import json
import os
import re
import urllib.request

from fraudscan import http_util
from fraudscan.config import DATA_DIR, ensure_data_dir
from fraudscan.registry import normalize

LEIE_URL = "https://oig.hhs.gov/exclusions/downloadables/UPDATED.csv"
CMS_REVOKED_UUID = "a6496a7d-4e19-479a-a9ad-d4c0a49e07c3"
_PLACEHOLDER_NPI = {"", "0", "0000000000"}

# OIG LEIE exclusion authorities. 1128(a)* are MANDATORY exclusions (the serious cases —
# criminal convictions, min 5 years); 1128(b)* are PERMISSIVE (license actions, defaults,
# control relationships). Tiering by this distinction is a precision lever: a mandatory
# conviction-based exclusion is a far harder lead than a permissive license lapse.
EXCL_CODES = {
    "1128a1": "Conviction of a program-related crime",
    "1128a2": "Conviction relating to patient abuse or neglect",
    "1128a3": "Felony conviction relating to health care fraud",
    "1128a4": "Felony conviction relating to a controlled substance",
    "1128b1": "Misdemeanor conviction relating to health care fraud",
    "1128b2": "Conviction relating to fraud in a non-health-care program",
    "1128b3": "Conviction relating to obstruction of an investigation",
    "1128b4": "License revocation, suspension, or surrender",
    "1128b5": "Exclusion/suspension under a federal or state health program",
    "1128b6": "Claims for excessive charges or medically unnecessary services",
    "1128b7": "Fraud, kickbacks, and other prohibited activities",
    "1128b8": "Entity controlled by a sanctioned individual",
    "1128b14": "Default on a health-education loan or scholarship",
    "1128b15": "Individual controlling a sanctioned entity",
    "1128b16": "Making false statements or misrepresentations of material fact",
}


def decode_exclusion(code):
    """(human description, mandatory?) for an OIG exclusion-authority code.

    mandatory is True for 1128(a)* (conviction-based), False for permissive (b/c)*,
    None when the code is unrecognized.
    """
    c = re.sub(r"[^0-9a-z]", "", (code or "").lower())
    desc = EXCL_CODES.get(c)
    if c.startswith("1128a"):
        mand = True
    elif c.startswith("1128b") or c.startswith("1128c"):
        mand = False
    else:
        mand = None
    return (desc or (code or "")), mand


def _fmt_ymd(s):
    """LEIE dates are YYYYMMDD; render YYYY-MM-DD (and drop 0/blank placeholders)."""
    s = (s or "").strip()
    if len(s) == 8 and s.isdigit() and s != "00000000":
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return "" if s in ("", "0", "00000000") else s


def _norm_city(s):
    return re.sub(r"[^A-Z]", "", (s or "").upper())


def _year_of(s):
    m = re.search(r"(19|20)\d\d", str(s or ""))
    return int(m.group()) if m else None


def _type_overlap(a, b):
    """Loose token overlap between a DOH entity_type and a LEIE provider type/specialty
    (e.g. 'Physician And Surgeon' vs 'PHYSICIAN (MD/DO)')."""
    ta = {t for t in re.findall(r"[a-z]+", (a or "").lower()) if len(t) > 3}
    tb = {t for t in re.findall(r"[a-z]+", (b or "").lower()) if len(t) > 3}
    return bool(ta & tb)


def _lic_core(s):
    """Comparable digit core of a license/credential number — 'MD.MD.00026765' and
    'MD00026765' both -> '26765'."""
    return re.sub(r"\D", "", str(s or "")).lstrip("0")


class Screening:
    def __init__(self):
        self.by_npi = {}                # npi -> match dict
        self.by_name_state = {}         # (normname, STATE) -> match dict
        self.by_license = {}            # license digit-core -> match dict (WA lists)
        self.lists = {}                 # list name -> count
        self.total = 0

    def add(self, list_name, npi, name, state, reason, date, url="", extra=None,
            license_no=None):
        rec = {"list": list_name, "reason": reason or "", "date": date or "",
               "matched_name": name, "url": url, "city": "", "provider_type": "",
               "dob": "", "address": "", "excltype": "", "excltype_desc": "",
               "mandatory": None, "state": (state or "").strip()}
        rec.update(extra or {})
        npi = (npi or "").strip()
        if npi not in _PLACEHOLDER_NPI:
            self.by_npi.setdefault(npi, rec)
        lic = _lic_core(license_no)
        if lic and len(lic) >= 4:
            self.by_license.setdefault(lic, rec)
        key = normalize(name)
        st = (state or "").strip().upper()
        if key and st:
            self.by_name_state.setdefault((key, st), rec)
        self.lists[list_name] = self.lists.get(list_name, 0) + 1
        self.total += 1

    def match(self, npi=None, name=None, state=None, city=None, entity_type=None,
              birth_year=None, license_no=None):
        """Return a graded match. An NPI or license-number hit is 'definitive' identity;
        a name+state hit is graded 'corroborated' (birth year, city, or provider-type
        also align) or 'name-only' (verify — possible namesake). A confident
        DOB/birth-year MISMATCH drops the match entirely (different person)."""
        if npi:
            hit = self.by_npi.get(str(npi).strip())
            if hit:
                return dict(hit, matched_via="NPI", confidence="definitive",
                            corroboration="NPI present on the exclusion record")
        lic = _lic_core(license_no)
        if lic and len(lic) >= 4:
            hit = self.by_license.get(lic)
            if hit:
                return dict(hit, matched_via="license #", confidence="definitive",
                            corroboration="credential number on the exclusion record")
        if name and state:
            hit = self.by_name_state.get((normalize(name), state.strip().upper()))
            if hit:
                return self._grade(hit, city, entity_type, birth_year)
        return None

    @staticmethod
    def _grade(hit, city, entity_type, birth_year=None):
        notes, conf = [], "name-only"
        # DOB / birth-year: the strongest cheap disambiguator. DOH credential data carries
        # birthyear; LEIE carries DOB. A confident mismatch means it is a different person.
        dob_y = _year_of(hit.get("dob"))
        by = _year_of(birth_year)
        if dob_y and by:
            if dob_y == by:
                conf = "corroborated"
                notes.append(f"birth year matches ({by})")
            else:
                return None  # DOB mismatch -> namesake; do not raise a false lead
        rec_city, in_city = _norm_city(hit.get("city")), _norm_city(city)
        if rec_city and in_city:
            if rec_city == in_city:
                conf = "corroborated"
                notes.append(f"city matches ({hit.get('city')})")
            else:
                notes.append(f"city differs ({city} vs {hit.get('city')}) — "
                             f"possible namesake")
        if entity_type and hit.get("provider_type") and _type_overlap(
                entity_type, hit.get("provider_type")):
            if conf == "name-only":
                conf = "corroborated"
            notes.append(f"provider type aligns ({hit.get('provider_type')})")
        return dict(hit, matched_via="name+state", confidence=conf,
                    corroboration="; ".join(notes) or "name + state only")


def _cache_path(name):
    d = os.path.join(DATA_DIR, "cache")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, name)


def _load_leie(scr, refresh=False):
    ensure_data_dir()
    path = _cache_path("leie.csv")
    if refresh or not os.path.exists(path):
        req = urllib.request.Request(LEIE_URL,
                                     headers={"User-Agent": "FraudScan/0.1 research"})
        data = urllib.request.urlopen(req, timeout=90).read()
        with open(path, "wb") as fh:
            fh.write(data)
    with open(path, "r", encoding="latin-1") as fh:
        rdr = csv.DictReader(fh)
        for r in rdr:
            # LEIE marks "still excluded" with REINDATE 00000000 (or blank); a real
            # date means the party was reinstated and is no longer excluded.
            if (r.get("REINDATE") or "").strip() not in ("", "0", "00000000"):
                continue
            name = r.get("BUSNAME") or " ".join(
                p for p in (r.get("FIRSTNAME"), r.get("LASTNAME")) if p)
            code = r.get("EXCLTYPE") or ""
            desc, mand = decode_exclusion(code)
            extra = {"excltype": code, "excltype_desc": desc, "mandatory": mand,
                     "dob": _fmt_ymd(r.get("DOB")), "address": r.get("ADDRESS") or "",
                     "city": r.get("CITY") or "", "zip": r.get("ZIP") or "",
                     "provider_type": " ".join(p for p in (r.get("GENERAL"),
                                                           r.get("SPECIALTY")) if p)}
            scr.add("OIG LEIE", r.get("NPI"), name, r.get("STATE"),
                    desc or code, _fmt_ymd(r.get("EXCLDATE")),
                    "https://oig.hhs.gov/exclusions/exclusions_list.asp", extra=extra)


def _load_cms_revoked(scr, state="WA"):
    rows = http_util.cms_dataapi_fetch(CMS_REVOKED_UUID,
                                       filters={"STATE_CD": state})
    for r in rows:
        name = r.get("ORG_NAME") or " ".join(
            p for p in (r.get("FIRST_NAME"), r.get("LAST_NAME")) if p)
        scr.add("CMS Revoked Medicare", r.get("NPI"), name, r.get("STATE_CD"),
                r.get("REVOCATION_RSN"), r.get("REVOCATION_EFCTV_DT"),
                "https://data.cms.gov/provider-characteristics/medicare-provider"
                "-supplier-enrollment/revoked-providers-and-suppliers")


def _add_sam_extract(scr, path, state):
    """SAM.gov Exclusions Public Extract V2 (auto-detected by its header)."""
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as fh:
        for r in csv.DictReader(fh):
            if (r.get("Record Status") or "").strip().lower() != "active":
                continue  # only currently-active exclusions
            cls = (r.get("Classification") or "").strip().lower()
            if cls == "individual":
                name = " ".join(p for p in (r.get("First"), r.get("Last")) if p)
            else:
                name = r.get("Name") or " ".join(
                    p for p in (r.get("First"), r.get("Last")) if p)
            st = (r.get("State / Province") or "").strip().upper()
            npi = (r.get("NPI") or "").strip()
            # keep only what we can match: an NPI, or a record in our state
            if not (npi not in _PLACEHOLDER_NPI or st == state.strip().upper()):
                continue
            reason = " ".join(p for p in (r.get("Exclusion Type"),
                                          r.get("Excluding Agency")) if p)
            scr.add("SAM.gov", npi, name, st, reason, r.get("Active Date"),
                    "https://sam.gov/content/exclusions")


def _add_generic_csv(scr, path):
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as fh:
        for r in csv.DictReader(fh):
            low = {k.lower(): v for k, v in r.items()}
            name = (low.get("name") or low.get("entity") or low.get("busname")
                    or low.get("lastname") or "")
            scr.add(os.path.basename(path), low.get("npi"), name, low.get("state"),
                    low.get("reason") or low.get("excltype"),
                    low.get("date") or low.get("excldate"))


def _load_local_csvs(scr, state="WA"):
    """Extra lists dropped in data/screening/ — SAM.gov extracts are auto-detected."""
    d = os.path.join(DATA_DIR, "screening")
    if not os.path.isdir(d):
        return
    paths = glob.glob(os.path.join(d, "*.csv")) + glob.glob(os.path.join(d, "*.CSV"))
    for path in sorted(set(os.path.realpath(p) for p in paths)):
        try:
            with open(path, newline="", encoding="utf-8-sig", errors="replace") as fh:
                header = next(csv.reader(fh))
        except (StopIteration, OSError):
            continue
        if "Classification" in header and "Exclusion Type" in header:
            _add_sam_extract(scr, path, state)
        else:
            _add_generic_csv(scr, path)


# WA's OWN Medicaid exclusion lists (public xlsx, updated ~monthly). These are the
# state-level analog of the LEIE — termination *for cause* from the very program whose
# dollars we hold (Medicaid-by-NPI), with License #s that match DOH credentials exactly.
HCA_LIST_URL = ("https://www.hca.wa.gov/assets/billers-and-providers/"
                "termination-exclusion.xlsx")
DSHS_LIST_URL = ("https://www.hca.wa.gov/assets/billers-and-providers/"
                 "termination-exclusion-dshs.xlsx")
HCA_PAGE = ("https://www.hca.wa.gov/billers-providers-partners/"
            "become-apple-health-provider/provider-termination-and-exclusion-list")


def _fetch_cached(url, cache_name, refresh=False):
    path = _cache_path(cache_name)
    if refresh or not os.path.exists(path):
        req = urllib.request.Request(url, headers={"User-Agent": "FraudScan/0.1"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = resp.read()
        with open(path, "wb") as fh:
            fh.write(data)
    with open(path, "rb") as fh:
        return fh.read()


def _load_hca_lists(scr, refresh=False):
    from fraudscan.xlsx_util import xlsx_rows, excel_date
    # HCA list: header row 'Name | License # | NPI # or P1 # | Date of Exclusion | Action'
    rows = xlsx_rows(_fetch_cached(HCA_LIST_URL, "hca_termination.xlsx", refresh))
    started = False
    for r in rows:
        cells = [str(x).strip() for x in (r + [""] * 5)[:5]]
        if not started:
            started = cells[0] == "Name" and "License" in cells[1]
            continue
        name, lic, ids, date_serial, action = cells
        if not name:
            continue
        date = excel_date(date_serial) or str(date_serial)[:10]
        npis = re.findall(r"\b1\d{9}\b", ids)        # NPIs start with 1, 10 digits
        # multi-line cells bundle an org + its people; index each line as a name
        names = [n.strip() for n in name.split("\n") if n.strip()]
        for i, nm in enumerate(names):
            scr.add("WA HCA terminated (Medicaid)",
                    npis[i] if i < len(npis) else (npis[0] if npis else None),
                    nm, "WA", action, date, url=HCA_PAGE, license_no=lic,
                    extra={"provider_type": "", "excltype_desc": action})
        for npi in npis[len(names):]:                # extra NPIs beyond named lines
            scr.add("WA HCA terminated (Medicaid)", npi, names[0] if names else "",
                    "WA", action, date, url=HCA_PAGE, license_no=lic)
    # DSHS list: 'Name | Location Address | DSHS License Number | Date of Exclusion | P1'
    rows = xlsx_rows(_fetch_cached(DSHS_LIST_URL, "dshs_exclusion.xlsx", refresh))
    for r in rows:
        cells = [str(x).strip() for x in (r + [""] * 5)[:5]]
        if cells[0] in ("", "Name") or "Name" in cells[0][:5] and "Address" in cells[1]:
            continue
        name, addr, lic, date_serial = cells[0], cells[1], cells[2], cells[3]
        scr.add("WA DSHS excluded", None, name, "WA", "DSHS exclusion",
                excel_date(date_serial) or str(date_serial)[:10], url=HCA_PAGE,
                license_no=lic, extra={"address": addr})


def load_screening(state="WA", refresh=False):
    scr = Screening()
    _load_leie(scr, refresh=refresh)
    try:
        _load_cms_revoked(scr, state=state)
    except Exception:
        pass  # CMS API hiccup shouldn't disable LEIE screening
    try:
        _load_hca_lists(scr, refresh=refresh)
    except Exception:
        pass  # HCA site hiccup shouldn't disable federal screening
    _load_local_csvs(scr, state=state)
    return scr
