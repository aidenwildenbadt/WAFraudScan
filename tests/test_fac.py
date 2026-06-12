"""Tests for FAC single-audit integration (mocked API)."""
import unittest

from fraudscan import fac


class TestFac(unittest.TestCase):
    def test_norm_ein(self):
        self.assertEqual(fac.norm_ein("91-1624764"), "911624764")
        self.assertEqual(fac.norm_ein(" 911624764 "), "911624764")
        self.assertEqual(fac.norm_ein(None), "")

    def test_lookup_ein_aggregates(self):
        calls = {}

        def fake_get(table, params, retries=3):
            calls[table] = params
            if table == "general":
                return [{"report_id": "2023-X-1", "audit_year": "2023",
                         "auditee_name": "Acme Nonprofit", "auditee_state": "WA",
                         "total_amount_expended": 2000000}]
            if table == "findings":
                return [{"is_questioned_costs": "Y", "is_material_weakness": "Y",
                         "is_modified_opinion": "N", "is_repeat_finding": "Y"},
                        {"is_questioned_costs": "N", "is_material_weakness": "N",
                         "is_modified_opinion": "N", "is_repeat_finding": "N"}]
            if table == "federal_awards":
                return [{"amount_expended": 500000, "findings_count": 2,
                         "federal_program_name": "Head Start"},
                        {"amount_expended": 250000, "findings_count": 1,
                         "federal_program_name": "CCDF"}]
            return []

        orig = fac._get
        fac._get = fake_get
        try:
            r = fac.lookup_ein("91-1624764")
        finally:
            fac._get = orig
        self.assertEqual(r["findings"], 2)
        self.assertEqual(r["questioned_costs"], 1)
        self.assertEqual(r["material_weakness"], 1)
        self.assertEqual(r["repeat_findings"], 1)
        self.assertEqual(r["flagged_amount"], 750000)
        self.assertIn("Head Start", r["flagged_programs"])
        self.assertTrue(r["url"].endswith("2023-X-1"))
        # general was queried by the normalized 9-digit EIN
        self.assertEqual(calls["general"]["auditee_ein"], "eq.911624764")

    def test_lookup_ein_bad_ein(self):
        self.assertIsNone(fac.lookup_ein("123"))      # too short, no network call

    def test_lookup_ein_no_audit(self):
        orig = fac._get
        fac._get = lambda t, p, retries=3: []
        try:
            self.assertIsNone(fac.lookup_ein("911624764"))
        finally:
            fac._get = orig

    def test_summary_line(self):
        s = fac.summary_line({"audit_year": "2023", "findings": 3,
                              "questioned_costs": 1, "material_weakness": 2,
                              "repeat_findings": 0, "flagged_amount": 750000})
        self.assertIn("3 audit finding", s)
        self.assertIn("questioned costs", s)
        self.assertIn("$750,000", s)
        self.assertEqual(fac.summary_line(None), "")


if __name__ == "__main__":
    unittest.main()
