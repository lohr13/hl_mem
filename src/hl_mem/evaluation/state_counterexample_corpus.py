"""Generate the frozen v0.30.0 state counterexample corpus.

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

from hl_mem.evaluation.state_counterexample_templates import SCHEMA_VERSION, build_bundle_payload

CORPUS_PREFIX = "v0300_state"

_CATEGORY_QUOTAS = {
    "software_version": {"dev": 84, "sealed": 36, "events": 3},
    "non_version_state": {"dev": 56, "sealed": 24, "events": 3},
    "compound_claim": {"dev": 56, "sealed": 24, "events": 2},
    "counterexample": {"dev": 56, "sealed": 24, "events": 2},
    "non_state_control": {"dev": 28, "sealed": 12, "events": 2},
}
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


def _validate_redacted_seed(seed: Mapping[str, Any], index: int) -> None:
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
        rows = list(
            connection.execute(
                "SELECT id,actor_type,content_json,content_hash FROM events "
                "WHERE event_type='message' AND sensitivity='normal' ORDER BY id"
            )
        )
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


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def generate_corpus(redacted_seeds: Sequence[Mapping[str, Any]], output_dir: str | Path) -> dict[str, Any]:
    """Generate the exact 400-bundle corpus and separated gold files."""

    if len(redacted_seeds) != 200:
        raise ValueError("exactly 200 redacted real-event seeds are required")
    for index, validation_seed in enumerate(redacted_seeds):
        _validate_redacted_seed(validation_seed, index)
    seed_ids = [str(seed.get("seed_id") or "") for seed in redacted_seeds]
    if any(not seed_id for seed_id in seed_ids) or len(set(seed_ids)) != 200:
        raise ValueError("redacted seed ids must be non-blank and unique")
    target = Path(output_dir).resolve()
    target.mkdir(parents=True, exist_ok=True)
    split_corpus: dict[str, list[dict[str, Any]]] = {"dev": [], "sealed": []}
    split_gold: dict[str, list[dict[str, Any]]] = {"dev": [], "sealed": []}
    real_seed_index = 0
    global_index = 0
    for split in ("dev", "sealed"):
        for category, quota in _CATEGORY_QUOTAS.items():
            count = int(quota[split])
            for category_index in range(count):
                source_kind = "real_deidentified" if category_index % 2 == 0 else "synthetic_adversarial"
                bundle_seed = redacted_seeds[real_seed_index] if source_kind == "real_deidentified" else None
                if bundle_seed is not None:
                    real_seed_index += 1
                corpus, gold = build_bundle_payload(
                    split=split,
                    category=category,
                    category_index=category_index,
                    global_index=global_index,
                    source_kind=source_kind,
                    seed=bundle_seed,
                )
                split_corpus[split].append(corpus)
                split_gold[split].append(gold)
                global_index += 1
    if real_seed_index != 200:
        raise RuntimeError(f"generator consumed {real_seed_index} real seeds instead of 200")

    paths = {
        "dev_corpus": target / f"{CORPUS_PREFIX}_dev_corpus.jsonl",
        "dev_gold": target / f"{CORPUS_PREFIX}_dev_gold.jsonl",
        "sealed_corpus": target / f"{CORPUS_PREFIX}_sealed_corpus.jsonl",
        "sealed_gold": target / f"{CORPUS_PREFIX}_sealed_gold.jsonl",
    }
    _write_jsonl(paths["dev_corpus"], split_corpus["dev"])
    _write_jsonl(paths["dev_gold"], split_gold["dev"])
    _write_jsonl(paths["sealed_corpus"], split_corpus["sealed"])
    _write_jsonl(paths["sealed_gold"], split_gold["sealed"])

    category_counts: dict[str, dict[str, int]] = {}
    for category, quota in _CATEGORY_QUOTAS.items():
        dev_count = int(quota["dev"])
        sealed_count = int(quota["sealed"])
        category_counts[category] = {"dev": dev_count, "sealed": sealed_count, "total": dev_count + sealed_count}
    source_counts = {
        source: {
            "dev": sum(row["source_kind"] == source for row in split_corpus["dev"]),
            "sealed": sum(row["source_kind"] == source for row in split_corpus["sealed"]),
            "total": sum(row["source_kind"] == source for split in split_corpus.values() for row in split),
        }
        for source in ("real_deidentified", "synthetic_adversarial")
    }
    split_counts = {
        split: {
            "bundles": len(split_corpus[split]),
            "events": sum(len(row["events"]) for row in split_corpus[split]),
        }
        for split in ("dev", "sealed")
    }
    file_manifest = {
        name: {
            "path": path.name,
            "sha256": _sha256(path),
            "records": (
                len(split_corpus["dev"] if name == "dev_corpus" else split_gold["dev"])
                if name.startswith("dev_")
                else len(split_corpus["sealed"] if name == "sealed_corpus" else split_gold["sealed"])
            ),
        }
        for name, path in paths.items()
    }
    all_corpus = [*split_corpus["dev"], *split_corpus["sealed"]]
    all_gold = [*split_gold["dev"], *split_gold["sealed"]]
    coverage = len({row["bundle_id"] for row in all_corpus} & {row["bundle_id"] for row in all_gold}) / len(all_corpus)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "protocol": "v0.30.0-batch2-state-counterexamples",
        "totals": {
            "bundles": len(all_corpus),
            "events": sum(len(row["events"]) for row in all_corpus),
            "gold_records": len(all_gold),
            "gold_coverage": coverage,
        },
        "splits": split_counts,
        "categories": category_counts,
        "sources": source_counts,
        "files": file_manifest,
    }
    manifest_path = target / f"{CORPUS_PREFIX}_corpus_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return manifest


def verify_sealed_manifest(manifest_path: str | Path) -> dict[str, Any]:
    """Verify sealed bytes and return aggregates without parsing sealed rows."""

    path = Path(manifest_path).resolve()
    manifest = json.loads(path.read_text(encoding="utf-8"))
    files = manifest["files"]
    sealed_entries = (files["sealed_corpus"], files["sealed_gold"])
    hashes_valid = all(_sha256(path.parent / entry["path"]) == entry["sha256"] for entry in sealed_entries)
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
