from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class ContentReview:
    decision: str
    reason: str


def _extract_json_array(text: str) -> list[dict]:
    value = text.strip()
    start, end = value.find("["), value.rfind("]")
    if start < 0 or end <= start:
        raise ValueError("reviewer did not return a JSON array")
    parsed = json.loads(value[start:end + 1])
    if not isinstance(parsed, list):
        raise ValueError("reviewer JSON must be an array")
    return [row for row in parsed if isinstance(row, dict)]


class JiyichengzaiAdReviewer:
    """Review newly discovered memory-carrier posts before they reach Reader."""
    TARGET_SOURCE_IDS = {"jiyichengzai", "jiyichengzai3"}

    def __init__(self, provider) -> None:
        self.provider = provider

    def review(self, articles: Iterable) -> dict[str, ContentReview]:
        targets = [article for article in articles if article.source_id in self.TARGET_SOURCE_IDS]
        if not targets:
            return {}
        lines = []
        for index, article in enumerate(targets, 1):
            lines.append(f"{index}. {article.title} | 发布时间 {article.published_at}")
        prompt = """你是微信公众号内容真实性审查器。使用实时网页搜索核验下面“记忆承载/记忆承载3”的新标题。
已知作者正常每天固定写两篇正文，偶尔会额外插入广告。把“每天两篇”作为软约束：出现额外条目时提高广告审查强度，但不要为了凑数量误删原创。
只识别广告、软文、转载、旧稿复用；不要因为别人讨论过同一个热点就排除。
严格规则：
- exclude：能找到更早的同标题或明显同一篇正文复刻；或有明确商业推广、购买、课程、产品导流证据。
- keep：只是同一新闻/热点被别人讨论过，但没有证据是同一篇稿件。
- 不确定时 keep。
只返回 JSON 数组，每项 {"title":"...","decision":"keep|exclude","reason":"..."}。

待审标题：
""" + "\n".join(lines)
        rows = _extract_json_array(self.provider.answer(prompt))
        return self._normalize(targets, rows)

    @staticmethod
    def _normalize(targets, rows: list[dict]) -> dict[str, ContentReview]:
        by_title = {}
        for row in rows:
            title = str(row.get("title", "")).strip()
            decision = str(row.get("decision", "keep")).strip().lower()
            reason = str(row.get("reason", "")).strip()
            if decision not in {"keep", "exclude"}:
                decision = "keep"
            if title:
                by_title[title] = ContentReview(decision, reason)
        result = {}
        for article in targets:
            result[article.id] = by_title.get(
                article.title,
                ContentReview("keep", "审查结果不确定，按保留处理"),
            )
        return result

