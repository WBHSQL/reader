from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http.cookiejar import CookieJar
import re
from threading import RLock
import time
from urllib import parse, request

from bs4 import BeautifulSoup

from reading_exact import normalize_title, titles_match

BASE = "https://weixin.sogou.com"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/152.0.0.0 Safari/537.36"
)


class SogouUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class SogouArticle:
    title: str
    publisher: str
    published_at: str
    url: str
    body: str


def title_compatible(expected: str, actual: str) -> bool:
    if titles_match(expected, actual):
        return True
    a, b = normalize_title(expected), normalize_title(actual)
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    return len(short) >= 8 and short in long and len(short) / max(1, len(long)) >= 0.68


class SogouWeixinClient:
    def __init__(self, *, min_interval_seconds: float = 2.0, timeout_seconds: int = 20) -> None:
        self.min_interval_seconds = max(0.5, float(min_interval_seconds))
        self.timeout_seconds = int(timeout_seconds)
        self._jar = CookieJar()
        self._opener = request.build_opener(request.HTTPCookieProcessor(self._jar))
        self._lock = RLock()
        self._last_sogou_request = 0.0
        self._warmed = False
        self._cache: dict[tuple[str, str], SogouArticle | None] = {}

    def _headers(self, referer: str = BASE + "/") -> dict[str, str]:
        return {
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
            "Referer": referer,
        }

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_sogou_request
        if elapsed < self.min_interval_seconds:
            time.sleep(self.min_interval_seconds - elapsed)
        self._last_sogou_request = time.monotonic()

    def _get(self, url: str, *, referer: str | None = None, sogou: bool = False) -> tuple[str, str]:
        if sogou:
            self._throttle()
        req = request.Request(url, headers=self._headers(referer or BASE + "/"))
        try:
            with self._opener.open(req, timeout=self.timeout_seconds) as response:
                raw = response.read(5_000_001)
                final_url = response.geturl()
        except Exception as exc:
            raise SogouUnavailable(f"network_error:{type(exc).__name__}") from exc
        if len(raw) > 5_000_000:
            raise SogouUnavailable("response_too_large")
        return final_url, raw.decode("utf-8", errors="replace")

    @staticmethod
    def _is_blocked(final_url: str, html: str) -> bool:
        low = html.lower()
        return (
            "antispider" in final_url
            or "antispider" in low
            or "seccoderight" in low
            or "验证码" in html
        )

    def _warm(self) -> None:
        if self._warmed:
            return
        self._get(BASE + "/", sogou=True)
        self._warmed = True

    @staticmethod
    def _publisher_from_target(source: str) -> str:
        value = str(source or "").replace("微信公众号", "").strip()
        if "·" in value:
            value = value.split("·")[-1].strip()
        return value

    @staticmethod
    def _epoch_to_iso(value: str) -> str:
        try:
            return datetime.fromtimestamp(int(value), tz=timezone(timedelta(hours=8))).isoformat()
        except Exception:
            return ""

    @staticmethod
    def _extract_time(li) -> str:
        script = li.find("script")
        text = script.get_text(" ", strip=True) if script else ""
        match = re.search(r"timeConvert\(['\"]?(\d{9,12})", text)
        return SogouWeixinClient._epoch_to_iso(match.group(1)) if match else ""

    @staticmethod
    def _exact_result(html: str, title: str, publisher: str):
        soup = BeautifulSoup(html, "lxml")
        fallback = None
        for li in soup.select("ul.news-list li"):
            anchor = li.select_one("h3 a[href]")
            if anchor is None or not titles_match(title, anchor.get_text(" ", strip=True)):
                continue
            pub_node = li.select_one(".all-time-y2")
            pub = pub_node.get_text(" ", strip=True) if pub_node else ""
            row = (anchor, pub, SogouWeixinClient._extract_time(li))
            if not publisher:
                return row
            if pub == publisher:
                return row
            if not pub and fallback is None:
                fallback = row
        return fallback

    @staticmethod
    def _extract_real_url(html: str) -> str:
        parts: list[str] = []
        for match in re.finditer(r"url\s*\+=\s*(['\"])(.*?)\1", html, re.S):
            frag = match.group(2)
            if not parts and not frag.startswith(("http://", "https://")):
                continue
            parts.append(frag)
        value = "".join(parts).replace("@", "").strip()
        if value.startswith(("http://", "https://")) and "mp.weixin.qq.com" in value:
            return value
        return ""

    @staticmethod
    def _article_from_html(html: str, fallback_title: str, fallback_publisher: str) -> tuple[str, str, str]:
        soup = BeautifulSoup(html, "lxml")
        title_node = soup.select_one("#activity-name")
        title = title_node.get_text(" ", strip=True) if title_node else fallback_title
        publisher_node = soup.select_one("#js_name")
        publisher = publisher_node.get_text(" ", strip=True) if publisher_node else fallback_publisher
        body_node = soup.select_one("#js_content")
        if body_node is None:
            return title, publisher, ""
        for tag in body_node(["script", "style", "noscript", "svg", "form"]):
            tag.decompose()
        body = "\n\n".join(x.strip() for x in body_node.stripped_strings if x.strip())
        return title, publisher, body

    def _search_page(self, query_text: str, page: int = 1) -> tuple[str, str]:
        params = {"type": "2", "query": query_text, "ie": "utf8"}
        if page > 1:
            params["page"] = str(page)
        url = BASE + "/weixin?" + parse.urlencode(params)
        final, html = self._get(url, sogou=True)
        if self._is_blocked(final, html):
            raise SogouUnavailable("blocked_or_captcha")
        return url, html

    def _find_result(self, title: str, publisher: str):
        for page in (1, 2, 3):
            search_url, html = self._search_page(title, page)
            result = self._exact_result(html, title, publisher)
            if result is not None:
                anchor, pub, published_at = result
                return search_url, parse.urljoin(BASE, str(anchor.get("href", ""))), pub, published_at
        if publisher:
            search_url, html = self._search_page(f"{title} {publisher}", 1)
            result = self._exact_result(html, title, publisher)
            if result is not None:
                anchor, pub, published_at = result
                return search_url, parse.urljoin(BASE, str(anchor.get("href", ""))), pub, published_at
        return None

    def _follow(self, search_url: str, jump_url: str) -> str:
        final, html = self._get(jump_url, referer=search_url, sogou=True)
        if self._is_blocked(final, html):
            raise SogouUnavailable("blocked_or_captcha")
        real = self._extract_real_url(html)
        if not real:
            raise SogouUnavailable("redirect_parse_failed")
        return real

    def resolve(self, title: str, source: str) -> SogouArticle | None:
        publisher = self._publisher_from_target(source)
        key = (title.strip(), publisher)
        if key in self._cache:
            return self._cache[key]
        with self._lock:
            if key in self._cache:
                return self._cache[key]
            self._warm()
            found = self._find_result(title.strip(), publisher)
            if found is None:
                self._cache[key] = None
                return None
            search_url, jump_url, result_publisher, published_at = found
            real_url = self._follow(search_url, jump_url)
            final, article_html = self._get(real_url, referer=BASE + "/")
            article_title, article_publisher, body = self._article_from_html(
                article_html, title.strip(), result_publisher or publisher
            )
            if not body or not title_compatible(title, article_title):
                self._cache[key] = None
                return None
            actual_publisher = article_publisher or result_publisher or publisher
            if publisher and actual_publisher and publisher != actual_publisher:
                self._cache[key] = None
                return None
            article = SogouArticle(
                title=article_title,
                publisher=actual_publisher,
                published_at=published_at,
                url=final,
                body=body,
            )
            self._cache[key] = article
            return article
