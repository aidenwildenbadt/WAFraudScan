"""Regression tests from the 10-case fleet audit: Part D refill runoff (Kimura/Bolling),
bar-date provenance (Aflatooni), and dollar materiality (Leinweber $233 vs Dees $472K)."""
import unittest

from fraudscan.rules.cross import paid_while_sanctioned
from fraudscan.sources.base import Entity


def _ctx(by_program, screening=None):
    total = sum(p["total"] for p in by_program.values())
    merged = {}
    for p in by_program.values():
        for k, v in p["periods"].items():
            merged[k] = merged.get(k, 0) + v
    dom = max(by_program, key=lambda k: by_program[k]["total"])
    return {"payments": {"by_entity": {"healthcare:X": {
                "total": total, "program": dom, "periods": merged,
                "by_program": by_program}}},
            "crosswalk": {}, "crosswalk_detail": {}, "screening": screening}


class _Scr:
    """Screening stub returning one formal exclusion match."""
    def __init__(self, date, lst="OIG LEIE"):
        self.date = date
        self.lst = lst

    def match(self, **kw):
        return {"list": self.lst, "date": self.date, "reason": "x",
                "confidence": "definitive", "matched_via": "NPI"}


def _ent():
    return Entity(source="healthcare", source_id="X", name="Pat Doe", state="WA",
                  status="Revoked",
                  raw={"credentialnumber": "MD.MD.1", "expirationdate": "03/01/2020"})


class TestBarDateProvenance(unittest.TestCase):
    def test_formal_date_beats_expiration(self):
        # Aflatooni pattern: license expired 2020, formal exclusion 2022. Part D money
        # in 2021 must NOT count as after-bar (it predates the real sanction).
        ctx = _ctx({"Medicare Part D (drugs)": {"total": 66000.0, "periods":
                    {"2020": 513000.0, "2021": 66000.0}}}, screening=_Scr("2022-02-20"))
        flags = {f.rule_id: f for f in paid_while_sanctioned([_ent()], {}, ctx)}
        self.assertNotIn("paid_after_barred", flags)
        # F1: all payments predate the 2022 formal bar -> informational disposition
        # (not a present-tense "still paid" lead), anchored to the FORMAL date.
        self.assertIn("paid_before_bar", flags)
        self.assertEqual(flags["paid_before_bar"].evidence["barred_year"], 2022)
        self.assertLessEqual(flags["paid_before_bar"].severity, 12)

    def test_expiration_fallback_carries_provenance(self):
        ctx = _ctx({"Medicare Part B": {"total": 50000.0,
                                        "periods": {"2021": 50000.0}}})
        flags = {f.rule_id: f for f in paid_while_sanctioned([_ent()], {}, ctx)}
        f = flags["paid_after_barred"]
        self.assertIn("credential expiration", f.evidence["bar_source"])


class TestRunoffAndMateriality(unittest.TestCase):
    def test_partd_refill_runoff_downgraded(self):
        # steep decline, Part-D-only, ends in bar_year+1 -> runoff, severity <= 20
        ctx = _ctx({"Medicare Part D (drugs)": {"total": 89000.0, "periods":
                    {"2020": 86000.0, "2021": 3000.0}}}, screening=_Scr("2020-03-20"))
        f = {f.rule_id: f for f in paid_while_sanctioned([_ent()], {}, ctx)}[
            "paid_after_barred"]
        self.assertTrue(f.evidence["runoff_consistent"])
        self.assertLessEqual(f.severity, 20)
        self.assertIn("runoff", f.explanation)

    def test_small_amount_scaled_down_large_full(self):
        ctx_small = _ctx({"Medicare Part B": {"total": 233.0,
                          "periods": {"2021": 233.0}}}, screening=_Scr("2020-06-01"))
        ctx_large = _ctx({"Medicare Part B": {"total": 472000.0, "periods":
                          {"2021": 250000.0, "2022": 222000.0}}},
                         screening=_Scr("2020-06-01"))
        small = {f.rule_id: f for f in paid_while_sanctioned([_ent()], {}, ctx_small)}[
            "paid_after_barred"]
        large = {f.rule_id: f for f in paid_while_sanctioned([_ent()], {}, ctx_large)}[
            "paid_after_barred"]
        self.assertEqual(small.severity, 22)      # < $1K
        self.assertEqual(large.severity, 50)      # > $100K
        self.assertEqual(large.evidence["program"], "Medicare Part B")

    def test_same_year_ambiguous_not_counted(self):
        ctx = _ctx({"Medicare Part B": {"total": 89000.0,
                                        "periods": {"2020": 89000.0}}},
                   screening=_Scr("2020-07-16"))
        flags = {f.rule_id: f for f in paid_while_sanctioned([_ent()], {}, ctx)}
        self.assertNotIn("paid_after_barred", flags)
        ws = flags["paid_while_sanctioned"]
        self.assertEqual(ws.evidence["same_year_amount"], 89000.0)
        self.assertIn("ambiguous", ws.explanation)


if __name__ == "__main__":
    unittest.main()


class TestProgramScopedBars(unittest.TestCase):
    def test_dees_pattern_proxy_covers_programs_formal_does_not(self):
        # HCA bar (Medicaid scope) + license loss same year: Medicare money after the
        # bar must still count as after-bar via the expiry/license proxy (F8).
        ctx = _ctx({"Medicare Part B": {"total": 90000.0,
                                        "periods": {"2021": 90000.0}}},
                   screening=_Scr("2020-03-01", lst="WA HCA terminated (Medicaid)"))
        e = Entity(source="healthcare", source_id="X", name="Pat Doe", state="WA",
                   status="Surrender",
                   raw={"credentialnumber": "MD.MD.1",
                        "expirationdate": "03/01/2020"})
        flags = {f.rule_id: f for f in paid_while_sanctioned([e], {}, ctx)}
        self.assertIn("paid_after_barred", flags)
        self.assertIn("credential expiration",
                      flags["paid_after_barred"].evidence["bars_applied"]
                      ["Medicare Part B"])

    def test_earl_pattern_reinstatement_invalidates_expiry_proxy(self):
        # license expired 2014 but re-issued 2017 (reinstatement): the 2014 expiry is
        # NOT a live bar, so 2018 Medicare money must not be 'after-bar' (F7).
        ctx = _ctx({"Medicare Part B": {"total": 90000.0,
                                        "periods": {"2018": 90000.0}}})
        e = Entity(source="healthcare", source_id="X", name="Pat Doe", state="WA",
                   status="Revoked",
                   raw={"credentialnumber": "MD.MD.1",
                        "expirationdate": "03/01/2014",
                        "lastissuedate": "08/02/2017"})
        flags = {f.rule_id: f for f in paid_while_sanctioned([e], {}, ctx)}
        self.assertNotIn("paid_after_barred", flags)


class TestG2G4Dispositions(unittest.TestCase):
    def test_earl_runoff_uses_applied_program_bar(self):
        # G4: Medicaid-scoped 2014 formal bar + 2018 expiry proxy; Part D collapses
        # 93% into one year after the APPLIED 2018 bar -> runoff must be True.
        ctx = _ctx({"Medicare Part D (drugs)": {"total": 510000.0, "periods":
                    {"2017": 333000.0, "2018": 470000.0, "2019": 40700.0}}},
                   screening=_Scr("2014-06-01", lst="WA HCA terminated (Medicaid)"))
        e = Entity(source="healthcare", source_id="X", name="Pat Doe", state="WA",
                   status="Surrender",
                   raw={"credentialnumber": "MD.MD.1",
                        "expirationdate": "07/27/2018"})
        flags = {f.rule_id: f for f in paid_while_sanctioned([e], {}, ctx)}
        f = flags["paid_after_barred"]
        self.assertTrue(f.evidence["runoff_consistent"])
        self.assertLessEqual(f.severity, 20)

    def test_same_year_only_money_is_ceased_with_coverage(self):
        # G2: bar 2015, all money in 2015, coverage through 2024 -> ceased disposition
        ctx = _ctx({"Medicare Part B": {"total": 204000.0,
                                        "periods": {"2014": 100000.0,
                                                    "2015": 104000.0}}},
                   screening=_Scr("2015-03-01"))
        ctx["payments"]["max_year"] = 2024
        e = Entity(source="healthcare", source_id="X", name="Pat Doe", state="WA",
                   status="Revoked",
                   raw={"credentialnumber": "MD.MD.1",
                        "expirationdate": "03/01/2015"})
        flags = {f.rule_id: f for f in paid_while_sanctioned([e], {}, ctx)}
        self.assertIn("paid_before_bar", flags)
        self.assertNotIn("paid_while_sanctioned", flags)

    def test_trivial_tail_routes_to_ceased(self):
        # G2: $591 tail on $470K total -> residue, not a 90+ contradiction
        ctx = _ctx({"Medicare Part B": {"total": 470757.0, "periods":
                    {"2015": 300000.0, "2016": 170166.0, "2017": 591.0}}},
                   screening=_Scr("2016-09-01"))
        ctx["payments"]["max_year"] = 2024
        e = Entity(source="healthcare", source_id="X", name="Pat Doe", state="WA",
                   status="Surrender",
                   raw={"credentialnumber": "MD.MD.1",
                        "expirationdate": "09/01/2016"})
        flags = {f.rule_id: f for f in paid_while_sanctioned([e], {}, ctx)}
        self.assertNotIn("paid_after_barred", flags)
        self.assertIn("paid_before_bar", flags)
        self.assertEqual(flags["paid_before_bar"].evidence["trivial_after_tail"], 591.0)

    def test_money_age_decay_on_after_bar(self):
        # G1: identical contradiction, decade-old money -> severity decays
        old = _ctx({"Medicare Part B": {"total": 90000.0,
                                        "periods": {"2016": 90000.0}}},
                   screening=_Scr("2015-06-01"))
        old["payments"]["max_year"] = 2024
        e = Entity(source="healthcare", source_id="X", name="Pat Doe", state="WA",
                   status="Revoked",
                   raw={"credentialnumber": "MD.MD.1",
                        "expirationdate": "06/01/2015"})
        f = {f.rule_id: f for f in paid_while_sanctioned([e], {}, old)}[
            "paid_after_barred"]
        self.assertLessEqual(f.severity, 15)        # 45 * 0.3 with floor
        self.assertEqual(f.evidence["money_age_years"], 8)
