"""Tests for nursing-home ownership rules."""
import unittest

from fraudscan.rules.cross import ownership_churn, shared_owner
from fraudscan.sources.base import Entity


class TestOwnership(unittest.TestCase):
    def _snf(self, ccn, name):
        return Entity(source="nursing", source_id=ccn, name=name, state="WA")

    def test_churn_flags_facility_in_cofo(self):
        # F16/G14: only repeated FACILITY-SPECIFIC dates flag as flips; a single sale
        # is silent; a chain-wide same-date transfer gets a low-severity context flag.
        ctx = {"ownership": {
            "churn": {"105001": "2023-05-01", "105003": "2024-01-01",
                      "105004": "2025-05-21"},
            "churn_n": {"105001": 3, "105003": 1, "105004": 1},
            "churn_dates": {"105001": ["2021-02-01", "2022-08-01", "2023-05-01"],
                            "105003": ["2024-01-01"],
                            "105004": ["2025-05-21"]},
            "chain_dates": ["2025-05-21"],
            "owner_to_orgs": {}, "org_to_owners": {}}}
        ents = [self._snf("105001", "Maple SNF"), self._snf("105002", "Oak SNF"),
                self._snf("105003", "Single Sale SNF"),
                self._snf("105004", "Chain Member SNF")]
        flags = {f.entity_uid: f for f in ownership_churn(ents, {"severity": 14}, ctx)}
        self.assertIn("nursing:105001", flags)              # repeated flips
        self.assertEqual(flags["nursing:105001"].severity, 14)
        self.assertNotIn("nursing:105003", flags)           # single sale: silent
        self.assertIn("nursing:105004", flags)              # chain transfer: context
        self.assertEqual(flags["nursing:105004"].severity, 6)
        self.assertIn("Chain-wide", flags["nursing:105004"].title)

    def test_shared_owner_flags_multi_facility_owner(self):
        from fraudscan.registry import normalize
        ctx = {"ownership": {
            "churn": {},
            "owner_to_orgs": {"acme": {"name": "ACME HOLDINGS",
                                       "orgs": [normalize("Maple SNF"),
                                                normalize("Oak SNF")]}},
            "org_to_owners": {normalize("Maple SNF"): ["acme"],
                              normalize("Oak SNF"): ["acme"],
                              normalize("Solo SNF"): []}}}
        ents = [self._snf("1", "Maple SNF"), self._snf("2", "Solo SNF")]
        flags = shared_owner(ents, {"severity": 16, "min_facilities": 2}, ctx)
        self.assertEqual({f.entity_uid for f in flags}, {"nursing:1"})
        self.assertEqual(flags[0].evidence["facility_count"], 2)

    def test_no_ownership_context_noop(self):
        ents = [self._snf("1", "X")]
        self.assertEqual(ownership_churn(ents, {}, {}), [])
        self.assertEqual(shared_owner(ents, {}, {}), [])


if __name__ == "__main__":
    unittest.main()
