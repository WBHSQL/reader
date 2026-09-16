from __future__ import annotations

import os
from pathlib import Path
import time


DEFAULT_TTL_HOURS = 72


def cache_dir() -> Path | None:
    raw = os.environ.get("READING_ARTICLE_CACHE_DIR", "").strip()
    return Path(raw) if raw else None


def _path(item_id: str) -> Path | None:
    root = cache_dir()
    if root is None:
        return None
    safe = "".join(ch for ch in item_id if ch.isalnum() or ch in "-_")
    return root / f"{safe}.txt"


def cleanup_expired(*, ttl_hours: int = DEFAULT_TTL_HOURS) -> None:
    root = cache_dir()
    if root is None or not root.exists():
        return
    cutoff = time.time() - max(1, ttl_hours) * 3600
    for path in root.glob("*.txt"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
        except OSError:
            continue


def put_article(item_id: str, body: str) -> None:
    path = _path(item_id)
    text = body.strip()
    if path is None or not text:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    cleanup_expired()
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def get_article(item_id: str) -> str:
    path = _path(item_id)
    if path is None or not path.exists():
        return ""
    ttl = int(os.environ.get("READING_ARTICLE_CACHE_TTL_HOURS", str(DEFAULT_TTL_HOURS)))
    if path.stat().st_mtime < time.time() - max(1, ttl) * 3600:
        path.unlink(missing_ok=True)
        return ""
    return path.read_text(encoding="utf-8").strip()


def delete_article(item_id: str) -> None:
    path = _path(item_id)
    if path is not None:
        path.unlink(missing_ok=True)
