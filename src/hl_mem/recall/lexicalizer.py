"""Deterministic lexical preparation for tokenized FTS5 indexes."""

from __future__ import annotations

import re
import threading
import unicodedata
from importlib.resources import files

import jieba

from hl_mem.recall.porter_stemmer import PorterStemmer
from hl_mem.settings import Settings

_TECHNICAL_IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:(?:[-_.+/][A-Za-z0-9]+)+)?")
_IDENTIFIER_SEPARATOR = re.compile(r"[-_.+/]+")
_ENGLISH_WORD = re.compile(r"[A-Za-z][A-Za-z']*[A-Za-z]|[A-Za-z]")


def _load_resource_lines(name: str) -> tuple[str, ...]:
    content = files(__package__).joinpath("resources", name).read_text(encoding="utf-8")
    return tuple(line.strip() for line in content.splitlines() if line.strip())


_STOPWORDS = frozenset(_load_resource_lines("stopwords.txt"))
_ENGLISH_STOPWORDS = frozenset(_load_resource_lines("stopwords_en.txt"))
_TOKENIZER = jieba.Tokenizer()
for _term in _load_resource_lines("domain_terms.txt"):
    _TOKENIZER.add_word(_term)

_PORTER_STEMMER = PorterStemmer()
_ENGLISH_STOPWORD_STEMS = frozenset(_PORTER_STEMMER.stem(w) for w in _ENGLISH_STOPWORDS)

_stemmer_local = threading.local()


def _get_stemmer() -> PorterStemmer:
    """Return a thread-local PorterStemmer (the class is stateful, not thread-safe)."""
    stemmer = getattr(_stemmer_local, "stemmer", None)
    if stemmer is None:
        stemmer = PorterStemmer()
        _stemmer_local.stemmer = stemmer
    return stemmer


_fts_language_cache: str | None = None


def _get_default_fts_language() -> str:
    """Lazily read the default fts_language from Settings (avoids import-time cycles)."""
    global _fts_language_cache
    if _fts_language_cache is None:
        _fts_language_cache = Settings().fts_language
    return _fts_language_cache


def _is_searchable(token: str) -> bool:
    return any(character.isalnum() for character in token)


def _is_chinese_text(text: str) -> bool:
    """Detect whether text contains a meaningful proportion of CJK characters."""
    if not text:
        return False
    chinese_count = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    return chinese_count > len(text) * 0.1


def _stem_english_word(word: str) -> str | None:
    """Stem a single English word; return ``None`` if it's a stopword."""
    lowered = word.lower()
    if lowered in _ENGLISH_STOPWORDS:
        return None
    stemmed = _get_stemmer().stem(lowered)
    if stemmed in _ENGLISH_STOPWORD_STEMS:
        return None
    return stemmed


def tokenize_for_fts(text: str, *, language: str | None = None) -> tuple[str, ...]:
    """Normalize and tokenize text into stable, unique FTS terms.

    Parameters
    ----------
    text:
        Raw input text.
    language:
        Override the tokenizer mode. When ``None`` (default), the value from
        :attr:`Settings.fts_language` is used. ``"auto"`` detects Chinese vs
        English per text; ``"zh"`` forces jieba; ``"en"`` forces the English
        stemmer.
    """
    normalized = unicodedata.normalize("NFKC", text)
    if not normalized.strip():
        return ()

    mode = language or _get_default_fts_language()

    tokens: list[str] = []
    seen: set[str] = set()

    def append(token: str) -> None:
        candidate = token.strip()
        key = candidate.casefold()
        if not candidate or candidate in _STOPWORDS or not _is_searchable(candidate) or key in seen:
            return
        seen.add(key)
        tokens.append(candidate)

    def append_english_segments(span: str) -> None:
        """Split English text into words, stem, and filter stopwords."""
        for raw in _ENGLISH_WORD.findall(span):
            stemmed = _stem_english_word(raw)
            if stemmed:
                append(stemmed)

    def append_chinese_span(span: str) -> None:
        for token in _TOKENIZER.cut(span, cut_all=False):
            append(token)

    def append_identifier_stemmed(identifier: str) -> None:
        """Split technical identifier into sub-segments and stem English parts."""
        for segment in _IDENTIFIER_SEPARATOR.split(identifier):
            if not segment:
                continue
            if segment[0].isdigit():
                append(segment)
                continue
            stemmed = _stem_english_word(segment)
            if stemmed:
                append(stemmed)

    def append_identifier_raw(identifier: str) -> None:
        """Keep technical identifier as-is plus its sub-segments (original behavior)."""
        append(identifier)
        for segment in _IDENTIFIER_SEPARATOR.split(identifier):
            append(segment)

    # ---- Dispatch based on mode ----

    if mode == "en":
        # Force English-only: extract identifiers and plain words → stem → filter
        cursor = 0
        for match in _TECHNICAL_IDENTIFIER.finditer(normalized):
            append_english_segments(normalized[cursor : match.start()])
            append_identifier_stemmed(match.group(0))
            cursor = match.end()
        append_english_segments(normalized[cursor:])
        return tuple(tokens)

    if mode == "zh":
        # Force Chinese-only path (original behavior, no English stemming)
        cursor = 0
        for match in _TECHNICAL_IDENTIFIER.finditer(normalized):
            append_chinese_span(normalized[cursor : match.start()])
            append_identifier_raw(match.group(0))
            cursor = match.end()
        append_chinese_span(normalized[cursor:])
        return tuple(tokens)

    # ---- auto mode (default) ----

    if _is_chinese_text(normalized):
        # Mixed or Chinese text: jieba for Chinese spans, identifiers kept raw (original behavior)
        cursor = 0
        for match in _TECHNICAL_IDENTIFIER.finditer(normalized):
            append_chinese_span(normalized[cursor : match.start()])
            append_identifier_raw(match.group(0))
            cursor = match.end()
        append_chinese_span(normalized[cursor:])
    else:
        # Pure English text: extract identifiers and words → stem → filter
        cursor = 0
        for match in _TECHNICAL_IDENTIFIER.finditer(normalized):
            append_english_segments(normalized[cursor : match.start()])
            append_identifier_stemmed(match.group(0))
            cursor = match.end()
        append_english_segments(normalized[cursor:])

    return tuple(tokens)


def prepare_fts_document(text: str) -> str:
    """Return a whitespace-delimited token stream for FTS document storage."""
    return " ".join(tokenize_for_fts(text))


def prepare_fts_query(text: str) -> str:
    """Return a safely quoted AND query for an FTS5 MATCH expression."""
    return " AND ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokenize_for_fts(text))
