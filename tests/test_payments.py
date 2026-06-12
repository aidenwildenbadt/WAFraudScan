"""Tests for the payments layer (contract derivation + money parsing)."""
import unittest

from fraudscan.payments import contract_payments, medicare_payments, _money
from fraudscan.sources.base import Entity


class TestPayments(unittest.TestCase):
    def test_money_parse(self):
        self.assertEqual(_money("$1,234.50"), 1234.5)
        self.assertEqual(_money("100000"), 100000.0)
        self.assertIsNone(_money(""))
        self.assertIsNone(_money(None))

    def test_contract_payments_with_period(self):
        e = Entity(source="contracts", source_id="c1", name="Acme Vendor",
                   amount=50000.0, source_url="http://x",
                   raw={"contract_effective_start": "2021-07-01",
                        "contract_effective_end_date": "2023-06-30"})
        rows = contract_payments([e])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["amount"], 50000.0)
        self.assertEqual(rows[0]["program"], "WA agency contract")
        self.assertIn("2021-07-01", rows[0]["period"])
        self.assertIn("2023-06-30", rows[0]["period"])

    def test_non_contract_and_null_amounts_skipped(self):
        ents = [Entity(source="childcare", source_id="a", name="A"),
                Entity(source="contracts", source_id="b", name="B", amount=None)]
        self.assertEqual(contract_payments(ents), [])

    def test_medicare_join_no_matching_source_is_empty(self):
        # no entities of the join_source -> no network call, empty result
        ents = [Entity(source="childcare", source_id="a", name="A")]
        pcfg = {"join_source": "dme", "npi_field": "Suplr_NPI",
                "amount_field": "Suplr_Mdcr_Pymt_Amt", "years": {}}
        self.assertEqual(medicare_payments(ents, pcfg), [])


if __name__ == "__main__":
    unittest.main()
