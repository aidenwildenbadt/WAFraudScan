"""Config-driven source for the CMS Provider Data Catalog (data.cms.gov).

Washington does NOT publish facility-level data for these categories (hospice, home
health, dialysis, nursing, etc.) on its open-data portal, and the NPPES NPI registry
API is not reachable from every environment. CMS's Provider Data Catalog *is* an open,
queryable API and is filtered here to Washington providers — the same facilities that
bill Apple Health / Medicare.

One class serves every category: the dataset id and a column map come from config, so
adding a category (nursing homes, inpatient rehab, FQHCs, …) is a config block, not new
code. These are federal Medicare-certified facilities; that provenance is shown in the UI.
"""
from fraudscan import http_util
from fraudscan.sources.base import Entity


class CmsProviderSource:
    def __init__(self, key, name, distribution_id, type_label, field_map,
                 state="WA"):
        self.key = key
        self.name = name
        self.distribution_id = distribution_id
        self.type_label = type_label
        self.map = field_map
        self.state = state
        # mimic SocrataSource attributes used by the CLI for display
        self.domain = "data.cms.gov"
        self.dataset_id = distribution_id

    def fetch(self, limit=None, progress=None):
        return http_util.cms_provider_fetch(
            self.distribution_id, state=self.state, limit=limit, progress=progress)

    def to_entity(self, rec):
        m = self.map
        sid = (rec.get(m["id"]) or "").strip() if rec.get(m["id"]) else ""
        name = (rec.get(m["name"]) or "").strip()
        if not sid and not name:
            return None
        sid = sid or name
        ownership = rec.get(m.get("ownership", "")) if m.get("ownership") else ""
        # normalized aliases so generic rules don't need per-dataset column names
        rec["_ownership"] = ownership
        rec["_cert_date"] = rec.get(m.get("cert_date", "")) if m.get("cert_date") else None
        return Entity(
            source=self.key,
            source_id=sid,
            name=name or sid,
            dba="",
            entity_type=self.type_label,
            status=ownership or "",
            address=(rec.get(m["address"]) or "").strip() if m.get("address") else "",
            city=(rec.get(m["city"]) or "").strip() if m.get("city") else "",
            state=(rec.get(m.get("state", "")) or "").strip() if m.get("state") else self.state,
            zip=(rec.get(m["zip"]) or "").strip() if m.get("zip") else "",
            county=(rec.get(m.get("county", "")) or "").strip() if m.get("county") else "",
            amount=None,
            source_url=http_util.cms_record_url(self.distribution_id, m["id"], sid),
            raw=rec,
        )

    def entities(self, limit=None, progress=None):
        out, seen = [], set()
        for rec in self.fetch(limit=limit, progress=progress):
            ent = self.to_entity(rec)
            if ent is None or ent.source_id in seen:
                continue  # some CMS datasets have many rows per facility (one/measure)
            seen.add(ent.source_id)
            out.append(ent)
        return out
