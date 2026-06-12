"""Tests for curated context (Tier 2) loading + matching."""
import csv
import os
import tempfile
import unittest

from fraudscan.context_sources import load_context, Context


def _write(d, rows, header):
    os.makedirs(os.path.join(d, "context"), exist_ok=True)
    p = os.path.join(d, "context", "c.csv")
    with open(p, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=header)
        w.writeheader()
        for r in rows:
            w.writerow(r)


class TestContext(unittest.TestCase):
    def test_credential_digit_core_match(self):
        # file says 'RN00120098', entity credential is 'RN.RN.00120098' -> must match
        c = Context()
        c.add({"url": "u", "title": "t", "date": "", "kind": "update"},
              cred="RN00120098")
        hits = c.lookup(credentialnumber="RN.RN.00120098")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["matched_via"], "credential")
        self.assertEqual(c.lookup(credentialnumber="RN.RN.99999999"), [])

    def test_npi_and_name_match_and_dedup(self):
        c = Context()
        c.add({"url": "u1", "title": "a", "date": "", "kind": "news"}, npi="123")
        c.add({"url": "u2", "title": "b", "date": "", "kind": "order"},
              name="Jane Doe")
        self.assertEqual(len(c.lookup(npi="123")), 1)
        self.assertEqual(c.lookup(npi="123")[0]["matched_via"], "NPI")
        self.assertEqual(len(c.lookup(name="JANE DOE")), 1)

    def test_load_autodetects_columns(self):
        with tempfile.TemporaryDirectory() as d:
            _write(d, [{"Credential #": "MD.MD.00044369", "URL": "http://k5",
                        "Title": "license suspended", "Date": "2025", "Kind": "news"}],
                   ["Credential #", "URL", "Title", "Date", "Kind"])
            ctx = load_context(d)
        self.assertEqual(ctx.count, 1)
        hits = ctx.lookup(credentialnumber="MD.MD.00044369")
        self.assertEqual(hits[0]["title"], "license suspended")

    def test_no_dir_is_empty(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(load_context(d).count, 0)


if __name__ == "__main__":
    unittest.main()
