import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from prefilter import classify_relevance


class Relevance(unittest.TestCase):
    def test_label_comes_from_tags_not_words(self):
        self.assertEqual(classify_relevance("x", "", "USD,CNY"), "high")
        self.assertEqual(classify_relevance("x", "", "XAU"), "high")
        self.assertEqual(classify_relevance("x", "", "NVDA,GOOGL"), "medium")
        # "Goldman" / "FedEx" used to match the gold / fed keywords
        self.assertEqual(classify_relevance("Goldman on FedEx", "", ""), "low")
        self.assertEqual(classify_relevance("x", "", None), "low")


if __name__ == "__main__":
    unittest.main()
