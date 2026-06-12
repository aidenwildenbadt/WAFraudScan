"""Local dashboard — stdlib http.server, JSON API + a single static HTML page.

Routes:
  GET /                 dashboard
  GET /api/summary      headline counts, score distribution, top rules
  GET /api/flags        ranked review queue (filterable)
  GET /api/entity?uid=  full detail for one entity incl. all flags + raw record
"""
import json
import os
import re
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from fraudscan import storage, taxonomy

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX = os.path.join(HERE, "index.html")


def _years_in(text):
    """Plausible payment years found in period strings — contract records carry
    placeholder dates (1900-01-01, 2099-12-31) that must not widen a coverage range."""
    return [int(y) for y in re.findall(r"(?:19|20)\d{2}", text or "")
            if 2000 <= int(y) <= 2035]


def _summary():
    conn = storage.connect()
    try:
        c = conn.execute
        ent_by_src = {r["source"]: r["n"] for r in c(
            "SELECT source, COUNT(*) n FROM entities GROUP BY source")}
        flagged_by_src = {r["source"]: r["n"] for r in c(
            "SELECT source, COUNT(*) n FROM scores GROUP BY source")}
        total_flags = c("SELECT COUNT(*) n FROM flags").fetchone()["n"]
        top_rules = [dict(r) for r in c(
            "SELECT rule_id, COUNT(*) n FROM flags GROUP BY rule_id "
            "ORDER BY n DESC")]
        buckets = [dict(r) for r in c(
            "SELECT CASE "
            "  WHEN risk_score >= 80 THEN '80-100' "
            "  WHEN risk_score >= 60 THEN '60-79' "
            "  WHEN risk_score >= 40 THEN '40-59' "
            "  WHEN risk_score >= 20 THEN '20-39' "
            "  ELSE '0-19' END AS bucket, COUNT(*) n "
            "FROM scores GROUP BY bucket ORDER BY bucket DESC")]
        last_ingest = {r["source"]: r["ran_at"] for r in c(
            "SELECT source, MAX(ran_at) ran_at FROM ingest_runs GROUP BY source")}
        total_funds = c("SELECT COALESCE(SUM(amount),0) n FROM payments").fetchone()["n"]
        funds_by_program = [dict(r) for r in c(
            "SELECT program, COALESCE(SUM(amount),0) amt, COUNT(*) n FROM payments "
            "GROUP BY program ORDER BY amt DESC")]
        triage = {r["state"]: r["n"] for r in c(
            "SELECT state, COUNT(*) n FROM triage WHERE state<>'' GROUP BY state")}
        triage["watched"] = c(
            "SELECT COUNT(*) n FROM triage WHERE watched=1").fetchone()["n"]
        # headline stories: the numbers that mean something, pre-wired to filters
        ab_n, ab_amt = 0, 0.0
        for r in c("SELECT evidence_json FROM flags WHERE rule_id IN "
                   "('paid_after_barred','paid_after_npi_deactivated')"):
            try:
                ab_amt += float(json.loads(r["evidence_json"] or "{}")
                                .get("amount_after") or 0)
                ab_n += 1
            except (ValueError, TypeError):
                pass
        fy_now = c("SELECT period, COALESCE(SUM(amount),0) amt FROM payments "
                   "WHERE program LIKE 'WA state%' AND period GLOB 'FY[0-9][0-9][0-9][0-9]' "
                   "GROUP BY period ORDER BY period DESC LIMIT 1").fetchone()
        complaints = c("SELECT COUNT(*) n FROM flags "
                       "WHERE rule_id='childcare_valid_complaint'").fetchone()["n"]
        untriaged_hi = c(
            "SELECT COUNT(*) n FROM scores s LEFT JOIN triage t ON t.entity_uid="
            "s.entity_uid WHERE s.risk_score>=40 AND (t.state IS NULL OR t.state='')"
        ).fetchone()["n"]
        stories = {"after_bar": {"n": ab_n, "amount": round(ab_amt)},
                   "fy_now": dict(fy_now) if fy_now else None,
                   "complaints": complaints, "untriaged_high": untriaged_hi}
        return {
            "entities_by_source": ent_by_src,
            "flagged_by_source": flagged_by_src,
            "total_entities": sum(ent_by_src.values()),
            "total_flagged": sum(flagged_by_src.values()),
            "total_flags": total_flags,
            "top_rules": top_rules,
            "score_buckets": buckets,
            "last_ingest": last_ingest,
            "total_funds": total_funds,
            "funds_by_program": funds_by_program,
            "triage": triage,
            "stories": stories,
            # operators built before the latest re-score → scores/flags shown on the
            # operators tab may not reflect current rules
            "operators_stale": bool(
                (c("SELECT value FROM meta WHERE key='scores_at'").fetchone() or [""])[0]
                > (c("SELECT value FROM meta WHERE key='operators_at'").fetchone()
                   or [""])[0]),
        }
    finally:
        conn.close()


# ---- evidence grade: how DEFENSIBLE is this lead, independent of its risk score ----
_CONF_GOOD = {"license-confirmed", "dob-confirmed"}
_CONF_BAD = {"license-mismatch", "dob-mismatch", "city-mismatch", "taxonomy-mismatch"}
_STRONG_SINGLE = {"credential_revoked_or_suspended", "nursing_enforcement",
                  "childcare_valid_complaint", "single_code_dominance",
                  "services_per_day_outlier", "billing_spike", "payment_outlier",
                  "contract_during_debarment", "lni_debarred"}


def _grade(rids, conf, funds, hi_year, after_amt=0.0, runoff=False, horizon=None):
    """A–D: identity confidence × signal tier × $ recency × MATERIALITY. A high RISK
    score says 'look here'; the GRADE says 'how far would this hold up'. $27 of Part D
    refill residue must not grade like $472K of post-bar billing (Bolling vs Dees)."""
    t1 = bool(rids & {"paid_after_barred", "paid_after_npi_deactivated"})
    prebar = "paid_before_bar" in rids and not t1
    t2 = (bool(rids & {"excluded_or_sanctioned", "excluded_license_derived",
                       "paid_while_sanctioned"}) and funds > 0 and not prebar)
    t3 = bool(rids & _STRONG_SINGLE)
    # G1: 'recent' is relative to the ingested data horizon, not a hardcoded year —
    # and large after-bar money offsets staleness (a $472K contradiction ending one
    # year before the cutoff is a recovery-window A, not a B).
    recent = (hi_year or 0) >= ((horizon or 2024) - 2)
    big = after_amt >= 100000
    material = (after_amt >= 1000 and not runoff) or big
    idtxt = ("identity confirmed (license/DOB)" if conf in _CONF_GOOD else
             "identity CONFLICT — likely namesake" if conf in _CONF_BAD else
             "identity unconfirmed (name-only)")
    rectxt = f"$ through {hi_year}" if hi_year else "no $ attributed"
    if prebar:
        # F3: payments PREDATE the bar — enforcement worked; never grade as "still paid"
        return "D", f"sanctioned, but payments predate the bar (billing ceased) · {rectxt}"
    if conf in _CONF_BAD and (t1 or t2):
        return "C", f"dated signal but {idtxt}; resolve identity before using the $ · {rectxt}"
    if t1 and conf in _CONF_GOOD and material and (recent or big):
        why = ("dated contradiction (paid after bar)" if recent else
               f"dated contradiction — $ ends {hi_year}, recovery-window lead")
        return "A", f"{why} · {idtxt} · {rectxt}"
    if t1 and not material:
        why = ("runoff-consistent Part D residue" if runoff else
               f"after-bar $ immaterial (${after_amt:,.0f})")
        return "B", f"dated contradiction but {why} · {idtxt} · {rectxt}"
    if t1 or (t2 and conf in _CONF_GOOD):
        return "B", (f"{'dated contradiction' if t1 else 'sanctioned + paid'} · {idtxt} · {rectxt}")
    if t2 or (t3 and funds > 0):
        return "C", f"{'sanctioned + paid' if t2 else 'strong single signal with $'} · {idtxt} · {rectxt}"
    return "D", f"correlational signals only · {idtxt} · {rectxt}"


def _flags(params):
    source = params.get("source", [""])[0]
    rule = params.get("rule", [""])[0]
    q = params.get("q", [""])[0].strip()
    min_score = params.get("min_score", ["0"])[0]
    limit = min(int(params.get("limit", ["200"])[0] or 200), 1000)
    try:
        min_score = float(min_score)
    except ValueError:
        min_score = 0.0

    where = ["s.risk_score >= ?"]
    args = [min_score]
    if source:
        where.append("e.source = ?")
        args.append(source)
    if q:
        where.append("(e.name LIKE ? OR e.city LIKE ? OR e.address LIKE ?)")
        args += [f"%{q}%", f"%{q}%", f"%{q}%"]
    if rule:
        where.append("EXISTS (SELECT 1 FROM flags f WHERE f.entity_uid = e.uid "
                     "AND f.rule_id = ?)")
        args.append(rule)
    if params.get("in_operator", [""])[0] == "1":
        where.append("e.operator_id IS NOT NULL")
    county = params.get("county", [""])[0].strip()
    if county:
        where.append("UPPER(TRIM(e.county)) = ?")
        args.append(county.upper())
    tri = params.get("triage", [""])[0]
    if tri == "untriaged":
        where.append("(t.state IS NULL OR t.state='')")
    elif tri in ("reviewed", "dismissed", "escalated"):
        where.append("t.state = ?")
        args.append(tri)
    elif tri == "watched":
        where.append("t.watched = 1")
    sql = (
        "SELECT e.uid, e.source, e.name, e.dba, e.entity_type, e.city, e.county, "
        "e.status, e.amount, e.source_url, s.risk_score, s.flag_count, "
        "s.family_count, s.top_rule, "
        "e.operator_id, op.canonical_name AS operator_name, "
        "op.member_count AS operator_members, op.combined_score AS operator_score, "
        "t.state AS triage_state, t.note AS triage_note, t.watched AS watched, "
        "(SELECT COALESCE(SUM(amount),0) FROM payments p WHERE p.entity_uid=e.uid) "
        "AS funds, "
        "(SELECT COUNT(*) FROM flags f3 WHERE f3.entity_uid=e.uid "
        " AND f3.rule_id='paid_after_barred') AS has_contradiction "
        "FROM scores s JOIN entities e ON e.uid = s.entity_uid "
        "LEFT JOIN operators op ON op.operator_id = e.operator_id "
        "LEFT JOIN triage t ON t.entity_uid = e.uid "
        "WHERE " + " AND ".join(where) +
        # de-saturate the wall of 100s: among equal scores, surface dated contradictions,
        # then the largest dollars, then multi-family leads.
        " ORDER BY s.risk_score DESC, has_contradiction DESC, funds DESC, "
        "s.family_count DESC, s.flag_count DESC LIMIT ?"
    )
    # ALL sorts re-rank in Python (the default leads with material after-bar $, which
    # SQL can't see — it lives in flag evidence), so always over-fetch: a 72-point $472K
    # contradiction must not be clipped by a wall of 78-point $0 conviction echoes.
    sort = params.get("sort", ["score"])[0]
    fetch = max(limit, 1000)
    args.append(fetch)

    conn = storage.connect()
    try:
        rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
        uids = [r["uid"] for r in rows]
        titles, rids, after, conf, ranges = {}, {}, {}, {}, {}
        runoffs = set()
        if uids:
            ph = ",".join("?" * len(uids))
            seen_groups = {}   # G5: one bullet per correlation group — 'Disciplinary
            for fr in conn.execute(  # action' + 'Credential surrendered' are ONE event
                f"SELECT entity_uid, title, severity, rule_id FROM flags "
                f"WHERE entity_uid IN ({ph}) ORDER BY severity DESC", uids
            ).fetchall():
                g = taxonomy.group(fr["rule_id"])
                if g not in seen_groups.setdefault(fr["entity_uid"], set()):
                    seen_groups[fr["entity_uid"]].add(g)
                    titles.setdefault(fr["entity_uid"], []).append(fr["title"])
                rids.setdefault(fr["entity_uid"], set()).add(fr["rule_id"])
            # after-bar $ + identity confidence from the money-attribution evidence —
            # shown beside the total so the contradiction $ is never buried in it
            for fr in conn.execute(
                f"SELECT entity_uid, rule_id, evidence_json FROM flags WHERE rule_id IN "
                f"('paid_after_barred','paid_after_npi_deactivated',"
                f"'paid_while_sanctioned') AND entity_uid IN ({ph})",
                    uids).fetchall():
                try:
                    ev = json.loads(fr["evidence_json"] or "{}")
                except ValueError:
                    ev = {}
                if fr["rule_id"] in ("paid_after_barred", "paid_after_npi_deactivated"):
                    try:
                        amt = float(ev.get("amount_after") or 0)
                    except (ValueError, TypeError):
                        amt = 0.0
                    after[fr["entity_uid"]] = after.get(fr["entity_uid"], 0.0) + amt
                    if ev.get("runoff_consistent"):
                        runoffs.add(fr["entity_uid"])
                if ev.get("identity_confidence"):
                    conf[fr["entity_uid"]] = ev["identity_confidence"]
            # which years the surfaced $ covers (periods are '2023', 'FY2026', or date
            # ranges) — so a money chip is never read as "current" when it's 2019 data
            spark = {}
            for pr in conn.execute(
                f"SELECT entity_uid, period, amount FROM payments "
                f"WHERE entity_uid IN ({ph})", uids).fetchall():
                yrs = _years_in(pr["period"])
                if yrs:
                    lo, hi = ranges.get(pr["entity_uid"], (9999, 0))
                    ranges[pr["entity_uid"]] = (min(lo, min(yrs)), max(hi, max(yrs)))
                    ymap = spark.setdefault(pr["entity_uid"], {})
                    ymap[yrs[0]] = ymap.get(yrs[0], 0.0) + (pr["amount"] or 0)
        # G1: the data horizon = latest year observed across THIS page's payment
        # ranges — 'recent' in the grade is measured against it, not a hardcoded year
        import datetime as _dt
        horizon = max((hi for (_, hi) in ranges.values()), default=None)
        if horizon:
            horizon = min(horizon, _dt.date.today().year)   # contract end-dates lie ahead
        for r in rows:
            r["flag_titles"] = titles.get(r["uid"], [])[:6]
            r["funds_after"] = round(after.get(r["uid"], 0.0))
            lo_hi = ranges.get(r["uid"])
            r["funds_range"] = ("" if not lo_hi else
                                (str(lo_hi[0]) if lo_hi[0] == lo_hi[1] else
                                 f"{lo_hi[0]}–{lo_hi[1]}"))
            g, why = _grade(rids.get(r["uid"], set()), conf.get(r["uid"]),
                            r.get("funds") or 0, lo_hi[1] if lo_hi else None,
                            after_amt=after.get(r["uid"], 0.0),
                            runoff=r["uid"] in runoffs, horizon=horizon)
            r["grade"], r["grade_why"] = g, why
            ymap = spark.get(r["uid"]) if uids else None
            r["spark"] = ([[y, round(a)] for y, a in sorted(ymap.items())]
                          if ymap else [])
        if sort == "grade":
            rows.sort(key=lambda r: (r["grade"], -(r["funds_after"] or 0),
                                     -(r["funds"] or 0), -(r["risk_score"] or 0)))
        elif sort == "funds":
            rows.sort(key=lambda r: -(r["funds"] or 0))
        elif sort == "after":
            rows.sort(key=lambda r: (-(r["funds_after"] or 0), -(r["funds"] or 0)))
        else:
            # F4/G-final default: MATERIAL after-bar dollars lead the queue outright —
            # a $472K dated contradiction outranks every $0-after conviction echo,
            # whatever their raw scores; then risk, then grade, then total $.
            rows.sort(key=lambda r: (-((r["funds_after"] or 0) >= 1000),
                                     -(r["risk_score"] or 0),
                                     -(r["funds_after"] or 0),
                                     r["grade"],
                                     -(r["funds"] or 0)))
        rows = rows[:limit]
        return {"count": len(rows), "results": rows}
    finally:
        conn.close()


_DATEISH = re.compile(r"^(?:(\d{4})-(\d{2})-(\d{2})|(\d{2})/(\d{2})/(\d{4})|(\d{8})|(\d{4}))$")


def _parse_dateish(v):
    """'2021-04-20' | '08/12/2016' | '20210420' | '2016' -> ISO date string or None."""
    m = _DATEISH.match(str(v or "").strip())
    if not m:
        return None
    if m.group(1):
        iso = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    elif m.group(4):
        iso = f"{m.group(6)}-{m.group(4)}-{m.group(5)}"
    elif m.group(7):
        s = m.group(7)
        iso = f"{s[:4]}-{s[4:6]}-{s[6:]}"
    else:
        iso = f"{m.group(8)}-01-01"
    yr = int(iso[:4])
    return iso if 1990 <= yr <= 2035 else None


_BAR_RULES = {"excluded_or_sanctioned", "paid_while_sanctioned", "paid_after_barred",
              "paid_after_npi_deactivated"}
_RAW_EVENTS = [("firstissuedate", "license issued"), ("expirationdate", "license expires"),
               ("certification_date", "certified"), ("cert_date", "certified"),
               ("licenseeffectivedate", "license effective")]


def _build_timeline(ent):
    """Chronology for the dossier: license events + bar dates (from flag evidence) +
    payment totals per year — so 'paid AFTER barred' is visible as bars to the right of
    the red line, not just a sentence."""
    events, seen = [], set()
    raw = ent.get("raw") or {}
    status_u = (ent.get("status") or "").strip().upper()
    for key, label in _RAW_EVENTS:
        iso = _parse_dateish(raw.get(key))
        if not iso or (label, iso) in seen:
            continue
        # F13: never fabricate precision — an expiration date shown for a Revoked/
        # Surrendered credential is the EFFECTIVE end, not a renewal lapse, and when it
        # stands in for an unknown order date it must say so.
        if key == "expirationdate" and status_u in ("REVOKED", "SURRENDER",
                                                    "VOLUNTARY SURRENDER",
                                                    "SUSPENDED", "TERMINATED"):
            label = f"license {status_u.lower()} (effective; no dated order held)"
        seen.add((label, iso))
        events.append({"date": iso, "label": label, "kind": "license"})
    bar_dates = []
    for f in ent.get("flags") or []:
        kind = "bar" if f["rule_id"] in _BAR_RULES else None
        for k, v in (f.get("evidence") or {}).items():
            if "date" not in k.lower():
                continue
            iso = _parse_dateish(v)
            if not iso:
                continue
            label = ("excluded/barred" if kind == "bar" else f["title"][:60])
            if (label, iso) in seen:
                continue
            seen.add((label, iso))
            events.append({"date": iso, "label": label, "kind": kind or "flag"})
            if kind == "bar":
                bar_dates.append(iso)
    pay = {}
    for p in ent.get("payments") or []:
        yrs = _years_in(p.get("period"))
        if yrs:
            pay[yrs[0]] = pay.get(yrs[0], 0.0) + (p.get("amount") or 0)
    payments = [{"year": y, "amount": round(a)} for y, a in sorted(pay.items())]
    events.sort(key=lambda e: e["date"])
    if not events and not payments:
        return None
    return {"events": events, "payments": payments,
            "bar_date": min(bar_dates) if bar_dates else None}


def _entity(uid):
    conn = storage.connect()
    try:
        row = conn.execute("SELECT * FROM entities WHERE uid=?", (uid,)).fetchone()
        if not row:
            return None
        ent = dict(row)
        ent["raw"] = json.loads(ent.pop("raw_json") or "{}")
        score = conn.execute(
            "SELECT risk_score, flag_count, top_rule FROM scores WHERE entity_uid=?",
            (uid,)).fetchone()
        ent["score"] = dict(score) if score else {"risk_score": 0, "flag_count": 0}
        ent["payments"] = storage.payments_for_entity(conn, uid)
        ent["funds_total"] = sum(p["amount"] for p in ent["payments"])
        tri = conn.execute("SELECT state, note, watched FROM triage WHERE entity_uid=?",
                           (uid,)).fetchone()
        ent["triage"] = dict(tri) if tri else {"state": "", "note": "", "watched": 0}
        ent["peer"] = None  # filled after crosswalk is loaded below
        xw = conn.execute(
            "SELECT npi, npi_name, npi_city, npi_taxonomy FROM crosswalk "
            "WHERE entity_uid=? AND npi <> ''", (uid,)).fetchone()
        ent["crosswalk"] = dict(xw) if xw else None
        ent["peer"] = _peer_stats(conn, ent)
        ent["operator"] = None
        if ent.get("operator_id"):
            op = conn.execute(
                "SELECT operator_id, canonical_name, source_count, member_count, "
                "combined_score FROM operators WHERE operator_id=?",
                (ent["operator_id"],)).fetchone()
            ent["operator"] = dict(op) if op else None
        flags = []
        for f in conn.execute(
            "SELECT rule_id, severity, title, explanation, evidence_json FROM flags "
            "WHERE entity_uid=? ORDER BY severity DESC", (uid,)
        ).fetchall():
            fd = dict(f)
            fd["evidence"] = json.loads(fd.pop("evidence_json") or "{}")
            flags.append(fd)
        ent["flags"] = flags
        # curated context (Tier 2) BEFORE the dossier — the adjudication banner reads it
        cred = ent["raw"].get("credentialnumber")
        npi = (ent.get("source_id") if ent["source"] in _NPI_NATIVE
               else (ent.get("crosswalk") or {}).get("npi"))
        ent["context"] = _get_context().lookup(
            credentialnumber=cred, npi=npi, name=ent.get("name"))
        ent["dossier"] = _build_dossier(ent)
        ent["timeline"] = _build_timeline(ent)
        ent["score_parts"] = _score_parts(conn, uid)
        ent["context_links"] = _context_links(ent, cred, npi)
        return ent
    finally:
        conn.close()


# NPI-native sources carry their NPI as source_id; others are crosswalked.
_NPI_NATIVE = {"aba", "nemt", "dme"}

_CONTEXT = None


def _get_context():
    """Curated context index (Tier 2), loaded once per process (restart to pick up new
    files dropped in data/context/)."""
    global _CONTEXT
    if _CONTEXT is None:
        from fraudscan.context_sources import load_context
        from fraudscan.config import DATA_DIR
        _CONTEXT = load_context(DATA_DIR)
    return _CONTEXT


def _context_links(ent, cred, npi):
    """On-demand source pointers (Tier 3) — we link to the source, never assert a match."""
    links, name = [], ent.get("name") or ""
    if ent.get("source") == "healthcare":
        links.append({
            "label": f"DOH credential record & legal documents — search "
                     f"{cred or name}",
            "url": "https://fortress.wa.gov/doh/providercredentialsearch/"})
    # child care: our source_id IS the findchildcarewa (DCYF/WACOMPASS) provider id, so
    # we can deep-link the per-provider page — Early Achievers rating, complaints,
    # inspections, and license history (the enforcement context we don't ingest).
    if ent.get("source") == "childcare" and ent.get("source_id"):
        links.append({
            "label": "Licensing, Early Achievers rating, complaints & inspections "
                     "(findchildcarewa)",
            "url": "https://www.findchildcarewa.org/PSS_Provider?id="
                   + str(ent["source_id"])})
    # per-provider child-care subsidy $ is NOT public open data (the SSPS#->NPI map in
    # AFRS is "restricted to DSHS / currently not used", confirmed by research), so the
    # SSPS number is the exact key for a DCYF records request. Frame it as the capacity-
    # billing test: subsidy paid vs ACTUAL enrolled headcount (what would expose over-
    # capacity / "milking" if it exists — the only way to settle it from outside).
    ssps = (ent.get("raw") or {}).get("sspsprovidernumber")
    if ent.get("source") == "childcare" and ssps:
        cap = (ent.get("raw") or {}).get("licensecapacity")
        tail = (f"; licensed for {cap}, so it compares billing against actual attendance"
                if cap else "; per-provider subsidy $ is records-request-only")
        links.append({
            "label": f"Request DCYF subsidy $ + enrolled headcount — SSPS #{ssps}{tail}",
            "url": "https://www.dcyf.wa.gov/public-records"})
    if npi:
        links.append({"label": f"NPPES registry record (NPI {npi})",
                      "url": f"https://npiregistry.cms.gov/provider-view/{npi}"})
    if ent.get("source") == "healthcare":
        links.append({"label": "WA OIC insurance enforcement orders — search this name",
                      "url": "https://fortress.wa.gov/oic/consumertoolkit/search.aspx"
                             "?searchtype=ord"})
    if ent.get("source", "").startswith("contracts"):
        links.append({"label": "L&I verify contractor (registration, debar history)",
                      "url": "https://secure.lni.wa.gov/verify/"})
        links.append({"label": "WA State Auditor reports — search this vendor",
                      "url": "https://sao.wa.gov/reports-data/audit-reports"})
    nm = ent.get("name") or ""
    if len(nm) >= 8:
        import urllib.parse as _up
        links.append({"label": "Federal dockets (RECAP) — search this name in WA courts",
                      "url": "https://www.courtlistener.com/?" + _up.urlencode(
                          {"q": f'"{nm}"', "type": "r",
                           "court": "wawd waed wawb waeb"})})
    links.append({"label": "OIG corporate-integrity-agreement archive (search the name)",
                  "url": "https://oig.hhs.gov/compliance/corporate-integrity-"
                         "agreements/cia-documents.asp"})
    q = " ".join(p for p in (f'"{name}"', "Washington",
                             ent.get("entity_type") or "", "license") if p)
    links.append({"label": "Search news & web for this provider",
                  "url": "https://www.google.com/search?q=" + urllib.parse.quote(q)})
    return links


def _build_dossier(ent):
    """Auto-assemble a reviewer-facing evidence chain for a lead: identity → the bar →
    the money → the contradiction, plus what would confirm vs refute it. Returns None
    when there's nothing decisive to assemble (no payments and no sanction flag)."""
    flags = {f["rule_id"]: f for f in ent.get("flags", [])}
    pays = ent.get("payments", [])
    excl = flags.get("excluded_or_sanctioned") or flags.get("excluded_license_derived")
    after = flags.get("paid_after_barred")
    while_s = flags.get("paid_while_sanctioned")
    prebar = flags.get("paid_before_bar")
    # identity-downgraded money flag (namesake suspected) — carries the same identity
    # evidence, so the dossier can explain WHY the strong lead was withdrawn
    unconf = flags.get("paid_attribution_unconfirmed")
    if not (after or while_s or unconf or excl or pays):
        return None

    src = ent.get("source") or ""
    xw = ent.get("crosswalk") or {}
    npi = (ent.get("source_id") if src in _NPI_NATIVE else xw.get("npi")) or ""

    steps, confirm, refute = [], [], []

    # --- Step 1: identity ---
    id_bits = [f"{ent.get('name') or '(unnamed)'} — {src} record"]
    if npi:
        nm = xw.get("npi_name")
        city = xw.get("npi_city")
        how = ("NPI is the record's own identifier" if src in _NPI_NATIVE else
               "NPI resolved from DOH name+state")
        extra = f"; NPPES: {nm}{', ' + city if city else ''}" if nm else ""
        id_bits.append(f"NPI {npi} ({how}){extra}")
    # identity confidence is computed by the money-attribution rule (license #, DOB vs
    # exclusion record, city) — surface its grade rather than re-deriving it here.
    src_flag = after or while_s or unconf
    iev = src_flag.get("evidence", {}) if src_flag else {}
    conf = iev.get("identity_confidence")
    _ID_STR = {"license-confirmed": "high", "dob-confirmed": "high",
               "city-corroborated": "medium", "exclusion-npi": "medium",
               "unique-name": "medium", "license-mismatch": "low",
               "dob-mismatch": "low", "city-mismatch": "low",
               "taxonomy-mismatch": "low"}
    if conf:
        id_strength = _ID_STR.get(conf, "medium")
        if iev.get("identity_note"):
            id_bits.append(iev["identity_note"])
    else:
        id_strength = "high" if (npi and src in _NPI_NATIVE) else (
            "medium" if npi else "low")
    steps.append({"n": 1, "label": "Identity", "strength": id_strength,
                  "detail": "; ".join(id_bits),
                  "source_url": ent.get("source_url")})

    # --- Step 2: the bar / sanction ---
    if excl:
        ev = excl["evidence"]
        mand = ev.get("mandatory")
        tier = ("MANDATORY" if mand else "permissive" if mand is False else "")
        bar = (f"On {ev.get('list')} — {tier} exclusion: "
               f"{ev.get('exclusion_reason')} (dated {ev.get('date') or '—'}). "
               f"Match confidence: {ev.get('confidence')}.")
        if ev.get("matched_dob"):
            bar += f" Listed DOB {ev.get('matched_dob')}, city {ev.get('matched_city') or '—'}."
        steps.append({"n": 2, "label": "The bar", "source_url": ev.get("list_url"),
                      "strength": ("high" if ev.get("confidence") == "definitive"
                                   else "medium" if ev.get("confidence") == "corroborated"
                                   else "low"),
                      "detail": bar})
    elif after or while_s:
        ev = (after or while_s)["evidence"]
        bar = f"Credential status '{ev.get('status')}'"
        if ev.get("barred_year"):
            bar += f", barred as of {ev.get('barred_year')}"
        if ev.get("exclusion_list"):
            bar += f"; corroborated by {ev.get('exclusion_list')} ({ev.get('exclusion_reason')})"
        steps.append({"n": 2, "label": "The bar", "source_url": ent.get("source_url"),
                      "strength": "medium", "detail": bar + "."})

    # --- Step 3: the money ---
    if pays:
        by_year, by_prog = {}, {}
        for p in pays:
            by_year[p["period"]] = by_year.get(p["period"], 0) + (p["amount"] or 0)
            by_prog[p["program"]] = by_prog.get(p["program"], 0) + (p["amount"] or 0)
        total = sum(by_year.values())
        prog_txt = "; ".join(f"{k} ${v:,.0f}" for k, v in
                             sorted(by_prog.items(), key=lambda kv: -kv[1]))
        dnote = (" Part D figures are drug COST of this prescriber's prescriptions, "
                 "paid to pharmacies — not income to the provider."
                 if any("Part D" in k for k in by_prog) else "")
        steps.append({"n": 3, "label": "The money", "strength": "high",
                      "detail": f"${total:,.0f} public funds surfaced — {prog_txt}."
                                + dnote,
                      "timeline": [{"period": k, "amount": round(v)}
                                   for k, v in sorted(by_year.items())]})

    # --- Step 4: the contradiction (the decisive line) ---
    headline, strength = "Payments surfaced", "payments"
    if after:
        ev = after["evidence"]
        abp = ev.get("after_by_program") or {}
        prog_after = "; ".join(
            p + " " + ", ".join(f"{y}: ${float(v):,.0f}" for y, v in sorted(ys.items()))
            for p, ys in sorted(abp.items())) or ev.get("program", "")
        runoff = ev.get("runoff_consistent")
        steps.append({"n": 4, "label": "The contradiction",
                      "strength": "medium" if runoff else "high",
                      "detail": f"${ev.get('amount_after'):,.0f} dated AFTER the "
                                f"{ev.get('barred_year')} bar "
                                f"({ev.get('bar_source') or 'bar'}) — {prog_after}."
                                + (" ⚠ Consistent with refill RUNOFF of pre-bar "
                                   "prescriptions — verify written dates before "
                                   "treating as post-bar conduct." if runoff else "")})
        headline = ("Paid after barred — runoff-consistent (verify)" if runoff else
                    "Verifiable contradiction: paid after barred")
        strength = "verifiable contradiction"
    elif prebar:
        pe = prebar["evidence"]
        cov = pe.get("coverage_through")
        unob = pe.get("after_unobservable")
        headline = ("Sanctioned — payments predate the bar (post-bar not yet "
                    "observable)" if unob else
                    "Sanctioned — payments predate the bar (billing ceased)")
        strength = "pre-bar only"
        if cov and not unob:
            confirm.append(f"Data coverage runs through CY{cov} with $0 after the "
                           f"{pe.get('barred_year')} bar — affirmative cessation; no "
                           f"action needed unless billing resumes.")
        elif unob:
            confirm.append(f"Re-check when CMS publishes CY{pe.get('barred_year')} "
                           f"data — post-bar billing is currently unobservable "
                           f"(auto-watched).")
    elif while_s:
        we = while_s.get("evidence", {})
        if we.get("after_unobservable"):
            headline = "Sanctioned recently — billing data lags the bar (watch)"
            strength = "watch (data lag)"
        else:
            headline = "Sanctioned credential + payments held"
            strength = "sanctioned + paid"
    elif excl:
        headline = "On a federal exclusion list"
        strength = "on exclusion list"

    # --- confirm / refute guidance (tailored to how solid the identity link is) ---
    if conf in ("license-confirmed", "dob-confirmed"):
        confirm.append(f"Identity already confirmed — {iev.get('identity_note')}. The "
                       f"NPI→money attribution holds.")
    elif conf in ("license-mismatch", "dob-mismatch", "taxonomy-mismatch"):
        refute.append(iev.get("identity_note") + " — likely a namesake; resolve before "
                      "acting.")
    elif npi:
        confirm.append(f"Confirm NPI {npi} belongs to this provider in NPPES "
                       f"(name, taxonomy, practice address).")
    if excl and excl["evidence"].get("matched_dob") and conf not in (
            "license-confirmed", "dob-confirmed"):
        confirm.append(f"Match the OIG LEIE record's DOB "
                       f"({excl['evidence']['matched_dob']}) / address to this person.")
        refute.append("If the LEIE DOB or city does not match this provider, it is a "
                      "namesake — dismiss the lead.")
    if after:
        confirm.append(f"Request the Medicare remittance for {after['evidence'].get('payment_years')} "
                       f"to confirm the payment dates post-date the bar.")
        # F14: when the bar predates our ingest window, the after-bar figure is a FLOOR
        held = sorted({int(y) for p in (ent.get("payments") or [])
                       for y in [str(p.get("period") or "")] if y.isdigit()})
        by = after["evidence"].get("barred_year")
        if by and held and by < held[0]:
            confirm.append(f"CMS publishes by-provider data back to 2013 and the bar "
                           f"({by}) predates the ingested window ({held[0]}–{held[-1]}) "
                           f"— query the earlier years; the after-bar total shown is a "
                           f"floor, not the full figure.")
        if after["evidence"].get("excluded_after_gap"):
            confirm.append("Years beyond the recency window were EXCLUDED from the "
                           "after-bar figure (see excluded_after_gap in evidence) — "
                           "include them if identity is independently confirmed.")
    # F15: if attached context already documents a conviction/sentencing, say so —
    # investigators should monitor, not rediscover, adjudicated cases.
    adj = next((c for c in (ent.get("context") or [])
                if re.search(r"sentenc|convict|guilty|prison|plea",
                             (c.get("title") or ""), re.I)), None)
    if adj:
        confirm.insert(0, f"⚖ Case appears ALREADY ADJUDICATED — attached context: "
                          f"“{(adj.get('title') or '')[:90]}”. Verify status and treat "
                          f"as monitoring (re-billing watch), not discovery.")
    if id_strength == "low" and not refute:
        refute.append("Identity is unconfirmed — resolve it before treating dollars as "
                      "attributable.")
    if not refute:
        refute.append("If the identity link (name→NPI) is wrong, the dollar attribution "
                      "does not hold — verify before acting.")

    return {"headline": headline, "strength": strength, "steps": steps,
            "confirm": confirm, "refute": refute}


_OP_FILTERS = {
    "barred": "barred_members > 0",
    "contradiction": "contradiction_amount > 0",
    "cross": "cross_program = 1",
    "hard": "strongest_link IN ('hard','shell')",
    "soft": "strongest_link IN ('fuzzy','geo','name')",
}
_OP_SORTS = {
    "score": "(contradiction_amount > 0) DESC, combined_score DESC, "
             "barred_members DESC, dollars_at_stake DESC",
    "stake": "dollars_at_stake DESC, combined_score DESC",
    "barred": "barred_members DESC, contradiction_amount DESC, combined_score DESC",
}


def _operators(params):
    q = params.get("q", [""])[0].strip()
    min_sources = int(params.get("min_sources", ["1"])[0] or 1)
    limit = min(int(params.get("limit", ["200"])[0] or 200), 1000)
    where = ["source_count >= ?"]
    args = [min_sources]
    if q:
        where.append("canonical_name LIKE ?")
        args.append(f"%{q}%")
    for f in params.get("filter", []):
        if f in _OP_FILTERS:
            where.append(_OP_FILTERS[f])
    order = _OP_SORTS.get(params.get("sort", ["score"])[0], _OP_SORTS["score"])
    sql = ("SELECT *, (SELECT COALESCE(SUM(p.amount),0) FROM payments p "
           "JOIN operator_members m ON m.entity_uid=p.entity_uid "
           "WHERE m.operator_id=operators.operator_id) AS funds, "
           "(SELECT GROUP_CONCAT(DISTINCT p.period) FROM payments p "
           "JOIN operator_members m2 ON m2.entity_uid=p.entity_uid "
           "WHERE m2.operator_id=operators.operator_id) AS _periods "
           "FROM operators WHERE " + " AND ".join(where) +
           " ORDER BY " + order + " LIMIT ?")
    args.append(limit)
    conn = storage.connect()
    try:
        rows = []
        for r in conn.execute(sql, args).fetchall():
            d = dict(r)
            d["sources"] = json.loads(d.pop("sources_json") or "[]")
            d["signals"] = json.loads(d.pop("signals_json") or "[]")
            d.pop("audit_json", None)  # detail only; keep the list payload light
            yrs = _years_in(d.pop("_periods"))
            d["funds_range"] = ("" if not yrs else (str(min(yrs)) if min(yrs) == max(yrs)
                                else f"{min(yrs)}–{max(yrs)}"))
            rows.append(d)
        return {"count": len(rows), "results": rows}
    finally:
        conn.close()


def _operator(op_id):
    conn = storage.connect()
    try:
        row = conn.execute("SELECT * FROM operators WHERE operator_id=?",
                           (op_id,)).fetchone()
        if not row:
            return None
        op = dict(row)
        op["sources"] = json.loads(op.pop("sources_json") or "[]")
        op["signals"] = json.loads(op.pop("signals_json") or "[]")
        op["audit"] = json.loads(op.pop("audit_json") or "null")
        op["members"] = [dict(m) for m in conn.execute(
            "SELECT m.entity_uid, m.source, m.name, m.entity_type, m.status, "
            "m.risk_score, m.barred, m.identity, m.top_flag, "
            "(SELECT COALESCE(SUM(amount),0) FROM payments p "
            " WHERE p.entity_uid=m.entity_uid) AS funds "
            "FROM operator_members m WHERE m.operator_id=? "
            "ORDER BY m.barred DESC, m.risk_score DESC, funds DESC",
            (op_id,)).fetchall()]
        op["funds_total"] = sum(m["funds"] for m in op["members"])
        # a concise "what to verify" list driven by the rollup
        todo = []
        if op.get("contradiction_amount"):
            todo.append(f"Verify the ${op['contradiction_amount']:,.0f} paid AFTER a bar "
                        f"against the linked Medicare + sanction records.")
        if op.get("barred_members"):
            todo.append(f"Confirm the {op['barred_members']} barred member(s) are the "
                        f"same individuals (NPI / license #) before attributing dollars.")
        if op.get("strongest_link") in ("hard", "shell"):
            todo.append("Pull the shared WA SOS / CMS owner record that links these "
                        "entities — a hard identity link worth documenting.")
        elif op.get("strongest_link") in ("fuzzy", "geo"):
            todo.append("This is a soft (name/proximity) link — confirm the entities are "
                        "actually the same operator before acting.")
        op["todo"] = todo
        return op
    finally:
        conn.close()


def _by_county():
    """Flag + $ density per WA county (only sources that carry a county field)."""
    conn = storage.connect()
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT UPPER(TRIM(e.county)) county, COUNT(*) flagged, "
            "COALESCE(SUM((SELECT SUM(p.amount) FROM payments p "
            "WHERE p.entity_uid=e.uid)),0) funds, "
            "SUM(CASE WHEN s.risk_score>=40 THEN 1 ELSE 0 END) hi "
            "FROM entities e JOIN scores s ON s.entity_uid=e.uid "
            "WHERE e.county IS NOT NULL AND e.county<>'' "
            "GROUP BY UPPER(TRIM(e.county))")]
        return {"counties": rows}
    finally:
        conn.close()


def _peer_stats(conn, ent):
    """Where this provider's $ sits among peers (same taxonomy when known, else same
    source) — an outlier-vs-peers statement is more decisive than a raw amount."""
    mine = ent.get("funds_total") or 0
    if not mine:
        return None
    amts, group = [], None
    tax = (ent.get("crosswalk") or {}).get("npi_taxonomy")
    if tax:
        rows = conn.execute(
            "SELECT COALESCE(SUM(p.amount),0) amt FROM crosswalk c "
            "JOIN payments p ON p.entity_uid=c.entity_uid "
            "WHERE c.npi_taxonomy=? GROUP BY c.entity_uid", (tax,)).fetchall()
        amts = sorted(r["amt"] for r in rows if r["amt"])
        group = tax
    if len(amts) < 8:                     # taxonomy group too small -> same-source group
        rows = conn.execute(
            "SELECT COALESCE(SUM(p.amount),0) amt FROM payments p "
            "JOIN entities e ON e.uid=p.entity_uid WHERE e.source=? "
            "GROUP BY p.entity_uid", (ent["source"],)).fetchall()
        amts = sorted(r["amt"] for r in rows if r["amt"])
        group = ent["source"]
    if len(amts) < 8:                     # still too few for a meaningful percentile
        return None
    import bisect
    pct = round(100 * bisect.bisect_left(amts, mine) / len(amts))
    return {"group": group, "n": len(amts), "median": round(amts[len(amts) // 2]),
            "p90": round(amts[int(len(amts) * 0.9)]), "pct": min(pct, 100),
            "mine": round(mine)}


def _changes():
    """What changed between the two most recent pipeline snapshots — turns a re-run
    into monitoring instead of a fresh pile."""
    conn = storage.connect()
    try:
        runs = [r[0] for r in conn.execute(
            "SELECT DISTINCT run_at FROM snapshots ORDER BY run_at DESC LIMIT 2")]
        if len(runs) < 2:
            return {"runs": runs, "items": []}
        cur, prev = runs[0], runs[1]
        items = []
        for r in conn.execute(
            "SELECT c.entity_uid uid, e.name, e.source, c.risk_score cs, p.risk_score ps, "
            "c.funds cf, p.funds pf, c.status cst, p.status pst, "
            "t.watched watched FROM snapshots c "
            "LEFT JOIN snapshots p ON p.entity_uid=c.entity_uid AND p.run_at=? "
            "JOIN entities e ON e.uid=c.entity_uid "
            "LEFT JOIN triage t ON t.entity_uid=c.entity_uid "
                "WHERE c.run_at=?", (prev, cur)).fetchall():
            d = dict(r)
            if d["ps"] is None:
                if (d["cs"] or 0) >= 40:
                    items.append({"uid": d["uid"], "name": d["name"],
                                  "source": d["source"], "watched": d["watched"],
                                  "kind": "new", "detail":
                                  f"newly flagged at score {round(d['cs'])}"})
                continue
            if (d["cs"] or 0) - (d["ps"] or 0) >= 10:
                items.append({"uid": d["uid"], "name": d["name"], "source": d["source"],
                              "watched": d["watched"], "kind": "risk",
                              "detail": f"score {round(d['ps'])} → {round(d['cs'])}"})
            if (d["cf"] or 0) - (d["pf"] or 0) > 25000:
                items.append({"uid": d["uid"], "name": d["name"], "source": d["source"],
                              "watched": d["watched"], "kind": "funds", "detail":
                              f"public $ +${(d['cf'] or 0) - (d['pf'] or 0):,.0f}"})
            if (d["cst"] or "") != (d["pst"] or "") and d["pst"]:
                items.append({"uid": d["uid"], "name": d["name"], "source": d["source"],
                              "watched": d["watched"], "kind": "status", "detail":
                              f"status {d['pst']} → {d['cst']}"})
        items.sort(key=lambda x: (not x.get("watched"), x["kind"] != "status"))
        return {"runs": runs, "items": items[:60]}
    finally:
        conn.close()


def _movers():
    """Recency-weighted views: who's growing fastest, who got after-bar $ — momentum,
    not static totals."""
    conn = storage.connect()
    try:
        per = {}
        for r in conn.execute(
            "SELECT entity_uid, period, SUM(amount) amt FROM payments "
                "WHERE program <> 'WA agency contract' GROUP BY entity_uid, period"):
            yrs = _years_in(r["period"])
            if yrs:
                per.setdefault(r["entity_uid"], {})
                per[r["entity_uid"]][yrs[0]] = (per[r["entity_uid"]].get(yrs[0], 0)
                                                + (r["amt"] or 0))
        growth = []
        for uid, ymap in per.items():
            yrs = sorted(ymap)
            if len(yrs) < 2:
                continue
            cur, prv = ymap[yrs[-1]], ymap[yrs[-2]]
            if cur >= 100000 and prv > 10000 and cur / prv >= 1.6:
                growth.append((cur / prv, uid, yrs[-2], yrs[-1], prv, cur))
        growth.sort(reverse=True)
        names = {}
        uids = [g[1] for g in growth[:12]]
        if uids:
            ph = ",".join("?" * len(uids))
            names = {r["uid"]: (r["name"], r["source"]) for r in conn.execute(
                f"SELECT uid, name, source FROM entities WHERE uid IN ({ph})", uids)}
        out = [{"uid": g[1], "name": names.get(g[1], ("?",))[0],
                "source": names.get(g[1], ("", ""))[1],
                "detail": f"{g[2]}: ${g[4]:,.0f} → {g[3]}: ${g[5]:,.0f} ({g[0]:.1f}×)"}
               for g in growth[:12]]
        return {"growth": out}
    finally:
        conn.close()


def _score_parts(conn, uid):
    """Mirror the de-correlated scorer: contribution = MAX severity per correlation
    group — so a 100 is explainable as a stacked bar, not a black box."""
    from fraudscan.taxonomy import group as rule_group
    parts = {}
    for r in conn.execute(
            "SELECT rule_id, severity FROM flags WHERE entity_uid=?", (uid,)):
        g = rule_group(r["rule_id"])
        if r["severity"] > parts.get(g, (0, ""))[0]:
            parts[g] = (r["severity"], r["rule_id"])
    return [{"group": g, "severity": s, "rule": rid}
            for g, (s, rid) in sorted(parts.items(), key=lambda kv: -kv[1][0])]


def _search(q):
    """One box that finds anything we hold: name, NPI, UBI, credential #, city."""
    q = (q or "").strip()
    if len(q) < 2:
        return {"results": []}
    conn = storage.connect()
    try:
        like = f"%{q}%"
        ents = [dict(r) for r in conn.execute(
            "SELECT e.uid, e.name, e.source, e.city, s.risk_score FROM entities e "
            "LEFT JOIN scores s ON s.entity_uid=e.uid "
            "WHERE e.name LIKE ? OR e.source_id LIKE ? OR e.city LIKE ? "
            "OR e.raw_json LIKE ? ORDER BY s.risk_score DESC LIMIT 10",
            (like, like, like, like))]
        ops = [dict(r) for r in conn.execute(
            "SELECT operator_id, canonical_name, combined_score, member_count "
            "FROM operators WHERE canonical_name LIKE ? "
            "ORDER BY combined_score DESC LIMIT 5", (like,))]
        return {"results": ents, "operators": ops}
    finally:
        conn.close()


def _random_lead():
    """A random mid-tier untriaged lead — the unexamined middle is where true positives
    hide; top-of-list bias is real."""
    conn = storage.connect()
    try:
        r = conn.execute(
            "SELECT s.entity_uid uid FROM scores s LEFT JOIN triage t "
            "ON t.entity_uid=s.entity_uid WHERE s.risk_score BETWEEN 25 AND 79 "
            "AND (t.state IS NULL OR t.state='') ORDER BY RANDOM() LIMIT 1").fetchone()
        return {"uid": r["uid"]} if r else {}
    finally:
        conn.close()


def _funds_breakdown():
    conn = storage.connect()
    try:
        c = conn.execute
        # per-program coverage range (years found in period strings) so every dollar in
        # the UI is anchored to WHEN it was paid
        cov = {}
        for r in c("SELECT program, GROUP_CONCAT(DISTINCT period) ps FROM payments "
                   "GROUP BY program"):
            yrs = _years_in(r["ps"])
            if yrs:
                cov[r["program"]] = (f"{min(yrs)}" if min(yrs) == max(yrs)
                                     else f"{min(yrs)}–{max(yrs)}")
        # contracts aren't an annual payment stream — they're FY snapshots of multi-year
        # awards, so a min/max of their effective dates would read as bogus "coverage"
        cov["WA agency contract"] = "FY2022–FY2025 snapshots"
        by_program = [{"label": r["program"], "amount": r["amt"],
                       "range": cov.get(r["program"], "")} for r in c(
            "SELECT program, COALESCE(SUM(amount),0) amt FROM payments "
            "GROUP BY program ORDER BY amt DESC")]
        fed = c("SELECT COALESCE(SUM(federal_funds),0) FROM operators").fetchone()[0]
        rev = c("SELECT COALESCE(SUM(nonprofit_revenue),0) FROM operators").fetchone()[0]
        if fed:
            by_program.append({"label": "Federal awards (USAspending)", "amount": fed})
        if rev:
            by_program.append({"label": "Nonprofit 990 revenue", "amount": rev})
        audit = c("SELECT COALESCE(SUM(audit_flagged_amount),0) amt, "
                  "COUNT(*) n FROM operators WHERE audit_findings > 0").fetchone()
        if audit["amt"]:
            by_program.append({
                "label": "Single-audit flagged programs (FAC)",
                "amount": audit["amt"], "count": audit["n"]})

        # Risk tiers cover PROVIDER payments only (Medicare etc.) — state contracts are
        # procurement ($7B, mostly legitimate vendors) and would swamp the signal.
        pay = {r["entity_uid"]: r["amt"] for r in c(
            "SELECT entity_uid, COALESCE(SUM(amount),0) amt FROM payments "
            "WHERE program <> 'WA agency contract' GROUP BY entity_uid")}
        eflags = {}
        if pay:
            ph = ",".join("?" * len(pay))
            for r in c(f"SELECT entity_uid, rule_id FROM flags "
                       f"WHERE entity_uid IN ({ph})", list(pay)):
                eflags.setdefault(r["entity_uid"], set()).add(r["rule_id"])
        t1 = {"paid_after_barred", "paid_after_npi_deactivated"}
        t2 = {"excluded_or_sanctioned", "paid_while_sanctioned"}
        t3 = {"single_code_dominance", "services_per_visit_outlier", "payment_outlier",
              "billing_spike"}
        labels = ["Verifiable contradiction (paid-after-barred)",
                  "Sanctioned / excluded + paid", "Billing anomaly", "Other flag",
                  "No risk flag"]
        tot = {l: [0.0, 0] for l in labels}
        for uid, amt in pay.items():
            rids = eflags.get(uid, set())
            l = (labels[0] if rids & t1 else labels[1] if rids & t2
                 else labels[2] if rids & t3 else labels[3] if rids else labels[4])
            tot[l][0] += amt
            tot[l][1] += 1
        by_risk = [{"label": l, "amount": round(tot[l][0]), "count": tot[l][1]}
                   for l in labels if tot[l][1]]
        # two explicit time series so calendar-year federal data and WA fiscal-year data
        # are never mixed on one axis:
        #   by_year    — Medicare + Medicaid, calendar years (period = 'YYYY')
        #   by_fy      — WA state Open Checkbook, fiscal years (period = 'FYYYYY';
        #                FY2026 = Jul 2025–Jun 2026, updated monthly = freshest data)
        by_year = [{"period": r["period"], "amount": round(r["amt"])} for r in c(
            "SELECT period, COALESCE(SUM(amount),0) amt FROM payments "
            "WHERE program <> 'WA agency contract' AND period GLOB '[0-9][0-9][0-9][0-9]' "
            "GROUP BY period ORDER BY period")]
        by_fy = [{"period": r["period"], "amount": round(r["amt"])} for r in c(
            "SELECT period, COALESCE(SUM(amount),0) amt FROM payments "
            "WHERE period GLOB 'FY[0-9][0-9][0-9][0-9]' AND program LIKE 'WA state%' "
            "GROUP BY period ORDER BY period")]
        return {"by_program": by_program, "by_risk_tier": by_risk,
                "by_year": by_year, "by_fy": by_fy}
    finally:
        conn.close()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass  # quiet

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        params = urllib.parse.parse_qs(parsed.query)
        try:
            if path in ("/", "/index.html"):
                with open(INDEX, "rb") as fh:
                    self._send(200, fh.read(), "text/html; charset=utf-8")
            elif path == "/api/summary":
                self._send(200, _summary())
            elif path == "/api/flags":
                self._send(200, _flags(params))
            elif path == "/api/entity":
                uid = params.get("uid", [""])[0]
                ent = _entity(uid)
                self._send(200 if ent else 404, ent or {"error": "not found"})
            elif path == "/api/operators":
                self._send(200, _operators(params))
            elif path == "/api/funds_breakdown":
                self._send(200, _funds_breakdown())
            elif path == "/api/by_county":
                self._send(200, _by_county())
            elif path == "/api/changes":
                self._send(200, _changes())
            elif path == "/api/movers":
                self._send(200, _movers())
            elif path == "/api/search":
                self._send(200, _search(params.get("q", [""])[0]))
            elif path == "/api/random":
                self._send(200, _random_lead())
            elif path == "/api/operator":
                op = _operator((params.get("op", params.get("id", [""]))[0]))
                self._send(200 if op else 404, op or {"error": "not found"})
            elif path == "/casefile":
                from fraudscan.web import casefile
                if params.get("uid", [""])[0]:
                    ent = _entity(params["uid"][0])
                    body = casefile.entity_casefile(ent) if ent else "not found"
                else:
                    op = _operator(params.get("op", [""])[0])
                    body = casefile.operator_casefile(op) if op else "not found"
                self._send(200 if body != "not found" else 404, body,
                           "text/html; charset=utf-8")
            else:
                self._send(404, {"error": "not found"})
        except Exception as exc:  # surface errors as JSON for easy debugging
            self._send(500, {"error": str(exc)})

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            if parsed.path == "/api/triage":
                uid = body.get("uid", "")
                if not uid:
                    self._send(400, {"error": "uid required"})
                    return
                state = body.get("state")            # None = unchanged; '' = clear
                if state not in (None, "", "reviewed", "dismissed", "escalated"):
                    self._send(400, {"error": "bad state"})
                    return
                conn = storage.connect()
                try:
                    storage.set_triage(conn, uid, state=state, note=body.get("note"),
                                       watched=body.get("watched"))
                    row = conn.execute("SELECT state, note, watched FROM triage "
                                       "WHERE entity_uid=?", (uid,)).fetchone()
                    self._send(200, dict(row) if row else {})
                finally:
                    conn.close()
            else:
                self._send(404, {"error": "not found"})
        except Exception as exc:
            self._send(500, {"error": str(exc)})


def serve(host="127.0.0.1", port=8000):
    storage.init_db()
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"FraudScan dashboard:  http://{host}:{port}")
    print("Reminder: flags are investigative leads, not findings of fraud.")
    print("Ctrl+C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
        httpd.server_close()
