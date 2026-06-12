"""Printable case files (entity or operator) + ready-to-send referral / PRA text.

The dashboard is for working leads; this is for ACTING on one. /casefile?uid=... renders
a self-contained, print-friendly HTML page: the evidence chain, dated payments, source
links, the reviewer's triage note, intake channels for the agencies that actually take
these referrals, and — where the records are request-only (childcare SSPS) — a complete
Public Records Act letter. Every page carries the leads-not-findings disclaimer; a case
file is a packet for VERIFICATION, not an accusation.
"""
import datetime
import html


def _e(v):
    return html.escape(str(v if v is not None else ""))


def _money(v):
    try:
        return "${:,.0f}".format(float(v))
    except (TypeError, ValueError):
        return "—"


_CSS = """
body { font-family: Georgia, 'Times New Roman', serif; color:#1a1a1a; margin:40px auto;
  max-width:820px; line-height:1.45; }
h1 { font-size:22px; margin:0; } h2 { font-size:15px; border-bottom:2px solid #1a1a1a;
  padding-bottom:4px; margin:26px 0 10px; text-transform:uppercase; letter-spacing:.5px; }
.sub { color:#555; font-size:13px; margin:4px 0 0; }
.box { border:2px solid #8a6d1a; background:#fdf6e3; padding:10px 14px; font-size:13px;
  margin:16px 0; }
table { border-collapse:collapse; width:100%; font-size:13px; }
th, td { border:1px solid #bbb; padding:5px 8px; text-align:left; vertical-align:top; }
th { background:#f0f0f0; }
.step { margin:8px 0; } .k { font-weight:bold; }
.strength { font-size:11px; font-weight:bold; padding:1px 6px; border:1px solid #888;
  border-radius:3px; margin-left:6px; }
.letter { border:1px solid #999; padding:18px 22px; font-size:13px; white-space:pre-wrap;
  background:#fafafa; margin:10px 0; }
.small { font-size:12px; color:#555; }
a { color:#1a4d8a; }
@media print { body { margin:14mm; } .noprint { display:none; } }
"""


def _head(title):
    return (f"<!doctype html><html><head><meta charset='utf-8'><title>{_e(title)}</title>"
            f"<style>{_CSS}</style></head><body>"
            f"<div class='noprint' style='margin-bottom:14px'>"
            f"<button onclick='window.print()'>Print / save as PDF</button></div>")


_DISCLAIMER = ("This packet contains <b>investigative leads assembled from public "
               "records</b>, not findings of fraud or wrongdoing. Statistical anomalies "
               "have innocent explanations; identity matches can be namesakes. Every item "
               "cites its source so it can be independently verified — verify before "
               "relying on it.")


def _referrals(ent):
    src = ent.get("source", "")
    rows = [("HHS Office of Inspector General hotline (Medicare/Medicaid provider fraud)",
             "https://oig.hhs.gov/fraud/report-fraud/"),
            ("WA Attorney General — Medicaid Fraud Control Division",
             "https://www.atg.wa.gov/medicaid-fraud-control-division"),
            ("WA State Auditor — citizen fraud hotline (state funds)",
             "https://sao.wa.gov/report-a-concern/")]
    if src == "childcare":
        rows.insert(0, ("DCYF child care licensing complaints (Child Care Check)",
                        "https://www.dcyf.wa.gov/safety/report-abuse"))
    if src == "healthcare":
        rows.insert(0, ("WA DOH — file a complaint against a provider credential",
                        "https://doh.wa.gov/licenses-permits-and-certificates/file-complaint-about-provider-or-facility"))
    out = "<table><tr><th>Channel</th><th>Where</th></tr>"
    for label, url in rows:
        out += f"<tr><td>{_e(label)}</td><td><a href='{_e(url)}'>{_e(url)}</a></td></tr>"
    return out + "</table>"


def _referral_text(ent):
    """A short, factual referral paragraph the user can paste into an intake form."""
    name = ent.get("name") or "(unnamed)"
    ids = []
    cred = (ent.get("raw") or {}).get("credentialnumber")
    if cred:
        ids.append(f"WA credential {cred}")
    xw = ent.get("crosswalk") or {}
    npi = ent.get("source_id") if ent.get("source") in ("aba", "nemt", "dme") else xw.get("npi")
    if npi:
        ids.append(f"NPI {npi}")
    ssps = (ent.get("raw") or {}).get("sspsprovidernumber")
    if ssps:
        ids.append(f"SSPS provider #{ssps}")
    dossier = ent.get("dossier") or {}
    money = [s for s in dossier.get("steps", []) if s.get("label") == "The money"]
    bar = [s for s in dossier.get("steps", []) if s.get("label") == "The bar"]
    lines = [f"Referral concerns {name} ({'; '.join(ids) if ids else ent.get('source')})."]
    if bar:
        lines.append("Public records show: " + bar[0].get("detail", ""))
    if money:
        lines.append(money[0].get("detail", ""))
    contra = [s for s in dossier.get("steps", []) if s.get("label") == "The contradiction"]
    if contra:
        lines.append(contra[0].get("detail", ""))
    lines.append("Sources: WA DOH credential data (data.wa.gov), OIG LEIE, SAM.gov, "
                 "CMS provider payment data (data.cms.gov), HHS Medicaid provider "
                 "spending (opendata.hhs.gov), WA fiscal.wa.gov Open Checkbook.")
    lines.append("This is a records-based lead assembled by an automated screen; "
                 "identity and payment timing should be verified against the cited "
                 "primary sources.")
    return "\n\n".join(lines)


def _pra_letter(ent):
    ssps = (ent.get("raw") or {}).get("sspsprovidernumber")
    if not ssps:
        return ""
    cap = (ent.get("raw") or {}).get("licensecapacity")
    today = datetime.date.today().strftime("%B %d, %Y")
    name = ent.get("name") or "(provider)"
    body = f"""{today}

Public Records Officer
Department of Children, Youth, and Families
PO Box 40992
Olympia, WA 98504-0992
dcyf.publicdisclosure@dcyf.wa.gov

RE: Public Records Act request (RCW 42.56) — provider payment and enrollment records

Dear Public Records Officer:

Under the Public Records Act, chapter 42.56 RCW, I request copies of the following
records for child care provider "{name}", SSPS provider number {ssps}:

1. Working Connections Child Care (WCCC) subsidy payment history for this provider
   for the period January 1, 2021 to the present, including payment dates and amounts.
2. Monthly enrollment or attendance counts submitted in support of subsidy billing for
   the same period.{f'''
3. Records sufficient to show licensed capacity over the same period (currently
   licensed for {cap}), for comparison of billed enrollment to capacity.''' if cap else ''}

I am requesting records that already exist. If any portion is exempt, please redact and
release the remainder, citing the specific exemption for each redaction (RCW
42.56.210(3)). Electronic copies (CSV or PDF by email) are preferred. Please let me know
of any copying costs in advance if they will exceed $25.

Thank you for your assistance.

Sincerely,

[Your name]
[Address / email / phone]"""
    return body


def entity_casefile(ent):
    name = ent.get("name") or "(unnamed)"
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    sc = ent.get("score") or {}
    tri = ent.get("triage") or {}
    h = _head(f"Case file — {name}")
    h += f"<h1>Case file: {_e(name)}</h1>"
    h += (f"<p class='sub'>{_e(ent.get('entity_type') or '')} · source: "
          f"{_e(ent.get('source'))} · status: {_e(ent.get('status') or '?')} · risk score "
          f"{_e(sc.get('risk_score', 0))} · generated {now} by FraudScan</p>")
    h += f"<div class='box'>{_DISCLAIMER}</div>"
    if tri.get("state") or tri.get("note"):
        h += (f"<p><span class='k'>Reviewer triage:</span> {_e(tri.get('state') or '—')}"
              f"{(' — ' + _e(tri['note'])) if tri.get('note') else ''}</p>")

    d = ent.get("dossier") or {}
    if d.get("steps"):
        h += f"<h2>Evidence chain — {_e(d.get('headline', ''))}</h2>"
        for s in d["steps"]:
            h += (f"<div class='step'><span class='k'>{s['n']}. {_e(s['label'])}</span>"
                  f"<span class='strength'>{_e((s.get('strength') or '').upper())}</span>"
                  f"<br>{_e(s.get('detail'))}</div>")
        if d.get("confirm"):
            h += "<p class='k'>Confirms if:</p><ul>" + "".join(
                f"<li>{_e(x)}</li>" for x in d["confirm"]) + "</ul>"
        if d.get("refute"):
            h += "<p class='k'>Refutes if:</p><ul>" + "".join(
                f"<li>{_e(x)}</li>" for x in d["refute"]) + "</ul>"

    tl = ent.get("timeline") or {}
    if tl.get("events") or tl.get("payments"):
        h += "<h2>Chronology</h2><table><tr><th>Date</th><th>Event</th></tr>"
        rows = [(e["date"], e["label"]) for e in tl.get("events", [])]
        rows += [(f"{p['year']}", f"public payments received: {_money(p['amount'])}")
                 for p in tl.get("payments", [])]
        for dt, label in sorted(rows):
            mark = (" ⚠" if tl.get("bar_date") and dt > tl["bar_date"]
                    and "payments" in label else "")
            h += f"<tr><td>{_e(dt)}</td><td>{_e(label)}{mark}</td></tr>"
        h += "</table>"
        if tl.get("bar_date"):
            h += (f"<p class='small'>⚠ marks payments dated after the earliest "
                  f"bar/exclusion date ({_e(tl['bar_date'])}).</p>")

    flags = ent.get("flags") or []
    if flags:
        h += "<h2>All flags</h2><table><tr><th>Flag</th><th>Detail</th><th>Sev.</th></tr>"
        for f in flags:
            h += (f"<tr><td>{_e(f.get('title'))}<br><span class='small'>"
                  f"{_e(f.get('rule_id'))}</span></td>"
                  f"<td>{_e(f.get('explanation'))}</td><td>{_e(f.get('severity'))}</td></tr>")
        h += "</table>"

    pays = ent.get("payments") or []
    if pays:
        h += (f"<h2>Public funds surfaced — {_money(ent.get('funds_total'))}</h2>"
              "<table><tr><th>Program</th><th>Period</th><th>Amount</th></tr>")
        for p in sorted(pays, key=lambda x: -(x.get("amount") or 0)):
            h += (f"<tr><td>{_e(p.get('program'))}</td><td>{_e(p.get('period'))}</td>"
                  f"<td>{_money(p.get('amount'))}</td></tr>")
        h += "</table>"

    links = ent.get("context_links") or []
    if links or ent.get("source_url"):
        h += "<h2>Verify against primary sources</h2><ul>"
        if ent.get("source_url"):
            h += (f"<li>Source record: <a href='{_e(ent['source_url'])}'>"
                  f"{_e(ent['source_url'])}</a></li>")
        for l in links:
            h += f"<li>{_e(l['label'])}: <a href='{_e(l['url'])}'>{_e(l['url'])}</a></li>"
        h += "</ul>"

    h += "<h2>Where to refer (after verification)</h2>" + _referrals(ent)
    h += ("<h2>Draft referral text</h2><div class='letter'>"
          + _e(_referral_text(ent)) + "</div>")
    pra = _pra_letter(ent)
    if pra:
        h += ("<h2>Draft Public Records Act request (DCYF subsidy + enrollment)</h2>"
              "<div class='letter'>" + _e(pra) + "</div>")
    return h + "</body></html>"


def operator_casefile(op):
    name = op.get("canonical_name") or "(operator)"
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    h = _head(f"Case file — operator {name}")
    h += f"<h1>Operator case file: {_e(name)}</h1>"
    h += (f"<p class='sub'>{op.get('member_count')} linked records · programs: "
          f"{_e(', '.join(op.get('sources') or []))} · operator score "
          f"{_e(op.get('combined_score'))} · generated {now} by FraudScan</p>")
    h += f"<div class='box'>{_DISCLAIMER}</div>"
    h += "<h2>Why these records are linked</h2><ul>"
    for s in op.get("signals") or []:
        h += f"<li>{_e(s)}</li>"
    h += "</ul>"
    if op.get("contradiction_amount"):
        h += (f"<p><span class='k'>Dollars dated after a bar:</span> "
              f"{_money(op['contradiction_amount'])}</p>")
    h += ("<h2>Members</h2><table><tr><th>Record</th><th>Source</th><th>Status</th>"
          "<th>Risk</th><th>Public $</th></tr>")
    for m in op.get("members") or []:
        h += (f"<tr><td>{_e(m.get('name'))}</td><td>{_e(m.get('source'))}</td>"
              f"<td>{_e(m.get('status') or '')}</td><td>{_e(m.get('risk_score'))}</td>"
              f"<td>{_money(m.get('funds'))}</td></tr>")
    h += "</table>"
    todo = op.get("what_to_verify") or []
    if todo:
        h += "<h2>What to verify</h2><ul>" + "".join(f"<li>{_e(t)}</li>" for t in todo) + "</ul>"
    return h + "</body></html>"
