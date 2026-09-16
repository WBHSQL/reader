import json
from pathlib import Path
import tempfile
import unittest

from reading_wechat import BodyCandidate, BodyResolver, OriginalityVerifier, WeChatPendingPreparer


class FakeBridge:
    def resolve(self, _targets):
        return {"ok": True, "reason": "", "items": [{
            "target_id": "d1", "title": "当天文章", "source": "微信公众号 · 请辩",
            "url": "https://mp.weixin.qq.com/s/abc", "body": "正文内容。" * 180,
        }]}


class FakeProvider:
    def answer(self, prompt):
        if "STAGE=UNDERSTANDING_MAP" in prompt:
            return json.dumps({
                "central_thesis": "core thesis",
                "neutral_core_problem": "core problem?",
                "argument_spine": ["step one", "step two"],
                "examples": [{"example": "case", "role": "support", "removable": True}],
                "title_role": "example",
                "title_role_reason": "title is only an example",
                "author_reasoning": "reasoning",
                "author_conclusion": "conclusion",
            })
        return json.dumps({
            "title": "model title", "topic": "judgment", "estimated_minutes": 5,
            "why_selected": "x", "background": "background",
            "question": "question?",
        })


class ReadingWeChatTests(unittest.TestCase):
    def test_pending_body_becomes_card_and_title_is_locked(self):
        with tempfile.TemporaryDirectory() as td:
            items = Path(td) / "items.json"
            items.write_text("[]", encoding="utf-8")
            src = WeChatPendingPreparer(items_path=items, provider=FakeProvider(), bridge=FakeBridge())
            result = src.refresh([{"id":"d1","source_id":"qingbian","source":"微信公众号 · 请辩","status":"pending_body"}])
            self.assertEqual(result["added"][0]["title"], "当天文章")
            saved = json.loads(items.read_text(encoding="utf-8"))
            self.assertEqual(saved[0]["title"], "当天文章")
            self.assertNotIn("正文内容", items.read_text(encoding="utf-8"))
    def test_browser_capture_uses_pending_title_and_does_not_store_raw_body(self):
        with tempfile.TemporaryDirectory() as td:
            items = Path(td) / "items.json"
            items.write_text("[]", encoding="utf-8")
            src = WeChatPendingPreparer(items_path=items, provider=FakeProvider(), bridge=FakeBridge())
            target = {"id":"d2","title":"当天文章","source":"微信公众号 · 请辩"}
            capture = {"title":"当天文章","url":"https://mp.weixin.qq.com/s/real","body":"浏览器真实正文。" * 200}
            result = src.prepare_capture(target, capture)
            self.assertEqual(result["added"][0]["title"], "当天文章")
            saved = items.read_text(encoding="utf-8")
            self.assertNotIn("浏览器真实正文", saved)
            self.assertIn("https://mp.weixin.qq.com/s/real", saved)


class ResolverProvider:
    def __init__(self, name, candidates=None, error=None, calls=None):
        self.name = name
        self.candidates = candidates or []
        self.error = error
        self.calls = calls if calls is not None else []

    def resolve(self, target):
        self.calls.append(self.name)
        if self.error:
            raise RuntimeError(self.error)
        return self.candidates


class OriginalityVerifierTests(unittest.TestCase):
    def setUp(self):
        self.target = {"id": "t1", "title": "完整文章", "source": "微信公众号 · 请辩"}
        self.verifier = OriginalityVerifier(min_chars=300)

    def test_rejects_short_or_summary_body(self):
        short = BodyCandidate("t1", "完整文章", "请辩", "https://example/a", "太短", "x")
        self.assertEqual(self.verifier.check(self.target, short).reason, "body_too_short")
        summary = BodyCandidate("t1", "完整文章", "请辩", "https://example/a", "本文摘要。" + "摘要内容" * 90 + "点击阅读原文", "x")
        self.assertEqual(self.verifier.check(self.target, summary).reason, "summary_or_truncated")

    def test_rejects_title_mismatch(self):
        candidate = BodyCandidate("t1", "另一篇文章", "请辩", "https://example/a", "正文。" * 300, "x")
        self.assertEqual(self.verifier.check(self.target, candidate).reason, "title_mismatch")

    def test_corroborated_high_similarity_is_preferred(self):
        body = ("这是完整正文的一段内容，用于验证多副本一致性。" * 80)
        a = BodyCandidate("t1", "完整文章", "请辩", "https://a", body, "a")
        b = BodyCandidate("t1", "完整文章", "请辩", "https://b", body + "\n", "b")
        chosen, verdict = self.verifier.choose(self.target, [a, b])
        self.assertIsNotNone(chosen)
        self.assertTrue(verdict.corroborated)
        self.assertGreaterEqual(verdict.similarity, 0.985)

    def test_resolver_keeps_trying_after_provider_failure(self):
        calls = []
        good = BodyCandidate("t1", "完整文章", "请辩", "https://good", "可验证的完整正文。" * 120, "good")
        resolver = BodyResolver([
            ResolverProvider("first", error="down", calls=calls),
            ResolverProvider("second", candidates=[good], calls=calls),
        ], verifier=self.verifier)
        candidate, verdict, errors = resolver.resolve_one(self.target)
        self.assertEqual(calls, ["first", "second"])
        self.assertEqual(candidate.url, "https://good")
        self.assertTrue(verdict.accepted)
        self.assertEqual(errors[0]["provider"], "first")

    def test_resolver_stops_after_first_verified_candidate(self):
        calls = []
        good = BodyCandidate("t1", "完整文章", "请辩", "https://good", "可验证的完整正文。" * 120, "good")
        resolver = BodyResolver([
            ResolverProvider("empty", calls=calls),
            ResolverProvider("good", candidates=[good], calls=calls),
            ResolverProvider("slow-fallback", error="should not run", calls=calls),
        ], verifier=self.verifier)
        candidate, verdict, errors = resolver.resolve_one(self.target)
        self.assertEqual(calls, ["empty", "good"])
        self.assertEqual(candidate.url, "https://good")
        self.assertTrue(verdict.accepted)
        self.assertEqual(errors, [])
