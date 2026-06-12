"""FraudScan command line: ingest -> score -> serve. Stdlib argparse only."""
import argparse
import json
import sys

from fraudscan import storage
from fraudscan.config import load_config
from fraudscan.registry import load_registry
from fraudscan.resolve import build_operators
from fraudscan.payments import build_payments
from fraudscan.screening import load_screening
from fraudscan.ownership import load_ownership
from fraudscan.legitimacy import is_institutional
from fraudscan.crosswalk import build_crosswalk
from fraudscan.rules import run_rules, rules_for_source, rules_cfg_for_source
from fraudscan.scoring import score_entities
from fraudscan.sources import (
    SOURCE_CLASSES, build_source, enabled_source_keys, all_source_keys,
)


def _print(msg):
    print(msg, flush=True)


def cmd_sources(args, config):
    _print("Available sources:")
    enabled = set(enabled_source_keys(config))
    for key in all_source_keys(config):
        scfg = config.get("sources", {}).get(key, {})
        mark = "on " if key in enabled else "off"
        kind = scfg.get("kind")
        if kind == "cms_provider":
            domain, ds = "data.cms.gov", scfg.get("dataset_id", "")
            name = scfg.get("description", key)
        elif kind == "nppes":
            domain = "npiregistry/NLM"
            ds = ",".join(scfg.get("taxonomies", []))[:34]
            name = scfg.get("description", key)
        else:
            domain = config.get("socrata_domain", "data.wa.gov")
            cls = SOURCE_CLASSES.get(key)
            ds = scfg.get("dataset_id", getattr(cls, "dataset_id", ""))
            name = getattr(cls, "name", scfg.get("description", key))
        _print(f"  [{mark}] {key:12s} {name[:40]:40s} ({domain}/{ds})")


def cmd_ingest(args, config):
    keys = [args.source] if args.source else enabled_source_keys(config)
    storage.init_db()
    conn = storage.connect()
    try:
        for key in keys:
            src = build_source(key, config)
            _print(f"Ingesting '{key}' from {src.domain}/{src.dataset_id} ...")
            ents = src.entities(
                limit=args.limit,
                progress=lambda n: _print(f"  fetched {n} records") if n % 5000 == 0 else None,
            )
            n = storage.upsert_entities(conn, ents)
            _print(f"  stored {n} entities for '{key}'.")
    finally:
        conn.close()


def cmd_score(args, config):
    storage.init_db()
    conn = storage.connect()
    cap = config.get("score_cap", 100)
    registry = load_registry(config)
    if registry:
        _print(f"Business registry loaded: {registry.row_count} rows from "
               f"{', '.join(registry.files)} — registration cross-check ON.")
    else:
        _print("No business registry CSV in data/registry/ — registration "
               "cross-check OFF (see README).")
    screening = None
    scfg = config.get("screening", {})
    if scfg.get("enabled", True):
        try:
            screening = load_screening(state=scfg.get("state", "WA"))
            _print("Exclusion/sanction lists loaded: " + ", ".join(
                f"{k} ({v})" for k, v in screening.lists.items()) + ".")
        except Exception as ex:
            _print(f"Exclusion lists unavailable ({ex}) — screening OFF.")
    context = {"registry": registry, "screening": screening,
               "crosswalk": storage.load_crosswalk(conn),
               "crosswalk_detail": storage.load_crosswalk_detail(conn)}
    try:    # G12: dated disciplinary orders upgrade expiry-proxy bars
        from fraudscan.doh_discipline import load_discipline_bars
        context["discipline_bars"] = load_discipline_bars()
        if context["discipline_bars"]:
            _print(f"Disciplinary-order dates loaded for "
                   f"{len(context['discipline_bars'])} credentials (bar provenance).")
    except Exception:
        context["discipline_bars"] = {}
    if config.get("lni_debar", {}).get("enabled", True):
        try:
            from fraudscan.lni_debar import load_lni
            lni = load_lni()
            context["lni"] = lni
            _print(f"L&I debar/strike lists loaded: {lni.get('count', 0)} records — "
                   f"public-works debarment cross-check ON.")
        except Exception as ex:
            _print(f"L&I debar list unavailable ({ex}) — check OFF.")
    if config.get("npi_deactivation", {}).get("enabled", True):
        try:
            from fraudscan.npi_deactivation import build_deactivated
            npis = {r[0] for r in conn.execute(
                "SELECT source_id FROM entities WHERE source IN ('aba','nemt','dme') "
                "AND source_id<>''")}
            npis |= set(context["crosswalk"].values())
            deact = build_deactivated(npis)
            context["npi_deactivated"] = deact
            _print(f"NPPES deactivation file: {len(deact)} of our tracked NPIs are "
                   f"deactivated — paid-after-deactivation check ON.")
        except Exception as ex:
            _print(f"NPPES deactivation file unavailable ({ex}) — check OFF.")
    pay = storage.payments_summary(conn)
    if pay:
        prog_tot = {}
        for d in pay.values():
            prog_tot.setdefault(d["program"], []).append(d["total"])

        def _p95(vals):
            s = sorted(vals)
            return s[int(round(0.95 * (len(s) - 1)))] if s else None
        # latest calendar year with ANY held CMS payment row — the data horizon. A bar
        # later than this is unobservable (CMS publishes ~18 months behind), which is
        # different from "verified $0 after the bar".
        max_year = 0
        import datetime as _dt
        import re as _re
        this_year = _dt.date.today().year
        for d in pay.values():
            for prog, prog_d in (d.get("by_program") or {}).items():
                if prog.startswith("WA agency contract"):
                    continue   # contract periods carry FUTURE end dates (e.g. 2032)
                for per in prog_d.get("periods", {}):
                    mm = _re.search(r"(19|20)\d\d", str(per))
                    if mm:                       # G11: '2015' and 'FY2026' both count
                        max_year = max(max_year, min(int(mm.group()), this_year))
        context["payments"] = {"by_entity": pay,
                               "p95": {p: _p95(v) for p, v in prog_tot.items()},
                               "max_year": max_year or None}
        _print(f"Payment data for {len(pay)} entities loaded — anomaly checks ON "
               f"(CMS data horizon: CY{max_year}).")
    partb = [r[0] for r in conn.execute(
        "SELECT entity_uid FROM payments WHERE program='Medicare Part B'").fetchall()]
    xw = context["crosswalk"]
    uid_to_npi = {u: xw[u] for u in partb if u in xw}
    if uid_to_npi:
        try:
            from fraudscan.billing_forensics import build_billing
            context["billing"] = build_billing(uid_to_npi)
            _print(f"Billing forensics: analyzed {len(context['billing'])} "
                   f"Part B providers (HCPCS-level).")
        except Exception as ex:
            _print(f"Billing forensics skipped ({ex}).")
    ocfg = config.get("ownership", {})
    if ocfg.get("enabled", True):
        try:
            own = load_ownership(state=ocfg.get("state", "WA"))
            context["ownership"] = own
            _print(f"Ownership data loaded: {len(own['churn'])} CHOW facilities, "
                   f"{len(own['owner_to_orgs'])} owners (WA-bounded).")
        except Exception as ex:
            _print(f"Ownership data unavailable ({ex}).")
    ncfg = config.get("nursing_enforcement", {})
    if ncfg.get("enabled", True):
        try:
            from fraudscan.nursing_enforcement import load_enforcement
            context["nursing_enforcement"] = load_enforcement(
                state=ncfg.get("state", "WA"))
            _print(f"Nursing enforcement loaded: "
                   f"{len(context['nursing_enforcement'])} facilities.")
        except Exception as ex:
            _print(f"Nursing enforcement skipped ({ex}).")
    if config.get("childcare_enforcement", {}).get("enabled", True):
        try:  # load the CACHED findchildcarewa scrape (no network during scoring)
            from fraudscan.childcare_enforcement import build_enforcement
            enf, _n = build_enforcement(conn, cap=0)
            if enf:
                context["childcare_enforcement"] = enf
                comp = sum(1 for d in enf.values() if d.get("complaints"))
                _print(f"Childcare enforcement loaded: {len(enf)} providers with data "
                       f"({comp} with valid complaints). Run 'childcare-enforcement' to "
                       f"refresh/extend.")
        except Exception as ex:
            _print(f"Childcare enforcement skipped ({ex}).")
    global_cfg = config.get("rules", {}).get("global", {})
    try:
        keys = [args.source] if args.source else enabled_source_keys(config)
        for key in keys:
            entities = storage.load_entities(conn, key)
            if not entities:
                _print(f"'{key}': no entities ingested yet — skipping.")
                continue
            rule_cfg = rules_cfg_for_source(key, config)
            source_rules = rules_for_source(key, config)
            flags = run_rules(key, entities, rule_cfg, source_rules, context=context,
                              global_cfg=global_cfg)
            storage.replace_flags(conn, key, flags)
            scores = score_entities(flags, score_cap=cap)
            supp = config.get("legitimacy", {}).get("institutional_suppression", 0.5)
            inst = {e.uid for e in entities if is_institutional(e.name)}
            if inst:
                for r in scores:
                    if r["entity_uid"] in inst:
                        r["risk_score"] = round(r["risk_score"] * supp, 1)
                scores.sort(key=lambda r: (r["risk_score"], r.get("family_count", 1),
                                           r["flag_count"]), reverse=True)
            storage.write_scores(conn, key, scores)
            _print(f"'{key}': {len(entities)} entities -> {len(flags)} flags across "
                   f"{len(scores)} entities.")
            _summary(flags)
        # vitals snapshot so the dashboard can show what CHANGED since the last run
        if not args.source:                       # full runs only, not single-source
            storage.take_snapshot(conn)
        # F6: bars newer than the CMS data horizon can't show post-bar billing yet —
        # auto-watch them so the next data year re-surfaces them without a queue slot.
        n_watch = conn.execute(
            "INSERT INTO triage (entity_uid, state, note, watched, updated_at) "
            "SELECT DISTINCT entity_uid, NULL, 'auto-watch: bar postdates CMS data "
            "horizon', 1, datetime('now') FROM flags "
            "WHERE evidence_json LIKE '%\"after_unobservable\": true%' "
            "ON CONFLICT(entity_uid) DO UPDATE SET watched=1").rowcount
        conn.commit()
        if n_watch:
            _print(f"  Auto-watching {n_watch} provider(s) whose bar postdates the "
                   f"data horizon (post-bar billing unobservable yet).")
        storage.set_meta(conn, "scores_at", storage._now())
        ops_at = storage.get_meta(conn, "operators_at")
        if ops_at:
            _print("  Note: operator clusters were built before this re-score — run "
                   "'python3 -m fraudscan resolve' to refresh them.")
    finally:
        conn.close()


def _summary(flags):
    counts = {}
    for f in flags:
        counts[f.rule_id] = counts.get(f.rule_id, 0) + 1
    for rule_id, c in sorted(counts.items(), key=lambda kv: -kv[1]):
        _print(f"    {c:5d}  {rule_id}")


# rules that make a member "sanctioned" (any adverse signal) vs genuinely "barred"
# (cannot lawfully operate / on a federal exclusion list — the actionable subset).
# F10: INTEGRITY sanctions (someone was barred/excluded/disciplined) are what make an
# operator cluster's structure suspicious. QUALITY enforcement (fines, jeopardy
# citations, deficiency counts) fires on 57-84% of WA SNFs — it is context about care,
# not common-control evidence, so it must NOT unlock structure bonuses.
_SANCTION_RULES = {"excluded_or_sanctioned", "excluded_license_derived",
                   "paid_while_sanctioned", "paid_after_barred",
                   "credential_revoked_or_suspended", "credential_surrendered",
                   "credential_active_with_conditions", "disciplinary_action_taken"}
_QUALITY_RULES = {"immediate_jeopardy_citation", "civil_monetary_penalty",
                  "many_health_deficiencies", "low_quality_rating"}
_BARRED_RULES = {"excluded_or_sanctioned", "paid_after_barred", "paid_while_sanctioned",
                 "credential_revoked_or_suspended", "credential_surrendered"}


def _member_facts(conn):
    """Per-entity facts for the operator risk rollup: $, sanctioned/barred flags, the
    dated contradiction $, identity confidence, and the top flag title."""
    funds = storage.funds_by_entity(conn)
    facts = {}

    def _blank(uid):
        return {"funds": funds.get(uid, 0.0), "sanctioned": False, "barred": False,
                "quality": False, "contradiction": 0.0, "identity": "", "top_flag": ""}
    for r in conn.execute("SELECT entity_uid, rule_id, title, evidence_json FROM flags "
                          "ORDER BY severity DESC"):
        f = facts.setdefault(r["entity_uid"], _blank(r["entity_uid"]))
        if not f["top_flag"]:
            f["top_flag"] = r["title"]
        if r["rule_id"] in _SANCTION_RULES:
            f["sanctioned"] = True
        if r["rule_id"] in _QUALITY_RULES:
            f["quality"] = True          # care-quality enforcement — context, not bar
        if r["rule_id"] in _BARRED_RULES:
            f["barred"] = True
        if r["rule_id"] in ("paid_after_barred", "paid_while_sanctioned",
                            "paid_attribution_unconfirmed"):
            ev = json.loads(r["evidence_json"] or "{}")
            if r["rule_id"] == "paid_after_barred":
                f["contradiction"] += ev.get("amount_after", 0) or 0
            if ev.get("identity_confidence") and not f["identity"]:
                f["identity"] = ev["identity_confidence"]
    for uid, amt in funds.items():
        facts.setdefault(uid, _blank(uid))
    return facts


def _national_count_lookup():
    """NPPES national name-frequency lookup (cached); None disables it on failure."""
    try:
        from fraudscan.name_freq import make_lookup
        return make_lookup()
    except Exception:
        return None


def _name_counts(conn):
    """How often a person-name (first+last key) appears across health-care credentials +
    child-care contacts — so a cross-program bridge on a COMMON name can be discounted."""
    from fraudscan.resolve import _person_key
    counts = {}
    for r in conn.execute("SELECT name FROM entities WHERE source='healthcare'"):
        k = _person_key(r["name"])
        if k:
            counts[k] = counts.get(k, 0) + 1
    for r in conn.execute("SELECT raw_json FROM entities WHERE source='childcare'"):
        nm = json.loads(r["raw_json"] or "{}").get("primarycontactpersonname")
        k = _person_key(nm)
        if k:
            counts[k] = counts.get(k, 0) + 1
    return counts


def cmd_resolve(args, config):
    storage.init_db()
    conn = storage.connect()
    try:
        entities = storage.load_all_entities(conn)
        if not entities:
            _print("No entities ingested yet — run 'ingest' first.")
            return
        scores = storage.scores_map(conn)
        registry = load_registry(config)
        from fraudscan.sos import load_sos
        from fraudscan.config import DATA_DIR
        sos = load_sos(DATA_DIR)
        if sos.count:
            _print(f"WA SOS extract loaded: {sos.count} business records — "
                   f"UBI/agent/officer linking ON.")
        else:
            _print("No WA SOS extract in data/sos/ — UBI/agent/officer linking OFF "
                   "(export from CCFS Advanced Business Search; see README).")
        # CMS ownership + reassignment operator edges (item 6)
        from fraudscan.registry import normalize as _norm
        extra_keys = {}
        try:
            from fraudscan.ownership import load_ownership
            own = load_ownership(state=config.get("ownership", {}).get("state", "WA"))
            o2o = own.get("org_to_owners", {})
            n = 0
            for e in entities:
                ok = o2o.get(_norm(e.name))
                if ok:
                    extra_keys.setdefault(e.uid, []).extend("owner:" + k for k in ok)
                    n += 1
            _print(f"CMS ownership links: {n} facilities matched to a beneficial owner "
                   f"(SNF + hospice + HHA).")
        except Exception as ex:
            _print(f"Ownership linking skipped ({ex}).")
        # NOTE: reassignment ("reassigns billing to a common group") was removed as an
        # operator-merge edge — it chains unrelated providers through large hospital
        # billing groups (Swedish, MultiCare, Kaiser) into false 100+-member blobs.
        # Shared employment is not a shared fraud operator. Ownership edges stay.
        member_facts = _member_facts(conn)
        name_counts = _name_counts(conn)
        rcfg = config.get("resolve", {})
        # sources with real street addresses participate in address/geo matching
        address_sources = {"childcare"} | {
            k for k, v in config.get("sources", {}).items()
            if v.get("kind") in ("cms_provider", "nppes")}
        operators, skipped = build_operators(
            entities, scores, registry=registry,
            max_key_members=rcfg.get("max_key_members", 8),
            fuzzy_threshold=rcfg.get("fuzzy_threshold", 0.9),
            fuzzy_max_block=rcfg.get("fuzzy_max_block", 40),
            geo_radius_m=rcfg.get("geo_radius_m", 50),
            geo_max_cell=rcfg.get("geo_max_cell", 15),
            address_sources=address_sources,
            institutional_suppression=config.get("legitimacy", {}).get(
                "institutional_suppression", 0.5),
            sos=sos if sos.count else None,
            extra_keys=extra_keys or None,
            member_facts=member_facts, name_counts=name_counts,
            national_count=_national_count_lookup())
        ecfg = config.get("external", {})
        if ecfg.get("enabled", True) and operators:
            try:
                from fraudscan.external import enrich_operators
                enrich_operators(operators, state=ecfg.get("state", "WA"),
                                 max_operators=ecfg.get("max_operators", 250))
                ff = sum(1 for o in operators if o.get("federal_funds"))
                npc = sum(1 for o in operators if o.get("nonprofit_ein"))
                fac_n = sum(1 for o in operators if o.get("audit_findings"))
                _print(f"Enriched operators: {ff} with federal awards (USAspending), "
                       f"{npc} matched to IRS 990, {fac_n} with FAC single-audit "
                       f"findings.")
                from fraudscan.fac import using_demo_key
                if using_demo_key():
                    _print("  Note: FAC using api.data.gov DEMO_KEY (~30 req/hr) — "
                           "coverage is limited & cached. Set FAC_API_KEY (free at "
                           "https://api.data.gov/signup) for full single-audit coverage.")
            except Exception as ex:
                _print(f"External enrichment skipped ({ex}).")
        if config.get("oig_cia", {}).get("enabled", True):
            try:
                from fraudscan.oig_cia import load_cia
                from fraudscan.registry import normalize as _n
                cia = load_cia()
                hits = 0
                for o in operators:
                    rec = cia.get("by_name", {}).get(_n(o["canonical_name"]))
                    if rec:
                        hits += 1
                        o["signals"] = list(o.get("signals", [])) + [
                            f"Under an OIG {rec.get('kind', 'integrity agreement')} "
                            f"({rec.get('city')}, {rec.get('state')}) — these are "
                            f"signed to settle federal fraud investigations; prior "
                            f"settled conduct is a strong repeat-risk signal"]
                        o["combined_score"] = min(100, round(
                            (o.get("combined_score") or 0) + 8, 1))
                _print(f"OIG CIA list (newest {cia.get('count', 0)}, partial): "
                       f"{hits} operator match(es).")
            except Exception as ex:
                _print(f"OIG CIA check skipped ({ex}).")
        if config.get("irs_revocation", {}).get("enabled", True):
            try:
                from fraudscan.irs_revocation import load_revoked, norm_ein
                eins = {norm_ein(o.get("nonprofit_ein")) for o in operators
                        if o.get("nonprofit_ein")}
                revoked = load_revoked(eins) if eins else {}
                hit = 0
                for o in operators:
                    r = revoked.get(norm_ein(o.get("nonprofit_ein")))
                    if not r:
                        continue
                    hit += 1
                    o["nonprofit_revoked"] = r.get("revoked") or "unknown date"
                    o["signals"] = list(o.get("signals", [])) + [
                        f"IRS tax-exempt status AUTO-REVOKED "
                        f"({r.get('revoked') or 'date unknown'}) — presents as a "
                        f"nonprofit (990 matched) but the IRS revoked the exemption "
                        f"for 3 years of non-filing"]
                    # an org whose nonprofit status was revoked must not enjoy the
                    # "established nonprofit" benefit of the doubt
                    o["combined_score"] = min(100, round(
                        (o.get("combined_score") or 0) + 10, 1))
                _print(f"IRS auto-revocation: checked {len(eins)} EINs — "
                       f"{hit} operator(s) with a REVOKED exemption.")
            except Exception as ex:
                _print(f"IRS revocation check skipped ({ex}).")
        storage.write_operators(conn, operators)
        cross = sum(1 for o in operators if o["source_count"] >= 2)
        combos = {}
        for o in operators:
            combos[" + ".join(o["sources"])] = combos.get(
                " + ".join(o["sources"]), 0) + 1
        _print(f"Resolved {len(entities)} entities -> {len(operators)} operators "
               f"of interest ({cross} cross-program, {len(operators) - cross} "
               f"single-program consolidations).")
        for combo, n in sorted(combos.items(), key=lambda kv: -kv[1]):
            _print(f"    {n:5d}  {combo}")
        if skipped:
            _print(f"  (skipped {len(skipped)} generic/chain keys shared by >"
                   f"{rcfg.get('max_key_members', 8)} entities)")
    finally:
        conn.close()


def cmd_payments(args, config):
    storage.init_db()
    conn = storage.connect()
    try:
        entities = storage.load_all_entities(conn)
        if not entities:
            _print("No entities ingested yet — run 'ingest' first.")
            return
        rows = build_payments(config, entities,
                              npi_map=storage.load_crosswalk(conn),
                              progress=lambda n: None)
        storage.write_payments(conn, rows)
        by_prog = {}
        for r in rows:
            by_prog.setdefault(r["program"], [0, 0.0])
            by_prog[r["program"]][0] += 1
            by_prog[r["program"]][1] += r["amount"]
        total = sum(r["amount"] for r in rows)
        _print(f"Surfaced {len(rows)} payment records totaling ${total:,.0f} "
               f"across {len({r['entity_uid'] for r in rows})} entities.")
        for prog, (n, amt) in sorted(by_prog.items(), key=lambda kv: -kv[1][1]):
            _print(f"    ${amt:>14,.0f}  {n:5d} records  {prog}")
        _print("  Note: Medicaid-by-NPI (HHS/T-MSIS) + WA Open Checkbook now included; "
               "childcare subsidy (SSPS#) + per-CLAIM Medicaid remain records-request-only.")
    finally:
        conn.close()


def cmd_crosswalk(args, config):
    ccfg = config.get("crosswalk", {})
    if not ccfg.get("enabled", True):
        return
    storage.init_db()
    conn = storage.connect()
    try:
        if getattr(args, "refresh_detail", False):
            from fraudscan.crosswalk import refresh_detail
            res = refresh_detail(conn, state=ccfg.get("state", "WA"),
                                 progress=lambda n: _print(f"  detail {n}")
                                 if n % 250 == 0 else None)
            _print(f"NPI crosswalk detail: backfilled practice city/taxonomy for "
                   f"{res['updated']}/{res['attempted']} resolved NPIs.")
            return
        res = build_crosswalk(conn, state=ccfg.get("state", "WA"),
                              cap=ccfg.get("cap", 500))
        _print(f"NPI crosswalk: resolved {res['found']}/{res['attempted']} "
               f"physician/prescriber providers to an NPI (cached/resumable).")
    finally:
        conn.close()


def cmd_discipline(args, config):
    import re
    from fraudscan.doh_discipline import build_discipline
    storage.init_db()
    conn = storage.connect()
    try:
        creds = set()
        for r in conn.execute("SELECT raw_json FROM entities WHERE source='healthcare'"):
            cn = json.loads(r[0] or "{}").get("credentialnumber")
            core = re.sub(r"\D", "", cn or "").lstrip("0")
            if core:
                creds.add(core)
    finally:
        conn.close()
    _print(f"Matching DOH disciplinary posts against {len(creds)} flagged credentials …")
    res = build_discipline(db_credentials=creds,
                           refresh=getattr(args, "refresh", False),
                           progress=lambda n: _print(f"  fetched {n} posts")
                           if n % 10 == 0 else None)
    _print(f"DOH discipline ingest: parsed {res['entries']} entries across "
           f"{res['posts']} posts; matched {res['matched']} to flagged providers → "
           f"data/context/doh_discipline.csv (restart 'serve' to surface).")


def cmd_medicaid_spending(args, config):
    """Stream the HHS 'Medicaid Provider Spending by HCPCS' extract (~3.76GB .csv.zip),
    keep only NPIs we track (ABA/NEMT/DME + crosswalked physicians), cache per-NPI $."""
    from fraudscan.medicaid_spending import build_medicaid_spending, save_cache
    storage.init_db()
    conn = storage.connect()
    try:
        npis = set()
        for r in conn.execute("SELECT DISTINCT source_id FROM entities WHERE source IN "
                              "('aba','nemt','dme') AND source_id<>''"):
            npis.add(str(r[0]))
        for npi in storage.load_crosswalk(conn).values():
            if npi:
                npis.add(str(npi))
        if not npis:
            _print("No NPIs to match (run ingest + crosswalk first).")
            return
        _print(f"Streaming HHS Medicaid spending (~3.76GB), filtering to {len(npis)} "
               f"tracked NPIs (this scans ~227M rows — several minutes)...")
        seen = {"gb": 0}

        def prog(b):
            gb = int(b / 5e8)                          # log every ~0.5 GB
            if gb > seen["gb"]:
                seen["gb"] = gb
                _print(f"  ...{b / 1e9:.1f} GB scanned")
        agg = build_medicaid_spending(npis, progress=prog)
        save_cache(agg)
        tot = sum(a.get("total", 0) for a in agg.values())
        _print(f"Medicaid spending: matched {len(agg)} of our NPIs; "
               f"${tot:,.0f} Medicaid $ surfaced (2018-2024). Re-run 'payments' + 'score'.")
    finally:
        conn.close()


def cmd_courts(args, config):
    """CourtListener/RECAP lookups for ESCALATED/WATCHED leads only (WA federal
    courts); hits become unverified context items."""
    from fraudscan.courtlistener import enrich_escalated
    storage.init_db()
    conn = storage.connect()
    try:
        r = enrich_escalated(conn, config,
                             max_lookups=config.get("courtlistener", {})
                             .get("max_lookups", 25))
        _print(f"CourtListener: {r['candidates']} escalated/watched candidate(s), "
               f"{r['looked_up']} looked up, {r['new_context']} new context item(s)."
               + ("" if r["token"] else "  (anonymous mode — set COURTLISTENER_TOKEN "
                  "for higher rate limits; free at courtlistener.com)"))
    finally:
        conn.close()


def cmd_news_ingest(args, config):
    """Pull DOJ USAO-WDWA/EDWA press feeds; write matches into data/context/ (curated
    context pipeline) for flagged entities."""
    from fraudscan.doj_news import ingest
    storage.init_db()
    conn = storage.connect()
    try:
        seen, new = ingest(conn)
        _print(f"DOJ news: scanned {seen} fraud-related release(s); "
               f"{new} new match(es) written to data/context/doj_auto.csv.")
    finally:
        conn.close()


def cmd_state_checkbook(args, config):
    """Stream WA fiscal.wa.gov Open Checkbook (vendor payments) and attach WA-state $
    (HCA/DSHS/DCYF/DOH) to entities by normalized vendor name."""
    from fraudscan.state_checkbook import build_checkbook, save_cache
    from fraudscan.registry import normalize
    storage.init_db()
    conn = storage.connect()
    try:
        # providers only — contract entities are excluded (their disbursements are the
        # same money as the contract amounts we already hold; see state_checkbook docs)
        names = {normalize(r[0]) for r in conn.execute(
            "SELECT name FROM entities WHERE name<>'' AND source NOT LIKE 'contracts%'")
            if r[0]}
        names.discard("")
        _print(f"Streaming WA Open Checkbook, matching against {len(names)} provider "
               f"names (HCA/DSHS/DCYF/DOH payments)...")
        agg = build_checkbook(names, progress=lambda b: _print(
            f"  downloaded {b/1e6:.0f} MB; parsing..."))
        save_cache(agg)
        tot = sum(a.get("total", 0) for a in agg.values())
        _print(f"WA Open Checkbook: matched {len(agg)} vendors to our entities; "
               f"${tot:,.0f} in WA-state payments surfaced. Re-run 'payments' + 'score'.")
    finally:
        conn.close()


def cmd_childcare_enforcement(args, config):
    from fraudscan.childcare_enforcement import build_enforcement
    storage.init_db()
    conn = storage.connect()
    try:
        enf, fetched = build_enforcement(
            conn, cap=getattr(args, "cap", None) or 100000,
            refresh=getattr(args, "refresh", False),
            progress=lambda n: _print(f"  fetched {n} provider pages"))
        comp = sum(1 for d in enf.values() if d.get("complaints"))
        _print(f"Childcare enforcement: fetched {fetched} new page(s); "
               f"{len(enf)} providers with data, {comp} with valid complaint(s) "
               f"(findchildcarewa / DCYF Child Care Check). Re-run 'score' to apply.")
    finally:
        conn.close()


def cmd_run(args, config):
    cmd_ingest(args, config)
    cmd_crosswalk(args, config)  # resolve NPIs before payments/score use them
    cmd_payments(args, config)   # before score so payment-anomaly rules have data
    cmd_score(args, config)
    try:
        cmd_discipline(args, config)  # context narratives (best-effort, network)
    except Exception as ex:
        _print(f"Discipline ingest skipped ({ex}).")
    cmd_resolve(args, config)
    _print("\nDone. Launch the dashboard with:  python -m fraudscan serve")


def cmd_serve(args, config):
    from fraudscan.web.server import serve
    serve(host=args.host, port=args.port)


def main(argv=None):
    config = load_config()
    keys = all_source_keys(config)
    parser = argparse.ArgumentParser(
        prog="fraudscan",
        description="Surface anomaly/fraud-risk LEADS in WA public spending. "
                    "Flags are leads for human review, never determinations.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("sources", help="list data sources")
    p.set_defaults(func=cmd_sources)

    p = sub.add_parser("ingest", help="pull public data into the local database")
    p.add_argument("--source", choices=keys)
    p.add_argument("--limit", type=int, default=None)
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("score", help="run rules + scoring over ingested data")
    p.add_argument("--source", choices=keys)
    p.set_defaults(func=cmd_score)

    p = sub.add_parser("resolve", help="cluster entities into cross-source operators")
    p.set_defaults(func=cmd_resolve)

    p = sub.add_parser("payments", help="attach public $ amounts + periods to entities")
    p.set_defaults(func=cmd_payments)

    p = sub.add_parser("crosswalk", help="resolve sanctioned physicians to NPIs (NPPES)")
    p.add_argument("--refresh-detail", action="store_true",
                   help="backfill NPPES practice city/taxonomy for resolved NPIs")
    p.set_defaults(func=cmd_crosswalk)

    p = sub.add_parser("run", help="ingest all enabled sources, then score")
    p.add_argument("--source", choices=keys)
    p.add_argument("--limit", type=int, default=None)
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("discipline", help="ingest DOH disciplinary-action narratives "
                       "→ data/context/ (matched by credential)")
    p.add_argument("--refresh", action="store_true", help="re-fetch all posts")
    p.set_defaults(func=cmd_discipline)

    p = sub.add_parser("medicaid-spending", help="stream HHS Medicaid-by-NPI spending "
                       "(T-MSIS) and attach Medicaid $ to ABA/NEMT/DME/physicians")
    p.set_defaults(func=cmd_medicaid_spending)
    p = sub.add_parser("courts", help="RECAP docket lookups for escalated/watched "
                       "leads (WA federal courts; context only, never flags)")
    p.set_defaults(func=cmd_courts)
    p = sub.add_parser("news-ingest", help="ingest DOJ WDWA/EDWA press releases as "
                       "context for flagged entities (namesake-guarded)")
    p.set_defaults(func=cmd_news_ingest)
    p = sub.add_parser("state-checkbook", help="stream WA Open Checkbook vendor payments "
                       "and attach WA-state $ (HCA/DSHS/DCYF/DOH) by vendor name")
    p.set_defaults(func=cmd_state_checkbook)
    p = sub.add_parser("childcare-enforcement", help="scrape findchildcarewa for "
                       "per-provider complaints/inspections (DCYF Child Care Check)")
    p.add_argument("--cap", type=int, default=None, help="max new pages to fetch")
    p.add_argument("--refresh", action="store_true", help="re-fetch all providers")
    p.set_defaults(func=cmd_childcare_enforcement)

    p = sub.add_parser("serve", help="launch the local web dashboard")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.set_defaults(func=cmd_serve)

    args = parser.parse_args(argv)
    args.func(args, config)
    return 0


if __name__ == "__main__":
    sys.exit(main())
