from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from html import unescape
import json
import os
from pathlib import Path
import re
from typing import Any
from urllib import request

from reading_prepare import append_item, fetch_article, prepare_item

ARCHIVE_BASE = "https://jiyichengzai.com"
INDEX_URL = ARCHIVE_BASE + "/data/articles-index.json"
WECHAT_RE = re.compile(r'href=["\']([^"\']*mp\.weixin\.qq\.com[^"\']*)["\']')


def _get_bytes(url: str, *, limit: int = 4_000_000) -> bytes:
    req = request.Request(url, headers={"User-Agent": "Mozilla/5.0 CognitiveFeed/0.3"})
    with request.urlopen(req, timeout=30) as response:
        raw = response.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("source response is too large")
    return raw


def _load_json(url: str) -> Any:
    return json.loads(_get_bytes(url, limit=12_000_000).decode("utf-8", errors="replace"))


def _archive_url(path: str) -> str:
    return ARCHIVE_BASE + (path if path.startswith("/") else "/" + path)


def extract_wechat_url(archive_url: str) -> str:
    html = _get_bytes(archive_url).decode("utf-8", errors="replace")
    match = WECHAT_RE.search(html)
    return unescape(match.group(1)) if match else ""


def source_label(buckets: list[str]) -> str:
    if "jiyichengzai-3" in buckets:
        return "微信公众号 · 记忆承载3"
    return "微信公众号 · 记忆承载"


def _read_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "seen": {}}
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else {"version": 1, "seen": {}}


def _save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _key(row: dict[str, Any]) -> str:
    return str(row.get("urlPath", "")).strip()


def _is_public_target(row: dict[str, Any]) -> bool:
    buckets = [str(x) for x in row.get("sourceBuckets", [])]
    return bool({"jiyichengzai", "jiyichengzai-3"} & set(buckets)) and not bool(row.get("isPaid"))


class JiyichengzaiPrimarySource:
    def __init__(self, *, items_path: Path, state_path: Path, provider, max_age_days: int = 4) -> None:
        self.items_path = Path(items_path)
        self.state_path = Path(state_path)
        self.provider = provider
        self.max_age_days = max(1, int(max_age_days))

    def refresh(self) -> dict[str, Any]:
        payload = _load_json(INDEX_URL)
        rows = payload.get("articles", []) if isinstance(payload, dict) else []
        if not isinstance(rows, list):
            raise ValueError("记忆承载索引格式异常")
        state = _read_state(self.state_path)
        seen = state.setdefault("seen", {})
        public_rows = [row for row in rows if isinstance(row, dict) and _is_public_target(row)]
        public_rows.sort(key=lambda row: (str(row.get("date", "")), _key(row)), reverse=True)
        if not public_rows:
            return {"source": "记忆承载", "added": [], "paid": [], "failed": []}

        baseline = str(state.get("baseline_date", "")).strip()
        if not baseline:
            baseline = str(public_rows[0].get("date", ""))
            state["baseline_date"] = baseline
            _save_state(self.state_path, state)
        not_before = os.environ.get("READING_NOT_BEFORE", "").strip()
        not_before_date = not_before[:10] if len(not_before) >= 10 else ""
        candidates = [row for row in public_rows
                      if str(row.get("date", "")) >= baseline
                      and (not not_before_date or str(row.get("date", "")) >= not_before_date)
                      and _key(row) not in seen]
        candidates.sort(key=lambda row: (str(row.get("date", "")), _key(row)))

        added: list[dict[str, str]] = []
        failed: list[dict[str, str]] = []
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        for row in candidates:
            key = _key(row)
            archive_url = _archive_url(key)
            buckets = [str(x) for x in row.get("sourceBuckets", [])]
            label = source_label(buckets)
            try:
                original_url = extract_wechat_url(archive_url) or archive_url
                article, _ = fetch_article(archive_url)
                item = prepare_item(article, self.provider, source=label, url=original_url)
                item = replace(item, title=str(row.get("title", item.title)).strip() or item.title,
                               source=label, url=original_url,
                               why_selected="指定来源 · 每日必读")
                append_item(self.items_path, item)
                seen[key] = {"date": row.get("date", ""), "title": item.title,
                             "source": label, "original_url": original_url, "seen_at": now}
                _save_state(self.state_path, state)
                added.append({"id": item.id, "title": item.title, "source": label})
            except Exception as exc:
                failed.append({"title": str(row.get("title", "")), "error": str(exc)[:240]})
        paid = []
        for row in rows:
            if not isinstance(row, dict) or not row.get("isPaid"):
                continue
            if str(row.get("date", "")) < baseline:
                continue
            if not_before_date and str(row.get("date", "")) < not_before_date:
                continue
            paid.append({"title": str(row.get("title", "")), "date": str(row.get("date", "")),
                         "archive_url": _archive_url(_key(row))})

        state["last_refresh"] = now
        _save_state(self.state_path, state)
        return {"source": "记忆承载", "added": added, "paid": paid, "failed": failed,
                "baseline_date": baseline}
