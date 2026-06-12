"""Map each source to its rules.

Built-in sources map directly; config-driven sources select a shared rule *profile*
(e.g. "facility") via their config, so a new category needs no code here.
"""
from fraudscan.rules.childcare import CHILDCARE_RULES
from fraudscan.rules.contracts import CONTRACT_RULES
from fraudscan.rules.healthcare import HEALTHCARE_RULES
from fraudscan.rules.facility import FACILITY_RULES
from fraudscan.rules.cross import CROSS_RULES_BY_SOURCE, GLOBAL_CROSS_RULES

RULES_BY_SOURCE = {
    "childcare": CHILDCARE_RULES,
    "contracts": CONTRACT_RULES,
    "healthcare": HEALTHCARE_RULES,
}

# Reusable rule sets selectable from a source's config via "rules_profile".
PROFILES = {
    "facility": FACILITY_RULES,
}


def rules_for_source(source, config):
    if source in RULES_BY_SOURCE:
        return RULES_BY_SOURCE[source]
    profile = config.get("sources", {}).get(source, {}).get("rules_profile")
    if profile in RULES_BY_SOURCE:          # e.g. contracts_2025 -> contracts rules
        return RULES_BY_SOURCE[profile]
    return PROFILES.get(profile, [])


def rules_cfg_for_source(source, config):
    """Per-source rules config, falling back to the source's rules_profile section so
    config-driven year-series sources (contracts_2025 etc.) inherit severities."""
    rules = config.get("rules", {})
    if source in rules:
        return rules[source]
    profile = config.get("sources", {}).get(source, {}).get("rules_profile")
    return rules.get(profile, {})


def run_rules(source, entities, source_rule_cfg, source_rules, context=None,
              global_cfg=None):
    """Run a source's rules + per-source cross rules + global cross rules."""
    flags = []
    cfg = source_rule_cfg or {}
    gcfg = global_cfg or {}
    for rule_id, fn in source_rules:
        flags.extend(fn(entities, cfg.get(rule_id, {})))
    for rule_id, fn in CROSS_RULES_BY_SOURCE.get(source, []):
        flags.extend(fn(entities, cfg.get(rule_id, {}), context))
    for rule_id, fn in GLOBAL_CROSS_RULES:
        flags.extend(fn(entities, gcfg.get(rule_id, {}), context))
    return flags
