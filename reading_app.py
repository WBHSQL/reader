from __future__ import annotations

import argparse
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import tempfile
import threading
from typing import Any
from urllib import request
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
DEFAULT_ITEMS = ROOT / "reading_items.json"
DEFAULT_UI = ROOT / "reading_ui.html"
READER_HOME = Path(os.environ.get("READER_HOME", str(Path.home() / ".reader")))
STATE_DIR = READER_HOME / "state"


@dataclass(frozen=True)
class ReadingItem:
    id: str
    title: str
    source: str
    background: str
    question: str
    author_reasoning: str
    author_conclusion: str
    url: str = ""
    topic: str = ""
    why_selected: str = ""
    estimated_minutes: int = 8

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ReadingItem":
        required = ("id", "title", "source", "background", "question", "author_reasoning", "author_conclusion")
        missing = [key for key in required if not str(value.get(key, "")).strip()]
        if missing:
            raise ValueError(f"reading item missing fields: {missing}")
        return cls(
            id=str(value["id"]).strip(), title=str(value["title"]).strip(),
            source=str(value["source"]).strip(), background=str(value["background"]).strip(),
            question=str(value["question"]).strip(), author_reasoning=str(value["author_reasoning"]).strip(),
            author_conclusion=str(value["author_conclusion"]).strip(), url=str(value.get("url", "")).strip(),
            topic=str(value.get("topic", "")).strip(), why_selected=str(value.get("why_selected", "")).strip(),
            estimated_minutes=max(1, int(value.get("estimated_minutes", 8))),
        )

    def blind(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in (
            "id", "title", "source", "background", "question", "url", "topic", "why_selected", "estimated_minutes"
        )}


class ReadingStore:
    def __init__(self, path: str | Path = DEFAULT_ITEMS, completed_path: str | Path | None = None) -> None:
        self.path = Path(path)
        self.completed_path = Path(completed_path) if completed_path else self.path.with_name("completed_items.json")
        self._completion_lock = threading.RLock()

    def load(self) -> tuple[ReadingItem, ...]:
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise ValueError("reading items must be a JSON list")
        items = tuple(ReadingItem.from_dict(item) for item in raw)
        ids = [item.id for item in items]
        if len(ids) != len(set(ids)):
            raise ValueError("reading item ids must be unique")
        return items

    def get(self, item_id: str) -> ReadingItem:
        for item in self.load():
            if item.id == item_id:
                return item
        raise KeyError(item_id)

    def completed_ids(self) -> set[str]:
        with self._completion_lock:
            if not self.completed_path.exists():
                return set()
            try:
                value = json.loads(self.completed_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return set()
            if not isinstance(value, list):
                return set()
            return {str(item) for item in value if str(item).strip()}

    def load_active(self) -> tuple[ReadingItem, ...]:
        completed = self.completed_ids()
        return tuple(item for item in self.load() if item.id not in completed)

    def mark_completed(self, item_id: str) -> None:
        item_id = str(item_id).strip()
        if not item_id:
            raise ValueError("item_id is required")
        with self._completion_lock:
            ids = self.completed_ids()
            if item_id in ids:
                return
            ids.add(item_id)
            self.completed_path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(
                prefix=f".{self.completed_path.name}.", suffix=".tmp", dir=self.completed_path.parent
            )
            tmp = Path(tmp_name)
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                    json.dump(sorted(ids), handle, ensure_ascii=False, indent=2)
                    handle.write("\n")
                    handle.flush(); os.fsync(handle.fileno())
                os.replace(tmp, self.completed_path)
            finally:
                try: tmp.unlink(missing_ok=True)
                except OSError: pass


class FlomoWriter:
    def __init__(self, url: str | None = None) -> None:
        self.url = (url or os.environ.get("FLOMO_INCOMING_API_URL", "")).strip()

    def write(self, content: str) -> None:
        if not self.url:
            raise RuntimeError("FLOMO_INCOMING_API_URL is not configured")
        payload = json.dumps({"content": content}, ensure_ascii=False).encode("utf-8")
        req = request.Request(self.url, data=payload, method="POST",
                              headers={"Content-Type": "application/json"})
        try:
            with request.urlopen(req, timeout=20) as response:
                if not 200 <= int(response.status) < 300:
                    raise RuntimeError(f"flomo write failed: HTTP {response.status}")
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError("flomo write failed") from exc


class NoopWriter:
    def __init__(self) -> None:
        self.contents: list[str] = []
    def write(self, content: str) -> None:
        self.contents.append(content)


@dataclass
class ReadingSession:
    item: ReadingItem
    answer: str
    comparison: str = ""
    reflection: str = ""
    finalized: bool = False


class ReadingService:
    def __init__(self, store: ReadingStore, writer, comparer=None, preparer=None, rss_refresher=None,
                 source_coordinator=None, article_loader=None, article_cleanup=None, weread_login=None) -> None:
        self.store, self.writer, self.comparer, self.preparer = store, writer, comparer, preparer
        self.rss_refresher = rss_refresher
        self.source_coordinator = source_coordinator
        self.article_loader = article_loader
        self.article_cleanup = article_cleanup
        self.weread_login = weread_login
        self.sessions: dict[str, ReadingSession] = {}
        self._source_refresh_lock = threading.RLock()

    def list_items(self) -> list[dict[str, Any]]:
        return [item.blind() for item in self.store.load_active()]

    def get_item(self, item_id: str) -> dict[str, Any]:
        return self.store.get(item_id).blind()

    def submit_answer(self, item_id: str, answer: str) -> dict[str, Any]:
        answer = answer.strip()
        if not answer:
            raise ValueError("answer is required")
        item = self.store.get(item_id)
        token = secrets.token_urlsafe(24)
        self.sessions[token] = ReadingSession(item, answer)
        return {"session_token": token}

    def get_article(self, token: str) -> dict[str, str]:
        if token not in self.sessions:
            raise KeyError("reading session not found or expired")
        session = self.sessions[token]
        if not self.article_loader:
            raise RuntimeError("article body is unavailable")
        body = str(self.article_loader(session.item) or "").strip()
        if not body:
            raise RuntimeError("article body is unavailable; reacquire the article first")
        return {"title": session.item.title, "body": body, "url": session.item.url}

    def submit_reflection(self, token: str, reflection: str) -> None:
        reflection = reflection.strip()
        if not reflection:
            raise ValueError("reflection is required")
        if token not in self.sessions:
            raise KeyError("reading session not found or expired")
        self.sessions[token].reflection = reflection

    def generate_comparison(self, token: str) -> str:
        if token not in self.sessions:
            raise KeyError("reading session not found or expired")
        session = self.sessions[token]
        if not session.reflection:
            raise ValueError("read the article and write your second judgment first")
        if session.comparison:
            return session.comparison
        if not self.comparer:
            raise RuntimeError("AI comparison is unavailable")
        article = self.get_article(token)["body"]
        comparison = self.comparer(
            session.item, session.answer, session.reflection, article
        ).strip()
        if not comparison:
            raise RuntimeError("AI comparison returned empty")
        session.comparison = comparison
        return session.comparison

    def finalize(self, token: str) -> None:
        if token not in self.sessions:
            raise KeyError("reading session not found or expired")
        session = self.sessions[token]
        if not session.reflection:
            raise ValueError("second judgment is required")
        if self.comparer is not None and not session.comparison:
            raise ValueError("AI comparison must succeed before finalizing")
        if session.finalized:
            self.store.mark_completed(session.item.id)
            return
        self.writer.write(final_memo(session))
        session.finalized = True
        self.store.mark_completed(session.item.id)
        if self.article_cleanup:
            try:
                self.article_cleanup(session.item)
            except Exception:
                pass

    def import_url(self, url: str) -> dict[str, Any]:
        url = url.strip()
        if not url:
            raise ValueError("url is required")
        if not self.preparer:
            raise RuntimeError("article preparation is unavailable")
        item = self.preparer(url)
        from reading_prepare import append_item
        append_item(self.store.path, item)
        return item.blind()

    def refresh_rss(self) -> dict[str, Any]:
        if not self.rss_refresher:
            raise RuntimeError("RSS aggregation is unavailable")
        return self.rss_refresher()

    def list_source_inbox(self) -> list[dict[str, Any]]:
        return self.source_coordinator.list_inbox() if self.source_coordinator else []

    def weread_status(self) -> dict[str, Any]:
        if self.weread_login is None:
            return {"configured": False, "state": "unavailable", "message": "微信读书实时源不可用", "qr": ""}
        return self.weread_login.status()

    def start_weread_login(self) -> dict[str, Any]:
        if self.weread_login is None:
            raise RuntimeError("微信读书实时源不可用")
        return self.weread_login.start()

    def discover_sources(self) -> dict[str, Any]:
        if not self.source_coordinator:
            raise RuntimeError("source discovery is unavailable")
        return self.source_coordinator.discover()

    def refresh_primary(self) -> dict[str, Any]:
        if not self.source_coordinator:
            raise RuntimeError("primary sources are unavailable")
        with self._source_refresh_lock:
            return self.source_coordinator.refresh()

    def prepare_source(self, target_id: str) -> dict[str, Any]:
        if not self.source_coordinator:
            raise RuntimeError("source preparation is unavailable")
        return self.source_coordinator.prepare_one(target_id)

    def capture_source(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.source_coordinator:
            raise RuntimeError("source capture is unavailable")
        return self.source_coordinator.capture(payload)

def final_memo(session: ReadingSession) -> str:
    item = session.item
    lines = [
        "#Reader",
        "",
        f"《{item.title}》",
        "",
        "先知道这些",
        item.background.strip(),
        "",
        "当时在问",
        item.question.strip(),
        "",
        "我一开始怎么想",
        session.answer.strip(),
        "",
        "看完以后",
        session.reflection.strip(),
    ]
    if session.comparison:
        lines += ["", "再对照一下", session.comparison.strip()]
    if item.url:
        lines += ["", f"原文 {item.url}"]
    return "\n".join(lines)


class ContextualReadingProvider:
    """Attach ephemeral, evidence-grounded user context only to card generation."""

    def __init__(self, delegate, context_builder) -> None:
        self.delegate = delegate
        self.context_builder = context_builder

    def answer_for(self, prompt: str, query: str = "") -> str:
        context = str(self.context_builder(query) or "").strip()
        if not context:
            return self.delegate.answer(prompt)
        prefix = (
            "## 用户当前上下文（临时读取，只用于让训练入口贴近用户）\n"
            "只在与文章确实相关时使用。不要硬套，不要推断人格，不要复述用户过去对同一文章的答案。"
            "这些材料只帮助判断他熟悉什么、正在做什么、什么表达更容易进入。\n"
        )
        return self.delegate.answer(prefix + context + "\n\n## 训练卡任务\n" + prompt)

    def answer(self, prompt: str) -> str:
        return self.answer_for(prompt, "")


def build_reader_personal_context(article_query: str = "") -> str:
    if os.environ.get("READING_PERSONALIZE", "0") == "0":
        return ""
    current_query = "最近现在目前正在做什么，正在关心什么，正在解决什么问题，正在学习什么"
    query = (article_query.strip() + " " + current_query).strip() if article_query.strip() else current_query
    lines: list[str] = []
    try:
        from agent.context_builder import AgentContextBuilder
        from memory_engine.repository import MemoryRepository
        data_dir = ROOT / "data"
        reviewed = sorted(data_dir.glob("*reviewed*.sqlite3"), key=lambda p: p.stat().st_mtime, reverse=True)
        db_path = reviewed[0] if reviewed else data_dir / "derived_memory.sqlite3"
        if db_path.exists():
            with MemoryRepository(db_path) as repository:
                builder = AgentContextBuilder(repository)
                memories = list(builder.retrieve(query, limit=5))
                if article_query.strip():
                    memories += [m for m in builder.retrieve(current_query, limit=4) if m not in memories]
            if memories:
                lines.append("已审核的相关记忆：")
                for item in memories[:7]:
                    fragment = item.fragment
                    lines.append(f"- {fragment.topic or fragment.type}: {fragment.statement[:500]}")
    except Exception:
        pass
    try:
        from agent.live_context import rank_recent_memos
        from config import FlomoConfig
        from flomo_client import FlomoClient
        client = FlomoClient(FlomoConfig.from_env(ROOT / ".env"))
        memos = client.get_latest_memos(limit=24)
        ranked = list(rank_recent_memos(memos, query, limit=5))
        if article_query.strip():
            ranked += [m for m in rank_recent_memos(memos, current_query, limit=4) if m not in ranked]
        safe = []
        for memo in ranked:
            stripped = memo.text.lstrip()
            if stripped.startswith("#Reader") or stripped.startswith("#认知训练"):
                continue
            safe.append(memo)
        if safe:
            lines.append("最近 flomo（只在本次生成时临时读取）：")
            for memo in safe[:6]:
                text = " ".join(memo.text.split())
                lines.append(f"- [{memo.created_at}] {text[:600]}")
    except Exception:
        pass
    return "\n".join(lines)[:6000]



READER_CODEX_BOOTSTRAP = """Reader 后台工作会话。这个会话只用于 Cognitive Feed / Reader。

长期规则：
1. 每条新消息都是一个明确任务；沿用 Reader 的表达和判断标准，但文章事实只以当前消息为准，不把上一篇文章的事实带进下一篇。
2. 训练卡要先找作者真正花篇幅建立的核心命题，再用具体案例帮助读者进入；不要把案例本身误当整篇文章的核心。
3. 解释陌生概念，用普通中文，不让读者凭空扮演陌生职业。
4. AI 对照要贴着读者两次原话和原文，不打分、不做人格判断、不写导师腔。
5. 除非当前任务明确要求网页核验，否则不使用工具；绝不修改本地文件，只返回当前任务需要的文本。"""


def build_reading_provider(*, effort_override: str | None = None):
    mode = os.environ.get("READING_LLM_PROVIDER", "codex").strip().lower()
    timeout = int(os.environ.get("READING_LLM_TIMEOUT", "180"))
    model = os.environ.get("READING_CODEX_MODEL", "gpt-5.6-luna")
    effort = effort_override or os.environ.get("READING_CODEX_EFFORT", "max")
    session_key = os.environ.get("READING_CODEX_SESSION_KEY", "reader").strip() or None
    session_store = Path(os.environ.get("READING_CODEX_SESSION_STORE", str(STATE_DIR / "codex_sessions.json")))
    bootstrap = READER_CODEX_BOOTSTRAP

    try:
        if mode == "hermes":
            from llm_provider import HermesCliProvider
            return HermesCliProvider(model=model, effort=effort, timeout_seconds=timeout)
        if mode == "codex":
            from llm_provider import CodexCliProvider
            return CodexCliProvider(
                model=model, effort=effort, timeout_seconds=timeout,
                session_key=session_key, session_store=session_store,
                session_bootstrap=bootstrap,
            )
        if mode in {"claude", "deepseek"}:
            from llm_provider import ClaudeCliProvider
            return ClaudeCliProvider(timeout_seconds=timeout)
        raise ValueError(f"unknown READING_LLM_PROVIDER: {mode}")
    except Exception:
        if mode == "hermes":
            try:
                from llm_provider import CodexCliProvider
                return CodexCliProvider(model=model, effort=effort, timeout_seconds=timeout, session_key=session_key, session_store=session_store, session_bootstrap=READER_CODEX_BOOTSTRAP)
            except Exception:
                return None
        return None


def build_comparer(provider=None):
    if os.environ.get("READING_AI_COMPARE", "1") == "0":
        return None
    provider = provider or build_reading_provider()
    if provider is None:
        return None

    def compare(item: ReadingItem, answer: str, reflection: str, article: str) -> str:
        # Voice rules are adapted from KKKKhazix/human-writing v1.1.0 (MIT):
        # material first, clear speaking position, natural Chinese, no model/report voice.
        prompt = f"""你在和一个刚读完文章的人对照思路。不要给他打分，不要把作者当标准答案，也不要总结他的性格或能力。

文章：{item.title}
最初的问题：{item.question}

他读文章前写的是：
{answer}

他读完文章后写的是：
{reflection}

原文：
{article[:30000]}

先以原文为准找出作者真正花最多篇幅建立的核心命题，再看用户两次判断和这个核心命题分别碰到了哪里。不要被“最初的问题”绑住：如果训练卡的问题本身把文章缩窄成了一个案例，直接指出这一点，并把对照拉回作者真正讨论的机制。

写 2 到 4 段自然中文。直接抓双方真正有差异的具体地方，也指出其实已经想到一起的地方。每个判断都要贴着上面的原话或文章内容，文章没有回答的就明确说没有回答。允许作者有盲点，也允许用户前后的判断都只对了一部分。

不要称呼用户姓名，也不要用名字开头。不要用小标题、编号、清单，不要写“你展现了”“这说明你”“值得注意的是”“真正的问题是”“更深一层”。不要用导师口吻，不要为了显得口语化塞网络梗。避免“不是……而是……”式翻案句、冒号和破折号。每一段都推进一个新的事实、差异或边界，写完就停。"""
        return provider.answer(prompt)
    return compare


def build_article_loader():
    def load(item: ReadingItem) -> str:
        from reading_article_cache import get_article, put_article
        body = get_article(item.id)
        if body:
            return body

        source_id = ""
        if "记忆承载3" in item.source:
            source_id = "jiyichengzai3"
        elif "记忆承载" in item.source:
            source_id = "jiyichengzai"
        elif "请辩" in item.source:
            source_id = "qingbian"
        if source_id:
            try:
                from reading_wechat import BodyResolver
                target = {"id": item.id, "title": item.title, "source": item.source,
                          "source_id": source_id, "url": item.url, "detail_url": item.url}
                candidate, verdict, _ = BodyResolver().resolve_one(target)
                if candidate is not None and verdict.accepted:
                    put_article(item.id, candidate.body)
                    return candidate.body
            except Exception:
                pass

        if not item.url:
            return ""
        from reading_prepare import fetch_article
        article, _ = fetch_article(item.url)
        put_article(item.id, article)
        return article
    return load


def build_article_cleanup():
    def cleanup(item: ReadingItem) -> None:
        from reading_article_cache import delete_article
        delete_article(item.id)
    return cleanup


def build_preparer(provider=None):
    provider = provider or build_reading_provider()
    if provider is None:
        return None

    def prepare(url: str) -> ReadingItem:
        from reading_prepare import fetch_article, prepare_item
        article, source = fetch_article(url)
        return prepare_item(article, provider, source=source, url=url)
    return prepare


def build_rss_refresher(provider, items_path: Path):
    if os.environ.get("READING_RSS_ENABLED", "0") == "0" or provider is None:
        return None
    from reading_rss import DEFAULT_SEEN, DEFAULT_SOURCES, RssAggregator
    sources = Path(os.environ.get("READING_RSS_SOURCES", str(DEFAULT_SOURCES)))
    seen = Path(os.environ.get("READING_RSS_SEEN", str(DEFAULT_SEEN)))
    aggregator = RssAggregator(
        sources_path=sources, seen_path=seen, items_path=Path(items_path), provider=provider,
        max_selected=int(os.environ.get("READING_RSS_MAX_SELECTED", "2")),
        max_age_hours=int(os.environ.get("READING_RSS_MAX_AGE_HOURS", "96")),
    )
    return aggregator.refresh


def build_source_coordinator(provider, items_path: Path):
    from reading_discovery import DiscoverySource, SourceCoordinator, SourceDiscovery
    inbox = Path(os.environ.get("READING_SOURCE_INBOX", str(STATE_DIR / "source_inbox.json")))
    sources = (
        DiscoverySource("jiyichengzai", "微信公众号 · 记忆承载", "https://www.jintiankansha.com/column/4k0SK6QY1U?type=recent"),
        DiscoverySource("jiyichengzai3", "微信公众号 · 记忆承载3", "https://www.jintiankansha.com/column/mQJLCUg4xH"),
        DiscoverySource("qingbian", "微信公众号 · 请辩", "https://www.jintiankansha.com/column/QYd1oKfBHb"),
    )
    reviewer = None
    if os.environ.get("READING_AD_REVIEW", "0") != "0":
        try:
            from llm_provider import CodexCliProvider
            from reading_content_policy import JiyichengzaiAdReviewer
            review_provider = CodexCliProvider(
                model="gpt-5.6-luna", effort="low", timeout_seconds=120, web_search=True,
                session_key=os.environ.get("READING_CODEX_SESSION_KEY", "reader").strip() or None,
                session_store=Path(os.environ.get("READING_CODEX_SESSION_STORE", str(STATE_DIR / "codex_sessions.json"))),
                session_bootstrap=READER_CODEX_BOOTSTRAP,
            )
            reviewer = JiyichengzaiAdReviewer(review_provider)
        except Exception:
            reviewer = None
    live_discoverer = None
    if os.environ.get("READING_WEREAD_LIVE_DISCOVERY", "0") != "0":
        try:
            from reading_weread import WeReadLiveDiscovery
            live_discoverer = WeReadLiveDiscovery()
        except Exception:
            live_discoverer = None
    discovery = SourceDiscovery(inbox_path=inbox, sources=sources, max_age_hours=36, reviewer=reviewer, live_discoverer=live_discoverer)
    body_refresh = None
    body_capture = None
    if provider is not None and os.environ.get("READING_WECHAT_ENABLED", "0") != "0":
        from reading_wechat import WeChatPendingPreparer
        body_preparer = WeChatPendingPreparer(items_path=Path(items_path), provider=provider)
        body_refresh = body_preparer.refresh
        body_capture = body_preparer.prepare_capture
    primary_refresh = None
    if provider is not None and os.environ.get("READING_PRIMARY_ENABLED", "0") != "0":
        from reading_primary import JiyichengzaiPrimarySource
        state = Path(os.environ.get("READING_PRIMARY_SEEN", str(STATE_DIR / "primary_seen.json")))
        primary = JiyichengzaiPrimarySource(items_path=Path(items_path), state_path=state, provider=provider)
        primary_refresh = primary.refresh
    quality_refresh = None
    if provider is not None and os.environ.get("READING_ZHIHU_ENABLED", "0") != "0":
        from reading_zhihu import ZhihuQualitySource
        zhihu_state = Path(os.environ.get("READING_ZHIHU_SEEN", str(STATE_DIR / "zhihu_seen.json")))
        quality = ZhihuQualitySource(items_path=Path(items_path), state_path=zhihu_state, provider=provider)
        quality_refresh = quality.refresh
    return SourceCoordinator(discovery, primary_refresh, quality_refresh, body_refresh, body_capture)

def start_background_source_refresh(service: ReadingService) -> threading.Thread | None:
    if os.environ.get("READING_BACKGROUND_REFRESH", "0") == "0":
        return None
    interval = max(300, int(os.environ.get("READING_BACKGROUND_REFRESH_SECONDS", "1800")))
    initial_delay = max(0, int(os.environ.get("READING_BACKGROUND_REFRESH_INITIAL_DELAY", "5")))

    def loop() -> None:
        if initial_delay:
            threading.Event().wait(initial_delay)
        while True:
            try:
                service.refresh_primary()
            except Exception as exc:
                print(f"Reader background refresh failed: {exc}", flush=True)
            threading.Event().wait(interval)

    thread = threading.Thread(target=loop, name="reader-source-refresh", daemon=True)
    thread.start()
    return thread


def make_handler(service: ReadingService, ui_path: Path = DEFAULT_UI):
    class Handler(BaseHTTPRequestHandler):
        def send_json(self, status: int, payload: object) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status); self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

        def body_json(self) -> dict[str, Any]:
            size = int(self.headers.get("Content-Length", "0")); raw = self.rfile.read(size)
            value = json.loads(raw.decode("utf-8")) if raw else {}
            if not isinstance(value, dict): raise ValueError("JSON object required")
            return value

        def do_GET(self) -> None:
            path = urlparse(self.path).path
            if path == "/":
                body = ui_path.read_bytes(); self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(body)))
                self.end_headers(); self.wfile.write(body); return
            if path.startswith("/api/sessions/") and path.endswith("/article"):
                token = path[len("/api/sessions/"):-len("/article")].strip("/")
                try: self.send_json(200, service.get_article(token))
                except KeyError: self.send_json(404, {"error":"reading session not found or expired"})
                except Exception as exc: self.send_json(400, {"error": str(exc)})
                return
            if path == "/api/items": self.send_json(200, service.list_items()); return
            if path == "/api/sources/inbox": self.send_json(200, service.list_source_inbox()); return
            if path == "/api/weread/status": self.send_json(200, service.weread_status()); return
            if path.startswith("/api/items/"):
                try: self.send_json(200, service.get_item(path.removeprefix("/api/items/")))
                except KeyError: self.send_json(404, {"error":"item not found"})
                return
            self.send_json(404, {"error":"not found"})

        def do_POST(self) -> None:
            path = urlparse(self.path).path
            try:
                body = self.body_json()
                if path.startswith("/api/items/") and path.endswith("/answer"):
                    item_id = path[len("/api/items/"):-len("/answer")].strip("/")
                    self.send_json(200, service.submit_answer(item_id, str(body.get("answer", "")))); return
                if path.startswith("/api/sessions/") and path.endswith("/comparison"):
                    token = path[len("/api/sessions/"):-len("/comparison")].strip("/")
                    self.send_json(200, {"comparison": service.generate_comparison(token)}); return
                if path.startswith("/api/sessions/") and path.endswith("/reflection"):
                    token = path[len("/api/sessions/"):-len("/reflection")].strip("/")
                    service.submit_reflection(token, str(body.get("reflection", "")))
                    self.send_json(200, {"ok": True}); return
                if path.startswith("/api/sessions/") and path.endswith("/finalize"):
                    token = path[len("/api/sessions/"):-len("/finalize")].strip("/")
                    service.finalize(token)
                    self.send_json(200, {"ok": True}); return
                if path == "/api/intake/url":
                    self.send_json(201, service.import_url(str(body.get("url", "")))); return
                if path == "/api/rss/refresh":
                    self.send_json(200, service.refresh_rss()); return
                if path == "/api/sources/capture":
                    self.send_json(200, service.capture_source(body)); return
                if path.startswith("/api/sources/") and path.endswith("/prepare"):
                    target_id = path[len("/api/sources/"):-len("/prepare")].strip("/")
                    self.send_json(200, service.prepare_source(target_id)); return
                if path == "/api/weread/login/start":
                    self.send_json(200, service.start_weread_login()); return
                if path == "/api/sources/discover":
                    self.send_json(200, service.discover_sources()); return
                if path == "/api/sources/refresh":
                    self.send_json(200, service.refresh_primary()); return
                self.send_json(404, {"error":"not found"})
            except KeyError as exc: self.send_json(404, {"error": str(exc)})
            except Exception as exc: self.send_json(400, {"error": str(exc)})

        def log_message(self, *_args) -> None: return
    return Handler


def main() -> int:
    parser = argparse.ArgumentParser(description="blind-first cognitive reading app")
    parser.add_argument("--host", default="127.0.0.1"); parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--items", type=Path, default=DEFAULT_ITEMS); parser.add_argument("--demo", action="store_true")
    args = parser.parse_args()
    writer = NoopWriter() if args.demo else FlomoWriter()
    compare_provider = build_reading_provider()
    prepare_effort = os.environ.get("READING_PREPARE_EFFORT", "high")
    prepare_provider = build_reading_provider(effort_override=prepare_effort)
    if prepare_provider is not None and os.environ.get("READING_PERSONALIZE", "0") != "0":
        prepare_provider = ContextualReadingProvider(prepare_provider, build_reader_personal_context)
    try:
        from reading_weread import get_weread_login
        weread_login = get_weread_login()
    except Exception:
        weread_login = None
    service = ReadingService(
        ReadingStore(args.items), writer, build_comparer(compare_provider),
        build_preparer(prepare_provider), build_rss_refresher(prepare_provider, args.items),
        build_source_coordinator(prepare_provider, args.items),
        article_loader=build_article_loader(), article_cleanup=build_article_cleanup(),
        weread_login=weread_login,
    )
    server = ThreadingHTTPServer((args.host, args.port), make_handler(service))
    start_background_source_refresh(service)
    print(f"Reading app: http://{args.host}:{args.port}")
    server.serve_forever(); return 0


if __name__ == "__main__":
    raise SystemExit(main())
