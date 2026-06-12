"""Vercel Web Analytics integration checks."""
import json
import pathlib
import unittest

from fraudscan.web import casefile


ROOT = pathlib.Path(__file__).resolve().parents[1]
ANALYTICS_SCRIPT = "/_vercel/insights/script.js"


class TestVercelAnalytics(unittest.TestCase):
    def test_dashboard_includes_vercel_analytics_script(self):
        html = (ROOT / "fraudscan" / "web" / "index.html").read_text()
        self.assertIn("window.va = window.va || function", html)
        self.assertIn(ANALYTICS_SCRIPT, html)

    def test_casefile_pages_include_vercel_analytics_script(self):
        html = casefile.entity_casefile({
            "name": "Example Provider",
            "source": "healthcare",
            "score": {"risk_score": 10},
        })
        self.assertIn("window.va = window.va || function", html)
        self.assertIn(ANALYTICS_SCRIPT, html)

    def test_vercel_rewrite_does_not_capture_internal_analytics_path(self):
        config = json.loads((ROOT / "vercel.json").read_text())
        source = config["rewrites"][0]["source"]
        self.assertIn("_vercel", source)
        self.assertIn("?!", source)


if __name__ == "__main__":
    unittest.main()
