from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading
from typing import Any
from urllib import request
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

SHANGHAI = ZoneInfo("Asia/Shanghai")
TIMESTAMP_RE = re.compile(r"20\d{2}-\d{2}-\d{2}\s+\d{2}:\d{2}")


@dataclass(frozen=True)
class DiscoverySource:
    id: str
    name: str
    url: str


@dataclass(frozen=True)
class DiscoveredArticle:
    id: str
    source_id: str
    source: str
    title: str
    detail_url: str
    published_at: str
    status: str = "pending_body"
    discovered_at: str = ""


def _fetch_html(url: str) -> str:
    req = request.Request(url, headers={"User-Agent": "Mozilla/5.0 CognitiveFeed/0.4"})
    with request.urlopen(req, timeout=20) as response:
        raw = response.read(1_500_001)
    if len(raw) > 1_500_000:
        raise ValueError("discovery page is too large")
    return raw.decode("utf-8", errors="replace")


def _exact_published_at(detail_url: str) -> datetime | None:
    html = _fetch_html(detail_url)
    match = TIMESTAMP_RE.search(" ".join(BeautifulSoup(html, "lxml").stripped_strings))
    if not match:
        return None
    return datetime.strptime(match.group(0), "%Y-%m-%d %H:%M").replace(tzinfo=SHANGHAI)


def _article_id(source_id: str, detail_url: str) -> str:
    digest = hashlib.sha256(f"{source_id}|{detail_url}".encode("utf-8")).hexdigest()[:16]
    return f"discovery-{digest}"


def _title_key(source_id: str, title: str) -> tuple[str, str]:
    normalized = re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "", str(title or "")).lower()
    return str(source_id or ""), normalized


def _load_inbox(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, list) else []


def _save_inbox(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows.sort(key=lambda row: str(row.get("published_at", "")), reverse=True)
    payload = json.dumps(rows, ensure_ascii=False, indent=2) + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


class _InboxStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()

    def read(self) -> list[dict[str, Any]]:
        with self._lock:
            return _load_inbox(self.path)

    def modify(self, mutator) -> list[dict[str, Any]]:
        with self._lock:
            rows = _load_inbox(self.path)
            mutator(rows)
            _save_inbox(self.path, rows)
            return rows



def _meta_maybe_recent(meta: str, max_age_hours: int) -> bool:
    text = " ".join(meta.split())
    if not text:
        return True
    if "刚刚" in text or "分钟前" in text:
        return True
    match = re.search(r"(\d+)\s*小时(?:前)?", text)
    if match:
        return int(match.group(1)) <= max_age_hours
    if "昨天" in text:
        return max_age_hours >= 24
    match = re.search(r"(\d+)\s*天前", text)
    if match:
        return int(match.group(1)) * 24 <= max_age_hours
    if "周前" in text or "个月前" in text:
        return False
    return True

def _configured_not_before() -> datetime | None:
    raw = os.environ.get("READING_NOT_BEFORE", "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SHANGHAI)
    return parsed.astimezone(SHANGHAI)


def discover_column(source: DiscoverySource, *, now: datetime, max_age_hours: int = 36) -> list[DiscoveredArticle]:
    soup = BeautifulSoup(_fetch_html(source.url), "lxml")
    cutoff = now.astimezone(SHANGHAI) - timedelta(hours=max_age_hours)
    not_before = _configured_not_before()
    if not_before and not_before > cutoff:
        cutoff = not_before
    found: list[DiscoveredArticle] = []
    for cell in soup.select("div.cell.item"):
        anchor = cell.select_one("span.item_title a[href]")
        if not anchor:
            continue
        href = str(anchor.get("href", "")).strip()
        title = " ".join(anchor.stripped_strings).strip()
        if not title or "/t/" not in href:
            continue
        meta_node = cell.select_one("span.small.fade")
        meta = " ".join(meta_node.stripped_strings) if meta_node else ""
        if not _meta_maybe_recent(meta, max_age_hours):
            continue
        detail_url = urljoin(source.url, href.replace("http://", "https://", 1))
        published = _exact_published_at(detail_url)
        if published is None or published < cutoff:
            continue
        found.append(DiscoveredArticle(
            id=_article_id(source.id, detail_url), source_id=source.id, source=source.name,
            title=title, detail_url=detail_url, published_at=published.isoformat(),
            discovered_at=now.astimezone(SHANGHAI).isoformat(),
        ))
    return found


class SourceDiscovery:
    def __init__(self, *, inbox_path: Path, sources: tuple[DiscoverySource, ...], max_age_hours: int = 36, reviewer=None, live_discoverer=None) -> None:
        self.inbox_path = Path(inbox_path)
        self.sources = sources
        self.max_age_hours = max_age_hours
        self.reviewer = reviewer
        self.live_discoverer = live_discoverer
        self._inbox = _InboxStore(self.inbox_path)

    def list_inbox(self) -> list[dict[str, Any]]:
        return self._inbox.read()

    def refresh(self) -> dict[str, Any]:
        now = datetime.now(SHANGHAI)
        found_articles: list[DiscoveredArticle] = []
        failed: list[dict[str, str]] = []
        for source in self.sources:
            try:
                found_articles.extend(discover_column(source, now=now, max_age_hours=self.max_age_hours))
            except Exception as exc:
                failed.append({"source": source.name, "error": str(exc)[:240]})
        if self.live_discoverer is not None:
            try:
                found_articles.extend(self.live_discoverer(now))
            except Exception as exc:
                failed.append({"source": "公众号实时源", "error": str(exc)[:240]})

        # Same publisher + same normalized title is one article even if different indexes discover it later.
        collapsed: dict[tuple[str, str], DiscoveredArticle] = {}
        for article in found_articles:
            key = _title_key(article.source_id, article.title)
            if key[1] and key not in collapsed:
                collapsed[key] = article
        found_articles = list(collapsed.values())

        snapshot = self._inbox.read()
        known_ids = {str(row.get("id", "")) for row in snapshot if row.get("id")}
        known_keys = {_title_key(str(row.get("source_id", "")), str(row.get("title", ""))) for row in snapshot}
        unseen = [article for article in found_articles if article.id not in known_ids and _title_key(article.source_id, article.title) not in known_keys]
        reviews = {}
        if unseen and self.reviewer:
            try:
                reviews = self.reviewer.review(unseen)
            except Exception as exc:
                failed.append({"source": "????????", "error": str(exc)[:240]})

        discovered: list[dict[str, Any]] = []
        excluded: list[dict[str, Any]] = []

        def merge(rows: list[dict[str, Any]]) -> None:
            by_id = {str(row.get("id", "")): row for row in rows if row.get("id")}
            existing_keys = {_title_key(str(row.get("source_id", "")), str(row.get("title", ""))) for row in rows}
            for article in found_articles:
                row = asdict(article)
                article_key = _title_key(article.source_id, article.title)
                if article.id not in by_id and article_key in existing_keys:
                    continue
                if article.id in by_id:
                    old = by_id[article.id]
                    row["status"] = old.get("status", row["status"])
                    row["ready_item_id"] = old.get("ready_item_id", "")
                    row["review_reason"] = old.get("review_reason", "")
                else:
                    review = reviews.get(article.id)
                    if review and review.decision == "exclude":
                        row["status"] = "excluded_ad"
                        row["review_reason"] = review.reason
                        excluded.append(row)
                    else:
                        discovered.append(row)
                by_id[article.id] = row
                existing_keys.add(article_key)
            rows[:] = list(by_id.values())

        rows = self._inbox.modify(merge)
        return {"discovered": discovered, "excluded": excluded, "inbox": rows, "failed": failed}

    def mark_ready_by_id(self, target_id: str, item_id: str) -> None:
        def mark(rows: list[dict[str, Any]]) -> None:
            for row in rows:
                if str(row.get("id", "")) == target_id:
                    row["status"] = "ready"
                    row["ready_item_id"] = item_id
        self._inbox.modify(mark)

    def mark_ready_by_title(self, title: str, item_id: str) -> None:
        def mark(rows: list[dict[str, Any]]) -> None:
            for row in rows:
                if str(row.get("title", "")).strip() == title.strip():
                    row["status"] = "ready"
                    row["ready_item_id"] = item_id
        self._inbox.modify(mark)


class SourceCoordinator:
    def __init__(self, discovery: SourceDiscovery, primary_refresh=None, quality_refresh=None, body_refresh=None, body_capture=None) -> None:
        self.discovery = discovery
        self.primary_refresh = primary_refresh
        self.quality_refresh = quality_refresh
        self.body_refresh = body_refresh
        self.body_capture = body_capture

    def list_inbox(self) -> list[dict[str, Any]]:
        return self.discovery.list_inbox()

    def discover(self) -> dict[str, Any]:
        return self.discovery.refresh()

    def prepare_one(self, target_id: str) -> dict[str, Any]:
        target = next((row for row in self.discovery.list_inbox()
                       if str(row.get("id", "")) == target_id and row.get("status") == "pending_body"), None)
        if target is None:
            raise KeyError("pending source item not found")
        if not self.body_refresh:
            raise RuntimeError("body preparation is unavailable")
        result = self.body_refresh([target])
        for item in result.get("added", []):
            self.discovery.mark_ready_by_id(str(item.get("target_id", "")), str(item.get("id", "")))
        result["inbox"] = self.discovery.list_inbox()
        return result

    def capture(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.body_capture:
            raise RuntimeError("browser capture is unavailable")
        from reading_exact import titles_match
        target_id = str(payload.get("target_id", "")).strip()
        title = str(payload.get("title", "")).strip()
        rows = self.discovery.list_inbox()
        target = next((row for row in rows if target_id and str(row.get("id", "")) == target_id), None)
        if target is None:
            target = next((row for row in rows
                           if row.get("status") == "pending_body" and titles_match(str(row.get("title", "")), title)), None)
        if target is None:
            raise KeyError("matching pending source item not found")
        result = self.body_capture(target, payload)
        for item in result.get("added", []):
            self.discovery.mark_ready_by_id(str(item.get("target_id", "")), str(item.get("id", "")))
        result["inbox"] = self.discovery.list_inbox()
        return result

    def refresh(self) -> dict[str, Any]:
        quick = self.discovery.refresh()
        body = self.body_refresh(self.discovery.list_inbox()) if self.body_refresh else {"added": [], "failed": [], "reason": ""}
        for item in body.get("added", []):
            self.discovery.mark_ready_by_id(str(item.get("target_id", "")), str(item.get("id", "")))
        prepared = self.primary_refresh() if self.primary_refresh else {"added": [], "paid": [], "failed": []}
        quality = self.quality_refresh() if self.quality_refresh else {"added": [], "failed": []}
        for item in prepared.get("added", []):
            self.discovery.mark_ready_by_title(str(item.get("title", "")), str(item.get("id", "")))
        inbox = self.discovery.list_inbox()
        return {
            "discovered": quick.get("discovered", []),
            "inbox": inbox,
            "added": list(body.get("added", [])) + list(prepared.get("added", [])) + list(quality.get("added", [])),
            "body_reason": body.get("reason", ""),
            "paid": prepared.get("paid", []),
            "failed": list(quick.get("failed", [])) + list(body.get("failed", [])) + list(prepared.get("failed", [])) + list(quality.get("failed", [])),
        }
