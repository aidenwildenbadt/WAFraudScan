"""Tests for the findchildcarewa childcare-enforcement parser + rule."""
import unittest

from fraudscan.childcare_enforcement import parse_provider
from fraudscan.rules.cross import childcare_enforcement
from fraudscan.sources.base import Entity

_CLEAN = (
    '<div class="tab-pane" id="complaints">No Provider Cases available. Child Care Check '
    'posts valid complaints from June 10, 2023 through June 9, 2026.</div>'
    '<div class="tab-pane" id="inspections">No Inspection Data available</div>'
    '<div class="tab-pane" id="license_history">normal</div>')

_POPULATED = (
    '<div class="tab-pane" id="complaints">Provider Cases'
    '<tr><td>03/14/2024</td><td>Substantiated</td></tr>'
    '<tr><td>11/02/2023</td><td>Valid</td></tr></div>'
    '<div class="tab-pane" id="inspections">Inspections'
    '<tr><td>12/10/2024</td></tr><tr><td>01/24/2024</td></tr>'
    '<tr><td>06/01/2023</td></tr></div>'
    '<div class="tab-pane" id="license_history">x</div>')


class TestChildcareEnforcement(unittest.TestCase):
    def test_parse_clean(self):
        d = parse_provider(_CLEAN)
        self.assertEqual((d["complaints"], d["inspections"]), (0, 0))

    def test_parse_populated(self):
        d = parse_provider(_POPULATED)
        self.assertEqual((d["complaints"], d["inspections"]), (2, 3))

    def test_rule_flags_complaint(self):
        e = Entity(source="childcare", source_id="001x", name="Tiny Tots Daycare")
        ctx = {"childcare_enforcement": {e.uid: {"complaints": 2, "inspections": 7}}}
        flags = {f.rule_id: f for f in childcare_enforcement([e], {}, ctx)}
        self.assertIn("childcare_valid_complaint", flags)
        self.assertIn("childcare_many_inspections", flags)
        self.assertEqual(flags["childcare_valid_complaint"].evidence["complaints"], 2)
        # 2 complaints -> base 18 + 6
        self.assertEqual(flags["childcare_valid_complaint"].severity, 24)

    def test_rule_noop_when_clean(self):
        e = Entity(source="childcare", source_id="001y", name="Good Care")
        ctx = {"childcare_enforcement": {e.uid: {"complaints": 0, "inspections": 2}}}
        self.assertEqual(childcare_enforcement([e], {}, ctx), [])


if __name__ == "__main__":
    unittest.main()
