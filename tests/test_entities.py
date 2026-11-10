"""Tests for entities.py tagging. Run: python3 -m unittest discover -s tests"""
import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))
import entities  # noqa: E402
import store  # noqa: E402


class EntitiesTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript((ROOT / "schema.sql").read_text())
        store.migrate(self.conn)

    def tags(self, title, body=""):
        return entities.summarize_tags(entities.tag_text(self.conn, title, body))

    def test_named_then_implied(self):
        self.assertEqual(self.tags("Fed's Powell says rates on hold; gold slips"), ("XAU,USD", "Federal Reserve"))

    def test_longer_alias_wins(self):
        symbols, _ = self.tags("Australian dollar jumps after RBA holds; US dollar steady")
        self.assertEqual(symbols, "AUD,USD")

    def test_pair_tags_both_sides(self):
        self.assertEqual(self.tags("PBOC sets USD/ CNY central rate at 6.7167")[0], "USD,CNY")
        self.assertEqual(self.tags("XAUUSD technical outlook")[0], "XAU,USD")

    def test_no_false_matches_inside_words(self):
        self.assertEqual(self.tags("Goldman Sachs analysts fed up; golden week travel surges"), (None, None))

    def test_ticker_aliases_are_case_sensitive(self):
        self.assertIsNone(self.tags("We eat an apple and meta commentary follows")[0])
        self.assertEqual(self.tags("Apple and Meta report earnings")[0], "AAPL,META")

    def test_generic_cpi_does_not_imply_usd(self):
        self.assertEqual(self.tags("Canada CPI 2.1% y/y vs 2.0% expected"), ("CAD", "Canada, Inflation"))
        self.assertEqual(self.tags("US CPI takes centre stage")[0], "USD,XAU")

    def test_headline_decides_over_body(self):
        body = "Sterling was little changed. Elsewhere oil prices fell and the Australian dollar rose."
        self.assertEqual(self.tags("UK retail sales rise 1.5%, best since June", body), ("GBP", "United Kingdom"))

    def test_body_used_when_headline_names_nothing(self):
        self.assertEqual(self.tags("Morning wrap", "Gold rose while the yen fell.")[0], "XAU,JPY")

    def test_tag_article_writes_links_and_columns(self):
        res = store.insert_article(self.conn, "investinglive", {
            "title": "Nvidia slumps as chip selloff deepens", "description": "x", "url": "https://a.example/n",
            "published_at": "2026-11-10T03:00:00+00:00"})
        symbols, topics = self.conn.execute("SELECT symbols, topics FROM raw_events WHERE id=?", (res["id"],)).fetchone()
        self.assertEqual((symbols, topics), ("NVDA", "Semiconductors"))
        links = dict(self.conn.execute("SELECT entity_code, via FROM article_entities WHERE article_id=?", (res["id"],)).fetchall())
        self.assertEqual(links, {"NVDA": "mention", "SEMIS": "mention"})

    def test_new_entity_is_a_row_not_code(self):
        self.conn.execute("INSERT INTO entities (code, kind, name, aliases) VALUES ('SGD','currency','Singapore dollar','SGD|Singapore dollar')")
        self.assertEqual(self.tags("Singapore dollar firms after MAS decision")[0], "SGD")


if __name__ == "__main__":
    unittest.main()
