"""Tests for store.py dedup + metadata. Run: python3 -m unittest discover -s tests"""
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))
import store  # noqa: E402


def iso(hours_ago=0.0):
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()


def art(title="Gold jumps as dollar slides", body="<p>Spot gold rose 1% on Tuesday.</p>", url="https://a.example/1", **kw):
    return {"title": title, "description": body, "url": url, "published_at": kw.pop("published_at", iso()), **kw}


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript((ROOT / "schema.sql").read_text())
        store.migrate(self.conn)

    def row(self, rid):
        self.conn.row_factory = sqlite3.Row
        return self.conn.execute("SELECT * FROM raw_events WHERE id=?", (rid,)).fetchone()

    def test_insert_sets_metadata(self):
        res = store.insert_article(self.conn, "investinglive", art(), default_symbols="USD")
        self.assertEqual(res["status"], "inserted")
        r = self.row(res["id"])
        self.assertTrue(r["fetched_at"] and r["content_hash"])
        self.assertEqual((r["via_fallback"], r["is_stale"], r["dup_count"], r["symbols"]), (0, 0, 0, "USD"))

    def test_same_url_is_skipped(self):
        store.insert_article(self.conn, "investinglive", art())
        res = store.insert_article(self.conn, "investinglive", art(title="Different title", body="other"))
        self.assertEqual(res["status"], "url_exists")

    def test_same_story_from_other_source_merges(self):
        first = store.insert_article(self.conn, "marketaux", art())
        # same text, different URL, markup and case
        res = store.insert_article(
            self.conn, "gdelt", art(title="GOLD jumps as Dollar slides!", body="Spot gold rose 1% on Tuesday.", url="https://b.example/x")
        )
        self.assertEqual((res["status"], res["id"]), ("content_dup", first["id"]))
        r = self.row(first["id"])
        self.assertEqual((r["dup_count"], r["also_seen_in"]), (1, "gdelt"))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM raw_events").fetchone()[0], 1)

    def test_same_title_different_body_is_kept(self):
        store.insert_article(self.conn, "investinglive", art(title="What are the main events for today?", body="Monday: CPI", url="https://a.example/mon"))
        res = store.insert_article(self.conn, "investinglive", art(title="What are the main events for today?", body="Tuesday: FOMC", url="https://a.example/tue"))
        self.assertEqual(res["status"], "inserted")

    def test_recurring_calendar_event_is_stored_per_date(self):
        ev = dict(title="Calendar: CPI m/m (USD)", body="[USD High] CPI m/m — Actual: Forecast: Prev:", url="https://ff.example/calendar#CPI m/m")
        a = store.insert_article(self.conn, "forexfactory", art(published_at="2026-10-13T08:30:00-04:00", **ev))
        b = store.insert_article(self.conn, "forexfactory", art(published_at="2026-11-12T08:30:00-05:00", **ev))
        again = store.insert_article(self.conn, "forexfactory", art(published_at="2026-11-12T08:30:00-05:00", **ev))
        self.assertEqual((a["status"], b["status"], again["status"]), ("inserted", "inserted", "url_exists"))

    def test_old_article_is_marked_stale(self):
        res = store.insert_article(self.conn, "marketaux", art(published_at=iso(hours_ago=30)))
        self.assertTrue(res["is_stale"])
        self.assertEqual(self.row(res["id"])["is_stale"], 1)

    def test_via_fallback_flag(self):
        res = store.insert_article(self.conn, "gdelt", art(), via_fallback=True)
        self.assertEqual(self.row(res["id"])["via_fallback"], 1)

    def test_migrate_upgrades_and_backfills_old_db(self):
        old = sqlite3.connect(":memory:")
        old.execute("CREATE TABLE raw_events (id INTEGER PRIMARY KEY, source TEXT, external_id TEXT, title TEXT, description TEXT, url TEXT, published_at TEXT, symbols TEXT, raw_json TEXT NOT NULL, created_at TEXT)")
        old.execute("INSERT INTO raw_events (source,title,description,url,published_at,raw_json,created_at) VALUES ('marketaux','T','B','u','2026-10-19T05:31:15.000000Z','{}','2026-10-21T00:01:11.931Z')")
        store.migrate(old)
        store.migrate(old)  # idempotent
        h, f, s = old.execute("SELECT content_hash, fetched_at, is_stale FROM raw_events").fetchone()
        self.assertEqual((h, f, s), (store.content_hash("T", "B"), "2026-10-21T00:01:11.931Z", 1))


if __name__ == "__main__":
    unittest.main()
