from bs4 import BeautifulSoup
import unittest

from reading_sogou import SogouWeixinClient


class ReadingSogouTests(unittest.TestCase):
    def test_extracts_concatenated_wechat_url(self):
        html = """<script>
        var url=''; url += 'https://mp.'; url += 'weixin.qq.com/s?x=1';
        url += '&timestamp=2@'; window.location.replace(url)
        </script>"""
        self.assertEqual(
            SogouWeixinClient._extract_real_url(html),
            "https://mp.weixin.qq.com/s?x=1&timestamp=2",
        )

    def test_exact_result_requires_matching_publisher(self):
        html = """<ul class='news-list'>
        <li><h3><a href='/bad'>同一标题</a></h3><span class='all-time-y2'>别的号</span></li>
        <li><h3><a href='/good'>同一标题</a></h3><span class='all-time-y2'>请辩</span></li>
        </ul>"""
        anchor, pub, _ = SogouWeixinClient._exact_result(html, "同一标题", "请辩")
        self.assertEqual(anchor["href"], "/good")
        self.assertEqual(pub, "请辩")
