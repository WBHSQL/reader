from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from reading_article_cache import delete_article, get_article, put_article


class ReadingArticleCacheTests(unittest.TestCase):
    def test_body_is_ephemeral_and_separate_from_items(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.dict(os.environ, {"READING_ARTICLE_CACHE_DIR": td}, clear=False):
                put_article("item-abc", "完整正文")
                self.assertEqual(get_article("item-abc"), "完整正文")
                self.assertTrue((Path(td) / "item-abc.txt").exists())
                delete_article("item-abc")
                self.assertEqual(get_article("item-abc"), "")


if __name__ == "__main__":
    unittest.main()
