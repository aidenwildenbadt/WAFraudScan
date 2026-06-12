"""Contract audit-heuristic rules (demonstration of multi-source scoring).

These are classic procurement-audit signals, not accusations: duplicate amounts to
the same vendor can indicate split or double billing; very round large amounts can
indicate estimate-based rather than itemized contracting. Both warrant a look.
"""
from fraudscan.rules.base import Flag, norm_text


def contract_duplicate_amount_vendor(entities, cfg):
    sev = cfg.get("severity", 14)
    min_amount = cfg.get("min_amount", 5000)
    groups = {}
    for e in entities:
        if e.amount is None or e.amount < min_amount:
            continue
        key = (norm_text(e.name), round(e.amount, 2))
        groups.setdefault(key, []).append(e)
    out = []
    for (vendor, amount), members in groups.items():
        if len({e.uid for e in members}) < 2:
            continue
        for e in members:
            out.append(Flag(
                e.uid, "contract_duplicate_amount_vendor", sev,
                "Duplicate contract amount to same vendor",
                f"{len(members)} contracts to '{e.name}' share the identical amount "
                f"${amount:,.2f}.",
                {"vendor": e.name, "amount": amount, "occurrences": len(members)},
            ))
    return out


def contract_round_large_amount(entities, cfg):
    sev = cfg.get("severity", 6)
    min_amount = cfg.get("min_amount", 100000)
    round_to = cfg.get("round_to", 10000)
    out = []
    for e in entities:
        if e.amount is None or e.amount < min_amount:
            continue
        if e.amount % round_to == 0:
            out.append(Flag(
                e.uid, "contract_round_large_amount", sev,
                "Large, very round contract amount",
                f"Contract amount ${e.amount:,.0f} is an exact multiple of "
                f"${round_to:,} — often estimate-based rather than itemized.",
                {"amount": e.amount, "round_to": round_to},
            ))
    return out


CONTRACT_RULES = [
    ("contract_duplicate_amount_vendor", contract_duplicate_amount_vendor),
    ("contract_round_large_amount", contract_round_large_amount),
]
