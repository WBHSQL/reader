from __future__ import annotations

from difflib import SequenceMatcher
import hashlib
import re
import unicodedata


def normalize_title(value: str) -> str:
    text = unicodedata.normalize("NFKC", value or "").strip().lower()
    return re.sub(r"[\s\W_]+", "", text, flags=re.UNICODE)


def normalize_body(value: str) -> str:
    text = unicodedata.normalize("NFKC", value or "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\u200b", "").replace("\ufeff", "")
    lines = []
    for raw in text.split("\n"):
        line = re.sub(r"[ \t\u00a0]+", " ", raw).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def text_fingerprint(value: str) -> str:
    return hashlib.sha256(normalize_body(value).encode("utf-8")).hexdigest()


def titles_match(expected: str, actual: str) -> bool:
    a, b = normalize_title(expected), normalize_title(actual)
    if not a or not b:
        return False
    if a == b:
        return True
    return SequenceMatcher(None, a, b, autojunk=False).ratio() >= 0.94


def body_similarity(left: str, right: str) -> float:
    a, b = normalize_body(left), normalize_body(right)
    if not a or not b:
        return 0.0
    ratio = min(len(a), len(b)) / max(len(a), len(b))
    if ratio < 0.96:
        return 0.0
    return SequenceMatcher(None, a, b, autojunk=False).ratio()


def copies_are_effectively_exact(left: str, right: str) -> bool:
    return body_similarity(left, right) >= 0.995
