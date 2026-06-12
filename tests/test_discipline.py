"""Tests for DOH disciplinary-narrative parsing (Tier 1), no network."""
import unittest

from fraudscan import doh_discipline as dd

_SAMPLE = (
    "Disciplinary actions. King County In July 2025 the Department of Health charged "
    "registered nursing assistant Jorge A. Brito (NA61419591) with unprofessional "
    "conduct. In March 2024, Brito allegedly failed to properly transfer a patient, "
    "resulting in serious injuries. Pierce County In June 2025 the Dental Quality "
    "Assurance Commission and dentist Adam Zigmund Mileski (DE60601618) entered an "
    "agreed order requiring a $5,000 fine and an ethics assessment. Done."
)


class TestDiscipline(unittest.TestCase):
    def test_slug_date(self):
        self.assertEqual(
            dd._slug_date("state-disciplines-health-care-providers-08-15-2025"),
            "2025-08-15")

    def test_entries_and_reason(self):
        ents = dd._entries_from_text(_SAMPLE, "2025-08-15", "http://x")
        creds = {e["credential"]: e for e in ents}
        self.assertIn("NA61419591", creds)
        self.assertIn("DE60601618", creds)
        self.assertEqual(creds["NA61419591"]["name"], "Jorge A. Brito")
        # reason captures the action sentence + the detail sentence
        self.assertIn("unprofessional conduct", creds["NA61419591"]["reason"])
        self.assertIn("$5,000 fine", creds["DE60601618"]["reason"])
        # county prefix stripped from the lead
        self.assertFalse(creds["DE60601618"]["reason"].startswith("Pierce County"))

    def test_text_strips_tags(self):
        self.assertEqual(dd._text("<p>Hi&nbsp;<b>there</b></p>"), "Hi there")


if __name__ == "__main__":
    unittest.main()
