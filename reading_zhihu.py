from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from reading_prepare import append_item, prepare_item


def _canonical_url(url: str) -> str:
    parts = urlsplit(url.strip())
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not k.lower().startswith("utm_")]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def _plain_text(value: str) -> str:
    text = re.sub(r"<[^>]+>", "", value or "")
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "seen": {}}
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else {"version": 1, "seen": {}}


def _save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


class ZhihuCli:
    def __init__(self, command: str | Path | None = None, *, timeout_seconds: int = 25) -> None:
        if command is None:
            base = Path(os.environ.get("LOCALAPPDATA", "")) / "ZhihuCLI" / "current" / "zhihu-cli.exe"
            command = base
        self.command = str(command)
        self.timeout_seconds = timeout_seconds
        if not Path(self.command).exists():
            raise RuntimeError("zhihu-cli is not installed")

    def search(self, query: str, *, count: int = 10) -> list[dict[str, Any]]:
        completed = subprocess.run(
            [self.command, "search", "zhihu", "--query", query, "--count", str(count)],
            capture_output=True, timeout=self.timeout_seconds, check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError("zhihu-cli search failed")
        payload = json.loads(completed.stdout.decode("utf-8", errors="replace"))
        items = payload.get("Data", {}).get("Items", [])
        return items if isinstance(items, list) else []


DEFAULT_AUTHORS = (
    {
        "id": "mocangli",
        "name": "墨苍离",
        "min_votes": 80,
        "queries": ("墨苍离 文章", "墨苍离 人生", "墨苍离 心理", "墨苍离 财富"),
    },
    {
        "id": "babuganchan",
        "name": "八步赶蝉",
        "min_votes": 500,
        "queries": (
            "为什么说上大学不谈一场恋爱是一种人生遗憾",
            "想问和女生第几次约会尝试牵手比较好",
        ),
    },
)


class ZhihuQualitySource:
    def __init__(self, *, items_path: Path, state_path: Path, provider,
                 cli: ZhihuCli | None = None, max_per_author: int = 1) -> None:
        self.items_path = Path(items_path)
        self.state_path = Path(state_path)
        self.provider = provider
        self.cli = cli or ZhihuCli()
        self.max_per_author = max(1, int(max_per_author))

    def _collect(self, author: dict[str, Any]) -> list[dict[str, Any]]:
        by_url: dict[str, dict[str, Any]] = {}
        for query in author["queries"]:
            for item in self.cli.search(str(query), count=10):
                if str(item.get("AuthorName", "")).strip() != author["name"]:
                    continue
                if str(item.get("ContentType", "")) not in {"Answer", "Article"}:
                    continue
                url = _canonical_url(str(item.get("Url", "")))
                text = _plain_text(str(item.get("ContentText", "")))
                votes = int(item.get("VoteUpCount", 0) or 0)
                if not url or len(text) < 300 or votes < int(author["min_votes"]):
                    continue
                item = dict(item)
                item["_url"] = url
                item["_text"] = text
                item["_score"] = votes + min(len(text), 4000) / 100
                by_url[url] = item
        return sorted(by_url.values(), key=lambda x: float(x["_score"]), reverse=True)


    def refresh(self) -> dict[str, Any]:
        if self.provider is None:
            return {"source": "知乎", "added": [], "failed": []}
        state = _load_state(self.state_path)
        last = str(state.get("last_refresh", "")).strip()
        if last:
            try:
                previous = datetime.fromisoformat(last.replace("Z", "+00:00"))
                if datetime.now(timezone.utc) - previous < timedelta(hours=20):
                    return {"source": "知乎", "added": [], "failed": [], "skipped": "cooldown"}
            except ValueError:
                pass
        seen = state.setdefault("seen", {})
        added: list[dict[str, Any]] = []
        failed: list[dict[str, str]] = []
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        for author in DEFAULT_AUTHORS:
            try:
                candidates = [item for item in self._collect(author) if item["_url"] not in seen]
            except Exception as exc:
                failed.append({"source": f"知乎 · {author['name']}", "error": str(exc)[:240]})
                continue
            for item in candidates[:self.max_per_author]:
                try:
                    article = f"标题：{item.get('Title', '')}\n\n{item['_text']}"
                    source = f"知乎 · {author['name']}"
                    card = prepare_item(article, self.provider, source=source, url=item["_url"])
                    card = replace(card, title=str(item.get("Title", card.title)).strip() or card.title,
                                   source=source, url=item["_url"],
                                   why_selected="指定来源 · 知乎优质内容")
                    append_item(self.items_path, card)
                    seen[item["_url"]] = {
                        "author": author["name"], "title": str(item.get("Title", "")),
                        "votes": int(item.get("VoteUpCount", 0) or 0), "item_id": card.id,
                        "processed_at": now,
                    }
                    _save_state(self.state_path, state)
                    added.append({"id": card.id, "title": card.title, "source": source,
                                  "votes": int(item.get("VoteUpCount", 0) or 0)})
                except Exception as exc:
                    failed.append({"source": f"知乎 · {author['name']}",
                                   "title": str(item.get("Title", "")), "error": str(exc)[:240]})
        state["last_refresh"] = now
        _save_state(self.state_path, state)
        return {"source": "知乎", "added": added, "failed": failed}
