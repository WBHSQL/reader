from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from reading_app import NoopWriter, ReadingItem, ReadingService, ReadingStore, build_reading_provider


class ReadingAppTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "items.json"
        self.path.write_text(json.dumps([{
            "id": "a", "title": "T", "source": "S", "background": "B", "question": "Q",
            "author_reasoning": "SECRET R", "author_conclusion": "SECRET C", "url": "https://example.com/a"
        }], ensure_ascii=False), encoding="utf-8")
        self.writer = NoopWriter()
        self.article = "这是完整原文。" * 100
        self.calls = []
        self.cleaned = []

        def comparer(item, first, second, article):
            self.calls.append((item.id, first, second, article))
            return "自然的对照结果"

        self.service = ReadingService(
            ReadingStore(self.path), self.writer, comparer,
            article_loader=lambda item: self.article,
            article_cleanup=lambda item: self.cleaned.append(item.id),
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_blind_payload_hides_author_answer(self):
        payload = self.service.get_item("a")
        text = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("author_reasoning", payload)
        self.assertNotIn("SECRET", text)

    def test_first_answer_reveals_only_article_and_writes_nothing(self):
        result = self.service.submit_answer("a", "第一判断")
        self.assertEqual(set(result), {"session_token"})
        self.assertEqual(self.writer.contents, [])
        article = self.service.get_article(result["session_token"])
        self.assertEqual(article["body"], self.article)
        self.assertNotIn("author_reasoning", result)
        self.assertNotIn("author_conclusion", result)

    def test_ai_cannot_run_before_second_judgment(self):
        result = self.service.submit_answer("a", "第一判断")
        token = result["session_token"]
        with self.assertRaises(ValueError):
            self.service.generate_comparison(token)
        self.assertEqual(self.calls, [])

        self.service.submit_reflection(token, "看完后的判断")
        comparison = self.service.generate_comparison(token)
        self.assertEqual(comparison, "自然的对照结果")
        self.assertEqual(self.calls[0][1:], ("第一判断", "看完后的判断", self.article))

    def test_second_judgment_still_does_not_write_flomo(self):
        token = self.service.submit_answer("a", "第一判断")["session_token"]
        self.service.submit_reflection(token, "看完后的判断")
        self.assertEqual(self.writer.contents, [])

    def test_finalize_writes_one_reader_memo_without_raw_article(self):
        token = self.service.submit_answer("a", "第一判断")["session_token"]
        self.service.submit_reflection(token, "看完后的判断")
        self.service.generate_comparison(token)
        self.service.finalize(token)
        self.service.finalize(token)

        self.assertEqual(len(self.writer.contents), 1)
        memo = self.writer.contents[0]
        self.assertTrue(memo.startswith("#Reader\n"))
        self.assertEqual(memo.count("#"), 1)
        self.assertIn("先知道这些", memo)
        self.assertIn("B", memo)
        self.assertIn("当时在问", memo)
        self.assertIn("Q", memo)
        self.assertIn("第一判断", memo)
        self.assertIn("看完后的判断", memo)
        self.assertIn("自然的对照结果", memo)
        self.assertIn("https://example.com/a", memo)
        self.assertNotIn("SECRET", memo)
        self.assertNotIn(self.article, memo)
        self.assertEqual(self.cleaned, ["a"])
        self.assertEqual(self.service.list_items(), [])
        self.assertIn("a", ReadingStore(self.path).completed_ids())
        self.assertEqual(ReadingStore(self.path).get("a").title, "T")

    def test_comparison_failure_stays_unfinished_and_writes_nothing(self):
        def broken_comparer(*_args):
            raise RuntimeError("temporary model failure")
        service = ReadingService(
            ReadingStore(self.path), self.writer, broken_comparer,
            article_loader=lambda item: self.article,
        )
        token = service.submit_answer("a", "第一判断")["session_token"]
        service.submit_reflection(token, "看完后的判断")
        with self.assertRaisesRegex(RuntimeError, "temporary model failure"):
            service.generate_comparison(token)
        with self.assertRaisesRegex(ValueError, "AI comparison must succeed"):
            service.finalize(token)
        self.assertEqual(self.writer.contents, [])
        self.assertEqual(len(service.list_items()), 1)

    def test_import_url_appends_prepared_item(self):
        prepared = ReadingItem(
            id="imported", title="Imported", source="Example", background="B2", question="Q2",
            author_reasoning="R2", author_conclusion="C2", url="https://example.com/b"
        )
        service = ReadingService(ReadingStore(self.path), self.writer, preparer=lambda url: prepared)
        payload = service.import_url("https://example.com/b")
        self.assertEqual(payload["id"], "imported")
        self.assertEqual(service.get_item("imported")["title"], "Imported")

    def test_reading_provider_allows_task_specific_effort_override(self):
        class FakeCodex:
            def __init__(self, *, model, effort, timeout_seconds, **kwargs):
                self.model = model
                self.effort = effort
                self.timeout_seconds = timeout_seconds
                self.kwargs = kwargs

        with patch("llm_provider.CodexCliProvider", FakeCodex):
            provider = build_reading_provider(effort_override="high")
        self.assertEqual(provider.effort, "high")


if __name__ == "__main__":
    unittest.main()
