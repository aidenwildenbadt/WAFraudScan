import unittest

from fraudscan.sources.base import Entity
from fraudscan.state_checkbook import checkbook_payments
from fraudscan.medicaid_spending import medicaid_payments


class TestStateCheckbook(unittest.TestCase):
    def _agg(self):
        from fraudscan.registry import normalize
        return {normalize("Acme Home Care"): {"total": 300.0, "by_fy": {"2024": 100.0,
                "2025": 200.0}, "agencies": ["DSHS"], "raw_name": "ACME HOME CARE"},
                normalize("Shared Name"): {"total": 999.0, "by_fy": {"2024": 999.0},
                "agencies": ["DCYF"], "raw_name": "SHARED NAME"}}

    def test_unique_name_attributed(self):
        e = Entity(source="nemt", source_id="1", name="Acme Home Care", source_url="u")
        rows = checkbook_payments([e], self._agg())
        self.assertEqual(len(rows), 2)                      # one per FY
        self.assertEqual(sum(r["amount"] for r in rows), 300.0)
        self.assertTrue(rows[0]["program"].startswith("WA state (DSHS"))

    def test_ambiguous_name_skipped(self):
        # two entities share the normalized name -> vendor $ must NOT be attributed
        ents = [Entity(source="childcare", source_id="a", name="Shared Name"),
                Entity(source="childcare", source_id="b", name="SHARED NAME")]
        rows = checkbook_payments(ents, self._agg())
        self.assertEqual(rows, [])

    def test_contracts_entities_excluded(self):
        # checkbook $ never lands on contract entities (same money as the contract
        # amounts we already hold) — and a contracts namesake must not block a provider
        ents = [Entity(source="contracts_2025", source_id="K1", name="Acme Home Care"),
                Entity(source="nemt", source_id="1", name="Acme Home Care",
                       source_url="u")]
        rows = checkbook_payments(ents, self._agg())
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r["entity_uid"].startswith("nemt:") for r in rows))
        rows2 = checkbook_payments(
            [Entity(source="contracts", source_id="K2", name="Acme Home Care")],
            self._agg())
        self.assertEqual(rows2, [])


class TestMedicaidSpending(unittest.TestCase):
    def test_one_attribution_per_npi(self):
        agg = {"1144655176": {"total": 500.0, "by_year": {"2020": 200.0, "2021": 300.0}}}
        # two entities crosswalk to the same NPI -> count the money once, not twice
        ents = [Entity(source="aba", source_id="1144655176", name="X", source_url="u"),
                Entity(source="healthcare", source_id="c1", name="Y", source_url="u2")]
        npi_map = {ents[1].uid: "1144655176"}
        rows = medicaid_payments(ents, agg, npi_map=npi_map)
        self.assertEqual(sum(r["amount"] for r in rows), 500.0)
        self.assertEqual(rows[0]["program"], "Medicaid (HCPCS, T-MSIS)")


if __name__ == "__main__":
    unittest.main()
