from __future__ import annotations

from dataclasses import dataclass
import json
from urllib import parse, request

from bs4 import BeautifulSoup

from reading_exact import titles_match

BASE = "https://user.guancha.cn"
DEFAULT_QINGBIAN_UID = "242783"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/152.0.0.0 Safari/537.36"


class GuanchaUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class GuanchaArticle:
    title: str
    publisher: str
    url: str
    body: str
    published_at: str = ""


class GuanchaAuthorClient:
    def __init__(self, *, uid: str = DEFAULT_QINGBIAN_UID, publisher: str = "请辩", timeout_seconds: int = 15) -> None:
        self.uid = str(uid)
        self.publisher = publisher
        self.timeout_seconds = int(timeout_seconds)

    def _get(self, url: str) -> tuple[str, bytes]:
        req = request.Request(url, headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"})
        try:
            with request.urlopen(req, timeout=self.timeout_seconds) as response:
                raw = response.read(2_000_001)
                final = response.geturl()
        except Exception as exc:
            raise GuanchaUnavailable(f"network_error:{type(exc).__name__}") from exc
        if len(raw) > 2_000_000:
            raise GuanchaUnavailable("response_too_large")
        return final, raw

    def _list_page(self, page: int) -> list[dict]:
        query = parse.urlencode({"page_no": int(page), "uid": self.uid, "isSelf": 0})
        _, raw = self._get(f"{BASE}/user/get-published-list?{query}")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            raise GuanchaUnavailable("invalid_list_json") from exc
        if payload.get("code") != 0:
            raise GuanchaUnavailable("list_api_error")
        rows = payload.get("data", {}).get("items", [])
        return rows if isinstance(rows, list) else []

    def _find(self, title: str) -> dict | None:
        for page in (1, 2):
            for row in self._list_page(page):
                if str(row.get("user_nick", "")).strip() != self.publisher:
                    continue
                if titles_match(title, str(row.get("title", ""))):
                    return row
        return None

    @staticmethod
    def _extract_body(html: str) -> str:
        soup = BeautifulSoup(html, "lxml")
        node = soup.select_one(".article-txt-content")
        if node is None:
            return ""
        for tag in node(["script", "style", "noscript", "svg", "form"]):
            tag.decompose()
        return "\n\n".join(x.strip() for x in node.stripped_strings if x.strip())

    def resolve(self, title: str) -> GuanchaArticle | None:
        row = self._find(title.strip())
        if row is None:
            return None
        url = str(row.get("post_url", "")).strip()
        if not url:
            article_id = str(row.get("id", "")).strip()
            if not article_id:
                return None
            url = f"{BASE}/main/content?id={article_id}"
        final, raw = self._get(url)
        body = self._extract_body(raw.decode("utf-8", errors="replace"))
        if not body:
            return None
        return GuanchaArticle(
            title=str(row.get("title", title)).strip(),
            publisher=self.publisher,
            url=final,
            body=body,
        )
