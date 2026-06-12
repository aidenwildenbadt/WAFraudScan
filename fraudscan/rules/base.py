"""Rule primitives + small shared helpers.

A rule is a function (entities, rule_cfg) -> list[Flag]. Rules operate over the
*whole* set of entities for a source so they can detect cross-record patterns
(shared addresses, shared contacts, capacity outliers), not just per-row checks.
Every Flag carries human-readable evidence so a person can verify the lead.
"""
import re
from dataclasses import dataclass, field
from datetime import date, datetime


@dataclass
class Flag:
    entity_uid: str
    rule_id: str
    severity: float
    title: str
    explanation: str
    evidence: dict = field(default_factory=dict)


# ---- normalization / parsing helpers ----

def norm_phone(v):
    if not v:
        return ""
    digits = re.sub(r"\D", "", str(v))
    return digits[-10:] if len(digits) >= 10 else ""


def norm_email(v):
    return str(v).strip().lower() if v else ""


def norm_text(v):
    return " ".join(str(v).upper().split()) if v else ""


def norm_addr(entity):
    return norm_text(f"{entity.address} {entity.city} {entity.zip}")


def parse_date(v):
    if not v:
        return None
    s = str(v)[:10]
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def today():
    return date.today()


def mean_std(values):
    n = len(values)
    if n == 0:
        return 0.0, 0.0
    mean = sum(values) / n
    var = sum((x - mean) ** 2 for x in values) / n
    return mean, var ** 0.5


def percentile(values, pct):
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * (pct / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)
