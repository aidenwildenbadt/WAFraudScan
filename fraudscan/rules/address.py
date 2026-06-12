"""Address / shell forensics — the physical address as a fraud signal.

A provider whose "physical" address is a commercial mail-receiving agency (UPS Store,
PMB), a virtual-office chain, or a PO box is a classic shell indicator. Pattern-based and
self-contained (no external call); each is a lead to verify, since small legitimate
businesses sometimes use a mailbox.
"""
import re

from fraudscan.rules.base import Flag

_MAILBOX = re.compile(
    r"\b(PMB|UPS STORE|MAIL ?BOXES ?ETC|POSTAL ANNEX|PAK ?MAIL|PACKAGING|"
    r"PARCEL|GOIN ?POSTAL|POSTNET|MAILBOX)\b")
_VOFFICE = re.compile(r"\b(REGUS|WEWORK|INTELLIGENT OFFICE|DAVINCI|OPUS VIRTUAL)\b")
_POBOX = re.compile(r"^\s*(P\.?\s*O\.?\s*BOX|POST OFFICE BOX|BOX\s+\d)")


def address_forensics(entities, cfg):
    sev_mb = cfg.get("mailbox_severity", 12)
    sev_vo = cfg.get("virtual_severity", 12)
    sev_po = cfg.get("pobox_severity", 8)
    out = []
    for e in entities:
        a = (e.address or "").upper().strip()
        if not a:
            continue
        if _MAILBOX.search(a):
            out.append(Flag(
                e.uid, "commercial_mailbox", sev_mb,
                "Address is a commercial mailbox",
                f"The physical address looks like a commercial mail-receiving agency "
                f"(mailbox store) — '{e.address}'. A shell-company indicator.",
                {"address": e.address}))
        elif _VOFFICE.search(a):
            out.append(Flag(
                e.uid, "virtual_office", sev_vo,
                "Address is a virtual office",
                f"The physical address is a known virtual-office provider — "
                f"'{e.address}'.",
                {"address": e.address}))
        elif _POBOX.match(a):
            out.append(Flag(
                e.uid, "po_box_address", sev_po,
                "PO box as physical address",
                f"A PO box is listed as the physical address ('{e.address}') — unusual "
                f"for a facility.",
                {"address": e.address}))
    return out
