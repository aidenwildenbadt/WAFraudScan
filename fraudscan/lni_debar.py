"""WA L&I debarred / strike contractor lists — contractors barred from public works.

L&I debars contractors for prevailing-wage violations (RCW 39.12), unregistered
contracting (RCW 18.27), and industrial-insurance violations (RCW 51.48); a debarred
contractor may not bid on or work public contracts. The list is served by the page's
own JSON method (paged), and each record carries the UBI — a HARD join key to our SOS
extract and contract vendors. "Strike" entries are violation strikes short of debarment
(two strikes within 3 years → debarment) — context, weighted lower.

The decisive cross-check: a vendor debarred on date X whose agency contract runs after X.
"""
import json
import os
import ssl
import urllib.request

from fraudscan.config import DATA_DIR

BASE = "https://secure.lni.wa.gov/debarandstrike/"
PAGES = {"debarred": ("ContractorDebarList.aspx/GetDebarList", "Debarred"),
         "strike": ("ContractorStrikeList.aspx/GetStrikeList", "Strike")}
_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE


def _cache_path():
    d = os.path.join(DATA_DIR, "cache")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "lni_debar.json")


def _page(endpoint, page, rows=250):
    payload = {"Echo": 1, "PageNumber": page, "RowspPage": rows, "OrderByColumn": 0,
               "OrderType": "asc", "co_name": "", "ubi_num": "", "license_id": "",
               "principals": "", "rcw_code": ""}
    if "Strike" in endpoint:           # each page method has its own exact signature
        # the method returns nothing without a date window — use an effectively-open one
        payload.update({"effective_date_start": "01/01/2000",
                        "effective_date_end": "12/31/2049"})
    else:
        payload.update({"status_code": "", "debar_begin_date": "",
                        "debar_end_date": "", "penalty_due_flg": "",
                        "wage_due_flg": ""})
    req = urllib.request.Request(
        BASE + endpoint, data=json.dumps(payload).encode(),
        headers={"User-Agent": "FraudScan/0.1",
                 "Content-Type": "application/json; charset=utf-8"}, method="POST")
    raw = urllib.request.urlopen(req, timeout=60, context=_CTX).read().decode()
    return json.loads(json.loads(raw)["d"])


def _iso(mdy):
    p = str(mdy or "").strip().split("/")
    return f"{p[2]}-{p[0]:0>2}-{p[1]:0>2}" if len(p) == 3 else ""


def fetch_lists():
    """All debar + strike records: {kind, name, ubi, license, principals, status,
    rcw, begin, end}."""
    out = []
    for kind, (endpoint, _) in PAGES.items():
        page = 1
        while True:
            d = _page(endpoint, page)
            rows = d.get("aaData") or []
            if not d.get("IsSuccess") or not rows:
                break
            for r in rows:
                out.append({
                    "kind": kind, "name": (r.get("co_name") or "").strip(),
                    "ubi": (r.get("ubi_num") or "").strip(),
                    "license": (r.get("license_id") or "").strip(),
                    "principals": " ".join((r.get("principals") or "").split()),
                    "status": (r.get("status_desc") or "").strip(),
                    "rcw": (r.get("rcw_code") or "").strip(),
                    "begin": _iso(r.get("debar_begin_date")
                                  or r.get("effective_date")),
                    "end": _iso(r.get("debar_end_date"))})
            # the strike method reports iTotalRecords=0 even with rows — paginate by
            # page fill instead of trusting the total
            if len(rows) < 250:
                break
            page += 1
    return out


def load_lni(refresh=False):
    """Cached {'by_ubi': {ubi: rec}, 'by_name': {normname: rec}} (debar wins ties)."""
    path = _cache_path()
    if not refresh and os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    from fraudscan.registry import normalize
    rows = fetch_lists()
    rows.sort(key=lambda r: r["kind"] != "debarred")   # debarred first → wins setdefault
    by_ubi, by_name = {}, {}
    for r in rows:
        if r["ubi"]:
            by_ubi.setdefault(r["ubi"], r)
        k = normalize(r["name"])
        if k:
            by_name.setdefault(k, r)
    data = {"by_ubi": by_ubi, "by_name": by_name, "count": len(rows)}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    return data
