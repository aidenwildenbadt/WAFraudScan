"""Health-care provider sanction / integrity rules (DOH credential data).

Leads, not conclusions: a sanctioned credential is a reason to check whether that
provider is still receiving public money, not proof that they are. Adverse status +
disciplinary action are the standard signals Medicaid program-integrity teams screen.
"""
from fraudscan.rules.base import Flag, parse_date, today

REVOKED_SUSPENDED = {"REVOKED", "SUSPENDED"}
SURRENDERED = {"SURRENDER", "VOLUNTARY SURRENDER"}


def _status(e):
    return (e.status or "").strip().upper()


def disciplinary_action_taken(entities, cfg):
    sev = cfg.get("severity", 22)
    out = []
    for e in entities:
        if (e.raw.get("actiontaken") or "").strip().upper() == "YES":
            out.append(Flag(
                e.uid, "disciplinary_action_taken", sev,
                "Disciplinary action on record",
                f"A disciplinary action has been taken against this "
                f"{e.entity_type or 'credential'} (status: {e.status}).",
                {"credential_type": e.entity_type, "status": e.status,
                 "credential_number": e.source_id},
            ))
    return out


def disciplinary_action_pending(entities, cfg):
    sev = cfg.get("severity", 12)
    out = []
    for e in entities:
        if (e.raw.get("actiontaken") or "").strip().upper() == "PENDING":
            out.append(Flag(
                e.uid, "disciplinary_action_pending", sev,
                "Disciplinary action pending",
                f"A disciplinary action is pending (status: {e.status}).",
                {"credential_type": e.entity_type, "status": e.status},
            ))
    return out


def credential_revoked_or_suspended(entities, cfg):
    sev = cfg.get("severity", 30)
    out = []
    for e in entities:
        if _status(e) in REVOKED_SUSPENDED:
            out.append(Flag(
                e.uid, "credential_revoked_or_suspended", sev,
                "Credential revoked or suspended",
                f"Credential status is '{e.status}'. A provider in this state is not "
                f"authorized to practice; verify they are not still billing.",
                {"credential_type": e.entity_type, "status": e.status,
                 "expiration_date": e.raw.get("expirationdate")},
            ))
    return out


def credential_surrendered(entities, cfg):
    sev = cfg.get("severity", 16)
    out = []
    for e in entities:
        if _status(e) in SURRENDERED:
            out.append(Flag(
                e.uid, "credential_surrendered", sev,
                "Credential surrendered",
                f"Credential was surrendered (status: '{e.status}') — often resolves a "
                f"complaint or investigation.",
                {"credential_type": e.entity_type, "status": e.status},
            ))
    return out


def credential_active_with_conditions(entities, cfg):
    sev = cfg.get("severity", 18)
    out = []
    for e in entities:
        if _status(e) == "ACTIVE WITH CONDITIONS":
            out.append(Flag(
                e.uid, "credential_active_with_conditions", sev,
                "Practicing under conditions/restrictions",
                "Credential is active but subject to conditions or restrictions.",
                {"credential_type": e.entity_type, "status": e.status},
            ))
    return out


HEALTHCARE_RULES = [
    ("credential_revoked_or_suspended", credential_revoked_or_suspended),
    ("disciplinary_action_taken", disciplinary_action_taken),
    ("credential_active_with_conditions", credential_active_with_conditions),
    ("credential_surrendered", credential_surrendered),
    ("disciplinary_action_pending", disciplinary_action_pending),
]
