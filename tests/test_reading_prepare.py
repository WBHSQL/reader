from __future__ import annotations
import json
from pathlib import Path
import tempfile
import unittest
from reading_prepare import append_item, prepare_item


class FakeProvider:
    def __init__(self):
        self.prompts = []

    def answer(self, prompt):
        self.prompts.append(prompt)
        if "STAGE=UNDERSTANDING_MAP" in prompt:
            return json.dumps({
                "central_thesis": "不同经验背景会形成不同判断，处理分歧时应先理解这种差异",
                "neutral_core_problem": "经验范围不同的人产生冲突时，应该如何处理彼此无法统一的判断？",
                "argument_spine": ["提出分歧", "用多个案例解释经验差异", "回到如何相处"],
                "examples": [{"example": "英语", "role": "说明统一答案未必存在", "removable": True}],
                "title_role": "example",
                "title_role_reason": "删去英语案例，主论证仍成立",
                "author_reasoning": "作者先看分歧来源，再比较经验范围，最后讨论沟通方式",
                "author_conclusion": "不必强迫不同处境的人活成同一种样子",
            }, ensure_ascii=False)
        return json.dumps({
            "title": "测试文章", "topic": "判断", "estimated_minutes": 5,
            "why_selected": "有明确判断链", "background": "只给背景",
            "question": "仅看上面的信息，你怎么看？",
        }, ensure_ascii=False)


class ContextProvider:
    def __init__(self):
        self.delegate = FakeProvider()
        self.query = None
        self.prompt = None

    def answer_for(self, prompt, query):
        self.query = query
        self.prompt = prompt
        return self.delegate.answer(prompt)


class ReadingPrepareTests(unittest.TestCase):
    def test_prepare_separates_blind_and_reveal_fields(self):
        provider = FakeProvider()
        item = prepare_item("很长的一篇文章", provider, source="测试", url="https://example.com/a")
        self.assertEqual(item.background, "只给背景")
        self.assertEqual(item.author_conclusion, "不必强迫不同处境的人活成同一种样子")
        self.assertTrue(item.id.startswith("item-"))
        self.assertEqual(len(provider.prompts), 2)

    def test_append_writes_item(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "items.json"
            item = prepare_item("文章", FakeProvider(), source="测试")
            append_item(path, item)
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data[0]["author_reasoning"], "作者先看分歧来源，再比较经验范围，最后讨论沟通方式")

    def test_source_understanding_is_separate_from_contextual_card_generation(self):
        provider = ContextProvider()
        prepare_item("文章", provider, source="测试", context_query="具体标题")
        self.assertEqual(provider.query, "具体标题")
        self.assertIn("neutral_core_problem", provider.prompt)
        self.assertIn("隐去文章标题后仍然应该成立", provider.prompt)
        self.assertIn("title_role 是 example 或 hook", provider.prompt)
        self.assertEqual(len(provider.delegate.prompts), 2)
        self.assertIn("标题只是一个不可信提示", provider.delegate.prompts[0])
        self.assertIn("删除测试", provider.delegate.prompts[0])


if __name__ == "__main__":
    unittest.main()
