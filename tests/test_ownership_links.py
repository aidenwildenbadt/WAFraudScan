"""Tests for item 6: CMS ownership + reassignment operator edges."""
import unittest

from fraudscan import reassignment
from fraudscan.resolve import build_operators
from fraudscan.sources.base import Entity


class TestReassignmentLoader(unittest.TestCase):
    def test_build_groups_by_npi(self):
        sample = [
            {"Individual NPI": "1", "Group PAC ID": "G100",
             "Group Legal Business Name": "Acme Group", "Individual State Code": "WA"},
            {"Individual NPI": "1", "Group PAC ID": "G200",
             "Group Legal Business Name": "Beta Group", "Individual State Code": "WA"},
            {"Individual NPI": "2", "Group PAC ID": "G100",
             "Group Legal Business Name": "Acme Group", "Individual State Code": "WA"},
        ]
        orig = reassignment.http_util.cms_dataapi_fetch
        reassignment.http_util.cms_dataapi_fetch = lambda *a, **k: sample
        try:
            d = reassignment._build("WA")
        finally:
            reassignment.http_util.cms_dataapi_fetch = orig
        self.assertEqual(set(d["npi_to_pacs"]["1"]), {"G100", "G200"})
        self.assertEqual(d["group_names"]["G100"], "Acme Group")


class TestOperatorEdges(unittest.TestCase):
    def test_shared_owner_links_facilities(self):
        ents = [
            Entity(source="hospice", source_id="A", name="Cedar Hospice LLC"),
            Entity(source="home_health", source_id="B", name="Pine Home Health Inc"),
        ]
        # both facilities map to the same beneficial owner associate id
        extra = {"hospice:A": ["owner:aid:999"], "home_health:B": ["owner:aid:999"]}
        ops, _ = build_operators(ents, {"hospice:A": 5, "home_health:B": 5},
                                 extra_keys=extra)
        merged = [o for o in ops if o["member_count"] == 2]
        self.assertTrue(merged, "shared owner should link the two facilities")
        self.assertTrue(any("beneficial owner" in s.lower()
                            for s in merged[0]["signals"]))

    def test_reassignment_does_not_merge(self):
        # reassignment was removed as a merge edge — it chains providers through hospital
        # billing groups into false clusters. Shared group must NOT link two providers.
        ents = [
            Entity(source="aba", source_id="111", name="Alpha ABA"),
            Entity(source="aba", source_id="222", name="Beta ABA"),
        ]
        extra = {"aba:111": ["reassign:G100"], "aba:222": ["reassign:G100"]}
        ops, _ = build_operators(ents, {}, extra_keys=extra)
        self.assertFalse(any(o["member_count"] == 2 for o in ops))

    def test_owner_under_one_name_does_not_falsely_split(self):
        # a single facility with an owner key shouldn't form a 2-member operator
        ents = [Entity(source="hospice", source_id="A", name="Solo Hospice")]
        ops, _ = build_operators(ents, {}, extra_keys={"hospice:A": ["owner:aid:1"]})
        self.assertFalse(any(o["member_count"] >= 2 for o in ops))


if __name__ == "__main__":
    unittest.main()
