"""Tests for crosswalk identity grading (license #, DOB, city)."""
import unittest

from fraudscan.rules.cross import _xwalk_identity, _lic_core
from fraudscan.sources.base import Entity


def _ent(uid_id="1", birthyear="1956", cred="MD.MD.00028366", city=""):
    return Entity(source="healthcare", source_id=uid_id, name="Ignatius Medani",
                  state="WA", city=city,
                  raw={"birthyear": birthyear, "credentialnumber": cred})


class TestIdentity(unittest.TestCase):
    def test_lic_core(self):
        self.assertEqual(_lic_core("MD.MD.00028366"), "28366")
        self.assertEqual(_lic_core("00028366"), "28366")
        self.assertEqual(_lic_core(""), "")

    def test_license_confirmed(self):
        e = _ent()
        detail = {e.uid: {"npi": "1295831659", "name": "MEDANI, IGNATIUS",
                          "city": "FEDERAL WAY", "license": "00028366"}}
        r = _xwalk_identity(e, detail, m=None)
        self.assertEqual(r["confidence"], "license-confirmed")

    def test_license_mismatch(self):
        e = _ent(cred="MD.MD.00099999")
        detail = {e.uid: {"npi": "1295831659", "license": "00028366", "city": "X"}}
        r = _xwalk_identity(e, detail, m=None)
        self.assertEqual(r["confidence"], "license-mismatch")

    def test_dob_confirmed_via_exclusion(self):
        e = _ent(cred="")  # no license to compare -> falls to DOB path
        detail = {e.uid: {"npi": "1295831659", "city": "FEDERAL WAY", "license": ""}}
        m = {"matched_via": "NPI", "dob": "1956-02-01", "list": "OIG LEIE",
             "city": "FEDERAL WAY"}
        r = _xwalk_identity(e, detail, m=m)
        self.assertEqual(r["confidence"], "dob-confirmed")

    def test_dob_mismatch_via_exclusion(self):
        e = _ent(cred="", birthyear="1970")
        detail = {e.uid: {"npi": "1295831659", "license": "", "city": ""}}
        m = {"matched_via": "NPI", "dob": "1956-02-01", "list": "OIG LEIE"}
        r = _xwalk_identity(e, detail, m=m)
        self.assertEqual(r["confidence"], "dob-mismatch")

    def test_unique_name_fallback(self):
        e = _ent(cred="")
        detail = {e.uid: {"npi": "1295831659", "license": "", "city": ""}}
        r = _xwalk_identity(e, detail, m=None)
        self.assertEqual(r["confidence"], "unique-name")

    def test_not_crosswalked(self):
        self.assertIsNone(_xwalk_identity(_ent(), {}, m=None))

    def test_taxonomy_mismatch_namesake(self):
        # Robert Parks case: Dentist credential, but the unique-name NPI is an
        # Emergency-Medicine physician -> different professions, likely two people.
        e = _ent(cred="")
        e.raw["credentialtype"] = "Dentist License"
        detail = {e.uid: {"npi": "1770581332", "license": "", "city": "WALLA WALLA",
                          "taxonomy": "Physician/Emergency Medicine"}}
        r = _xwalk_identity(e, detail, m=None)
        self.assertEqual(r["confidence"], "taxonomy-mismatch")
        self.assertIn("different professions", r["note"])

    def test_taxonomy_compatible_resident(self):
        # a resident license matched to a student/trainee taxonomy is the SAME person
        e = _ent(cred="")
        e.raw["credentialtype"] = "Physician And Surgeon Resident License"
        detail = {e.uid: {"npi": "1", "license": "", "city": "",
                          "taxonomy": "Student in an Organized Health Care Program"}}
        r = _xwalk_identity(e, detail, m=None)
        self.assertEqual(r["confidence"], "unique-name")

    def test_license_confirmed_beats_taxonomy(self):
        # hard ID (license #) proves a dual-credentialed person — no downgrade
        e = _ent()
        e.raw["credentialtype"] = "Dentist License"
        detail = {e.uid: {"npi": "1295831659", "license": "00028366",
                          "taxonomy": "Physician/General Surgery", "city": ""}}
        r = _xwalk_identity(e, detail, m=None)
        self.assertEqual(r["confidence"], "license-confirmed")

    def test_taxonomy_mismatch_downweights_paid_flag(self):
        from fraudscan.rules.cross import paid_while_sanctioned
        e = Entity(source="healthcare", source_id="1", name="Robert Parks", state="WA",
                   status="Surrender",
                   raw={"credentialnumber": "DENT.DE.00004389",
                        "credentialtype": "Dentist License",
                        "expirationdate": "03/02/2013"})
        ctx = {"payments": {"by_entity": {e.uid: {
                   "total": 254065.0, "program": "Medicare Part B",
                   "periods": {"2018": 100000.0, "2019": 154065.0}}}},
               "crosswalk_detail": {e.uid: {"npi": "1770581332", "license": "",
                                            "city": "WALLA WALLA",
                                            "taxonomy": "Physician/Emergency Medicine"}},
               "crosswalk": {e.uid: "1770581332"}, "screening": None}
        flags = paid_while_sanctioned([e], {"severity": 35, "severity_after": 50}, ctx)
        rules = {f.rule_id: f for f in flags}
        self.assertIn("paid_attribution_unconfirmed", rules)
        self.assertNotIn("paid_after_barred", rules)

    def test_license_mismatch_downweights_paid_flag(self):
        from fraudscan.rules.cross import paid_while_sanctioned
        e = Entity(source="healthcare", source_id="1", name="Pat Lee", state="WA",
                   status="Suspended",
                   raw={"credentialnumber": "MD.MD.00011111", "expirationdate": ""})
        ctx = {"payments": {"by_entity": {e.uid: {
                   "total": 500000.0, "program": "Medicare Part B",
                   "periods": {"2023": 500000.0}}}},
               # resolved NPI's license (99999999) != the credential (00011111)
               "crosswalk_detail": {e.uid: {"npi": "9", "license": "99999999"}},
               "crosswalk": {e.uid: "9"}, "screening": None}
        flags = paid_while_sanctioned([e], {"severity": 35, "severity_after": 50}, ctx)
        rules = {f.rule_id: f for f in flags}
        self.assertIn("paid_attribution_unconfirmed", rules)
        self.assertNotIn("paid_while_sanctioned", rules)
        self.assertNotIn("paid_after_barred", rules)
        self.assertEqual(rules["paid_attribution_unconfirmed"].severity, 8)


if __name__ == "__main__":
    unittest.main()
