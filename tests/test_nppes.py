"""Tests for the NPPES (NPI registry via NLM) source mapping."""
import unittest

from fraudscan.sources.nppes import NppesSource


class TestNppesSource(unittest.TestCase):
    def test_to_entity_maps_record(self):
        src = NppesSource("nemt", "NEMT", "NON-EMERGENCY MEDICAL TRANSPORT",
                          ["Non-emergency Medical Transport (VAN)"], entity_kind="org")
        e = src.to_entity({
            "npi": "1750599361", "name": "NORTHWEST TRANSPORT",
            "provider_type": "Non-emergency Medical Transport (VAN)",
            "line1": "22627 85TH PL S", "city": "KENT", "state": "WA",
            "zip": "98031"})
        self.assertEqual((e.source, e.source_id, e.name),
                         ("nemt", "1750599361", "NORTHWEST TRANSPORT"))
        self.assertEqual(e.entity_type, "NON-EMERGENCY MEDICAL TRANSPORT")
        self.assertEqual((e.address, e.city, e.zip),
                         ("22627 85TH PL S", "KENT", "98031"))
        self.assertIn("1750599361", e.source_url)

    def test_blank_npi_dropped(self):
        src = NppesSource("aba", "ABA", "ABA", ["Behavior Analyst"])
        self.assertIsNone(src.to_entity({"npi": "", "name": "X"}))


if __name__ == "__main__":
    unittest.main()
