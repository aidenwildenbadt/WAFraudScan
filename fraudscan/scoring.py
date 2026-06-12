"""Aggregate per-entity flags into a transparent, de-correlated risk score.

risk_score = min(cap, Σ over correlation-groups of the MAX severity in that group).
Grouping prevents double-counting correlated facts (e.g. a suspended credential and its
disciplinary action are one event, not two). We also track how many independent signal
*families* an entity spans — a multi-family lead outranks a one-family pile-up — and use
that (then flag count) as the sort tie-breaker. It is an ordering aid, not a probability;
the contributing flags are always shown alongside.
"""
from fraudscan.taxonomy import family, group


def score_entities(flags, score_cap=100):
    by_entity = {}
    for f in flags:
        d = by_entity.setdefault(f.entity_uid, {
            "groups": {}, "families": set(), "count": 0, "top": None, "top_sev": -1})
        g = group(f.rule_id)
        d["groups"][g] = max(d["groups"].get(g, 0.0), f.severity)
        d["families"].add(family(f.rule_id))
        d["count"] += 1
        if f.severity > d["top_sev"]:
            d["top_sev"], d["top"] = f.severity, f.rule_id

    rows = []
    for uid, d in by_entity.items():
        base = sum(d["groups"].values())
        rows.append({
            "entity_uid": uid,
            "risk_score": round(min(score_cap, base), 1),
            "flag_count": d["count"],
            "family_count": len(d["families"]),
            "top_rule": d["top"],
        })
    rows.sort(key=lambda r: (r["risk_score"], r["family_count"], r["flag_count"]),
              reverse=True)
    return rows
