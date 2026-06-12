"""Tests for the verifiable-signal rules: paid-after-barred, billing forensics,
nursing enforcement, and address forensics."""
import unittest

from fraudscan.rules.cross import (paid_while_sanctioned, billing_forensics,
                                   nursing_enforcement)
from fraudscan.rules.address import address_forensics
from fraudscan.sources.base import Entity


def hc(uid, status, periods, expir=None):
    e = Entity(source="healthcare", source_id=uid, name=uid, status=status,
               raw={"expirationdate": expir} if expir else {})
    ctx = {"payments": {"by_entity": {e.uid: {"total": sum(periods.values()),
                                              "program": "Medicare Part D",
                                              "periods": periods}}}}
    return e, ctx


class TestPaidAfterBarred(unittest.TestCase):
    def test_paid_after_barred_dated(self):
        e, ctx = hc("a", "Revoked", {"2023": 362310}, expir="11/10/2017")
        flags = paid_while_sanctioned([e], {}, ctx)
        self.assertEqual(flags[0].rule_id, "paid_after_barred")
        self.assertEqual(flags[0].evidence["barred_year"], 2017)

    def test_undated_falls_back(self):
        e, ctx = hc("b", "Suspended", {"2023": 5000})   # no expiration, no screening
        flags = paid_while_sanctioned([e], {}, ctx)
        self.assertEqual(flags[0].rule_id, "paid_while_sanctioned")

    def test_active_not_flagged(self):
        e, ctx = hc("c", "Active", {"2023": 9000}, expir="01/01/2010")
        self.assertEqual(paid_while_sanctioned([e], {}, ctx), [])


class TestBillingForensics(unittest.TestCase):
    def test_dominance_and_services(self):
        e = Entity(source="healthcare", source_id="x", name="x")
        ctx = {"billing": {e.uid: {"total": 200000, "top_code": "G0123",
                                   "top_desc": "Test", "top_share": 0.95,
                                   "max_srv_per_day": 14}}}
        ids = {f.rule_id for f in billing_forensics([e], {}, ctx)}
        self.assertIn("single_code_dominance", ids)
        self.assertIn("services_per_visit_outlier", ids)


class TestNursingEnforcement(unittest.TestCase):
    def test_jeopardy_and_fines(self):
        e = Entity(source="nursing", source_id="105001", name="Maple")
        ctx = {"nursing_enforcement": {"105001": {
            "deficiencies": 30, "jeopardy": 2, "worst": "L",
            "fines": 50000.0, "penalties": 3}}}
        ids = {f.rule_id for f in nursing_enforcement([e], {}, ctx)}
        self.assertEqual(ids, {"immediate_jeopardy_citation",
                               "civil_monetary_penalty", "many_health_deficiencies"})


class TestAddressForensics(unittest.TestCase):
    def test_mailbox_and_pobox(self):
        ents = [Entity(source="childcare", source_id="a", name="A",
                       address="123 Main St PMB 45"),
                Entity(source="childcare", source_id="b", name="B",
                       address="PO BOX 900"),
                Entity(source="childcare", source_id="c", name="C",
                       address="500 Real Street")]
        by = {f.entity_uid: f.rule_id for f in address_forensics(ents, {})}
        self.assertEqual(by.get("childcare:a"), "commercial_mailbox")
        self.assertEqual(by.get("childcare:b"), "po_box_address")
        self.assertNotIn("childcare:c", by)


if __name__ == "__main__":
    unittest.main()


class TestSubstantiveContracts(unittest.TestCase):
    def test_routine_contracts_dont_count_as_cross_program(self):
        from fraudscan.resolve import _substantive_contract
        from fraudscan.sources.base import Entity
        dsa = Entity(source="contracts_2025", source_id="K1300", name="Acme Care",
                     amount=0.0, raw={"purpose_of_the_contract": "Data Sharing Agreement"})
        mou = Entity(source="contracts", source_id="K2", name="Acme Care",
                     amount=5000.0,
                     raw={"purpose_of_the_contract": "Organizational License MOU"})
        nf = Entity(source="contracts", source_id="K3", name="Acme Care",
                    amount=2.3e7, raw={"purpose_of_the_contract":
                                       "Nursing Facility Services"})
        real = Entity(source="contracts", source_id="K4", name="Acme Care",
                      amount=250000.0,
                      raw={"purpose_of_the_contract": "Early learning services"})
        self.assertFalse(_substantive_contract(dsa))
        self.assertFalse(_substantive_contract(mou))
        self.assertFalse(_substantive_contract(nf))
        self.assertTrue(_substantive_contract(real))


class TestPercentileFines(unittest.TestCase):
    def test_fine_severity_scales_with_state_percentile(self):
        from fraudscan.rules.cross import nursing_enforcement
        from fraudscan.sources.base import Entity
        enf = {f"CCN{i}": {"fines": 10000 + i * 30000, "penalties": 1,
                           "deficiencies": 0, "jeopardy": 0} for i in range(20)}
        ctx = {"nursing_enforcement": enf}
        lo = Entity(source="nursing", source_id="CCN0", name="Low Fine SNF")
        hi = Entity(source="nursing", source_id="CCN19", name="High Fine SNF")
        flags = {(f.entity_uid, f.rule_id): f
                 for f in nursing_enforcement([lo, hi], {}, ctx)}
        sev_lo = flags[(lo.uid, "civil_monetary_penalty")].severity
        sev_hi = flags[(hi.uid, "civil_monetary_penalty")].severity
        self.assertLess(sev_lo, sev_hi)
        self.assertGreaterEqual(sev_hi, 19)   # top of state distribution
        self.assertLessEqual(sev_lo, 8)       # bottom of state distribution
