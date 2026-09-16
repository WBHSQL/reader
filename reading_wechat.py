from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from difflib import SequenceMatcher
import hashlib
import os
from pathlib import Path
import re
import sqlite3
import subprocess
from typing import Any, Protocol
from urllib import request
from urllib.parse import quote_plus, urljoin

from bs4 import BeautifulSoup

from reading_exact import normalize_body, titles_match
from reading_prepare import append_item, fetch_article, prepare_item
from reading_weread import WeReadClient, account_name, mp_link_from_review
from reading_sogou import SogouUnavailable, SogouWeixinClient
from reading_guancha import GuanchaAuthorClient

READER_HOME = Path(os.environ.get("READER_HOME", str(Path.home() / ".reader")))
DEFAULT_VENDOR = READER_HOME / "vendors" / "weread-mp"
DEFAULT_MPRSS_DB = READER_HOME / "state" / "we_mprss.db"
DEFAULT_CHROME_PROFILE = READER_HOME / "runtime" / "chrome-profile"
TARGET_SOURCE_IDS = {"jiyichengzai", "jiyichengzai3", "qingbian"}


@dataclass(frozen=True)
class BodyCandidate:
    target_id: str
    title: str
    source: str
    url: str
    body: str
    provider: str
    published_at: str = ""


@dataclass(frozen=True)
class Verification:
    accepted: bool
    reason: str
    sha256: str = ""
    paragraph_hashes: tuple[str, ...] = ()
    corroborated: bool = False
    similarity: float = 0.0


class OriginalityVerifier:
    def __init__(self, *, min_chars: int = 500, corroboration_threshold: float = 0.985) -> None:
        self.min_chars = min_chars
        self.corroboration_threshold = corroboration_threshold

    @staticmethod
    def _paragraph_hashes(body: str) -> tuple[str, ...]:
        parts = [p.strip() for p in re.split(r"\n\s*\n|(?<=[。！？!?])\s*\n", normalize_body(body)) if len(p.strip()) >= 30]
        return tuple(hashlib.sha256(p.encode("utf-8")).hexdigest() for p in parts)

    @staticmethod
    def _looks_truncated(body: str) -> bool:
        text = normalize_body(body)
        markers = ("本文摘要", "内容摘要", "阅读全文", "点击阅读原文", "以下为摘要")
        return len(text) < 1400 and any(marker in text for marker in markers)

    def check(self, target: dict[str, Any], candidate: BodyCandidate) -> Verification:
        expected = str(target.get("title", "")).strip() or candidate.title.strip()
        if not titles_match(expected, candidate.title):
            return Verification(False, "title_mismatch")
        expected_source = re.sub(r"[\s·|/\-]+", "", str(target.get("source", "")).replace("微信公众号", ""))
        actual_source = re.sub(r"[\s·|/\-]+", "", candidate.source.replace("微信公众号", ""))
        if expected_source and actual_source and expected_source not in actual_source and actual_source not in expected_source:
            return Verification(False, "source_mismatch")
        body = normalize_body(candidate.body)
        if len(body) < self.min_chars:
            return Verification(False, "body_too_short")
        if self._looks_truncated(body):
            return Verification(False, "summary_or_truncated")
        expected_date = str(target.get("published_at", ""))[:10]
        actual_date = str(candidate.published_at or "")[:10]
        if expected_date and actual_date and expected_date != actual_date:
            return Verification(False, "publish_date_mismatch")
        if not candidate.url:
            return Verification(False, "url_missing")
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        return Verification(True, "strong_single_copy", digest, self._paragraph_hashes(body))

    def choose(self, target: dict[str, Any], candidates: list[BodyCandidate]) -> tuple[BodyCandidate | None, Verification]:
        valid: list[tuple[BodyCandidate, Verification]] = []
        last = Verification(False, "no_candidate")
        for candidate in candidates:
            verdict = self.check(target, candidate)
            last = verdict
            if verdict.accepted:
                valid.append((candidate, verdict))
        if not valid:
            return None, last
        for i, (left, left_v) in enumerate(valid):
            a = normalize_body(left.body)
            for right, _ in valid[i + 1:]:
                b = normalize_body(right.body)
                length_ratio = min(len(a), len(b)) / max(len(a), len(b))
                if length_ratio < 0.94:
                    continue
                similarity = SequenceMatcher(None, a, b, autojunk=False).ratio()
                if similarity >= self.corroboration_threshold:
                    chosen = left if len(a) >= len(b) else right
                    base = self.check(target, chosen)
                    return chosen, replace(base, reason="corroborated", corroborated=True, similarity=similarity)
        return valid[0]


class BodyProvider(Protocol):
    name: str
    def resolve(self, target: dict[str, Any]) -> list[BodyCandidate]: ...


class ArchiveProvider:
    name = "archive"

    def resolve(self, target: dict[str, Any]) -> list[BodyCandidate]:
        source_id = str(target.get("source_id", ""))
        if source_id not in {"jiyichengzai", "jiyichengzai3"}:
            return []
        from reading_primary import INDEX_URL, _archive_url, _load_json, extract_wechat_url
        payload = _load_json(INDEX_URL)
        rows = payload.get("articles", []) if isinstance(payload, dict) else []
        wanted_bucket = "jiyichengzai-3" if source_id == "jiyichengzai3" else "jiyichengzai"
        expected = str(target.get("title", ""))
        published = str(target.get("published_at", ""))[:10]
        found = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict) or row.get("isPaid"):
                continue
            if wanted_bucket not in [str(x) for x in row.get("sourceBuckets", [])]:
                continue
            if not titles_match(expected, str(row.get("title", ""))):
                continue
            if published and str(row.get("date", "")) and str(row.get("date", "")) != published:
                continue
            archive_url = _archive_url(str(row.get("urlPath", "")))
            article, _ = fetch_article(archive_url)
            url = extract_wechat_url(archive_url) or archive_url
            found.append(BodyCandidate(str(target.get("id", "")), expected, str(target.get("source", "")), url, article, self.name, str(row.get("date", ""))))
        return found


class WeMpRssProvider:
    name = "we-mp-rss"

    def __init__(self, db_path: Path = DEFAULT_MPRSS_DB) -> None:
        self.db_path = Path(db_path)

    def resolve(self, target: dict[str, Any]) -> list[BodyCandidate]:
        if not self.db_path.exists():
            return []
        con = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            cols = {row[1] for row in con.execute("pragma table_info(articles)")}
            required = {"title", "url"}
            if not required <= cols:
                return []
            fields = [x for x in ("title", "url", "content", "content_html", "description", "publish_time", "mp_id") if x in cols]
            sql = "select " + ",".join(fields) + " from articles where title = ? order by rowid desc limit 8"
            rows = con.execute(sql, (str(target.get("title", "")),)).fetchall()
            feed_names: dict[str, str] = {}
            feed_cols = {row[1] for row in con.execute("pragma table_info(feeds)")}
            if {"id", "mp_name"} <= feed_cols:
                feed_names = {str(r[0]): str(r[1] or "") for r in con.execute("select id,mp_name from feeds")}
            found = []
            for row in rows:
                raw = str(row["content"] or "") if "content" in row.keys() else ""
                if not raw and "content_html" in row.keys() and row["content_html"]:
                    raw = "\n\n".join(BeautifulSoup(str(row["content_html"]), "lxml").stripped_strings)
                if not raw:
                    continue
                source = str(target.get("source", ""))
                if "mp_id" in row.keys() and row["mp_id"] is not None:
                    source = feed_names.get(str(row["mp_id"]), source) or source
                pub = ""
                if "publish_time" in row.keys() and row["publish_time"]:
                    try: pub = datetime.fromtimestamp(int(row["publish_time"]), tz=timezone.utc).isoformat()
                    except Exception: pass
                found.append(BodyCandidate(str(target.get("id", "")), str(row["title"]), source, str(row["url"] or ""), raw, self.name, pub))
            return found
        finally:
            con.close()


class SogouWeixinProvider:
    name = "sogou-weixin"

    def __init__(self, client: SogouWeixinClient | None = None) -> None:
        self.client = client or SogouWeixinClient()

    def resolve(self, target: dict[str, Any]) -> list[BodyCandidate]:
        title = str(target.get("title", "")).strip()
        source = str(target.get("source", "")).strip()
        if not title or not source:
            return []
        article = self.client.resolve(title, source)
        if article is None:
            return []
        return [BodyCandidate(
            str(target.get("id", "")), title, source, article.url,
            article.body, self.name, article.published_at,
        )]


class GuanchaQingbianProvider:
    name = "guancha-qingbian"

    def __init__(self, client: GuanchaAuthorClient | None = None) -> None:
        self.client = client or GuanchaAuthorClient()

    def resolve(self, target: dict[str, Any]) -> list[BodyCandidate]:
        if str(target.get("source_id", "")) != "qingbian":
            return []
        title = str(target.get("title", "")).strip()
        source = str(target.get("source", "")).strip()
        if not title or not source:
            return []
        article = self.client.resolve(title)
        if article is None:
            return []
        return [BodyCandidate(
            str(target.get("id", "")), article.title, source, article.url,
            article.body, self.name, article.published_at,
        )]


class WeReadMpProvider:
    name = "weread-mp"

    def __init__(self, client: WeReadClient | None = None) -> None:
        self.client = client or WeReadClient()

    def resolve(self, target: dict[str, Any]) -> list[BodyCandidate]:
        expected = str(target.get("title", "")).strip()
        source = str(target.get("source", "")).strip()
        if not expected or not source:
            return []
        book = self.client.find_mp_book(account_name(source))
        if not book:
            return []
        book_id = str(book.get("bookId", ""))
        cover = self.client.latest_cover(book_id)
        title = str(cover.get("title", "")).strip()
        if not titles_match(expected, title):
            return []
        review_id = str(cover.get("reviewId", "")).strip()
        body = self.client.article_html(review_id)
        url = mp_link_from_review(review_id, book_id)
        return [BodyCandidate(str(target.get("id", "")), title, source, url, body, self.name,
                              str(target.get("published_at", "")))]


class MirrorProvider:
    name = "mirror-zxmop"
    base_url = "https://www.zxmop.com"

    @staticmethod
    def _html(url: str) -> str:
        req = request.Request(url, headers={"User-Agent": "Mozilla/5.0 CognitiveFeed/0.5"})
        with request.urlopen(req, timeout=15) as response:
            raw = response.read(3_000_001)
        if len(raw) > 3_000_000:
            raise ValueError("mirror response too large")
        return raw.decode("utf-8", errors="replace")

    def resolve(self, target: dict[str, Any]) -> list[BodyCandidate]:
        expected = str(target.get("title", "")).strip()
        if not expected:
            return []
        search_url = f"{self.base_url}/?s={quote_plus(expected)}"
        soup = BeautifulSoup(self._html(search_url), "lxml")
        links: list[str] = []
        for anchor in soup.select("a[href]"):
            label = str(anchor.get("title", "")).strip() or anchor.get_text(" ", strip=True)
            if titles_match(expected, label):
                href = urljoin(self.base_url, str(anchor.get("href", "")))
                if href not in links:
                    links.append(href)
        found: list[BodyCandidate] = []
        expected_source = re.sub(r"[\s·|/\-]+", "", str(target.get("source", "")).replace("微信公众号", ""))
        for url in links[:3]:
            page = BeautifulSoup(self._html(url), "lxml")
            title_node = page.select_one("h1.post-title") or page.find("h1")
            title = title_node.get_text(" ", strip=True) if title_node else ""
            article = page.select_one("article.post-content")
            if article is None:
                continue
            category_text = " ".join(text for x in page.select(".article-meta, .entry-tags") for text in x.stripped_strings)
            actual_source = re.sub(r"[\s·|/\-]+", "", category_text)
            if expected_source and expected_source not in actual_source:
                continue
            body = "\n\n".join(node.get_text(" ", strip=True) for node in article.find_all(["p", "h2", "h3", "li"]) if node.get_text(" ", strip=True))
            date_node = page.select_one(".meta-date, time.pub-date")
            published = date_node.get_text(" ", strip=True) if date_node else ""
            found.append(BodyCandidate(str(target.get("id", "")), title, str(target.get("source", "")), url, body, self.name, published))
        return found


class BrowserDomProvider:
    name = "browser-dom"

    def __init__(self, *, profile: Path = DEFAULT_CHROME_PROFILE, timeout_seconds: int = 25) -> None:
        self.profile = Path(profile)
        self.timeout_seconds = timeout_seconds

    @staticmethod
    def _chrome() -> Path | None:
        candidates = [
            os.environ.get("READER_CHROME", ""),
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            str(Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe"),
        ]
        for value in candidates:
            path = Path(value) if value else None
            if path and path.exists():
                return path
        return None

    def _dump_dom(self, url: str) -> str:
        chrome = self._chrome()
        if chrome is None or not url.startswith(("http://", "https://")):
            return ""
        self.profile.mkdir(parents=True, exist_ok=True)
        cmd = [str(chrome), "--headless=new", "--disable-gpu", "--remote-debugging-port=9223",
               f"--user-data-dir={self.profile}", "--dump-dom", url]
        done = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=self.timeout_seconds, check=False)
        return done.stdout if done.returncode == 0 else ""

    def resolve(self, target: dict[str, Any]) -> list[BodyCandidate]:
        url = str(target.get("detail_url", target.get("url", ""))).strip()
        html = self._dump_dom(url)
        if not html:
            return []
        soup = BeautifulSoup(html, "lxml")
        original = soup.select_one('a[href*="mp.weixin.qq.com"]')
        if original and str(original.get("href", "")).startswith("http"):
            original_url = str(original.get("href", ""))
            original_html = self._dump_dom(original_url)
            if original_html:
                soup = BeautifulSoup(original_html, "lxml")
                url = original_url
        for tag in soup(["script", "style", "noscript", "svg", "nav", "footer", "aside", "form"]):
            tag.decompose()
        meta = soup.find("meta", attrs={"property": "og:title"})
        title = (str(meta.get("content", "")) if meta else "").strip()
        if not title:
            h1 = soup.find("h1")
            title = h1.get_text(" ", strip=True) if h1 else str(target.get("title", ""))
        container = soup.select_one("#js_content") or soup.find("article") or soup.find("main") or soup.find(attrs={"role": "main"})
        if container is None:
            return []
        body = "\n\n".join(x.strip() for x in container.stripped_strings if x.strip())
        if not body:
            return []
        return [BodyCandidate(str(target.get("id", "")), title, str(target.get("source", "")),
                              url, body, self.name, str(target.get("published_at", "")))]


class _BridgeCompatibilityProvider:
    name = "weread-bridge-compat"

    def __init__(self, bridge) -> None:
        self.bridge = bridge

    def resolve(self, target: dict[str, Any]) -> list[BodyCandidate]:
        result = self.bridge.resolve([target])
        if not isinstance(result, dict) or not result.get("ok"):
            return []
        found: list[BodyCandidate] = []
        for row in result.get("items", []):
            found.append(BodyCandidate(
                str(target.get("id", "")),
                str(row.get("title", target.get("title", ""))),
                str(row.get("source", target.get("source", ""))),
                str(row.get("url", "")), str(row.get("body", "")), self.name,
                str(target.get("published_at", "")),
            ))
        return found


class BodyResolver:
    def __init__(self, providers: list[BodyProvider] | None = None, *, verifier: OriginalityVerifier | None = None) -> None:
        self.verifier = verifier or OriginalityVerifier()
        self.providers = providers or [WeMpRssProvider(), SogouWeixinProvider(), GuanchaQingbianProvider(), MirrorProvider(), ArchiveProvider(), WeReadMpProvider(), BrowserDomProvider()]

    def resolve_one(self, target: dict[str, Any]) -> tuple[BodyCandidate | None, Verification, list[dict[str, str]]]:
        candidates: list[BodyCandidate] = []
        errors: list[dict[str, str]] = []
        verdict = Verification(False, "no_candidate")
        for provider in self.providers:
            try:
                rows = provider.resolve(target)
            except Exception as exc:
                errors.append({"provider": getattr(provider, "name", type(provider).__name__), "error": str(exc)[:240]})
                continue
            if not rows:
                continue
            candidates.extend(rows)
            candidate, verdict = self.verifier.choose(target, candidates)
            if candidate is not None:
                return candidate, verdict, errors
        return None, verdict, errors



class WeChatPendingPreparer:
    def __init__(self, *, items_path: Path, provider, resolver: BodyResolver | None = None, bridge=None,
                 verifier: OriginalityVerifier | None = None) -> None:
        self.items_path = Path(items_path)
        self.provider = provider
        self.verifier = verifier or OriginalityVerifier()
        if resolver is not None:
            self.resolver = resolver
        elif bridge is not None:
            self.resolver = BodyResolver([_BridgeCompatibilityProvider(bridge)], verifier=self.verifier)
        else:
            self.resolver = BodyResolver(verifier=self.verifier)

    def _prepare(self, target: dict[str, Any], candidate: BodyCandidate) -> dict[str, Any]:
        expected = str(target.get("title", candidate.title)).strip()
        source = str(target.get("source", candidate.source)).strip() or candidate.source
        card = prepare_item(f"标题：{expected}\n\n{candidate.body}", self.provider, source=source, url=candidate.url, raw_body=candidate.body, context_query=expected)
        card = replace(card, title=expected, source=source, url=candidate.url, why_selected="指定来源 · 每日必读")
        append_item(self.items_path, card)
        return {"id": card.id, "title": expected, "source": source, "target_id": str(target.get("id", ""))}

    def prepare_capture(self, target: dict[str, Any], capture: dict[str, Any]) -> dict[str, Any]:
        candidate = BodyCandidate(str(target.get("id", "")), str(capture.get("title", "")),
                                  str(capture.get("source", target.get("source", ""))),
                                  str(capture.get("url", "")), str(capture.get("body", "")), "browser-capture",
                                  str(target.get("published_at", "")))
        verdict = self.verifier.check(target, candidate)
        if not verdict.accepted:
            return {"added": [], "failed": [{"title": candidate.title, "error": verdict.reason}], "reason": verdict.reason}
        try:
            return {"added": [self._prepare(target, candidate)], "failed": [], "reason": ""}
        except Exception as exc:
            return {"added": [], "failed": [{"title": candidate.title, "error": str(exc)[:240]}], "reason": "prepare_failed"}

    def refresh(self, inbox: list[dict[str, Any]]) -> dict[str, Any]:
        pending = [row for row in inbox if row.get("status") == "pending_body"
                   and str(row.get("source_id", "")) in TARGET_SOURCE_IDS]
        if not pending or self.provider is None:
            return {"added": [], "failed": [], "reason": ""}
        added: list[dict[str, Any]] = []
        failed: list[dict[str, str]] = []
        reasons: list[str] = []
        for target in pending:
            candidate, verdict, provider_errors = self.resolver.resolve_one(target)
            if candidate is None:
                reason = verdict.reason or "body_not_verified"
                reasons.append(reason)
                detail = reason
                if provider_errors:
                    detail += "; " + "; ".join(f"{x['provider']}:{x['error']}" for x in provider_errors[:3])
                failed.append({"title": str(target.get("title", "")), "error": detail[:240]})
                continue
            try:
                added.append(self._prepare(target, candidate))
            except Exception as exc:
                failed.append({"title": str(target.get("title", "")), "error": str(exc)[:240]})
        return {"added": added, "failed": failed, "reason": "" if added else (reasons[0] if reasons else "")}
