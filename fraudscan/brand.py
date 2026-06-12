"""Brand extraction for multi-site organizations.

The chain-structure signals (shared contact, co-located registrations, fuzzy name
variants) were built to catch HIDDEN common control — one operator behind several
differently-branded registrations. A 10-site nonprofit whose sites are all openly named
"MARTHA & MARY <school>" is the opposite of hidden: the brand IS the disclosure (case
study: Martha & Mary Lutheran Services, Poulsbo — scored 79 purely on chain structure).

brand_key() reduces a facility name to its distinctive brand root (generic childcare/
facility words and site numerals dropped), so rules can distinguish:
  same brand across sites  -> open chain, weak signal (discounted)
  different brands sharing contacts/addresses -> the actual shell/hidden-control pattern
"""
import re

from fraudscan.registry import normalize

# Words too generic to identify an operator on their own (shared with resolve's
# org-key guard). A name made up ENTIRELY of these must not form a merge key either.
GENERIC_TOKENS = {
    "EARLY", "LEARNING", "CENTER", "CENTERS", "CENTRE", "CHILD", "CHILDS", "CHILDREN",
    "CHILDRENS", "CARE", "KIDS", "KID", "DAY", "DAYCARE", "CHILDCARE", "PRESCHOOL",
    "SCHOOL", "ACADEMY", "DEVELOPMENT", "CREATIVE", "LITTLE", "EDUCATION", "EDUCATIONAL",
    "PROGRAM", "PROGRAMS", "LEARNERS", "ENRICHMENT", "THE", "AND", "OF", "FOR", "A",
    "INC", "LLC", "CO", "CENTER'S", "AGE", "AGED", "SERVICES", "SERVICE", "FAMILY",
    "COMMUNITY", "HOUSE", "CLUB", "SITE", "CAMPUS", "ELEMENTARY", "ELEM",
}
_SITE_NUMERAL = re.compile(r"^(#?\d+|[IVXLC]{1,5})$")   # site numbers: 2, #906, IX, XII


def is_generic_name(k):
    toks = [t for t in k.split() if t]
    return bool(toks) and all(t.upper() in GENERIC_TOKENS or _SITE_NUMERAL.match(t)
                              for t in toks)


_ALIAS = {"MT": "MOUNT", "ST": "SAINT"}
_SITE_SUFFIX = re.compile(r"\s*[-#]?\s*\d{1,3}$")


def strip_site_suffix(normalized_name):
    """Drop a trailing site-number token — DSHS routinely registers facilities as
    'Sullivan Park Care Center - 02' / 'Heartwood Extended Healthcare - 01', which
    broke exact DBA↔name matching (fleet audit G8)."""
    return _SITE_SUFFIX.sub("", normalized_name or "").strip()


def brand_key(name):
    """Distinctive brand root of a facility name: first two non-generic, non-numeral
    tokens of the normalized name ('MARTHA & MARY VINLAND SCHOOL AGE PROGRAM' ->
    'MARTHA MARY'; 'CHILDS TIME IX' -> 'TIME'). 'OF'/'AT' end the brand — franchise
    naming is 'BRAND OF <city> AT <site>' ('KIDDIE ACADEMY OF SEATTLE AT QUEEN ANNE'
    -> 'KIDDIE'), and 'MT'/'ST' normalize so PROVIDENCE MT ST VINCENT folds with
    PROVIDENCE MOUNT SAINT VINCENT. Empty when nothing distinctive."""
    toks = []
    for t in normalize(name).upper().split():
        if t in ("OF", "AT"):
            if toks:
                break
            continue
        if not t or t in GENERIC_TOKENS or _SITE_NUMERAL.match(t):
            continue
        toks.append(_ALIAS.get(t, t))
        if len(toks) == 2:
            break
    key = " ".join(toks)
    return key if len(key) >= 4 else ""


_VENUE = re.compile(
    r"COMMUNITY CENTER|COMMUNITY DEVELOPMENT|NEIGHBORHOOD CENTER|"
    r"DEVELOPMENT ASSOCIATION|ELEMENTARY|\bELEM\b|MIDDLE SCHOOL|HIGH SCHOOL|"
    r"\bK-?8\b|\bCHURCH\b|\bPARISH\b|\bTEMPLE\b|\bMOSQUE\b|\bSYNAGOGUE\b|"
    r"\bFELLOWSHIP\b|\bYMCA\b|\bYWCA\b|BOYS (&|AND) GIRLS|\bLIBRARY\b|\bCOLLEGE\b|"
    r"\bUNIVERSITY\b|PARKS (&|AND) REC|RECREATION CENTER|\bGRANGE\b|\bLODGE\b")


def is_venue(name, dba=""):
    """Host-venue facilities (schools, community centers, churches...). A provider
    registered at the same address as its HOST venue is a normal hosting arrangement —
    not the differently-named-shells-at-one-address pattern. Checks the DBA too — the
    West Central audit: legal name 'X Community DEVELOPMENT', dba 'X Community CENTER'."""
    return bool((name and _VENUE.search(name.upper()))
                or (dba and _VENUE.search(dba.upper())))


def distinctive_tokens(name):
    """Non-generic, non-numeral tokens of a normalized name. A DBA like 'Little
    Blessings Preschool' has only ONE ('BLESSINGS') — too weak to merge operators on
    (it glued a Vancouver parish school into a Port Angeles cluster)."""
    return [t for t in normalize(name).upper().split()
            if t and t not in GENERIC_TOKENS and not _SITE_NUMERAL.match(t)]


def open_brand(names, min_count=3, min_share=0.7):
    """The openly-shared brand root across a set of facility names, or None.
    Requires at least `min_count` named entities and `min_share` of the branded names
    agreeing on one root — i.e. the chain is advertising its common ownership."""
    keys = [brand_key(n) for n in names if n]
    keys = [k for k in keys if k]
    if len(keys) < min_count:
        return None
    # prefix folding (Gateway audit): 'GATEWAY EXTENDED' must count toward 'GATEWAY'
    # when the bare single-token key also occurs in the cluster — one-token vs
    # two-token roots otherwise fail to align and the discount never fires. Folding
    # only toward an OCCURRING single key avoids merging unrelated 'CASCADE *' brands.
    singles = {k for k in keys if " " not in k}
    keys = [k.split()[0] if (" " in k and k.split()[0] in singles) else k
            for k in keys]
    counts = {}
    for k in keys:
        counts[k] = counts.get(k, 0) + 1
    best, n = max(counts.items(), key=lambda kv: kv[1])
    return best if n / len(keys) >= min_share else None
