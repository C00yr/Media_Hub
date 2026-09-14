from __future__ import annotations

import re
from pathlib import Path
from typing import Any


def knowledge_root() -> Path:
    candidates = [
        Path(__file__).resolve().parents[3] / "docs" / "knowledge",
        Path("/app/docs/knowledge"),
    ]
    return next((path for path in candidates if path.is_dir()), candidates[0])


def _frontmatter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}, text
    metadata: dict[str, str] = {}
    for line in text[4:end].splitlines():
        key, separator, value = line.partition(":")
        if separator:
            metadata[key.strip()] = value.strip().strip("[]")
    return metadata, text[end + 5 :]


def _terms(query: str) -> set[str]:
    normalized = " ".join(str(query or "").lower().split())
    terms = {item for item in re.findall(r"[a-z0-9%_-]+|[\u4e00-\u9fff]{2,}", normalized) if len(item) >= 2}
    for run in re.findall(r"[\u4e00-\u9fff]{3,}", normalized):
        terms.update(run[index : index + 2] for index in range(len(run) - 1))
    return terms


def search_knowledge(query: str, limit: int = 3) -> dict[str, Any]:
    terms = _terms(query)
    if not terms:
        return {"query": query, "items": [], "count": 0, "source": "local_versioned_knowledge"}
    scored: list[tuple[int, dict[str, Any]]] = []
    for path in sorted(knowledge_root().glob("*.md")):
        if path.name.lower() == "readme.md":
            continue
        text = path.read_text(encoding="utf-8")
        metadata, body = _frontmatter(text)
        haystack = f"{metadata.get('title', '')} {metadata.get('keywords', '')} {body}".lower()
        score = sum(5 if term in str(metadata.get("keywords") or "").lower() else 1 for term in terms if term in haystack)
        if score <= 0:
            continue
        paragraphs = [re.sub(r"\s+", " ", value).strip() for value in re.split(r"\n\s*\n", body) if value.strip() and not value.lstrip().startswith("#")]
        matching = [value for value in paragraphs if any(term in value.lower() for term in terms)]
        excerpt = (matching[0] if matching else (paragraphs[0] if paragraphs else ""))[:800]
        scored.append(
            (
                score,
                {
                    "title": metadata.get("title") or path.stem,
                    "updated_at": metadata.get("updated_at") or "",
                    "scope": metadata.get("scope") or "",
                    "excerpt": excerpt,
                    "document": path.name,
                },
            )
        )
    items = [item for _, item in sorted(scored, key=lambda value: (-value[0], value[1]["title"]))[: max(1, min(limit, 5))]]
    return {"query": query, "items": items, "count": len(items), "source": "local_versioned_knowledge"}
