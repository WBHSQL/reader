import json
from pathlib import Path
import tempfile
import unittest

from reading_zhihu import ZhihuQualitySource


class FakeCli:
    def search(self, query, *, count=10):
        if "墨苍离" in query:
            return [{"AuthorName":"墨苍离","ContentType":"Answer","Title":"好文章 - 知乎",
                     "ContentText":"这是正文。" * 120,"VoteUpCount":200,
                     "Url":"https://www.zhihu.com/question/1/answer/2?utm_source=x"}]
        return []


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


class ReadingZhihuTests(unittest.TestCase):
    def test_refresh_adds_quality_item_without_persisting_raw_body(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); items=root/'items.json'; items.write_text('[]',encoding='utf-8')
            state=root/'seen.json'
            src=ZhihuQualitySource(items_path=items,state_path=state,provider=FakeProvider(),cli=FakeCli())
            result=src.refresh()
            self.assertEqual(len(result['added']),1)
            saved=state.read_text(encoding='utf-8')
            self.assertNotIn('这是正文', saved)
            self.assertIn('zhihu.com/question/1/answer/2', saved)


if __name__ == '__main__': unittest.main()
