"""Rule taxonomy: each rule's signal *family* and *correlation group*.

Scoring uses this to (a) avoid double-counting correlated facts — rules in the same
correlation group contribute only their MAX severity, not their sum — and (b) reward
*diversity*: an entity flagged across several independent families is a stronger lead
than one tripping three correlated rules. Families: integrity (barred/unauthorized),
network (shells/shared identity/ownership), billing (dollar anomalies), quality (data
or care-quality context).
"""

RULE_FAMILY = {
    # integrity — barred, unauthorized, or sanctioned
    "excluded_or_sanctioned": "integrity",
    "excluded_license_derived": "integrity",
    "paid_before_bar": "quality",
    "no_active_business_registration": "integrity",
    "credential_revoked_or_suspended": "integrity",
    "disciplinary_action_taken": "integrity",
    "disciplinary_action_pending": "integrity",
    "credential_surrendered": "integrity",
    "credential_active_with_conditions": "integrity",
    "license_expired_active": "integrity",
    "payment_only_certificate": "integrity",
    "childcare_valid_complaint": "integrity",
    "paid_after_npi_deactivated": "billing",
    "lni_debarred": "integrity",
    "lni_strike": "integrity",
    "contract_during_debarment": "integrity",
    # network — shells, shared identity, ownership
    "address_shared_multiple_providers": "network",
    "shared_contact_multiple_providers": "network",
    "concentration_same_contact_person": "network",
    "shared_owner": "network",
    "ownership_churn": "network",
    "commercial_mailbox": "network",
    "virtual_office": "network",
    "po_box_address": "network",
    # billing — dollar anomalies
    "payment_outlier": "billing",
    "billing_spike": "billing",
    "paid_while_sanctioned": "billing",
    "paid_after_barred": "billing",
    "paid_attribution_unconfirmed": "billing",
    "single_code_dominance": "billing",
    "services_per_visit_outlier": "billing",
    "contract_duplicate_amount_vendor": "billing",
    "contract_round_large_amount": "billing",
    # quality / context
    "low_quality_rating": "quality",
    "payment_denial_imposed": "billing",
    "immediate_jeopardy_citation": "quality",
    "civil_monetary_penalty": "quality",
    "many_health_deficiencies": "quality",
    "capacity_outlier_high": "quality",
    "capacity_missing_or_zero": "quality",
    "recent_license_high_capacity": "quality",
    "recently_certified": "quality",
    "childcare_many_inspections": "quality",
    "missing_contact_info": "quality",
    "ungeocoded_address": "quality",
    "for_profit_ownership": "quality",
    "misspelled_word_in_name": "quality",
}

# Rules sharing a correlation group describe the SAME underlying fact — count the max,
# not the sum (e.g. a DOH credential being suspended IS the disciplinary action).
RULE_GROUP = {
    "credential_revoked_or_suspended": "doh_sanction",
    "disciplinary_action_taken": "doh_sanction",
    "disciplinary_action_pending": "doh_sanction",
    "credential_surrendered": "doh_sanction",
    "credential_active_with_conditions": "doh_sanction",
    "shared_contact_multiple_providers": "shared_identity",
    "concentration_same_contact_person": "shared_identity",
    "capacity_outlier_high": "capacity",
    "capacity_missing_or_zero": "capacity",
    # paid-after-deactivation correlates with paid-after-barred when both stem from the
    # same departure event (death/retirement) — same group so they don't double-stack
    "paid_after_npi_deactivated": "paid_after",
    "lni_debarred": "lni",
    "lni_strike": "lni",
    "contract_during_debarment": "lni",
    "paid_while_sanctioned": "paid_after",
    "paid_after_barred": "paid_after",
    # one license event must score ONCE: the DOH action, its federal 1128(b)(4)/(b)(5)
    # echo, and the informational pre-bar disposition all share the doh_sanction group
    "excluded_license_derived": "doh_sanction",
    "paid_before_bar": "doh_sanction",
}


def family(rule_id):
    return RULE_FAMILY.get(rule_id, "quality")


def group(rule_id):
    return RULE_GROUP.get(rule_id, rule_id)
