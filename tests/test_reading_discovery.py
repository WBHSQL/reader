from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from reading_discovery import DiscoverySource, SHANGHAI, SourceDiscovery, discover_column


COLUMN_HTML = """
<div class="cell item"><span class="item_title"><a href="http://www.jintiankansha.com/t/new1">今天文章</a></span></div>
<div class="cell item"><span class="item_title"><a href="http://www.jintiankansha.com/t/old1">旧文章</a></span></div>
"""


class ReadingDiscoveryTests(unittest.TestCase):
    def test_discover_column_keeps_only_recent_exact_timestamp(self):
        source = DiscoverySource("src", "来源", "https://example.test/column")
        pages = {
            source.url: COLUMN_HTML,
            "https://www.jintiankansha.com/t/new1": "<html><body>2026-09-02 07:43</body></html>",
            "https://www.jintiankansha.com/t/old1": "<html><body>2026-08-29 07:43</body></html>",
        }
        now = datetime(2026, 9, 2, 13, 0, tzinfo=SHANGHAI)
        with patch("reading_discovery._fetch_html", side_effect=lambda url: pages[url]):
            rows = discover_column(source, now=now, max_age_hours=36)
        self.assertEqual([row.title for row in rows], ["今天文章"])

    def test_configured_not_before_blocks_pre_reset_articles(self):
        source = DiscoverySource("src", "来源", "https://example.test/column")
        pages = {
            source.url: COLUMN_HTML,
            "https://www.jintiankansha.com/t/new1": "<html><body>2026-09-08 08:30</body></html>",
            "https://www.jintiankansha.com/t/old1": "<html><body>2026-09-07 23:30</body></html>",
        }
        now = datetime(2026, 9, 8, 9, 0, tzinfo=SHANGHAI)
        with patch.dict("os.environ", {"READING_NOT_BEFORE": "2026-09-08T00:00:00+08:00"}, clear=False):
            with patch("reading_discovery._fetch_html", side_effect=lambda url: pages[url]):
                rows = discover_column(source, now=now, max_age_hours=36)
        self.assertEqual([row.title for row in rows], ["今天文章"])

    def test_refresh_preserves_ready_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            inbox = Path(tmp) / "inbox.json"
            source = DiscoverySource("src", "来源", "https://example.test/column")
            discovery = SourceDiscovery(inbox_path=inbox, sources=(source,))
            ready = {
                "id": "discovery-old", "source_id": "src", "source": "来源", "title": "A",
                "detail_url": "https://example.test/t/a", "published_at": "2026-09-02T08:00:00+08:00",
                "status": "ready", "ready_item_id": "item-a", "discovered_at": "x",
            }
            inbox.write_text(json.dumps([ready], ensure_ascii=False), encoding="utf-8")
            with patch("reading_discovery.discover_column", return_value=[]):
                discovery.refresh()
            saved = json.loads(inbox.read_text(encoding="utf-8"))
            self.assertEqual(saved[0]["status"], "ready")
            self.assertEqual(saved[0]["ready_item_id"], "item-a")


if __name__ == "__main__":
    unittest.main()


class _Review:
    def __init__(self, decision, reason):
        self.decision = decision
        self.reason = reason


class _Reviewer:
    def review(self, rows):
        return {row.id: _Review("exclude", "广告") for row in rows}


class ReadingDiscoveryReviewTests(unittest.TestCase):
    def test_excluded_ad_is_persisted_but_not_discovered(self):
        with tempfile.TemporaryDirectory() as tmp:
            inbox = Path(tmp) / "inbox.json"
            source = DiscoverySource("jiyichengzai", "记忆承载", "https://example.test/column")
            discovery = SourceDiscovery(inbox_path=inbox, sources=(source,), reviewer=_Reviewer())
            article = __import__('reading_discovery').DiscoveredArticle(
                "ad1", "jiyichengzai", "记忆承载", "广告标题", "u", "2026-09-02T08:00:00+08:00"
            )
            with patch("reading_discovery.discover_column", return_value=[article]):
                result = discovery.refresh()
            self.assertEqual(result["discovered"], [])
            saved = json.loads(inbox.read_text(encoding="utf-8"))
            self.assertEqual(saved[0]["status"], "excluded_ad")


class ReadingDiscoveryConcurrencyTests(unittest.TestCase):
    def test_concurrent_ready_updates_are_serialized_without_lost_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            inbox = Path(tmp) / "inbox.json"
            rows = [
                {
                    "id": f"discovery-{i}", "source_id": "src", "source": "??", "title": f"A{i}",
                    "detail_url": f"https://example.test/t/{i}", "published_at": f"2026-09-02T08:{i:02d}:00+08:00",
                    "status": "pending_body", "ready_item_id": "", "discovered_at": "x",
                }
                for i in range(20)
            ]
            inbox.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
            discovery = SourceDiscovery(inbox_path=inbox, sources=())
            barrier = threading.Barrier(len(rows))
            errors = []

            def mark(i):
                try:
                    barrier.wait()
                    discovery.mark_ready_by_id(f"discovery-{i}", f"item-{i}")
                except Exception as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=mark, args=(i,)) for i in range(len(rows))]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            self.assertEqual(errors, [])
            saved = {row["id"]: row for row in json.loads(inbox.read_text(encoding="utf-8"))}
            for i in range(len(rows)):
                self.assertEqual(saved[f"discovery-{i}"]["status"], "ready")
                self.assertEqual(saved[f"discovery-{i}"]["ready_item_id"], f"item-{i}")
            self.assertEqual(list(Path(tmp).glob(".inbox.json.*.tmp")), [])
