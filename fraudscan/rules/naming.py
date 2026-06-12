"""Name-quality rule: flag likely misspellings in a provider/vendor name.

Data-driven and dependency-free. We learn the "correct" vocabulary from the corpus of
all names in a source (words that appear often are real words), seeded with common
domain words, then flag a *rare* word in a name that is exactly one edit (insert /
delete / replace / transpose) away from a common word — e.g. "learing" → "learning",
"acadmy" → "academy".

This is a lead, not proof: a typo can be innocent, but misspelled near-copies of
legitimate names are a known shell/impersonation tactic, so they're worth a look.
"""
import os
import re
from collections import Counter

from fraudscan.rules.base import Flag

_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_WORD = re.compile(r"[A-Za-z]+")

# Optional real-word filter: a token that IS a real English word (e.g. PEACE, ALLEY,
# PALACE) is not a misspelling, even if it's one edit from a domain word. We load a
# system word list if present; absent it, the rule still works (just noisier).
_DICT_PATHS = ("/usr/share/dict/words", "/usr/dict/words",
               "/usr/share/dict/web2")
_REAL_WORDS = None


def _real_words():
    global _REAL_WORDS
    if _REAL_WORDS is None:
        _REAL_WORDS = set()
        for p in _DICT_PATHS:
            if os.path.exists(p):
                try:
                    with open(p, "r", encoding="utf-8", errors="ignore") as fh:
                        _REAL_WORDS = {w.strip().upper() for w in fh if w.strip()}
                    break
                except OSError:
                    continue
    return _REAL_WORDS


def _is_plural_variant(a, b):
    for x, y in ((a, b), (b, a)):
        if x == y + "S" or x == y + "ES":
            return True
    return False


def _lev_within(a, b, maxd):
    """True if Levenshtein(a, b) <= maxd, with early exit. Cheap for short words."""
    la, lb = len(a), len(b)
    if abs(la - lb) > maxd:
        return False
    prev = list(range(lb + 1))
    for i in range(1, la + 1):
        cur = [i] + [0] * lb
        best = cur[0]
        ca = a[i - 1]
        for j in range(1, lb + 1):
            cost = 0 if ca == b[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if cur[j] < best:
                best = cur[j]
        if best > maxd:
            return False
        prev = cur
    return prev[lb] <= maxd

# Seed vocabulary so the check works even where the corpus is small (e.g. 50 hospices).
# Domain words + quality/marketing words common in provider names + frequently-
# misspelled English words. Adding a word only ENABLES catching misspellings of it; the
# real-word / plural / min-length guards prevent it from causing false positives.
COMMON_SEED = {
    # child care / education
    "LEARNING", "ACADEMY", "CHILDCARE", "PRESCHOOL", "CENTER", "CENTERS",
    "CHILDREN", "CHILDRENS", "DEVELOPMENT", "MONTESSORI", "EDUCATION", "EARLY",
    "SCHOOL", "CHILD", "CARE", "KIDS", "DAYCARE", "FAMILY", "COMMUNITY",
    "PROGRAM", "PROGRAMS", "PLACE", "CAMPUS", "INFANT", "TODDLER", "ELEMENTARY",
    "EXTENDED", "ENRICHMENT", "DISCOVERY", "CURIOSITY", "IMAGINATION",
    "ADVENTURE", "EXPLORERS", "MILESTONES", "PATHWAYS", "POTENTIAL", "GENERATION",
    "KNOWLEDGE", "BEGINNINGS", "BLESSINGS", "TREASURE", "SUNSHINE", "RAINBOW",
    # health / facility / transport
    "HEALTH", "HOSPICE", "HOSPITAL", "MEDICAL", "TRANSPORT", "TRANSPORTATION",
    "NURSING", "THERAPY", "BEHAVIOR", "BEHAVIORAL", "REHABILITATION", "CLINIC",
    "EQUIPMENT", "SUPPLIES", "DIALYSIS", "CONSULTING", "HOME", "PEDIATRIC",
    "COUNSELING", "WELLNESS", "RECOVERY", "TREATMENT", "RESIDENTIAL", "HOSPICE",
    # org / quality / marketing
    "SERVICES", "SERVICE", "CHRISTIAN", "PARTNERS", "INTERNATIONAL", "FOUNDATION",
    "MINISTRY", "MINISTRIES", "INSTITUTE", "REGIONAL", "NETWORK", "SUPPORT",
    "PROVIDERS", "INCLUSION", "ASSOCIATION", "ASSOCIATES", "RESOURCES",
    "SOLUTIONS", "MANAGEMENT", "ENVIRONMENT", "GUARDIAN", "ASSISTANCE",
    "OPPORTUNITY", "OPPORTUNITIES", "EXCEPTIONAL", "EXCELLENCE", "PROFESSIONAL",
    "ACHIEVEMENT", "INDEPENDENT", "COMPREHENSIVE", "INTEGRATED", "ACCREDITED",
    "CERTIFIED", "LICENSED", "REGISTERED", "DEDICATED", "NURTURING", "SPECIALIZED",
    "CONNECTION", "CONNECTIONS", "MAINTENANCE", "ACCOMMODATION", "NECESSARY",
}

# 2-edit matching is only allowed against these: long, frequently-misspelled words
# with few real near-variants (so we don't map "Medicare"→"Medical" or
# "Tendercare"→"Kindercare"). Keep this list tight on purpose.
EDIT2_ANCHORS = {
    "PROFESSIONAL", "ACCOMMODATION", "REHABILITATION", "COMPREHENSIVE",
    "DEVELOPMENT", "ENVIRONMENT", "INDEPENDENT", "OPPORTUNITY", "INTERNATIONAL",
    "ACHIEVEMENT", "MANAGEMENT", "ASSOCIATION", "FOUNDATION", "TRANSPORTATION",
    "EXCELLENCE", "ENRICHMENT", "MONTESSORI", "COUNSELING", "ELEMENTARY",
    "EDUCATION", "BEHAVIORAL", "RESIDENTIAL", "PEDIATRIC",
}


def _edits1(w):
    splits = [(w[:i], w[i:]) for i in range(len(w) + 1)]
    deletes = [a + b[1:] for a, b in splits if b]
    transposes = [a + b[1] + b[0] + b[2:] for a, b in splits if len(b) > 1]
    replaces = [a + c + b[1:] for a, b in splits if b for c in _ALPHABET]
    inserts = [a + c + b for a, b in splits for c in _ALPHABET]
    return set(deletes + transposes + replaces + inserts)


def misspelled_word_in_name(entities, cfg):
    sev = cfg.get("severity", 8)
    common_min = cfg.get("common_min", 25)
    rare_max = cfg.get("rare_max", 3)
    min_len = cfg.get("min_len", 5)
    min_target_len = cfg.get("min_target_len", 5)  # avoid coincidences on short words
    max_edits = cfg.get("max_edits", 2)            # allow a 2-edit fallback for long words
    edit2_min_len = cfg.get("edit2_min_len", 8)

    freq = Counter()
    for e in entities:
        for w in _WORD.findall((e.name or "").upper()):
            freq[w] += 1
    common = {w for w, c in freq.items() if c >= common_min and len(w) >= 4}
    common |= COMMON_SEED
    real = _real_words()

    out = []
    for e in entities:
        hits = []
        for w in _WORD.findall((e.name or "").upper()):
            if len(w) < min_len or w in common or freq.get(w, 0) > rare_max:
                continue
            if w in real:                     # a real English word isn't a misspelling
                continue
            cands = {c for c in (_edits1(w) & common)
                     if len(c) >= min_target_len and not _is_plural_variant(w, c)}
            if not cands and max_edits >= 2 and len(w) >= edit2_min_len:
                cands = {c for c in EDIT2_ANCHORS
                         if not _is_plural_variant(w, c) and _lev_within(w, c, 2)}
            if cands:
                best = max(cands, key=lambda x: freq.get(x, 0))
                hits.append((w, best))
        if hits:
            shown = "; ".join(f"'{w.title()}' → '{c.title()}'" for w, c in hits[:3])
            out.append(Flag(
                e.uid, "misspelled_word_in_name", sev,
                "Possible misspelling in name",
                f"Name contains a likely misspelling ({shown}) — a sloppy/hasty "
                f"registration, or a near-copy of a legitimate name.",
                {"misspellings": [{"word": w, "likely": c} for w, c in hits[:5]],
                 "name": e.name},
            ))
    return out
