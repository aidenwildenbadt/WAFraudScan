"""DCYF Licensed Childcare Center and School Age Program Providers (was8-3ni8).

This is the anchor dataset: it carries the SSPSProviderNumber (the Social Service
Payment System id used to pay subsidies), license status/dates, capacity, geocoded
address and contact info — the fields that make subsidy-misuse leads tractable.
"""
from fraudscan import http_util
from fraudscan.sources.base import Entity, SocrataSource


class ChildCareSource(SocrataSource):
    key = "childcare"
    name = "DCYF Licensed Child Care Providers"
    dataset_id = "was8-3ni8"

    def to_entity(self, rec):
        sid = self._s(rec, "wacompassid") or self._s(rec, "sspsprovidernumber")
        if not sid:
            return None
        return Entity(
            source=self.key,
            source_id=sid,
            name=self._s(rec, "providername"),
            dba=self._s(rec, "doingbusinessas"),
            entity_type=self._s(rec, "facilitytypegeneric"),
            status=self._s(rec, "latestoperatingstatus"),
            address=self._s(rec, "physicalstreetaddress"),
            city=self._s(rec, "physicalcity"),
            state=self._s(rec, "physicalstate"),
            zip=self._s(rec, "physicalzip"),
            county=self._s(rec, "physicalcounty"),
            lat=self._f(rec, "physciallatitude"),   # sic: dataset spells it this way
            lon=self._f(rec, "physicallongitude"),
            amount=None,
            source_url=http_util.record_query_url(
                self.domain, self.dataset_id, "wacompassid", sid
            ),
            raw=rec,
        )
