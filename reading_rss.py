from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
from pathlib import Path
from typing import Any
from urllib import request
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
import xml.etree.ElementTree as ET

from reading_prepare import append_item, fetch_article, prepare_item

ROOT = Path(__file__).resolve().parent
DEFAULT_SOURCES = ROOT / "rss_sources.json"
DEFAULT_SEEN = ROOT / "data" / "rss_seen.json"
TRACKING_QUERY_PREFIXES = ("utm_",)
TRACKING_QUERY_KEYS = {"fbclid", "gclid", "mc_cid", "mc_eid", "ref", "ref_src"}

@dataclass(frozen=True)
class RssSource:
    name: str
    url: str
    enabled: bool = True
    priority: int = 1


@dataclass(frozen=True)
class FeedEntry:
    key: str
    title: str
    url: str
    source: str
    summary: str = ""
    published: str = ""
    priority: int = 1


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()

def canonical_url(value: str) -> str:
    parsed = urlparse(value.strip())
    kept = []
    for key, val in parse_qsl(parsed.query, keep_blank_values=True):
        lower = key.lower()
        if lower in TRACKING_QUERY_KEYS or any(lower.startswith(prefix) for prefix in TRACKING_QUERY_PREFIXES):
            continue
        kept.append((key, val))
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, "", urlencode(kept), ""))


def _first_text(node: ET.Element, names: set[str]) -> str:
    for child in node.iter():
        if _local(child.tag) in names and child.text and child.text.strip():
            return " ".join(child.text.split())
    return ""


def _entry_link(node: ET.Element) -> str:
    for child in node.iter():
        if _local(child.tag) != "link":
            continue
        href = str(child.attrib.get("href", "")).strip()
        rel = str(child.attrib.get("rel", "alternate")).strip().lower()
        if href and rel in {"", "alternate"}:
            return href
        if child.text and child.text.strip():
            return child.text.strip()
    return _first_text(node, {"guid", "id"})


def _published_iso(value: str) -> str:
    raw = value.strip()
    if not raw:
        return ""
    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def load_sources(path: Path = DEFAULT_SOURCES) -> tuple[RssSource, ...]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("rss sources must be a JSON list")
    sources = []
    for value in raw:
        if not isinstance(value, dict):
            continue
        name, url = str(value.get("name", "")).strip(), str(value.get("url", "")).strip()
        if not name or not url:
            continue
        sources.append(RssSource(name=name, url=url, enabled=bool(value.get("enabled", True)),
                                 priority=max(1, int(value.get("priority", 1)))))
    return tuple(sources)


def fetch_feed(source: RssSource) -> list[FeedEntry]:
    req = request.Request(source.url, headers={"User-Agent": "Mozilla/5.0 CognitiveFeedRSS/0.1"})
    with request.urlopen(req, timeout=25) as response:
        raw = response.read(2_000_001)
    if len(raw) > 2_000_000:
        raise ValueError("RSS feed is too large")
    root = ET.fromstring(raw)
    entries = []
    for node in root.iter():
        if _local(node.tag) not in {"item", "entry"}:
            continue
        title = _first_text(node, {"title"})
        url = canonical_url(_entry_link(node))
        if not title or not url.startswith(("http://", "https://")):
            continue
        summary = _first_text(node, {"description", "summary", "content"})[:700]
        published = _published_iso(_first_text(node, {"pubdate", "published", "updated", "date"}))
        key = hashlib.sha256(url.encode("utf-8")).hexdigest()[:20]
        entries.append(FeedEntry(key=key, title=title, url=url, source=source.name,
                                 summary=summary, published=published, priority=source.priority))
    return entries


def _published_dt(entry: FeedEntry) -> datetime:
    if not entry.published:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(entry.published.replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)


def _load_seen(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "seen": {}}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("seen", {}), dict):
        return {"version": 1, "seen": {}}
    return value

def _save_seen(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _extract_json(text: str) -> dict[str, Any]:
    value = text.strip()
    start, end = value.find("{"), value.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("model did not return JSON")
    parsed = json.loads(value[start:end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("model JSON must be an object")
    return parsed


def select_entries(entries: list[FeedEntry], provider, limit: int = 3) -> list[FeedEntry]:
    if not entries:
        return []
    if len(entries) <= limit:
        return entries
    candidates = []
    for entry in entries[:30]:
        candidates.append({"id": entry.key, "title": entry.title, "source": entry.source,
                           "published": entry.published, "summary": entry.summary[:500]})
    prompt = f"""You are the editor for a private cognitive-training feed. Select at most {limit} articles that are most useful for independent judgment practice.
Prefer articles with a real decision, tradeoff, causal claim, disagreement, uncertainty, or model of how the world works. Deprioritize pure announcements, thin news updates, listicles, repetitive stories, and marketing.
Do not assume any source is correct. Use only the supplied metadata.
Return JSON only: {{"selected":["candidate_id", ...]}}.

Candidates:
{json.dumps(candidates, ensure_ascii=False)}
"""
    try:
        selected_ids = _extract_json(provider.answer(prompt)).get("selected", [])
        allowed = {entry.key: entry for entry in entries}
        chosen = [allowed[str(key)] for key in selected_ids if str(key) in allowed]
        if chosen:
            return chosen[:limit]
    except Exception:
        pass
    return entries[:limit]


class RssAggregator:
    def __init__(self, *, sources_path: Path, seen_path: Path, items_path: Path,
                 provider, max_selected: int = 3, max_age_hours: int = 96) -> None:
        self.sources_path = Path(sources_path)
        self.seen_path = Path(seen_path)
        self.items_path = Path(items_path)
        self.provider = provider
        self.max_selected = max(1, int(max_selected))
        self.max_age_hours = max(1, int(max_age_hours))

    def refresh(self) -> dict[str, Any]:
        state = _load_seen(self.seen_path)
        seen = state.setdefault("seen", {})
        errors: list[str] = []
        discovered: dict[str, FeedEntry] = {}
        source_count = 0
        for source in load_sources(self.sources_path):
            if not source.enabled:
                continue
            try:
                source_count += 1
                for entry in fetch_feed(source):
                    discovered.setdefault(entry.key, entry)
            except Exception as exc:
                errors.append(f"{source.name}: {exc}")

        cutoff = datetime.now(timezone.utc) - timedelta(hours=self.max_age_hours)
        fresh = [entry for entry in discovered.values()
                 if not entry.published or _published_dt(entry) >= cutoff]
        fresh.sort(key=lambda item: (item.priority, _published_dt(item)), reverse=True)
        unseen = [entry for entry in fresh if entry.key not in seen][:30]
        selected = select_entries(unseen, self.provider, self.max_selected)
        selected_keys = {entry.key for entry in selected}
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        added: list[dict[str, str]] = []
        failed: list[dict[str, str]] = []
        for entry in selected:
            try:
                article, detected_source = fetch_article(entry.url)
                item = prepare_item(article, self.provider,
                                    source=detected_source or entry.source, url=entry.url)
                item = replace(item, title=entry.title.strip() or item.title)
                append_item(self.items_path, item)
                added.append({"id": item.id, "title": item.title, "source": item.source})
                seen[entry.key] = {"url": entry.url, "title": entry.title,
                                   "source": entry.source, "status": "added", "seen_at": now}
            except Exception as exc:
                failed.append({"title": entry.title, "source": entry.source, "error": str(exc)[:240]})

        for entry in unseen:
            if entry.key in selected_keys:
                continue
            seen[entry.key] = {"url": entry.url, "title": entry.title,
                               "source": entry.source, "status": "skipped", "seen_at": now}
        state["last_refresh"] = now
        _save_seen(self.seen_path, state)
        return {"sources": source_count, "discovered": len(discovered), "fresh": len(fresh),
                "unseen": len(unseen), "selected": len(selected), "added": added,
                "failed": failed, "source_errors": errors}
