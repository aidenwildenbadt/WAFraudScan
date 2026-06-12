"""Washington agency contracts (pwse-3zea). Demonstrates a second source + the
extension path for vendor/expenditure analysis. Swap dataset_id in config.json for
a newer biennium when desired.
"""
from fraudscan import http_util
from fraudscan.sources.base import Entity, SocrataSource


class ContractsSource(SocrataSource):
    key = "contracts"
    name = "WA Agency Contracts"
    dataset_id = "pwse-3zea"

    def to_entity(self, rec):
        sid = self._s(rec, "agency_contract_no")
        name = self._s(rec, "contractor_name_search_for")
        if not sid and not name:
            return None
        sid = sid or name
        return Entity(
            source=self.key,
            source_id=sid,
            name=name,
            dba=self._s(rec, "contractor_name_d_b_a_optional"),
            entity_type="STATE CONTRACT",
            status=self._s(rec, "procurement_type"),
            address=self._s(rec, "agency_number_agency_name"),
            amount=self._money(rec, "cost_of_contract"),
            source_url=http_util.dataset_url(self.domain, self.dataset_id),
            raw=rec,
        )
