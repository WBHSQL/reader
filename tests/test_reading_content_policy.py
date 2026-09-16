import unittest

from reading_content_policy import JiyichengzaiAdReviewer
from reading_discovery import DiscoveredArticle


class FakeProvider:
    def answer(self, _prompt: str) -> str:
        return '''Lai，[
          {"title":"旧稿","decision":"exclude","reason":"更早已有同稿"},
          {"title":"原创","decision":"keep","reason":"只有同热点讨论"}
        ]'''


class ReadingContentPolicyTests(unittest.TestCase):
    def test_review_is_strict_about_reposts_but_keeps_same_topic(self):
        reviewer = JiyichengzaiAdReviewer(FakeProvider())
        rows = [
            DiscoveredArticle("a", "jiyichengzai", "记忆承载", "旧稿", "u1", "2026-09-02T08:00:00+08:00"),
            DiscoveredArticle("b", "jiyichengzai", "记忆承载", "原创", "u2", "2026-09-02T08:00:00+08:00"),
        ]
        result = reviewer.review(rows)
        self.assertEqual(result["a"].decision, "exclude")
        self.assertEqual(result["b"].decision, "keep")
