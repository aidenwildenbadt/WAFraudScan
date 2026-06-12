"""Config-driven NPPES (NPI registry) source for Medicaid-only provider categories.

Categories like ABA/autism therapy and non-emergency medical transport (NEMT) aren't in
Medicare/CMS facility data, but every billing provider has an NPI with a taxonomy code.
We read NPPES through the NLM Clinical Table Search Service (npiregistry.cms.gov itself
isn't reachable in every environment), filtered to a state and exact taxonomy.

Adding a category is a config block: kind "nppes", entity_kind (org/individual), and the
list of taxonomy descriptions to pull.
"""
from fraudscan import http_util
from fraudscan.sources.base import Entity


class NppesSource:
    def __init__(self, key, name, type_label, taxonomies, entity_kind="org",
                 state="WA", cap=2000):
        self.key = key
        self.name = name
        self.type_label = type_label
        self.taxonomies = taxonomies
        self.entity_kind = entity_kind
        self.state = state
        self.cap = cap
        self.domain = "npiregistry (via NLM)"
        self.dataset_id = "+".join(taxonomies)[:40]

    def to_entity(self, rec):
        npi = (rec.get("npi") or "").strip()
        name = (rec.get("name") or "").strip()
        if not npi:
            return None
        return Entity(
            source=self.key, source_id=npi, name=name or npi, dba="",
            entity_type=self.type_label, status="",
            address=(rec.get("line1") or "").strip(),
            city=(rec.get("city") or "").strip(),
            state=(rec.get("state") or self.state).strip(),
            zip=(rec.get("zip") or "").strip(),
            amount=None,
            source_url=http_util.nppes_provider_url(npi),
            raw=rec,
        )

    def entities(self, limit=None, progress=None):
        seen, out = set(), []
        for tax in self.taxonomies:
            for rec in http_util.nppes_fetch(self.entity_kind, tax, state=self.state,
                                             cap=self.cap, progress=progress):
                npi = rec.get("npi")
                if not npi or npi in seen:
                    continue
                seen.add(npi)
                ent = self.to_entity(rec)
                if ent is not None:
                    out.append(ent)
                if limit and len(out) >= limit:
                    return out
        return out
