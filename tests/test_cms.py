"""Tests for the config-driven CMS provider source + facility rules."""
import unittest

from fraudscan.sources.cms import CmsProviderSource
from fraudscan.rules import facility as fac
from fraudscan.rules import rules_for_source
from fraudscan.resolve import build_operators
from fraudscan.sources.base import Entity

HOSPICE_MAP = {
    "id": "cms_certification_number_ccn", "name": "facility_name",
    "address": "address_line_1", "city": "citytown", "state": "state",
    "zip": "zip_code", "ownership": "ownership_type",
    "cert_date": "certification_date",
}


class TestCmsSource(unittest.TestCase):
    def test_to_entity_maps_fields_and_aliases(self):
        src = CmsProviderSource("hospice", "Hospice", "dist-1", "HOSPICE", HOSPICE_MAP)
        e = src.to_entity({
            "cms_certification_number_ccn": "111", "facility_name": "Sunrise Hospice",
            "address_line_1": "1 Main St", "citytown": "Olympia", "state": "WA",
            "zip_code": "98501", "ownership_type": "For-Profit",
            "certification_date": "05/01/2026"})
        self.assertEqual((e.source, e.source_id, e.name), ("hospice", "111",
                                                           "Sunrise Hospice"))
        self.assertEqual(e.entity_type, "HOSPICE")
        self.assertEqual(e.status, "For-Profit")
        self.assertEqual((e.city, e.zip), ("Olympia", "98501"))
        self.assertEqual(e.raw["_ownership"], "For-Profit")
        self.assertEqual(e.raw["_cert_date"], "05/01/2026")
        self.assertIn("111", e.source_url)


class TestFacilityRules(unittest.TestCase):
    def _e(self, uid, cert=None, owner=None):
        return Entity(source="hospice", source_id=uid, name=uid, entity_type="HOSPICE",
                      raw={"_cert_date": cert, "_ownership": owner})

    def test_recently_certified(self):
        ents = [self._e("a", cert="05/01/2026"), self._e("b", cert="01/01/2010")]
        flags = fac.recently_certified(ents, {"severity": 8, "days": 365})
        self.assertEqual({f.entity_uid for f in flags}, {"hospice:a"})

    def test_for_profit_ownership(self):
        ents = [self._e("a", owner="For-Profit"), self._e("b", owner="Non-Profit"),
                self._e("c", owner="PROPRIETARY")]
        flags = fac.for_profit_ownership(ents, {"severity": 5})
        self.assertEqual({f.entity_uid for f in flags}, {"hospice:a", "hospice:c"})

    def test_profile_selection(self):
        cfg = {"sources": {"hospice": {"rules_profile": "facility"}}}
        ids = {rid for rid, _ in rules_for_source("hospice", cfg)}
        self.assertIn("recently_certified", ids)
        self.assertIn("address_shared_multiple_providers", ids)


class TestCmsResolution(unittest.TestCase):
    def _h(self, uid, name, address):
        return Entity(source="hospice", source_id=uid, name=name, address=address,
                      city="OLYMPIA", zip="98501")

    def test_address_match_when_source_is_address_bearing(self):
        ents = [self._h("a", "Sunrise Hospice", "1 Main St"),
                self._h("b", "Moonset Hospice", "1 MAIN STREET")]   # abbrev variant
        ops, _ = build_operators(ents, {}, address_sources={"hospice"})
        self.assertEqual(len(ops), 1)
        self.assertTrue(any("one address" in s for s in ops[0]["signals"]))

    def test_no_address_match_when_source_excluded(self):
        ents = [self._h("a", "Sunrise Hospice", "1 Main St"),
                self._h("b", "Moonset Hospice", "1 MAIN STREET")]
        ops, _ = build_operators(ents, {}, address_sources={"childcare"})
        self.assertEqual(ops, [])


if __name__ == "__main__":
    unittest.main()
