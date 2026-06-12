"""Tests for exclusion/sanction screening."""
import unittest

import csv
import os
import tempfile

from fraudscan.screening import Screening, _add_sam_extract, decode_exclusion
from fraudscan.rules.cross import excluded_or_sanctioned
from fraudscan.sources.base import Entity

_SAM_HDR = ["Classification", "Name", "Prefix", "First", "Middle", "Last", "Suffix",
            "Address 1", "Address 2", "Address 3", "Address 4", "City",
            "State / Province", "Country", "Zip Code", "Open Data Flag",
            "Blank (Deprecated)", "Unique Entity ID", "Exclusion Program",
            "Excluding Agency", "CT Code", "Exclusion Type", "Additional Comments",
            "Active Date", "Termination Date", "Record Status", "Cross-Reference",
            "SAM Number", "CAGE", "NPI", "Creation_Date"]


class TestScreening(unittest.TestCase):
    def test_match_by_npi(self):
        s = Screening()
        s.add("OIG LEIE", "1234567890", "Bad Co", "WA", "1128b8", "20200101")
        self.assertEqual(s.match(npi="1234567890")["list"], "OIG LEIE")
        self.assertEqual(s.match(npi="1234567890")["matched_via"], "NPI")
        self.assertIsNone(s.match(npi="9999999999"))

    def test_placeholder_npi_not_indexed(self):
        s = Screening()
        s.add("OIG LEIE", "0000000000", "Ghost", "WA", "r", "d")
        self.assertIsNone(s.match(npi="0000000000"))

    def test_match_by_name_state_requires_state(self):
        s = Screening()
        s.add("OIG LEIE", "", "Sketchy Care LLC", "WA", "1128a1", "20210101")
        self.assertEqual(s.match(name="Sketchy Care", state="WA")["matched_via"],
                         "name+state")
        self.assertIsNone(s.match(name="Sketchy Care", state="OR"))
        self.assertIsNone(s.match(name="Sketchy Care"))   # no state -> no name match

    def test_rule_flags_only_matches(self):
        s = Screening()
        s.add("OIG LEIE", "1900000000", "X", "WA", "1128b8", "20200101")
        ents = [Entity(source="dme", source_id="1900000000", name="X", state="WA"),
                Entity(source="dme", source_id="1111111111", name="Clean", state="WA")]
        flags = excluded_or_sanctioned(ents, {"severity": 40}, {"screening": s})
        self.assertEqual({f.entity_uid for f in flags}, {"dme:1900000000"})
        self.assertEqual(flags[0].severity, 40)

    def test_no_screening_is_noop(self):
        ents = [Entity(source="dme", source_id="1", name="X", state="WA")]
        self.assertEqual(excluded_or_sanctioned(ents, {}, {"screening": None}), [])

    def test_sam_extract_parsing(self):
        rows = [
            {"Classification": "Individual", "First": "Jane", "Last": "Doe",
             "State / Province": "WA", "Exclusion Type": "Ineligible",
             "Excluding Agency": "HHS", "Active Date": "2024-01-01",
             "Record Status": "Active", "NPI": "1234567890"},
            {"Classification": "Firm", "Name": "Bad Vendor LLC",
             "State / Province": "WA", "Record Status": "Active",
             "Exclusion Type": "Debarred", "Active Date": "2023-01-01"},
            {"Classification": "Individual", "First": "Inactive", "Last": "Guy",
             "State / Province": "WA", "Record Status": "Inactive"},
            {"Classification": "Individual", "First": "Outof", "Last": "State",
             "State / Province": "CA", "Record Status": "Active"},  # no NPI, not WA
        ]
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "sam.csv")
            with open(p, "w", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=_SAM_HDR)
                w.writeheader()
                for r in rows:
                    w.writerow({k: r.get(k, "") for k in _SAM_HDR})
            s = Screening()
            _add_sam_extract(s, p, "WA")
        self.assertEqual(s.match(npi="1234567890")["list"], "SAM.gov")
        self.assertIsNotNone(s.match(name="Bad Vendor", state="WA"))   # LLC stripped
        self.assertIsNone(s.match(name="Inactive Guy", state="WA"))    # inactive
        self.assertIsNone(s.match(name="Outof State", state="CA"))     # out of scope


    # ---- item 1: exclusion-type decoding + confidence grading ----

    def test_decode_exclusion(self):
        desc, mand = decode_exclusion("1128a1")
        self.assertIn("program-related", desc)
        self.assertTrue(mand)                       # 1128(a) = mandatory
        desc, mand = decode_exclusion("1128b4")
        self.assertIn("License", desc)
        self.assertFalse(mand)                      # 1128(b) = permissive
        self.assertIsNone(decode_exclusion("ZZZ")[1])  # unknown

    def test_name_match_confidence_grading(self):
        s = Screening()
        s.add("OIG LEIE", "", "John Smith", "WA", "License revocation", "2020-01-01",
              extra={"city": "Spokane", "provider_type": "PHYSICIAN (MD/DO)",
                     "mandatory": False})
        name_only = s.match(name="John Smith", state="WA")
        self.assertEqual(name_only["confidence"], "name-only")
        corrob = s.match(name="John Smith", state="WA", city="SPOKANE")
        self.assertEqual(corrob["confidence"], "corroborated")
        mism = s.match(name="John Smith", state="WA", city="Seattle")
        self.assertEqual(mism["confidence"], "name-only")
        self.assertIn("possible namesake", mism["corroboration"])

    def test_birth_year_corroborates_and_refutes(self):
        s = Screening()
        s.add("OIG LEIE", "", "Chris Lee", "WA", "conviction", "2019-01-01",
              extra={"dob": "1980-05-02", "mandatory": True})
        # same birth year -> corroborated
        m = s.match(name="Chris Lee", state="WA", birth_year="1980")
        self.assertEqual(m["confidence"], "corroborated")
        self.assertIn("birth year matches", m["corroboration"])
        # different birth year -> dropped entirely (namesake)
        self.assertIsNone(s.match(name="Chris Lee", state="WA", birth_year="1975"))
        # no birth year on our side -> still a (name-only) match, not dropped
        self.assertIsNotNone(s.match(name="Chris Lee", state="WA"))

    def test_mandatory_exclusion_severity_tier(self):
        s = Screening()
        s.add("OIG LEIE", "1900000000", "X", "WA", "conviction", "2020-01-01",
              extra={"mandatory": True})
        ents = [Entity(source="dme", source_id="1900000000", name="X", state="WA")]
        flags = excluded_or_sanctioned(ents, {}, {"screening": s})
        self.assertEqual(flags[0].severity, 48)     # NPI + mandatory = top tier
        self.assertEqual(flags[0].evidence["confidence"], "definitive")
        self.assertTrue(flags[0].evidence["mandatory"])


class TestDossier(unittest.TestCase):
    def test_paid_after_barred_dossier(self):
        from fraudscan.web.server import _build_dossier
        ent = {
            "source": "healthcare", "source_id": "X1", "name": "Jane Doe",
            "city": "Spokane", "source_url": "http://doh",
            "crosswalk": {"npi": "1902000000", "npi_name": "JANE DOE",
                          "npi_city": "Spokane", "npi_taxonomy": "Physician"},
            "payments": [{"program": "Medicare Part B", "period": "2023",
                          "amount": 50000.0, "source_url": "http://cms"}],
            "flags": [{"rule_id": "paid_after_barred", "severity": 50,
                       "title": "t", "explanation": "e",
                       "evidence": {"status": "Suspended", "barred_year": 2019,
                                    "program": "Medicare Part B", "amount_after": 50000,
                                    "payment_years": "2023",
                                    "identity_confidence": "dob-confirmed",
                                    "identity_note": "NPI on OIG LEIE, DOB matches"}}],
        }
        d = _build_dossier(ent)
        self.assertEqual(d["strength"], "verifiable contradiction")
        labels = [s["label"] for s in d["steps"]]
        self.assertEqual(labels, ["Identity", "The bar", "The money",
                                  "The contradiction"])
        # dob-confirmed → identity high
        self.assertEqual(d["steps"][0]["strength"], "high")
        self.assertTrue(d["confirm"])

    def test_no_dossier_without_signal(self):
        from fraudscan.web.server import _build_dossier
        self.assertIsNone(_build_dossier(
            {"source": "childcare", "flags": [], "payments": []}))


if __name__ == "__main__":
    unittest.main()
