"""DOH Health Care Provider Credential Data (qxh8-f4bd).

This is provider *credentialing* data — every WA health-care credential with its
status, expiration, and whether a disciplinary action was taken. There is no open
dataset of Apple Health/Medicaid *enrolled* providers (that lives in HCA's
ProviderOne / dashboards, not bulk-public), so this is the closest public analog.

The fraud-relevant use is **provider sanction screening**: surfacing providers who
are revoked, suspended, surrendered, or disciplined yet may still be billing public
programs — exactly what Medicaid program-integrity units screen for.

The full table is ~2.4M rows, so config bounds ingest with a SoQL $where to the
integrity-relevant slice (disciplinary action or adverse status).
"""
from fraudscan import http_util
from fraudscan.sources.base import Entity, SocrataSource


class HealthcareCredentialSource(SocrataSource):
    key = "healthcare"
    name = "DOH Health Care Provider Credentials"
    dataset_id = "qxh8-f4bd"

    def to_entity(self, rec):
        sid = self._s(rec, "credentialnumber")
        if not sid:
            return None
        name = " ".join(p for p in (
            self._s(rec, "firstname"), self._s(rec, "lastname")) if p)
        return Entity(
            source=self.key,
            source_id=sid,
            name=name or sid,
            dba="",
            entity_type=self._s(rec, "credentialtype"),
            status=self._s(rec, "status"),
            amount=None,
            source_url=http_util.record_query_url(
                self.domain, self.dataset_id, "credentialnumber", sid
            ),
            raw=rec,
        )
