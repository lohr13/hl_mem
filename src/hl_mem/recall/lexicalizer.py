"""Deterministic lexical preparation for tokenized FTS5 indexes."""

from __future__ import annotations

import re
import unicodedata
from importlib.resources import files

import jieba

_TECHNICAL_IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:(?:[-_.+/][A-Za-z0-9]+)+)?")
_IDENTIFIER_SEPARATOR = re.compile(r"[-_.+/]+")


def _load_resource_lines(name: str) -> tuple[str, ...]:
    content = files(__package__).joinpath("resources", name).read_text(encoding="utf-8")
    return tuple(line.strip() for line in content.splitlines() if line.strip())


_STOPWORDS = frozenset(_load_resource_lines("stopwords.txt"))
_TOKENIZER = jieba.Tokenizer()
for _term in _load_resource_lines("domain_terms.txt"):
    _TOKENIZER.add_word(_term)


def _is_searchable(token: str) -> bool:
    return any(character.isalnum() for character in token)


def tokenize_for_fts(text: str) -> tuple[str, ...]:
    """Normalize and tokenize text into stable, unique FTS terms."""
    normalized = unicodedata.normalize("NFKC", text)
    if not normalized.strip():
        return ()

    tokens: list[str] = []
    seen: set[str] = set()

    def append(token: str) -> None:
        candidate = token.strip()
        key = candidate.casefold()
        if not candidate or candidate in _STOPWORDS or not _is_searchable(candidate) or key in seen:
            return
        seen.add(key)
        tokens.append(candidate)

    def append_chinese_span(span: str) -> None:
        for token in _TOKENIZER.cut(span, cut_all=False):
            append(token)

    cursor = 0
    for match in _TECHNICAL_IDENTIFIER.finditer(normalized):
        append_chinese_span(normalized[cursor : match.start()])
        identifier = match.group(0)
        append(identifier)
        for segment in _IDENTIFIER_SEPARATOR.split(identifier):
            append(segment)
        cursor = match.end()
    append_chinese_span(normalized[cursor:])
    return tuple(tokens)


def prepare_fts_document(text: str) -> str:
    """Return a whitespace-delimited token stream for FTS document storage."""
    return " ".join(tokenize_for_fts(text))


def prepare_fts_query(text: str) -> str:
    """Return a safely quoted AND query for an FTS5 MATCH expression."""
    return " AND ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokenize_for_fts(text))
