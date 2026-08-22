"""Reusable privacy-safe sampling and aggregate verification for state corpora.

The real-source sampler retains only irreversible structural features. It
opens SQLite in read-only/query-only mode and never emits source text, ids,
timestamps, paths, or actor identifiers.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

_SIGNAL_PATTERNS = {
    "version": re.compile(r"(?i)(?:版本|version|release|v\d+)", re.UNICODE),
    "service_health": re.compile(r"(?i)(?:服务|service|healthy|running|挂了)", re.UNICODE),
    "process": re.compile(r"(?i)(?:进程|process)", re.UNICODE),
    "deployment": re.compile(r"(?i)(?:部署|deployment|deployed)", re.UNICODE),
    "connectivity": re.compile(r"(?i)(?:连接|connectivity|reachable|timeout)", re.UNICODE),
    "job": re.compile(r"(?i)(?:任务|job|queued)", re.UNICODE),
    "plan": re.compile(r"(?:计划|打算|将来|下周)", re.UNICODE),
    "negation": re.compile(r"(?:不是|并非|没有|尚未)", re.UNICODE),
    "quotation": re.compile(r"(?:引用|文档|据称|转述)", re.UNICODE),
}
_SAFE_REDACTION_TOKENS = tuple(
    sorted(
        {
            "当前版本",
            "生产环境",
            "预发环境",
            "开发环境",
            "测试环境",
            "并不是",
            "不可用",
            "当前",
            "现在",
            "目前",
            "版本",
            "服务",
            "进程",
            "部署",
            "连接",
            "任务",
            "状态",
            "计划",
            "要求",
            "文档",
            "历史",
            "曾经",
            "过去",
            "之前",
            "当时",
            "补录",
            "回滚",
            "升级",
            "正常",
            "异常",
            "健康",
            "运行",
            "停止",
            "失败",
            "完成",
            "可用",
            "不是",
            "没有",
            "尚未",
            "api",
            "service",
            "process",
            "deployment",
            "connection",
            "connectivity",
            "job",
            "version",
            "release",
            "current",
            "running",
            "stopped",
            "failed",
            "completed",
            "healthy",
            "unhealthy",
            "的",
            "是",
            "为",
            "时",
            "在",
            "和",
            "与",
            "且",
            "到",
            "已",
        },
        key=len,
        reverse=True,
    )
)
_VERSION_TOKEN_RE = re.compile(r"(?i)v?\d+(?:\.\d+){1,4}(?:[-+][a-z0-9.-]+)?")
_REDACTED_SEED_FIELDS = {
    "seed_id",
    "source_hash",
    "actor_class",
    "language_profile",
    "length_bucket",
    "punctuation_profile",
    "state_signals",
    "structure_runs",
    "redacted_skeleton",
}
_PLACEHOLDER_RE = re.compile(r"<(?:(?:HAN|ASCII|DIGIT|SPACE|PUNCT|OTHER):[1-9]\d?|VERSION)>")
_STRUCTURE_RUN_RE = re.compile(r"(?:han|ascii|digit|space|punct|other):[1-9]\d?")


def open_readonly_event_database(database_path: str | Path) -> sqlite3.Connection:
    """Open an existing event database without migrations or write access."""

    path = Path(database_path).resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _character_class(character: str) -> str:
    code = ord(character)
    if 0x3400 <= code <= 0x9FFF:
        return "han"
    if character.isascii() and character.isalpha():
        return "ascii"
    if character.isdigit():
        return "digit"
    if character.isspace():
        return "space"
    if unicodedata.category(character).startswith("P"):
        return "punct"
    return "other"


def _structure_runs(text: str) -> list[str]:
    runs: list[str] = []
    current = ""
    count = 0
    for character in text:
        category = _character_class(character)
        if category == current:
            count += 1
            continue
        if current:
            runs.append(f"{current}:{min(count, 99)}")
        current = category
        count = 1
    if current:
        runs.append(f"{current}:{min(count, 99)}")
    return runs[:32]


def _language_profile(text: str) -> str:
    han = sum(_character_class(character) == "han" for character in text)
    ascii_letters = sum(_character_class(character) == "ascii" for character in text)
    if han and ascii_letters:
        return "mixed"
    if han:
        return "zh"
    if ascii_letters:
        return "en"
    return "other"


def _length_bucket(text: str) -> str:
    length = len(text)
    if length < 40:
        return "short"
    if length < 160:
        return "medium"
    return "long"


def _redacted_skeleton(text: str) -> str:
    result: list[str] = []
    index = 0
    normalized = unicodedata.normalize("NFKC", text)
    while index < len(normalized):
        character = normalized[index]
        if character.isspace():
            if not result or result[-1] != " ":
                result.append(" ")
            index += 1
            continue
        if unicodedata.category(character).startswith("P"):
            result.append(character)
            index += 1
            continue
        version_match = _VERSION_TOKEN_RE.match(normalized, index)
        if version_match:
            result.append("<VERSION>")
            index = version_match.end()
            continue
        safe_token = next(
            (
                token
                for token in _SAFE_REDACTION_TOKENS
                if normalized[index : index + len(token)].casefold() == token.casefold()
            ),
            None,
        )
        if safe_token is not None:
            result.append(safe_token.casefold() if safe_token.isascii() else safe_token)
            index += len(safe_token)
            continue
        category = _character_class(character)
        end = index + 1
        while end < len(normalized) and _character_class(normalized[end]) == category:
            if _VERSION_TOKEN_RE.match(normalized, end) or any(
                normalized[end : end + len(token)].casefold() == token.casefold() for token in _SAFE_REDACTION_TOKENS
            ):
                break
            end += 1
        result.append(f"<{category.upper()}:{end - index}>")
        index = end
    return "".join(result).strip()


def _safe_redacted_skeleton(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    index = 0
    while index < len(value):
        placeholder = _PLACEHOLDER_RE.match(value, index)
        if placeholder:
            index = placeholder.end()
            continue
        character = value[index]
        if character.isspace() or unicodedata.category(character).startswith("P"):
            index += 1
            continue
        safe_token = next(
            (
                token
                for token in _SAFE_REDACTION_TOKENS
                if value[index : index + len(token)].casefold() == token.casefold()
            ),
            None,
        )
        if safe_token is None:
            return False
        index += len(safe_token)
    return True


def validate_redacted_seed(seed: Mapping[str, Any], index: int) -> None:
    if set(seed) != _REDACTED_SEED_FIELDS:
        raise ValueError(f"redacted seed schema mismatch at index {index}")
    if not re.fullmatch(r"real-\d{3}", str(seed["seed_id"])) or not re.fullmatch(
        r"[0-9a-f]{64}", str(seed["source_hash"])
    ):
        raise ValueError(f"redacted seed schema mismatch at index {index}")
    if seed["actor_class"] not in {"user", "assistant", "system", "agent", "other"}:
        raise ValueError(f"redacted seed schema mismatch at index {index}")
    if seed["language_profile"] not in {"zh", "en", "mixed", "other"} or seed["length_bucket"] not in {
        "short",
        "medium",
        "long",
    }:
        raise ValueError(f"redacted seed schema mismatch at index {index}")
    punctuation = seed["punctuation_profile"]
    if not (
        isinstance(punctuation, Mapping)
        and set(punctuation) == {"comma", "period", "question"}
        and all(
            isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 9 for value in punctuation.values()
        )
    ):
        raise ValueError(f"redacted seed schema mismatch at index {index}")
    signals = seed["state_signals"]
    runs = seed["structure_runs"]
    if not (
        isinstance(signals, list)
        and all(isinstance(signal, str) and signal in _SIGNAL_PATTERNS for signal in signals)
        and isinstance(runs, list)
        and all(isinstance(run, str) and _STRUCTURE_RUN_RE.fullmatch(run) for run in runs)
    ):
        raise ValueError(f"redacted seed schema mismatch at index {index}")
    if not _safe_redacted_skeleton(seed["redacted_skeleton"]):
        raise ValueError(f"redacted skeleton is not closed-lexicon safe at index {index}")


def _event_text(row: Mapping[str, Any]) -> str | None:
    try:
        content = json.loads(str(row["content_json"]))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(content, Mapping):
        return None
    text = content.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    return text


def _redacted_seed(row: Mapping[str, Any], *, index: int, selection_seed: str) -> dict[str, Any]:
    text = _event_text(row)
    if text is None:
        raise ValueError("redacted seed source event must contain non-blank text")
    stable_source = "\0".join(
        (
            selection_seed,
            str(row["id"]),
            str(row["content_hash"] or ""),
            text,
        )
    )
    actor = str(row["actor_type"] or "").strip().casefold()
    actor_class = actor if actor in {"user", "assistant", "system", "agent"} else "other"
    return {
        "seed_id": f"real-{index:03d}",
        "source_hash": hashlib.sha256(stable_source.encode("utf-8")).hexdigest(),
        "actor_class": actor_class,
        "language_profile": _language_profile(text),
        "length_bucket": _length_bucket(text),
        "punctuation_profile": {
            "comma": min(text.count(",") + text.count("，"), 9),
            "period": min(text.count(".") + text.count("。"), 9),
            "question": min(text.count("?") + text.count("？"), 9),
        },
        "state_signals": [name for name, pattern in _SIGNAL_PATTERNS.items() if pattern.search(text)],
        "structure_runs": _structure_runs(text),
        "redacted_skeleton": _redacted_skeleton(text),
    }


def sample_redacted_seeds(
    database_path: str | Path,
    *,
    limit: int = 200,
    recorded_after: str | None = None,
    seed: str = "v0300-state-counterexamples-v1",
) -> list[dict[str, Any]]:
    """Sample event structures deterministically without returning source content."""

    if limit <= 0:
        raise ValueError("limit must be positive")
    connection = open_readonly_event_database(database_path)
    try:
        columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(events)")}
        required = {"id", "event_type", "actor_type", "content_json", "content_hash", "sensitivity"}
        missing = required - columns
        if missing:
            raise ValueError(f"events table is missing sampler columns: {', '.join(sorted(missing))}")
        query = (
            "SELECT id,actor_type,content_json,content_hash FROM events "
            "WHERE event_type='message' AND sensitivity='normal'"
        )
        parameters: tuple[str, ...] = ()
        if recorded_after is not None:
            if "recorded_at" not in columns:
                raise ValueError("events table is missing recorded_at for constrained sampling")
            query += " AND julianday(recorded_at)>julianday(?)"
            parameters = (recorded_after,)
        rows = list(connection.execute(query + " ORDER BY id", parameters))
    finally:
        connection.close()
    eligible_rows = [row for row in rows if _event_text(row) is not None]
    ranked = sorted(
        eligible_rows,
        key=lambda row: hashlib.sha256(f"{seed}\0{row['id']}\0{row['content_hash'] or ''}".encode("utf-8")).digest(),
    )
    if len(ranked) < limit:
        raise ValueError(f"source database contains {len(ranked)} eligible events; {limit} required")
    return [_redacted_seed(row, index=index, selection_seed=seed) for index, row in enumerate(ranked[:limit])]


def _write_jsonl(path: Path, records: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        for record in records:
            stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
            stream.write("\n")


def write_redacted_seeds(path_value: str | Path, seeds: Sequence[Mapping[str, Any]]) -> None:
    """Write privacy-safe sampler output for a later corpus generation step."""

    _write_jsonl(Path(path_value).resolve(), seeds)


def load_redacted_seeds(path_value: str | Path) -> list[dict[str, Any]]:
    """Load the JSONL boundary between the read-only sampler and generator."""

    return _load_jsonl(path_value)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_sealed_manifest(manifest_path: str | Path) -> dict[str, Any]:
    """Verify sealed bytes and return aggregates without parsing sealed rows."""

    path = Path(manifest_path).resolve()
    manifest = json.loads(path.read_text(encoding="utf-8"))
    files = manifest["files"]
    sealed_entries = (files["sealed_corpus"], files["sealed_gold"])
    hashes_valid = all(file_sha256(path.parent / entry["path"]) == entry["sha256"] for entry in sealed_entries)
    return {
        "sealed_bundles": int(manifest["splits"]["sealed"]["bundles"]),
        "sealed_events": int(manifest["splits"]["sealed"]["events"]),
        "sealed_gold_records": int(files["sealed_gold"]["records"]),
        "hashes_valid": hashes_valid,
    }


def _load_jsonl(path: str | Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def aggregate_dev_statistics(corpus_path: str | Path, gold_path: str | Path) -> dict[str, Any]:
    """Return development-only corpus statistics for reports and tuning."""

    corpus = _load_jsonl(corpus_path)
    gold = _load_jsonl(gold_path)
    corpus_ids = {str(row["bundle_id"]) for row in corpus}
    gold_ids = {str(row["bundle_id"]) for row in gold}
    return {
        "bundles": len(corpus),
        "events": sum(len(row["events"]) for row in corpus),
        "gold_records": len(gold),
        "gold_coverage": len(corpus_ids & gold_ids) / len(corpus_ids) if corpus_ids else 1.0,
        "categories": dict(sorted(Counter(str(row["category"]) for row in corpus).items())),
        "sources": dict(sorted(Counter(str(row["source_kind"]) for row in corpus).items())),
    }
