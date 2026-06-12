"""Tests for WA SOS business-identity loading + linking."""
import csv
import os
import tempfile
import unittest

from fraudscan.sos import load_sos, sos_keys
from fraudscan.resolve import build_operators
from fraudscan.sources.base import Entity


def _write_sos(d, rows, header):
    os.makedirs(os.path.join(d, "sos"), exist_ok=True)
    p = os.path.join(d, "sos", "export.csv")
    with open(p, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=header)
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return p


class TestSos(unittest.TestCase):
    def test_load_and_match_autodetect_columns(self):
        hdr = ["Business Name", "UBI Number", "Status", "Registered Agent Name",
               "Governors"]
        with tempfile.TemporaryDirectory() as d:
            _write_sos(d, [
                {"Business Name": "ABC Care LLC", "UBI Number": "601111111",
                 "Status": "ACTIVE", "Registered Agent Name": "John Operator",
                 "Governors": "John Operator; Jane Doe"},
            ], hdr)
            sos = load_sos(d)
        self.assertEqual(sos.count, 1)
        rec = sos.match("ABC CARE LLC")
        self.assertEqual(rec["ubi"], "601111111")
        self.assertEqual(rec["governors"], ["John Operator", "Jane Doe"])
        keys, _ = sos_keys(
            Entity(source="childcare", source_id="1", name="ABC Care LLC"), sos)
        self.assertIn("ubi:601111111", keys)
        self.assertTrue(any(k.startswith("agent:") for k in keys))
        self.assertTrue(any(k.startswith("gov:") for k in keys))

    def test_no_sos_dir_is_empty(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(load_sos(d).count, 0)

    def test_shared_agent_links_differently_named_entities(self):
        hdr = ["Business Name", "UBI Number", "Registered Agent Name"]
        with tempfile.TemporaryDirectory() as d:
            _write_sos(d, [
                {"Business Name": "Sunrise Childcare LLC", "UBI Number": "601000001",
                 "Registered Agent Name": "Pat Shellman"},
                {"Business Name": "Moonlight Services Inc", "UBI Number": "601000002",
                 "Registered Agent Name": "Pat Shellman"},
            ], hdr)
            sos = load_sos(d)
        ents = [
            Entity(source="childcare", source_id="1", name="Sunrise Childcare LLC",
                   address="1 A St", city="Olympia", zip="98501"),
            Entity(source="contracts", source_id="2", name="Moonlight Services Inc",
                   address="2 B St", city="Tacoma", zip="98402"),
        ]
        ops, _ = build_operators(ents, {"childcare:1": 10, "contracts:2": 10},
                                 sos=sos)
        # the two differently-named entities should be one operator via shared agent
        merged = [o for o in ops if o["member_count"] == 2]
        self.assertTrue(merged, "shared registered agent should link the two entities")
        self.assertTrue(any("registered agent" in s.lower()
                            for s in merged[0]["signals"]))

    def test_no_link_without_sos(self):
        ents = [
            Entity(source="childcare", source_id="1", name="Sunrise Childcare LLC"),
            Entity(source="contracts", source_id="2", name="Moonlight Services Inc"),
        ]
        ops, _ = build_operators(ents, {}, sos=None)
        self.assertFalse(any(o["member_count"] == 2 for o in ops))


if __name__ == "__main__":
    unittest.main()
