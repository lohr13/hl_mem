"""Deterministic lexical preparation for tokenized FTS5 indexes."""

from __future__ import annotations

import re
import threading
import unicodedata
from importlib.resources import files

import jieba

from hl_mem.recall.porter_stemmer import PorterStemmer
from hl_mem.settings import FtsLanguage

_TECHNICAL_IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:(?:[-_.+/][A-Za-z0-9]+)+)?")
_IDENTIFIER_SEPARATOR = re.compile(r"[-_.+/]+")
# English words (with optional apostrophes) plus standalone numbers, which are
# kept in the English path to match the zh/jieba behavior (e.g. years, versions).
_ENGLISH_WORD = re.compile(r"[A-Za-z][A-Za-z']*[A-Za-z]|[A-Za-z]|\d+")


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


def tokenize_for_fts(text: str, *, language: FtsLanguage | None = None) -> tuple[str, ...]:
    """Normalize and tokenize text into stable, unique FTS terms.

    Parameters
    ----------
    text:
        Raw input text.
    language:
        Override the tokenizer mode. ``None`` uses the library default
        ``"auto"``; repositories pass their active application setting
        explicitly. ``"auto"`` detects Chinese vs English per text; ``"zh"``
        forces jieba; ``"en"`` forces the English stemmer.
    """
    normalized = unicodedata.normalize("NFKC", text)
    if not normalized.strip():
        return ()

    mode = language or "auto"

    tokens: list[str] = []
    seen: set[str] = set()
    # Single ASCII letters (variables like x, version prefixes like v) are noise
    # in English FTS indexes. zh mode is untouched: Chinese single chars are
    # meaningful and non-ASCII, so the filter never applies to them.
    drop_single_letter = mode != "zh"

    def append(token: str) -> None:
        candidate = token.strip()
        key = candidate.casefold()
        if not candidate or candidate in _STOPWORDS or not _is_searchable(candidate) or key in seen:
            return
        if drop_single_letter and len(key) == 1 and key.isascii() and key.isalpha():
            return
        seen.add(key)
        tokens.append(candidate)

    def append_english_segments(span: str) -> None:
        """Split English text into words, stem, and filter stopwords."""
        for raw in _ENGLISH_WORD.findall(span):
            stemmed = _stem_english_word(raw)
            if stemmed:
                append(stemmed)

    def append_english_segments_both(span: str) -> None:
        """Split English text into words, append both raw and stemmed versions.

        Used in ``auto`` mode so that FTS indexes carry the raw surface form
        (backward-compatible with existing indexes) *and* a stemmed form for
        cross-morphology matching (running→run, databases→database).
        """
        for raw in _ENGLISH_WORD.findall(span):
            lowered = raw.lower()
            if lowered in _ENGLISH_STOPWORDS:
                continue
            append(lowered)
            stemmed = _get_stemmer().stem(lowered)
            if stemmed not in _ENGLISH_STOPWORD_STEMS and stemmed != lowered:
                append(stemmed)

    def append_chinese_span(span: str) -> None:
        for token in _TOKENIZER.cut(span, cut_all=False):
            append(token)

    def append_mixed_span(span: str) -> None:
        """Chinese span: jieba tokens (unchanged) + raw/stemmed English words.

        Identical to :func:`append_chinese_span` for the jieba path, but also
        emits raw + stemmed forms for any English words embedded in the span.
        """
        for token in _TOKENIZER.cut(span, cut_all=False):
            append(token)
        append_english_segments_both(span)

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

    def append_identifier_both(identifier: str) -> None:
        """Append raw identifier (full + sub-segments) and stemmed sub-segments.

        Combines :func:`append_identifier_raw` (backward-compatible raw forms)
        with :func:`append_identifier_stemmed` (stemmed sub-segments for
        cross-morphology matching). Stopwords are filtered at the segment level
        so neither raw nor stemmed stopword forms pollute the index.
        """
        # If the identifier is a single segment that is a stopword, skip entirely
        if _IDENTIFIER_SEPARATOR.search(identifier) is None:
            if identifier.lower() in _ENGLISH_STOPWORDS:
                return
        append(identifier)
        for segment in _IDENTIFIER_SEPARATOR.split(identifier):
            if not segment:
                continue
            seg_lower = segment.lower()
            if seg_lower in _ENGLISH_STOPWORDS:
                continue
            append(segment)
            if segment[0].isdigit():
                continue
            stemmed = _stem_english_word(segment)
            if stemmed:
                append(stemmed)

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
        # Mixed or Chinese text: jieba for Chinese spans, identifiers kept raw
        # (backward-compatible) plus stemmed sub-segments; embedded English
        # words also get raw + stemmed forms for cross-morphology matching.
        cursor = 0
        for match in _TECHNICAL_IDENTIFIER.finditer(normalized):
            append_mixed_span(normalized[cursor : match.start()])
            append_identifier_both(match.group(0))
            cursor = match.end()
        append_mixed_span(normalized[cursor:])
    else:
        # Pure English text: identifiers and words both emit raw + stemmed
        # forms (raw keeps existing FTS indexes valid; stemmed enables
        # cross-morphology matching).
        cursor = 0
        for match in _TECHNICAL_IDENTIFIER.finditer(normalized):
            append_english_segments_both(normalized[cursor : match.start()])
            append_identifier_both(match.group(0))
            cursor = match.end()
        append_english_segments_both(normalized[cursor:])

    return tuple(tokens)


def prepare_fts_document(text: str, *, language: FtsLanguage | None = None) -> str:
    """Return a whitespace-delimited token stream for FTS document storage."""
    return " ".join(tokenize_for_fts(text, language=language))


def _join_fts_terms(tokens: tuple[str, ...]) -> str:
    return " AND ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens)


def _auto_query_variants(text: str) -> tuple[tuple[str, ...], ...]:
    """Return conjunctive raw and stemmed branches for ``auto`` queries.

    Version 0.24.0 indexes contain only raw English terms, while newer indexes
    contain raw and stemmed forms. Keeping each form in its own AND branch lets
    one query match both layouts without weakening multi-term queries into a
    global OR.
    """
    auto_tokens = tokenize_for_fts(text, language="auto")
    raw_keys = {token.casefold() for token in tokenize_for_fts(text, language="zh")}
    stem_keys = {token.casefold() for token in tokenize_for_fts(text, language="en")}
    # The English tokenizer intentionally ignores CJK spans. They constrain both
    # branches of a mixed-language query.
    stem_keys.update(token.casefold() for token in auto_tokens if any("\u4e00" <= char <= "\u9fff" for char in token))

    raw_tokens = tuple(token for token in auto_tokens if token.casefold() in raw_keys)
    stem_tokens = tuple(token for token in auto_tokens if token.casefold() in stem_keys)
    variants: list[tuple[str, ...]] = []
    seen: set[tuple[str, ...]] = set()
    for tokens in (raw_tokens, stem_tokens):
        if not tokens:
            continue
        key = tuple(token.casefold() for token in tokens)
        if key not in seen:
            seen.add(key)
            variants.append(tokens)
    return tuple(variants)


def prepare_fts_query(text: str, *, language: FtsLanguage | None = None) -> str:
    """Return a safely quoted conjunctive FTS5 MATCH expression."""
    mode = language or "auto"
    if mode != "auto":
        return _join_fts_terms(tokenize_for_fts(text, language=mode))

    branches = tuple(_join_fts_terms(tokens) for tokens in _auto_query_variants(text))
    if len(branches) < 2:
        return branches[0] if branches else ""
    return " OR ".join(f"({branch})" for branch in branches)
