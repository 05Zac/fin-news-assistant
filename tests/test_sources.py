import sqlite3
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))
import sources  # noqa: E402


class Resp:
    def __init__(self, code, data=None):
        self.status_code, self._data = code, data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)

    def json(self):
        return self._data


class SourcesTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript((ROOT / "schema.sql").read_text())

    def test_gdelt_retries_429_then_normalises(self):
        art = {"url": "https://x.example/a", "title": "Abbott Laboratories ( ABT ) shares rise 2 . 5 % , analysts say",
               "seendate": "20261126T224500Z", "domain": "x.example"}
        replies = iter([Resp(429), Resp(429), Resp(200, {"articles": [art]})])
        with mock.patch.object(sources.requests, "get", lambda *a, **k: next(replies)):
            out = sources.fetch_gdelt(sleep=lambda s: None)
        self.assertEqual(out[0]["title"], "Abbott Laboratories (ABT) shares rise 2.5%, analysts say")
        self.assertEqual(out[0]["published_at"], "2026-11-26T22:45:00+00:00")

    def test_failure_cools_off_that_source_only(self):
        now = datetime(2026, 11, 27, 1, 0, tzinfo=timezone.utc)
        calls = []

        def boom(limit):
            calls.append(1)
            raise sources.RateLimited("429")

        with mock.patch.dict(sources.FETCHERS, {"gdelt": lambda limit, conn: boom(limit), "fed": lambda limit, conn: [{"title": "ok"}]}):
            self.assertIsNone(sources.fetch_with_cooloff(self.conn, "gdelt", 10, now))
            self.assertIsNone(sources.fetch_with_cooloff(self.conn, "gdelt", 10, now))  # cooling off: not called
            self.assertEqual(len(calls), 1)
            self.assertEqual(sources.fetch_with_cooloff(self.conn, "fed", 10, now), [{"title": "ok"}])
            later = datetime(2026, 11, 27, 3, 1, tzinfo=timezone.utc)
            self.assertIsNone(sources.fetch_with_cooloff(self.conn, "gdelt", 10, later))  # retried after 2h
            self.assertEqual(len(calls), 2)

    def test_sec_titles_and_window(self):
        self.conn.execute("CREATE TABLE entities (code TEXT, kind TEXT, name TEXT, active INTEGER)")
        self.conn.execute("INSERT INTO entities VALUES ('AAPL','stock','Apple',1)")
        today = datetime.now(timezone.utc).date().isoformat()
        reply = ('noise\n{"ok":true,"data":[{"type":"8-K","filing":"https://sec.example/1",'
                 '"description":"Current report, items 2.02 and 9.01","filed":"%s"},'
                 '{"type":"8-K","filing":"https://sec.example/old","description":"items 5.02","filed":"2020-01-01"}]}' % today)
        out = sources.fetch_sec(conn=self.conn, run=lambda args: reply)
        self.assertEqual([a["title"] for a in out], ["Apple (AAPL) 8-K: results (earnings)"])

    def test_title_only_releases_get_a_description(self):
        self.assertEqual(sources.describe("ecb", "https://www.ecb.europa.eu/press/accounts/2026/html/x.html",
                                          title="Meeting of 28-29 October 2026"),
                         "European Central Bank (ECB) account (minutes) of a monetary policy meeting.")
        self.assertEqual(sources.describe("fed", "https://f.example/monetary20261124a.htm", "Monetary Policy",
                                          "Minutes of the board", "Minutes of the board"),
                         "Federal Reserve monetary policy.")
        self.assertEqual(sources.describe("gdelt", "u", desc="keep"), "keep")


if __name__ == "__main__":
    unittest.main()
