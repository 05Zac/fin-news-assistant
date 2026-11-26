import sqlite3
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))
import agent_pm  # noqa: E402


def row(i, **kw):
    r = {"id": i, "source": "investinglive", "title": f"Headline {i}", "url": f"https://x.example/{i}",
         "symbols": "USD,XAU", "is_stale": 0, "via_fallback": 0, "also_seen_in": None}
    r.update(kw)
    return r


class DigestTest(unittest.TestCase):
    def test_points_normalised_from_any_model_style(self):
        text = "Live: USD 1.0\n## Summary\n**Key**\n* **USD** firmer\n1. Gold slips\n• Oil flat\nnot a bullet"
        self.assertEqual(agent_pm.clean_points(text), ["USD firmer", "Gold slips", "Oil flat"])

    def test_same_layout_with_and_without_ai(self):
        now = datetime(2026, 11, 26, tzinfo=timezone.utc)
        rows = [row(i) for i in range(12)]
        ai = agent_pm.build_summary(rows, ["Gold slips"], "Live: XAU $1", now)
        stub = agent_pm.build_summary(rows, [], "Live: XAU $1", now)
        heads = lambda t: [l for l in t.splitlines() if l.startswith("**")]  # noqa: E731
        self.assertEqual(heads(ai), heads(stub))
        self.assertIn("- [Headline 0](<https://x.example/0>) `USD, XAU`", ai)
        self.assertIn("unavailable", stub)

    def test_long_digest_split_on_lines_never_cut(self):
        text = "\n".join(f"- line {i} " + "x" * 90 for i in range(60))
        parts = agent_pm.split_message(text)
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(p) <= 2000 for p in parts))
        self.assertEqual("\n".join(parts), text)


if __name__ == "__main__":
    unittest.main()
