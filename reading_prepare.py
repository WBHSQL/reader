from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any
from urllib import request
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from reading_app import ReadingItem
from reading_questioning import build_card_prompt, build_understanding_prompt


def fetch_article(url: str) -> tuple[str, str]:
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("only http/https article URLs are supported")
    req = request.Request(url, headers={"User-Agent": "Mozilla/5.0 CognitiveFeed/0.2"})
    with request.urlopen(req, timeout=25) as response:
        content_type = response.headers.get("Content-Type", "")
        if "html" not in content_type.lower():
            raise ValueError("URL is not an HTML article")
        raw = response.read(3_000_001)
    if len(raw) > 3_000_000:
        raise ValueError("article page is too large")
    html = raw.decode("utf-8", errors="replace")
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript", "svg", "nav", "footer", "aside", "form"]):
        tag.decompose()
    title_tag = soup.find("meta", attrs={"property": "og:title"})
    title = (title_tag.get("content", "") if title_tag else "").strip()
    if not title and soup.title:
        title = soup.title.get_text(" ", strip=True)
    site_tag = soup.find("meta", attrs={"property": "og:site_name"})
    source = (site_tag.get("content", "") if site_tag else "").strip() or parsed.netloc.removeprefix("www.")
    container = soup.find("article") or soup.find("main") or soup.find(attrs={"role": "main"}) or soup.body
    if container is None:
        raise ValueError("article body not found")
    blocks = []
    seen = set()
    for node in container.find_all(["h1", "h2", "h3", "p", "li"]):
        text = " ".join(node.stripped_strings).strip()
        if len(text) < 20 or text in seen:
            continue
        seen.add(text)
        blocks.append(text)
    article = "\n\n".join(blocks).strip()
    if len(article) < 500:
        article = " ".join(container.stripped_strings).strip()
    if len(article) < 300:
        raise ValueError("could not extract enough article text")
    article = article[:60000]
    if title:
        article = f"标题：{title}\n\n{article}"
    return article, source


def _extract_json(text: str) -> dict[str, Any]:
    value = text.strip()
    if value.startswith("```"):
        lines = value.splitlines()
        value = "\n".join(lines[1:-1]).strip()
    start, end = value.find("{"), value.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("model did not return JSON")
    parsed = json.loads(value[start:end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("model JSON must be an object")
    return parsed


def _answer_source_only(provider, prompt: str) -> str:
    """Use the underlying model without personal context for source interpretation."""
    delegate = getattr(provider, "delegate", provider)
    if not hasattr(delegate, "answer"):
        raise RuntimeError("reading provider cannot perform source-only analysis")
    return delegate.answer(prompt)


def _understand_article(article: str, provider, *, source: str, url: str = "") -> dict[str, Any]:
    prompt = build_understanding_prompt(article, source=source, url=url)
    data = _extract_json(_answer_source_only(provider, prompt))
    required = ("central_thesis", "neutral_core_problem", "argument_spine", "examples", "title_role", "title_role_reason", "author_reasoning", "author_conclusion")
    missing = [key for key in required if not data.get(key)]
    if missing:
        raise ValueError(f"article understanding map missing fields: {missing}")
    if data["title_role"] not in {"core", "example", "hook", "mixed"}:
        raise ValueError("article understanding map has invalid title_role")
    if not isinstance(data["argument_spine"], list) or len(data["argument_spine"]) < 2:
        raise ValueError("article understanding map needs an argument spine")
    if not isinstance(data["examples"], list):
        raise ValueError("article understanding map examples must be a list")
    return data


def _build_card_prompt(article: str, understanding: dict[str, Any], *, source: str, url: str = "") -> str:
    return build_card_prompt(article, understanding, source=source, url=url)


def prepare_item(article: str, provider, *, source: str, url: str = "", raw_body: str | None = None, context_query: str = "") -> ReadingItem:
    if not article.strip():
        raise ValueError("article is empty")
    understanding = _understand_article(article, provider, source=source, url=url)
    prompt = _build_card_prompt(article, understanding, source=source, url=url)
    answer = provider.answer_for(prompt, context_query or source) if hasattr(provider, "answer_for") else provider.answer(prompt)
    data = _extract_json(answer)
    data["author_reasoning"] = str(understanding["author_reasoning"]).strip()
    data["author_conclusion"] = str(understanding["author_conclusion"]).strip()
    identity = url or (str(data.get("title", "")) + article[:300])
    item_id = "item-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
    data.update({"id": item_id, "source": source, "url": url})
    item = ReadingItem.from_dict(data)
    try:
        from reading_article_cache import put_article
        put_article(item.id, raw_body if raw_body is not None else article)
    except Exception:
        pass
    return item

def append_item(path: Path, item: ReadingItem) -> None:
    existing: list[dict[str, Any]] = []
    if path.exists():
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, list):
            raise ValueError("items file must contain a JSON list")
        existing = value
    serialized = item.blind() | {
        "author_reasoning": item.author_reasoning,
        "author_conclusion": item.author_conclusion,
    }
    existing = [value for value in existing if str(value.get("id")) != item.id]
    existing.append(serialized)
    path.write_text(json.dumps(existing, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="turn an article into a blind-first reading card")
    parser.add_argument("article", type=Path, help="UTF-8 text/markdown article file")
    parser.add_argument("--source", required=True)
    parser.add_argument("--url", default="")
    parser.add_argument("--items", type=Path, default=Path(__file__).with_name("reading_items.json"))
    args = parser.parse_args()
    from reading_app import build_reading_provider
    provider = build_reading_provider()
    if provider is None:
        raise RuntimeError("reading LLM provider is unavailable")
    article = args.article.read_text(encoding="utf-8")
    item = prepare_item(article, provider, source=args.source, url=args.url)
    append_item(args.items, item)
    print(f"added: {item.title} ({item.id})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
