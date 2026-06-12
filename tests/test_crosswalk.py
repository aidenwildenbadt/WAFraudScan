"""Tests for the NPI crosswalk name-matching + payment NPI resolution."""
import unittest

from fraudscan.crosswalk import _toks, _is_billing_type
from fraudscan.payments import _entity_npi
from fraudscan.sources.base import Entity


class TestCrosswalk(unittest.TestCase):
    def test_token_subset_matching(self):
        # query name tokens must all appear in the NPPES full name (handles middles)
        want = _toks("Hamid Roodneshin")
        self.assertTrue(want <= _toks("ROODNESHIN, HAMID"))
        self.assertTrue(want <= _toks("ROODNESHIN, HAMID R"))
        self.assertFalse(want <= _toks("ROODNESHIN, SARAH"))

    def test_billing_type_filter(self):
        self.assertTrue(_is_billing_type("Physician And Surgeon License"))
        self.assertTrue(_is_billing_type("Pharmacist License"))
        self.assertFalse(_is_billing_type("Nursing Assistant Certification"))
        self.assertFalse(_is_billing_type("Counselor Agency Affiliated Registration"))

    def test_payment_npi_resolution(self):
        npi_map = {"healthcare:c1": "1700000001"}
        dme = Entity(source="dme", source_id="1900000009", name="X")
        hc = Entity(source="healthcare", source_id="CRED1", name="Y")
        hc.source_id = "CRED1"
        # set uid via dataclass: uid is derived; healthcare:CRED1
        self.assertEqual(_entity_npi(dme, npi_map), "1900000009")   # source_id is NPI
        self.assertEqual(_entity_npi(
            Entity(source="healthcare", source_id="c1", name="Y"), npi_map),
            "1700000001")                                           # via crosswalk


if __name__ == "__main__":
    unittest.main()
