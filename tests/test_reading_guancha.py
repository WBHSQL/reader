import json
import unittest

from reading_guancha import GuanchaAuthorClient


class FakeGuanchaClient(GuanchaAuthorClient):
    def __init__(self, pages, article_html):
        super().__init__()
        self.pages = pages
        self.article_html = article_html

    def _get(self, url):
        if "get-published-list" in url:
            page = 2 if "page_no=2" in url else 1
            payload = {"code": 0, "data": {"items": self.pages.get(page, [])}}
            return url, json.dumps(payload, ensure_ascii=False).encode("utf-8")
        return url, self.article_html.encode("utf-8")


class GuanchaAuthorClientTests(unittest.TestCase):
    def test_resolves_exact_qingbian_article(self):
        pages = {1: [{"id": 1727320, "title": "你的钱花在刀刃上了吗？", "user_nick": "请辩",
                       "post_url": "https://user.guancha.cn/main/content?id=1727320"}]}
        html = "<div class='article-txt-content'><p>" + ("正文内容。" * 80) + "</p></div>"
        article = FakeGuanchaClient(pages, html).resolve("你的钱花在刀刃上了吗？")
        self.assertIsNotNone(article)
        self.assertEqual(article.publisher, "请辩")
        self.assertIn("正文内容", article.body)

    def test_rejects_same_title_from_wrong_publisher(self):
        pages = {1: [{"id": 1, "title": "同一标题", "user_nick": "别的作者", "post_url": "https://example.com/1"}]}
        html = "<div class='article-txt-content'>" + ("正文" * 100) + "</div>"
        self.assertIsNone(FakeGuanchaClient(pages, html).resolve("同一标题"))

    def test_extracts_only_article_text_container(self):
        html = """<div class='article-content'>导航
        <div class='article-txt-content'><p>第一段</p><script>noise()</script><p>第二段</p></div>
        <aside>侧栏</aside></div>"""
        body = GuanchaAuthorClient._extract_body(html)
        self.assertIn("第一段", body)
        self.assertIn("第二段", body)
        self.assertNotIn("noise", body)
        self.assertNotIn("侧栏", body)


if __name__ == '__main__':
    unittest.main()
