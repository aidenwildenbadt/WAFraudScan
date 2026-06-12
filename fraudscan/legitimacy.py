"""Recognize obviously-legitimate institutions so they don't dominate the queue.

Government bodies, school districts, tribes, and well-known nonprofits (YMCA, Boys &
Girls Club, Head Start / Community Action agencies, etc.) are *legitimately* multi-
program and high-dollar — they were saturating the top at score 100. We don't hide them
(a real issue could involve one), but we apply a suppression multiplier so genuinely
unusual operators rank above them. IRS-990 nonprofit status is layered on separately as
a softer "established" signal.
"""
import re

_PATTERNS = [
    r"SCHOOL DISTRICT", r"PUBLIC SCHOOLS?", r"\bUNIVERSITY\b", r"\bCOLLEGE\b",
    r"\bCOUNTY\b", r"\bCITY OF\b", r"\bSTATE OF\b", r"\bPORT OF\b", r"\bTOWN OF\b",
    r"HEAD START", r"\bYMCA\b", r"\bYWCA\b", r"BOYS & GIRLS CLUB",
    r"BOYS AND GIRLS CLUB", r"COMMUNITY ACTION", r"UNITED WAY", r"SALVATION ARMY",
    r"\bGOODWILL\b", r"CATHOLIC (CHARITIES|COMMUNITY)", r"\bTRIBE\b", r"\bTRIBAL\b",
    r"\bNATION\b", r"PARKS? (AND|&) REC", r"HOUSING AUTHORITY", r"FIRE DISTRICT",
    r"\bMEDICAL CENTER\b", r"REGIONAL HOSPITAL", r"\bECEAP\b", r"EARLY HEAD START",
    r"COMMUNITY COLLEGE", r"COMMUNITY (ACTION|SERVICES) (COUNCIL|AGENCY|PROGRAM)?",
    r"CHILDRENS HOME", r"CHILDREN'S HOME SOCIETY", r"\bWESLEY\b", r"\bDIOCESE\b",
    # major WA health systems (fleet audit F11) — legitimately huge, multi-program
    r"\bPROVIDENCE\b", r"\bMULTICARE\b", r"\bSWEDISH\b", r"\bPEACEHEALTH\b",
    r"\bKAISER\b", r"\bFRANCISCAN\b", r"\bCONFLUENCE HEALTH\b",
    # G9: military/federal facilities (JBLM Army CYS centers scored as a hidden
    # network) and religious institutions (Faith Lutheran Church at operator #18)
    r"\bARMY\b", r"\bNAVY\b", r"\bAIR FORCE\b", r"\bJOINT BASE\b", r"\bFORT \w+",
    r"\bMWR\b", r"\bCYSS?\b", r"\bNAF\b", r"\bMCCHORD\b", r"\bJBLM\b",
    r"\bCHURCH\b", r"\bPARISH\b", r"\bLUTHERAN\b", r"\bBAPTIST\b",
    r"\bMETHODIST\b", r"\bPRESBYTERIAN\b", r"\bTEMPLE\b", r"\bMOSQUE\b",
    r"\bSYNAGOGUE\b", r"\bMINISTRIES\b", r"\bADVENTIST\b",
]
_RE = re.compile("|".join(_PATTERNS))
# 'UNIVERSITY'/'COLLEGE' as part of a corporate care-facility name is usually a STREET
# (Gardens on University - Spokane Valley, LLC = a SNF on University Road) — for-profit
# care tokens veto the education match, but only the education match.
_EDU = re.compile(r"\bUNIVERSITY\b|\bCOLLEGE\b")
_CORP_CARE = re.compile(r"\bLLC\b|\bL\.L\.C\b|\bINC\b|REHABILITATION|POST ACUTE|"
                        r"HEALTH ?CARE|CARE CENTER|NURSING|ASSISTED LIVING")


def is_institutional(name, email=None):
    """Institutional by NAME pattern, or by a government CONTACT DOMAIN — the JBLM
    Army child-development centers carry plain names ('Hillside CDC') but .mil
    contacts; a .mil/.gov-operated facility is not a hidden fraud operator."""
    if email and str(email).strip().lower().rsplit(".", 1)[-1] in ("mil", "gov"):
        return True
    if not name:
        return False
    u = name.upper()
    if not _RE.search(u):
        return False
    if _EDU.search(u) and _CORP_CARE.search(u):
        # would it still match with the education words removed?
        return bool(_RE.search(_EDU.sub(" ", u)))
    return True
