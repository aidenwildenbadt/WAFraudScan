"""Federal Audit Clearinghouse (FAC) single-audit findings — an INDEPENDENT signal.

The Single Audit Act requires any non-federal entity spending >= $750k/yr in federal
assistance to undergo an annual independent audit; results are public via the FAC API.
Unlike our heuristics, a finding is an *independent auditor's* documented determination —
including whether costs were QUESTIONED. We look up an operator's EIN (from its IRS 990
match) and surface: the most recent audit, its findings, and the federal $ on programs
that drew findings (the auditor-flagged exposure — the closest public proxy to
"questioned costs", since the FAC API exposes the finding flags but not the dollar value).

Reachable with the public api.data.gov DEMO_KEY (rate-limited but free); set FAC_API_KEY
for a higher quota. Looked up only for operators with an EIN, and cached.
"""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

FAC_BASE = "https://api.fac.gov"
# api.data.gov DEMO_KEY is capped at ~30 req/hour — only good for a handful of EINs.
# Set FAC_API_KEY (free at https://api.data.gov/signup) for ~1,000/hour = full coverage.
FAC_KEY = os.environ.get("FAC_API_KEY", "DEMO_KEY")


def using_demo_key():
    return FAC_KEY == "DEMO_KEY"


def _get(table, params, retries=3):
    url = f"{FAC_BASE}/{table}?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "User-Agent": "FraudScan/0.1 research", "X-Api-Key": FAC_KEY,
        "Accept": "application/json"})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries - 1:
                time.sleep(2 * (attempt + 1))  # brief backoff for transient bursts
                continue
            raise


def norm_ein(ein):
    return "".join(ch for ch in str(ein or "") if ch.isdigit())


def _yes(f, k):
    return (f.get(k) or "").strip().upper() == "Y"


def lookup_ein(ein):
    """Most-recent single audit for an EIN with a findings summary, or None when the EIN
    is malformed or there is NO audit on file (FAC covers only entities above the
    federal-spend threshold). Network/quota errors PROPAGATE so callers can avoid caching
    a transient failure as a permanent 'no audit'.
    """
    ein = norm_ein(ein)
    if len(ein) != 9:
        return None
    gen = _get("general", {
        "auditee_ein": f"eq.{ein}",
        "order": "audit_year.desc,fac_accepted_date.desc", "limit": 1,
        "select": "report_id,audit_year,auditee_name,auditee_state,"
                  "total_amount_expended,fac_accepted_date"})
    if not gen:
        return None
    g = gen[0]
    rid = g["report_id"]
    findings = _get("findings", {
        "report_id": f"eq.{rid}",
        "select": "is_questioned_costs,is_material_weakness,is_modified_opinion,"
                  "is_repeat_finding,reference_number,type_requirement"})
    n = len(findings)
    qc = sum(1 for f in findings if _yes(f, "is_questioned_costs"))
    mw = sum(1 for f in findings if _yes(f, "is_material_weakness"))
    mo = sum(1 for f in findings if _yes(f, "is_modified_opinion"))
    rep = sum(1 for f in findings if _yes(f, "is_repeat_finding"))
    flagged_amt, programs = 0.0, []
    if n:
        awards = _get("federal_awards", {
            "report_id": f"eq.{rid}", "findings_count": "gt.0",
            "select": "amount_expended,findings_count,federal_program_name"})
        flagged_amt = sum((a.get("amount_expended") or 0) for a in awards)
        programs = [a.get("federal_program_name") for a in awards][:6]
    return {
        "report_id": rid, "audit_year": g.get("audit_year"),
        "auditee_name": g.get("auditee_name"),
        "total_expended": g.get("total_amount_expended") or 0,
        "findings": n, "questioned_costs": qc, "material_weakness": mw,
        "modified_opinion": mo, "repeat_findings": rep,
        "flagged_amount": round(flagged_amt, 2), "flagged_programs": programs,
        "url": f"https://app.fac.gov/dissemination/summary/{rid}",
    }


def summary_line(fac):
    """Short human signal string for the operators list."""
    if not fac or not fac.get("findings"):
        return ""
    bits = [f"FAC {fac['audit_year']}: {fac['findings']} audit finding(s)"]
    if fac.get("questioned_costs"):
        bits.append(f"{fac['questioned_costs']} with questioned costs")
    if fac.get("material_weakness"):
        bits.append(f"{fac['material_weakness']} material weakness")
    if fac.get("repeat_findings"):
        bits.append(f"{fac['repeat_findings']} repeat")
    tail = f" · ${fac['flagged_amount']:,.0f} on flagged programs" if fac.get(
        "flagged_amount") else ""
    return "; ".join(bits) + tail
