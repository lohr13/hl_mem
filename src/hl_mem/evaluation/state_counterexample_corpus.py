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

SCHEMA_VERSION = 1
CORPUS_PREFIX = "v0300_state"

_CATEGORY_QUOTAS = {
    "software_version": {"dev": 84, "sealed": 36, "events": 3},
    "non_version_state": {"dev": 56, "sealed": 24, "events": 3},
    "compound_claim": {"dev": 56, "sealed": 24, "events": 2},
    "counterexample": {"dev": 56, "sealed": 24, "events": 2},
    "non_state_control": {"dev": 28, "sealed": 12, "events": 2},
}
_CATEGORY_SHORT = {
    "software_version": "version",
    "non_version_state": "state",
    "compound_claim": "compound",
    "counterexample": "counter",
    "non_state_control": "control",
}
_VERSION_SUBTYPES = ("upgrade", "rollback", "delayed_recording", "subject_drift", "predicate_drift")
_STATE_SUBTYPES = ("service_health", "process", "deployment", "connectivity", "job")
_COMPOUND_SUBTYPES = ("health_process", "deployment_connectivity", "version_job", "two_services")
_COUNTER_SUBTYPES = (
    "historical_narrative",
    "plan",
    "requirement",
    "quotation",
    "negation",
    "multi_deployment",
    "multi_instance",
)
_CONTROL_SUBTYPES = ("preference", "identity", "architecture", "ordinary_fact")
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


def _coordinate(
    subject: str,
    slot: str,
    qualifiers: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "namespace": "default",
        "canonical_subject": subject,
        "canonical_slot": slot,
        "coordinate_qualifiers": dict(sorted((qualifiers or {}).items())),
    }


def _event(bundle_id: str, index: int, text: str, *, role: str = "user") -> dict[str, Any]:
    return {
        "event_index": index,
        "event_id": f"{bundle_id}:e{index}",
        "role": role,
        "content": {"text": text},
        "occurred_at": f"2026-06-{index + 1:02d}T00:00:00Z",
    }


def _atomic(
    bundle_id: str,
    index: int,
    source_event_index: int,
    coordinate: Mapping[str, Any] | None,
    state_value: str,
    *,
    source_claim_index: int | None = None,
    atomic_index: int = 0,
) -> dict[str, Any]:
    claim_index = index if source_claim_index is None else source_claim_index
    return {
        "assertion_id": f"{bundle_id}:c{claim_index}:a{atomic_index}",
        "source_claim_index": claim_index,
        "atomic_index": atomic_index,
        "source_event_indices": [source_event_index],
        "coordinate": dict(coordinate) if coordinate is not None else None,
        "atomicity": "atomic",
        "state_value": state_value,
    }


def _gold(
    bundle_id: str,
    split: str,
    category: str,
    claims: Sequence[Mapping[str, Any]],
    edges: Sequence[Sequence[str]],
    current: Sequence[str],
    historical: Sequence[str],
    *,
    counterexample: bool = False,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "bundle_id": bundle_id,
        "split": split,
        "category": category,
        "atomic_claims": [dict(claim) for claim in claims],
        "expected_supersede_edges": [list(edge) for edge in edges],
        "counterexample_zero_supersede": counterexample,
        "current_assertion_ids": list(current),
        "historical_assertion_ids": list(historical),
    }


def _version_bundle(
    bundle_id: str, split: str, subtype: str, variant: int
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    subject = f"component-{variant % 12:02d}"
    coordinate = _coordinate(subject, "config.version")
    versions = ("v1.0", "v1.1", "v1.2") if subtype != "rollback" else ("v1.0", "v1.1", "v1.0")
    surfaces = (subject, subject, subject)
    texts: tuple[str, ...]
    if subtype == "subject_drift":
        surfaces = (subject, f"{subject} 服务", f"{subject} 的 API 服务")
    if subtype == "delayed_recording":
        texts = (
            f"补录：2026 年 6 月 1 日时 {subject} 当前版本是 {versions[0]}",
            f"{subject} 当前版本是 {versions[1]}",
            f"{subject} 当前版本是 {versions[2]}",
        )
    elif subtype == "predicate_drift":
        texts = (
            f"{subject} 的配置版本是 {versions[0]}",
            f"事实记录：{subject} 当前 version 为 {versions[1]}",
            f"{subject} 现在运行 release {versions[2]}",
        )
    elif subtype == "rollback":
        texts = (
            f"{subject} 当前版本是 {versions[0]}",
            f"{subject} 已升级，当前版本是 {versions[1]}",
            f"{subject} 已回滚，当前版本是 {versions[2]}",
        )
    else:
        texts = tuple(f"{surface} 当前版本是 {version}" for surface, version in zip(surfaces, versions, strict=True))
    events = [_event(bundle_id, index, text) for index, text in enumerate(texts)]
    claims = [_atomic(bundle_id, index, index, coordinate, version) for index, version in enumerate(versions)]
    ids = [str(claim["assertion_id"]) for claim in claims]
    return events, _gold(
        bundle_id,
        split,
        "software_version",
        claims,
        ((ids[0], ids[1]), (ids[1], ids[2])),
        (ids[2],),
        (ids[0], ids[1]),
    )


def _state_bundle(
    bundle_id: str, split: str, subtype: str, variant: int
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    subject = f"service-{variant % 16:02d}"
    definitions = {
        "service_health": ("state.service_health", {"service": "api"}, ("healthy", "unhealthy", "healthy")),
        "process": ("state.process", {"process": "worker"}, ("running", "stopped", "running")),
        "deployment": ("state.deployment", {"deployment": "blue"}, ("ready", "failed", "ready")),
        "connectivity": ("state.connectivity", {"service": "api"}, ("reachable", "timeout", "reachable")),
        "job": ("state.job", {"job": "sync"}, ("queued", "running", "completed")),
    }
    slot, qualifiers, values = definitions[subtype]
    coordinate = _coordinate(subject, slot, qualifiers)
    noun = {
        "service_health": "API 服务",
        "process": "worker 进程",
        "deployment": "blue 部署",
        "connectivity": "API 连接",
        "job": "sync 任务",
    }[subtype]
    events = [_event(bundle_id, index, f"{subject} 的 {noun} 当前状态为 {value}") for index, value in enumerate(values)]
    claims = [_atomic(bundle_id, index, index, coordinate, value) for index, value in enumerate(values)]
    ids = [str(claim["assertion_id"]) for claim in claims]
    return events, _gold(
        bundle_id,
        split,
        "non_version_state",
        claims,
        ((ids[0], ids[1]), (ids[1], ids[2])),
        (ids[2],),
        (ids[0], ids[1]),
    )


def _compound_bundle(
    bundle_id: str,
    split: str,
    subtype: str,
    variant: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    subject = f"compound-{variant % 20:02d}"
    choices = {
        "health_process": (
            _coordinate(subject, "state.service_health", {"service": "api"}),
            _coordinate(subject, "state.process", {"process": "worker"}),
            "API 服务",
            "worker 进程",
        ),
        "deployment_connectivity": (
            _coordinate(subject, "state.deployment", {"deployment": "blue"}),
            _coordinate(subject, "state.connectivity", {"service": "api"}),
            "blue 部署",
            "API 连接",
        ),
        "version_job": (
            _coordinate(subject, "config.version"),
            _coordinate(subject, "state.job", {"job": "sync"}),
            "当前版本",
            "sync 任务",
        ),
        "two_services": (
            _coordinate(subject, "state.service_health", {"service": "api"}),
            _coordinate(subject, "state.service_health", {"service": "worker"}),
            "API 服务",
            "worker 服务",
        ),
    }
    first_coordinate, second_coordinate, first_noun, second_noun = choices[subtype]
    values = {
        "health_process": (("healthy", "running"), ("unhealthy", "stopped")),
        "deployment_connectivity": (("ready", "reachable"), ("failed", "unreachable")),
        "version_job": (("v1.0", "queued"), ("v1.1", "completed")),
        "two_services": (("healthy", "unhealthy"), ("unhealthy", "healthy")),
    }[subtype]
    events = [
        _event(
            bundle_id,
            event_index,
            f"{subject} 的 {first_noun} 当前 {first_value}；{second_noun} 当前 {second_value}",
        )
        for event_index, (first_value, second_value) in enumerate(values)
    ]
    claims = [
        _atomic(bundle_id, 0, 0, first_coordinate, values[0][0], source_claim_index=0, atomic_index=0),
        _atomic(bundle_id, 1, 0, second_coordinate, values[0][1], source_claim_index=0, atomic_index=1),
        _atomic(bundle_id, 2, 1, first_coordinate, values[1][0], source_claim_index=1, atomic_index=0),
        _atomic(bundle_id, 3, 1, second_coordinate, values[1][1], source_claim_index=1, atomic_index=1),
    ]
    ids = [str(claim["assertion_id"]) for claim in claims]
    return events, _gold(
        bundle_id,
        split,
        "compound_claim",
        claims,
        ((ids[0], ids[2]), (ids[1], ids[3])),
        (ids[2], ids[3]),
        (ids[0], ids[1]),
    )


def _counter_bundle(
    bundle_id: str,
    split: str,
    subtype: str,
    variant: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    subject = f"counter-{variant % 24:02d}"
    texts: tuple[str, str]
    coordinates: tuple[dict[str, Any] | None, dict[str, Any] | None]
    historical: tuple[str, ...]
    if subtype == "historical_narrative":
        coordinate = _coordinate(subject, "config.version")
        texts = (
            f"历史记录：2024 年时 {subject} 当前版本是 v0.8",
            f"回顾材料提到 2025 年时 {subject} 当前版本是 v0.9",
        )
        coordinates = (coordinate, coordinate)
        historical = ()
    elif subtype == "multi_deployment":
        texts = (
            f"{subject} 的 blue 部署当前版本是 v2.0",
            f"{subject} 的 green 部署当前版本是 v1.9",
        )
        coordinates = (
            _coordinate(subject, "config.version", {"deployment": "blue"}),
            _coordinate(subject, "config.version", {"deployment": "green"}),
        )
        historical = ()
    elif subtype == "multi_instance":
        texts = (
            f"{subject} 实例 node-a 当前版本是 v2.0",
            f"{subject} 实例 node-b 当前版本是 v1.9",
        )
        coordinates = (
            _coordinate(subject, "config.version", {"instance": "node-a"}),
            _coordinate(subject, "config.version", {"instance": "node-b"}),
        )
        historical = ()
    else:
        templates = {
            "plan": f"计划下周把 {subject} 升级到 v2.0",
            "requirement": f"要求 {subject} 必须保持 v2.0",
            "quotation": f"文档写道：{subject} 当前版本是 v2.0",
            "negation": f"{subject} 并不是 running 状态",
        }
        texts = (templates[subtype], f"这条{subtype}描述不得改变 {subject} 的当前状态")
        coordinates = (None, None)
        historical = ()
    events = [_event(bundle_id, index, text) for index, text in enumerate(texts)]
    claims = [_atomic(bundle_id, index, index, coordinates[index], f"counter-{subtype}-{index}") for index in range(2)]
    ids = [str(claim["assertion_id"]) for claim in claims]
    if subtype == "historical_narrative":
        historical = tuple(ids)
    current: Sequence[str] = ids if subtype in {"multi_deployment", "multi_instance"} else ()
    return events, _gold(
        bundle_id,
        split,
        "counterexample",
        claims,
        (),
        current,
        historical,
        counterexample=True,
    )


def _control_bundle(
    bundle_id: str,
    split: str,
    subtype: str,
    variant: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    subject = f"control-{variant % 12:02d}"
    texts = {
        "preference": (f"{subject} 偏好简洁回复", f"{subject} 喜欢深色主题"),
        "identity": (f"{subject} 是测试账号", f"{subject} 的角色是维护者"),
        "architecture": (f"{subject} 采用本地优先架构", f"{subject} 使用 SQLite 存储"),
        "ordinary_fact": (f"{subject} 包含三个模块", f"{subject} 的文档使用中文"),
    }[subtype]
    events = [_event(bundle_id, index, text) for index, text in enumerate(texts)]
    claims = [_atomic(bundle_id, index, index, None, f"non-state-{subtype}-{index}") for index in range(2)]
    return events, _gold(bundle_id, split, "non_state_control", claims, (), (), ())


def _bundle_payload(
    *,
    split: str,
    category: str,
    category_index: int,
    global_index: int,
    source_kind: str,
    seed: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    bundle_id = f"{split}-{_CATEGORY_SHORT[category]}-{category_index + 1:03d}"
    subtype_choices = {
        "software_version": _VERSION_SUBTYPES,
        "non_version_state": _STATE_SUBTYPES,
        "compound_claim": _COMPOUND_SUBTYPES,
        "counterexample": _COUNTER_SUBTYPES,
        "non_state_control": _CONTROL_SUBTYPES,
    }[category]
    subtype = subtype_choices[category_index % len(subtype_choices)]
    builders = {
        "software_version": _version_bundle,
        "non_version_state": _state_bundle,
        "compound_claim": _compound_bundle,
        "counterexample": _counter_bundle,
        "non_state_control": _control_bundle,
    }
    events, gold = builders[category](bundle_id, split, subtype, global_index)
    provenance: dict[str, Any]
    if source_kind == "real_deidentified":
        if seed is None:
            raise ValueError("real_deidentified bundle requires a redacted seed")
        provenance = {
            "source_kind": source_kind,
            "redaction": "irreversible_structural_v1",
            "composition": "redacted_real_context_plus_controlled_assertion_v1",
            "seed": dict(seed),
        }
        controlled_text = str(events[0]["content"]["text"])
        events[0] = {
            **events[0],
            "content": {
                "text": (
                    "【去标识真实上下文，仅保留结构，不作为事实证据】\n"
                    f"{seed['redacted_skeleton']}\n"
                    "【当前评测事件】\n"
                    f"{controlled_text}"
                )
            },
            "context_only": {
                "redacted_text": str(seed["redacted_skeleton"]),
                "source_hash": str(seed["source_hash"]),
            },
        }
    else:
        provenance = {
            "source_kind": source_kind,
            "generator": "adversarial_templates_v1",
            "seed_id": f"synthetic-{global_index:03d}",
        }
    corpus = {
        "schema_version": SCHEMA_VERSION,
        "bundle_id": bundle_id,
        "split": split,
        "category": category,
        "subtype": subtype,
        "source_kind": source_kind,
        "provenance": provenance,
        "events": events,
    }
    return corpus, gold


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
                corpus, gold = _bundle_payload(
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
