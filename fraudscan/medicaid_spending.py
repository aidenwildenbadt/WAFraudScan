"""Per-provider Medicaid spending from the HHS 'Medicaid Provider Spending by HCPCS'
open dataset (opendata.hhs.gov, T-MSIS-derived, released Feb 2026). For the first time
this exposes Medicaid dollars per provider keyed by NPI — covering the domains where we
hold an NPI but had NO money signal: autism/ABA, NEMT, DME, and crosswalked physicians.

Granularity is provider x HCPCS x month (aggregate, not per-claim) and it EXCLUDES
institutional/hospital and drug claims — so it does NOT cover nursing / hospice / home-
health / dialysis (CCN-keyed) or childcare (SSPS#-keyed). Managed-care amounts carry
known quality caveats (OIG/KFF). This is a money-LEAD signal, not claim-line proof.

Access: a single ~3.76 GB .csv.zip on Azure blob (no query API). We stream the HTTP body
and raw-inflate the single deflate member on the fly with stdlib zlib (no full-file
storage), keeping only rows whose billing/servicing NPI is one we already track. Output is
a small per-NPI cache that 'payments' folds into the existing payments table.
"""
import json
import os
import ssl
import struct
import urllib.request
import zlib

from fraudscan.config import DATA_DIR

URL = ("https://stopendataprod.blob.core.windows.net/datasets/medicaid-provider-spending/"
       "2026-02-09/dataset/medicaid-provider-spending.csv.zip")
_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE


def _cache_path():
    d = os.path.join(DATA_DIR, "cache")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "medicaid_spending.json")


def _stream_zip_lines(url, progress=None):
    """Yield text lines from a single-member .zip by streaming the HTTP body and
    raw-inflating the deflate member on the fly — no full download to disk, stdlib only."""
    resp = urllib.request.urlopen(
        urllib.request.Request(url, headers={"User-Agent": "FraudScan/0.1 research"}),
        timeout=180, context=_CTX)
    head = resp.read(30)
    if head[:4] != b"PK\x03\x04":
        raise ValueError("not a zip stream")
    method = struct.unpack("<H", head[8:10])[0]
    fnlen = struct.unpack("<H", head[26:28])[0]
    exlen = struct.unpack("<H", head[28:30])[0]
    resp.read(fnlen + exlen)                         # skip filename + extra field
    dec = zlib.decompressobj(-15) if method == 8 else None   # raw deflate (no zip header)
    buf = b""
    total = 0
    while True:
        chunk = resp.read(1 << 20)
        if not chunk:
            break
        total += len(chunk)
        data = dec.decompress(chunk) if dec else chunk
        if data:
            buf += data
            parts = buf.split(b"\n")
            buf = parts.pop()                        # keep trailing partial line
            for ln in parts:
                yield ln.decode("utf-8", "replace")
        if progress:
            progress(total)
        if dec is not None and dec.eof:
            break
    if buf:
        yield buf.decode("utf-8", "replace")


def build_medicaid_spending(npis, progress=None):
    """Stream the HHS file and aggregate TOTAL_PAID per tracked NPI.
    Columns: BILLING_NPI, SERVICING_NPI, HCPCS, MONTH(YYYY-MM), PATIENTS, LINES, PAID.
    Returns {npi: {total, by_year:{yr:amt}, lines, patients, top_hcpcs:[[code,amt],...]}}."""
    want = {str(n) for n in npis if n}
    if not want:
        return {}
    agg, hc = {}, {}
    first = True
    for line in _stream_zip_lines(URL, progress):
        if first:                                    # header
            first = False
            continue
        p = line.split(",")
        if len(p) < 7:
            continue
        npi = p[1] if p[1] in want else (p[0] if p[0] in want else None)
        if npi is None:
            continue
        try:
            paid = float(p[6])
        except ValueError:
            continue
        a = agg.get(npi)
        if a is None:
            a = agg[npi] = {"total": 0.0, "by_year": {}, "lines": 0, "patients": 0}
            hc[npi] = {}
        a["total"] += paid
        yr = p[3][:4]
        a["by_year"][yr] = a["by_year"].get(yr, 0.0) + paid
        try:
            a["lines"] += int(p[5])
            a["patients"] += int(p[4])
        except ValueError:
            pass
        hc[npi][p[2]] = hc[npi].get(p[2], 0.0) + paid
    for npi, a in agg.items():
        a["total"] = round(a["total"], 2)
        a["by_year"] = {y: round(v, 2) for y, v in a["by_year"].items()}
        a["top_hcpcs"] = [[c, round(v, 2)] for c, v in
                          sorted(hc[npi].items(), key=lambda kv: -kv[1])[:5]]
    return agg


def save_cache(agg):
    with open(_cache_path(), "w", encoding="utf-8") as fh:
        json.dump(agg, fh)


def load_cache():
    p = _cache_path()
    if not os.path.exists(p):
        return {}
    with open(p, "r", encoding="utf-8") as fh:
        return json.load(fh)


def medicaid_payments(entities, agg, npi_map=None):
    """Payment rows (one per matched entity per year) from the cached per-NPI totals."""
    from fraudscan.payments import _entity_npi
    npi_map = npi_map or {}
    by_npi = {}
    for e in entities:
        npi = _entity_npi(e, npi_map)
        if npi and str(npi) in agg:
            by_npi.setdefault(str(npi), e)           # one entity per NPI (no double-count)
    rows = []
    for npi, e in by_npi.items():
        for yr, amt in agg[npi].get("by_year", {}).items():
            if amt:
                rows.append({"entity_uid": e.uid, "program": "Medicaid (HCPCS, T-MSIS)",
                             "period": yr, "amount": amt, "source_url": e.source_url})
    return rows
