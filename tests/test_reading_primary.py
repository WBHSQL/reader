from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from reading_app import ReadingItem
from reading_primary import JiyichengzaiPrimarySource


class ReadingPrimaryTests(unittest.TestCase):
    def test_bootstrap_imports_latest_public_day_and_skips_paid_body(self):
        rows = [
            {"title": "Paid", "date": "2026-08-31", "urlPath": "/paid/", "sourceBuckets": ["paid"], "isPaid": True},
            {"title": "Main", "date": "2026-08-30", "urlPath": "/main/", "sourceBuckets": ["jiyichengzai"], "isPaid": False},
            {"title": "Three", "date": "2026-08-30", "urlPath": "/three/", "sourceBuckets": ["jiyichengzai-3"], "isPaid": False},
            {"title": "Old", "date": "2026-08-29", "urlPath": "/old/", "sourceBuckets": ["jiyichengzai"], "isPaid": False},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            items, state = root / "items.json", root / "seen.json"
            items.write_text("[]", encoding="utf-8")
            source = JiyichengzaiPrimarySource(items_path=items, state_path=state, provider=object())
            prepared_main = ReadingItem(id="main-id", title="Model title", source="x", background="B",
                                        question="Q", author_reasoning="R", author_conclusion="C")
            prepared_three = ReadingItem(id="three-id", title="Model title", source="x", background="B",
                                         question="Q", author_reasoning="R", author_conclusion="C")
            with patch("reading_primary._load_json", return_value={"articles": rows}), \
                 patch("reading_primary.extract_wechat_url", side_effect=["https://mp.weixin.qq.com/s/a", "https://mp.weixin.qq.com/s/b"]), \
                 patch("reading_primary.fetch_article", return_value=("ARTICLE BODY", "archive")), \
                 patch("reading_primary.prepare_item", side_effect=[prepared_main, prepared_three]):
                result = source.refresh()

            self.assertEqual([x["title"] for x in result["added"]], ["Main", "Three"])
            self.assertEqual(result["paid"][0]["title"], "Paid")
            self.assertEqual(result["baseline_date"], "2026-08-30")
            saved = json.loads(items.read_text(encoding="utf-8"))
            self.assertEqual(saved[0]["title"], "Main")
            self.assertEqual(saved[0]["source"], "微信公众号 · 记忆承载")
            self.assertEqual(saved[0]["url"], "https://mp.weixin.qq.com/s/a")
            self.assertEqual(saved[1]["source"], "微信公众号 · 记忆承载3")
            state_text = state.read_text(encoding="utf-8")
            self.assertNotIn("ARTICLE BODY", state_text)
            self.assertNotIn("/old/", state_text)


if __name__ == "__main__":
    unittest.main()
