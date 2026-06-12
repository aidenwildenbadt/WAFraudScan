"""Zero-dependency tests:  python -m unittest discover -s tests"""
import unittest

from fraudscan.rules import childcare as cc
from fraudscan.scoring import score_entities
from fraudscan.sources.base import Entity


def mk(uid, **raw_and_fields):
    fields = {k: raw_and_fields.pop(k) for k in
              ("name", "status", "address", "city", "zip", "entity_type", "lat", "lon")
              if k in raw_and_fields}
    return Entity(source="childcare", source_id=uid, raw=raw_and_fields, **fields)


class TestChildcareRules(unittest.TestCase):
    def test_license_expired_active(self):
        ents = [
            mk("a", name="A", status="Active",
               licenseexpirationdate="2000-01-01"),
            mk("b", name="B", status="Active",
               licenseexpirationdate="2999-01-01"),
            mk("c", name="C", status="Closed",
               licenseexpirationdate="2000-01-01"),
        ]
        flags = cc.license_expired_active(ents, {"severity": 30})
        uids = {f.entity_uid for f in flags}
        self.assertEqual(uids, {"childcare:a"})

    def test_shared_contact(self):
        ents = [
            mk("a", name="A", status="Active", primarycontactemail="x@y.com"),
            mk("b", name="B", status="Active", primarycontactemail="X@Y.com"),
            mk("c", name="C", status="Active", primarycontactemail="solo@z.com"),
        ]
        flags = cc.shared_contact_multiple_providers(ents, {"severity": 22})
        self.assertEqual({f.entity_uid for f in flags},
                         {"childcare:a", "childcare:b"})

    def test_address_shared(self):
        ents = [
            mk("a", name="Alpha Care", status="Active",
               address="1 MAIN ST", city="OLYMPIA", zip="98501"),
            mk("b", name="Beta Care", status="Active",
               address="1 main st", city="Olympia", zip="98501"),
            mk("c", name="Gamma", status="Active",
               address="2 OTHER RD", city="TACOMA", zip="98402"),
        ]
        flags = cc.address_shared_multiple_providers(
            ents, {"severity_2": 14, "severity_3plus": 24})
        self.assertEqual({f.entity_uid for f in flags},
                         {"childcare:a", "childcare:b"})

    def test_capacity_missing(self):
        ents = [mk("a", name="A", status="Active", licensecapacity="0"),
                mk("b", name="B", status="Active", licensecapacity="30")]
        flags = cc.capacity_missing_or_zero(ents, {"severity": 10})
        self.assertEqual({f.entity_uid for f in flags}, {"childcare:a"})

    def test_scoring_aggregates_and_caps(self):
        flags = (cc.license_expired_active(
                    [mk("a", name="A", status="Active",
                        licenseexpirationdate="2000-01-01")], {"severity": 30})
                 + cc.capacity_missing_or_zero(
                    [mk("a", name="A", status="Active", licensecapacity="0")],
                    {"severity": 10}))
        rows = score_entities(flags, score_cap=100)
        self.assertEqual(rows[0]["entity_uid"], "childcare:a")
        self.assertEqual(rows[0]["risk_score"], 40.0)
        self.assertEqual(rows[0]["flag_count"], 2)


if __name__ == "__main__":
    unittest.main()
