"""Tests for the business-registry cross-check.  python -m unittest discover -s tests"""
import os
import tempfile
import unittest

from fraudscan.registry import BusinessRegistry, normalize, load_registry
from fraudscan.rules.cross import no_active_business_registration
from fraudscan.sources.base import Entity


class TestNormalize(unittest.TestCase):
    def test_suffix_and_punctuation_stripping(self):
        self.assertEqual(normalize("Keller Child Care, LLC"), "KELLER CHILD CARE")
        self.assertEqual(normalize("The Bright Start Inc."), "BRIGHT START")
        self.assertEqual(normalize("Puget Learning Centers, P.L.L.C."),
                         "PUGET LEARNING CENTERS")


class TestRegistryLookup(unittest.TestCase):
    def setUp(self):
        self.reg = BusinessRegistry(active_values=["ACTIVE", "OPEN"])
        self.reg.add("Little Sprouts Learning LLC", "Active")
        self.reg.add("Sunnyside Daycare Co", "Closed")

    def test_active_match(self):
        self.assertTrue(self.reg.lookup("little sprouts learning")["active"])

    def test_inactive_match(self):
        m = self.reg.lookup("Sunnyside Daycare")
        self.assertTrue(m["found"])
        self.assertFalse(m["active"])

    def test_missing(self):
        self.assertFalse(self.reg.lookup("Totally Unknown Place")["found"])

    def test_best_match_uses_dba(self):
        m = self.reg.best_match("UNKNOWN LEGAL NAME", "Little Sprouts Learning")
        self.assertTrue(m["active"])


class TestCrossRule(unittest.TestCase):
    def _ent(self, uid, name, dba=""):
        return Entity(source="childcare", source_id=uid, name=name, dba=dba)

    def test_flags_missing_and_inactive_only(self):
        reg = BusinessRegistry(active_values=["ACTIVE"])
        reg.add("Good Care LLC", "Active")
        reg.add("Closed Care LLC", "Closed")
        ents = [self._ent("a", "Good Care"),
                self._ent("b", "Closed Care"),
                self._ent("c", "Ghost Care")]
        flags = no_active_business_registration(
            ents, {"severity_not_found": 12, "severity_inactive": 20},
            {"registry": reg})
        by = {f.entity_uid: f for f in flags}
        self.assertNotIn("childcare:a", by)          # active -> no flag
        self.assertEqual(by["childcare:b"].severity, 20)  # inactive
        self.assertEqual(by["childcare:c"].severity, 12)  # missing

    def test_no_registry_is_noop(self):
        ents = [self._ent("a", "Whatever")]
        self.assertEqual(
            no_active_business_registration(ents, {}, {"registry": None}), [])


class TestLoadRegistryFromCsv(unittest.TestCase):
    def test_load_and_detect_columns(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "reg.csv")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("BusinessName,TradeName,Status\n")
                fh.write("Alpha Care LLC,Alpha,Active\n")
                fh.write("Beta Care Inc,,Closed\n")
            cfg = {"registry": {"csv_glob": os.path.join(d, "*.csv"),
                                "active_values": ["ACTIVE"]}}
            reg = load_registry(cfg)
            self.assertEqual(reg.row_count, 2)
            self.assertTrue(reg.lookup("Alpha Care")["active"])
            self.assertTrue(reg.lookup("Alpha")["active"])      # trade name indexed
            self.assertFalse(reg.lookup("Beta Care")["active"])


if __name__ == "__main__":
    unittest.main()
