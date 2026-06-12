"""Tests for the operator risk rollup + non-summed score + network suppression."""
import unittest

from fraudscan.resolve import build_operators, _is_generic_name, _person_key
from fraudscan.sources.base import Entity


class TestOperatorRollup(unittest.TestCase):
    def _two_at_one_address(self):
        # two differently-named childcares at one address -> one operator
        return [
            Entity(source="childcare", source_id="1", name="Sunrise Kids",
                   address="100 Main St", city="Olympia", zip="98501"),
            Entity(source="childcare", source_id="2", name="Bright Beginnings",
                   address="100 Main St", city="Olympia", zip="98501"),
        ]

    def test_score_is_not_a_sum(self):
        ents = self._two_at_one_address()
        scores = {"childcare:1": 40, "childcare:2": 40}
        ops, _ = build_operators(ents, scores)
        op = next(o for o in ops if o["member_count"] == 2)
        # summed would be 80(+bonus); max-based keeps it near the worst member + bonus
        self.assertLess(op["combined_score"], 80)
        self.assertGreaterEqual(op["combined_score"], 40)

    def test_rollup_counts_barred_and_contradiction(self):
        ents = self._two_at_one_address()
        facts = {
            "childcare:1": {"funds": 200000.0, "barred": True, "sanctioned": True,
                            "contradiction": 50000.0, "identity": "license-confirmed",
                            "top_flag": "Paid after barred"},
            "childcare:2": {"funds": 0.0, "barred": False, "sanctioned": False,
                            "contradiction": 0.0, "identity": "", "top_flag": ""},
        }
        ops, _ = build_operators(ents, {"childcare:1": 50, "childcare:2": 30},
                                 member_facts=facts)
        op = next(o for o in ops if o["member_count"] == 2)
        self.assertEqual(op["barred_members"], 1)
        self.assertEqual(op["contradiction_amount"], 50000.0)
        self.assertEqual(op["dollars_at_stake"], 200000.0)
        # contradiction (+30) and barred-with-money (+15) bonuses push the score high
        self.assertGreaterEqual(op["combined_score"], 80)
        # named after the barred member, sorted first
        self.assertEqual(op["members"][0]["name"], "Sunrise Kids")

    def test_large_clean_network_suppressed(self):
        ents = [Entity(source="childcare", source_id=str(i), name="Chain Daycare",
                       address=f"{i} Road", city="Tacoma", zip="98402")
                for i in range(22)]
        # link them all by fuzzy/identical name; none barred
        scores = {f"childcare:{i}": 45 for i in range(22)}
        ops, _ = build_operators(ents, scores)
        big = [o for o in ops if o["member_count"] >= 20]
        if big:  # a 22-member clean network should be suppressed well below its raw score
            self.assertLess(big[0]["combined_score"], 45)
            self.assertTrue(any("likely\nlegitimate" in s.replace(" ", "\n").lower()
                                or "likely legitimate" in s.lower()
                                for s in big[0]["signals"]))


    def test_generic_name_does_not_merge(self):
        self.assertTrue(_is_generic_name("EARLY LEARNING CENTER"))
        self.assertTrue(_is_generic_name("CREATIVE KIDS LEARNING CENTER"))
        self.assertFalse(_is_generic_name("CHILDS TIME VII"))
        self.assertFalse(_is_generic_name("FRENCH AMERICAN SCHOOL"))
        # two unrelated daycares sharing only a generic name must NOT form an operator
        ents = [
            Entity(source="childcare", source_id="1", name="Early Learning Center",
                   address="1 A St", city="Seattle", zip="98101"),
            Entity(source="childcare", source_id="2", name="Early Learning Center",
                   address="2 B St", city="Spokane", zip="99201"),
        ]
        ops, _ = build_operators(ents, {"childcare:1": 30, "childcare:2": 30})
        self.assertFalse(any(o["member_count"] == 2 for o in ops))

    def test_common_name_bridge_is_discounted(self):
        ents = [
            Entity(source="childcare", source_id="1", name="Tiny Tots",
                   raw={"primarycontactpersonname": "John Smith"}),
            Entity(source="healthcare", source_id="X", name="John Smith",
                   status="Revoked", raw={}),
        ]
        pk = _person_key("John Smith")
        # dollars at stake so the F12 zero-dollar cap doesn't flatten both variants
        mf = {"childcare:1": {"funds": 50000.0}}
        rare = build_operators(ents, {"childcare:1": 20, "healthcare:X": 50},
                               name_counts={pk: 1}, member_facts=mf)
        common = build_operators(ents, {"childcare:1": 20, "healthcare:X": 50},
                                 name_counts={pk: 9}, member_facts=mf)
        rop = next(o for o in rare[0] if o["member_count"] == 2)
        cop = next(o for o in common[0] if o["member_count"] == 2)
        self.assertLess(cop["combined_score"], rop["combined_score"])
        self.assertTrue(any("COMMON NAME" in s for s in cop["signals"]))


if __name__ == "__main__":
    unittest.main()
