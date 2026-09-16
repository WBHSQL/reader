from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from reading_app import ReadingItem
from reading_rss import FeedEntry, RssAggregator, canonical_url


class ReadingRssTests(unittest.TestCase):
    def test_canonical_url_removes_tracking(self):
        value = canonical_url("https://Example.com/a?utm_source=x&keep=1&fbclid=y#frag")
        self.assertEqual(value, "https://example.com/a?keep=1")

    def test_refresh_adds_once_and_seen_state_has_no_raw_body(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sources = root / "sources.json"
            seen = root / "seen.json"
            items = root / "items.json"
            sources.write_text('[{"name":"Test","url":"https://feed.test/rss"}]', encoding="utf-8")
            items.write_text("[]", encoding="utf-8")
            entry = FeedEntry(key="k1", title="T", url="https://example.com/a", source="Test")
            prepared = ReadingItem(id="item-rss", title="Prepared", source="Test", background="B",
                                   question="Q", author_reasoning="R", author_conclusion="C",
                                   url=entry.url)
            aggregator = RssAggregator(sources_path=sources, seen_path=seen, items_path=items,
                                       provider=object(), max_selected=1)
            with patch("reading_rss.fetch_feed", return_value=[entry]), \
                 patch("reading_rss.select_entries", return_value=[entry]), \
                 patch("reading_rss.fetch_article", return_value=("RAW ARTICLE BODY", "Test")), \
                 patch("reading_rss.prepare_item", return_value=prepared):
                first = aggregator.refresh()
                second = aggregator.refresh()

            self.assertEqual(len(first["added"]), 1)
            self.assertEqual(second["unseen"], 0)
            stored = seen.read_text(encoding="utf-8")
            self.assertNotIn("RAW ARTICLE BODY", stored)
            self.assertIn('"status": "added"', stored)
            saved_items = json.loads(items.read_text(encoding="utf-8"))
            self.assertEqual(saved_items[0]["id"], "item-rss")
            self.assertEqual(saved_items[0]["title"], "T")


if __name__ == "__main__":
    unittest.main()
