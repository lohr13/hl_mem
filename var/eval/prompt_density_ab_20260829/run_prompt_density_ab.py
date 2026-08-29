"""Run the frozen qwen3.8-flash prompt-density A/B experiment.

The three commands are intentionally self-contained and eval-only:

    python run_prompt_density_ab.py prepare
    python run_prompt_density_ab.py run
    python run_prompt_density_ab.py score
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
import sqlite3
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Mapping, Sequence

import httpx
from jsonschema import Draft202012Validator

EQUIPMENT_DIR = Path(__file__).resolve().parent
REPO_ROOT = EQUIPMENT_DIR.parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "src"))

from evaluation.tools.run_memdaily_benchmark import load_trajectories  # noqa: E402
from hl_mem.ingest.chunking import ChunkingPolicy, split_extraction_content  # noqa: E402
from hl_mem.ingest.llm_extractor import (  # noqa: E402
    ENGLISH_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
    detect_extraction_language,
)
from hl_mem.ingest.schemas import temporal_gate_extraction_response_json_schema  # noqa: E402

PROTOCOL_ID = "prompt_density_ab_20260829_v1"
SEED = 20260829
MODEL = "qwen3.8-flash"
BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
ENDPOINT = f"{BASE_URL}/chat/completions"
ENABLE_THINKING = False
MAX_TOKENS = 6000
CONCURRENCY = 4
TIMEOUT_SECONDS = 90.0
MAX_ATTEMPTS = 1
SOFT_BUDGET_CNY = 0.80
HARD_BUDGET_CNY = 1.00
INPUT_PRICE_PER_MILLION = 1.0
CACHED_INPUT_PRICE_PER_MILLION = 0.1
OUTPUT_PRICE_PER_MILLION = 3.0

DEFAULT_SOURCE_MANIFEST = REPO_ROOT / "var/eval/softsplit_ab_20260827/manifest.json"
DEFAULT_MEMDAILY_SAMPLE = REPO_ROOT / "tests/eval/fixtures/chinese_e2e_sample.json"
DEFAULT_ENV_FILE = REPO_ROOT / ".env"
DEFAULT_MANIFEST = EQUIPMENT_DIR / "manifest.json"
DEFAULT_RUNS = EQUIPMENT_DIR / "runs.jsonl"
DEFAULT_RUN_METADATA = EQUIPMENT_DIR / "run_metadata.json"
DEFAULT_MANUAL_REVIEW = EQUIPMENT_DIR / "manual_review.json"
DEFAULT_REPORT = EQUIPMENT_DIR / "report.json"
DEFAULT_GATE_TABLE = EQUIPMENT_DIR / "gate_table.md"
DEFAULT_COMPARISON = EQUIPMENT_DIR / "comparison.csv"

ZH_DENSITY_LINES = (
    "- 覆盖优先：先逐项扫描全文，再输出所有有证据、可独立回答的原子事实；高密度长文通常应产出 12–30 条，不要在已有少量 claim 时提前停止。",
    "- 数量由原文决定：短文可以只有 0–少量；禁止为接近 12 或 30 而重复、拆碎同一事实、概括填充或虚构。",
)
EN_DENSITY_LINES = (
    "- Coverage first: scan the full source and emit every supported independently answerable atomic fact; a dense long source will often yield 12–30 claims, so do not stop after only a few.",
    "- Let the source determine the count: a short source may yield zero or only a few; never repeat, fragment, pad, generalize, or invent facts to approach 12 or 30.",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_text(value: str) -> str:
    return sha256_bytes(value.encode("utf-8"))


def file_sha256(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def frozen_configuration() -> dict[str, Any]:
    """Return the executable configuration that prepare freezes into the manifest."""
    return {
        "arms": {
            "A": {"prompt": "production prompt", "max_items": 20},
            "B": {"prompt": "production prompt + two density lines", "max_items": 30},
        },
        "model": MODEL,
        "base_url": BASE_URL,
        "enable_thinking": ENABLE_THINKING,
        "strict_json_schema": True,
        "max_tokens": MAX_TOKENS,
        "concurrency": CONCURRENCY,
        "timeout_seconds": TIMEOUT_SECONDS,
        "max_attempts": MAX_ATTEMPTS,
        "soft_budget_cny": SOFT_BUDGET_CNY,
        "hard_budget_cny": HARD_BUDGET_CNY,
        "pricing_cny_per_million": {
            "input": INPUT_PRICE_PER_MILLION,
            "cached_input": CACHED_INPUT_PRICE_PER_MILLION,
            "output": OUTPUT_PRICE_PER_MILLION,
            "source": "https://help.aliyun.com/zh/model-studio/qwen3-8-flash",
            "region": "China (Beijing)",
        },
        "prompt_sha256": {
            arm: {language: sha256_text(system_prompt(language, arm)) for language in ("zh", "en")}
            for arm in ("A", "B")
        },
        "schema_sha256": {arm: sha256_text(canonical_json(response_schema(arm))) for arm in ("A", "B")},
    }


def validate_frozen_configuration(manifest: Mapping[str, Any]) -> None:
    actual = manifest.get("configuration")
    expected = frozen_configuration()
    if actual != expected:
        raise ValueError("manifest configuration no longer matches the frozen executable configuration")


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"{path} line {line_number} is not a JSON object")
        rows.append(value)
    return rows


def arm_order(case_id: str) -> tuple[str, str]:
    """Assign AB/BA using only the stable case hash."""
    return ("A", "B") if int(sha256_text(case_id)[-2:], 16) % 2 == 0 else ("B", "A")


def system_prompt(language: str, arm: str) -> str:
    if language not in {"zh", "en"}:
        raise ValueError(f"unsupported language: {language}")
    if arm not in {"A", "B"}:
        raise ValueError(f"unsupported arm: {arm}")
    prompt = ENGLISH_SYSTEM_PROMPT if language == "en" else SYSTEM_PROMPT
    if arm == "A":
        return prompt
    if language == "zh":
        anchor = "\n限制：\n"
        if prompt.count(anchor) != 1:
            raise RuntimeError("Chinese prompt limits anchor changed")
        prompt = prompt.replace(anchor, "\n" + "\n".join(ZH_DENSITY_LINES) + anchor)
        prompt = prompt.replace("- max 20 claims per chunk。", "- max 30 claims per chunk。")
    else:
        anchor = "\nLimits:\n"
        if prompt.count(anchor) != 1:
            raise RuntimeError("English prompt limits anchor changed")
        prompt = prompt.replace(anchor, "\n" + "\n".join(EN_DENSITY_LINES) + anchor)
        prompt = prompt.replace("- Maximum 20 claims per chunk.", "- Maximum 30 claims per chunk.")
    return prompt


def response_schema(arm: str) -> dict[str, Any]:
    if arm not in {"A", "B"}:
        raise ValueError(f"unsupported arm: {arm}")
    schema = deepcopy(temporal_gate_extraction_response_json_schema())
    schema["properties"]["claims"]["maxItems"] = 20 if arm == "A" else 30
    return schema


def build_payload(
    messages: Sequence[Mapping[str, str]],
    *,
    arm: str,
    language: str,
    thinking_budget: int | None = None,
) -> dict[str, Any]:
    schema = response_schema(arm)
    full_messages = [{"role": "system", "content": system_prompt(language, arm)}]
    full_messages.extend({"role": str(item["role"]), "content": str(item["content"])} for item in messages)
    payload = {
        "model": MODEL,
        "messages": full_messages,
        "enable_thinking": ENABLE_THINKING or thinking_budget is not None,
        "max_tokens": MAX_TOKENS,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "extraction_response", "schema": schema, "strict": True},
        },
    }
    if thinking_budget is not None:
        payload["thinking_budget"] = thinking_budget
    return payload


class BudgetGuard:
    """Thread-safe settled-cost accounting with conservative in-flight reservations."""

    def __init__(self, *, soft_limit_cny: float, hard_limit_cny: float, spent_cny: float = 0.0) -> None:
        if not 0 <= spent_cny <= hard_limit_cny:
            raise ValueError("initial spend is outside the budget")
        if not 0 < soft_limit_cny <= hard_limit_cny:
            raise ValueError("budget limits are invalid")
        self.soft_limit_cny = float(soft_limit_cny)
        self.hard_limit_cny = float(hard_limit_cny)
        self._spent_cny = float(spent_cny)
        self._reservations: dict[str, float] = {}
        self._halted = False
        self._integrity_error: str | None = None
        self._lock = threading.Lock()

    def try_reserve(self, request_id: str, worst_case_cny: float) -> bool:
        with self._lock:
            if request_id in self._reservations:
                raise ValueError(f"duplicate budget reservation: {request_id}")
            if self._halted:
                return False
            if self._spent_cny >= self.soft_limit_cny:
                return False
            projected = self._spent_cny + sum(self._reservations.values()) + worst_case_cny
            if projected > self.hard_limit_cny + 1e-12:
                return False
            self._reservations[request_id] = float(worst_case_cny)
            return True

    def settle(self, request_id: str, actual_cny: float) -> None:
        with self._lock:
            if request_id not in self._reservations:
                raise ValueError(f"unknown budget reservation: {request_id}")
            reserved = self._reservations.pop(request_id)
            actual = float(actual_cny)
            if not math.isfinite(actual) or actual < 0:
                self._halted = True
                self._integrity_error = f"invalid actual request cost for {request_id}: {actual_cny!r}"
                self._spent_cny += reserved
                raise ValueError(self._integrity_error)
            self._spent_cny += actual
            if actual > reserved + 1e-12:
                self._halted = True
                self._integrity_error = (
                    f"actual request cost for {request_id} exceeds reservation: {actual:.9f} > {reserved:.9f}"
                )
                raise ValueError(self._integrity_error)

    def halt_unknown_cost(self, request_id: str, reason: str) -> float:
        """Charge the full reservation and fail closed when provider cost is unknowable."""
        with self._lock:
            if request_id not in self._reservations:
                raise ValueError(f"unknown budget reservation: {request_id}")
            reserved = self._reservations.pop(request_id)
            self._spent_cny += reserved
            self._halted = True
            self._integrity_error = reason
            return reserved

    def release(self, request_id: str) -> None:
        with self._lock:
            self._reservations.pop(request_id, None)

    def snapshot(self) -> dict[str, float | int | bool | str | None]:
        with self._lock:
            reserved = sum(self._reservations.values())
            return {
                "spent_cny": self._spent_cny,
                "reserved_cny": reserved,
                "outstanding_requests": len(self._reservations),
                "soft_stop_reached": self._spent_cny >= self.soft_limit_cny or self._halted,
                "hard_limit_cny": self.hard_limit_cny,
                "halted": self._halted,
                "integrity_error": self._integrity_error,
            }


def _quartiled(candidates: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for raw in candidates:
        grouped[str(raw["language"])].append(dict(raw))
    result: list[dict[str, Any]] = []
    for language, rows in sorted(grouped.items()):
        ordered = sorted(rows, key=lambda item: (int(item["body_length"]), str(item["case_id"])))
        total = len(ordered)
        for rank, row in enumerate(ordered):
            row["length_quartile"] = min(4, math.floor(rank * 4 / total) + 1)
            result.append(row)
    return result


def _seed_rank(seed: int, case_id: str) -> str:
    return sha256_text(f"{seed}:{case_id}")


def select_dense_cases(candidates: Sequence[Mapping[str, Any]], *, count: int, seed: int) -> list[dict[str, Any]]:
    """Stratify by language and within-language body-length quartile."""
    if count < 1 or len(candidates) < count:
        raise ValueError("not enough dense candidates")
    rows = _quartiled(candidates)
    by_language: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_language[str(row["language"])].append(row)

    selected: list[dict[str, Any]] = []
    selected_ids: set[str] = set()
    rare_cutoff = max(1, count // 4)
    for language in sorted(by_language):
        language_rows = by_language[language]
        if len(language_rows) <= rare_cutoff:
            for row in sorted(language_rows, key=lambda item: _seed_rank(seed, str(item["case_id"]))):
                selected.append(row)
                selected_ids.add(str(row["case_id"]))

    strata: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if str(row["case_id"]) not in selected_ids:
            strata[(str(row["language"]), int(row["length_quartile"]))].append(row)
    for key in strata:
        strata[key].sort(key=lambda item: _seed_rank(seed, str(item["case_id"])))

    offset = 0
    keys = sorted(strata)
    while len(selected) < count:
        progressed = False
        for key in keys:
            bucket = strata[key]
            if offset < len(bucket):
                row = bucket[offset]
                if str(row["case_id"]) not in selected_ids:
                    selected.append(row)
                    selected_ids.add(str(row["case_id"]))
                    progressed = True
                    if len(selected) == count:
                        break
        if not progressed:
            break
        offset += 1
    if len(selected) != count:
        raise RuntimeError(f"dense sampler selected {len(selected)} of {count}")
    for order, row in enumerate(selected):
        row["selection_order"] = order
        row["stratum"] = f"{row['language']}_q{row['length_quartile']}"
    return selected


def _open_read_only(database_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{database_path.resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _event_text(content: Any) -> str:
    if isinstance(content, dict) and "text" in content:
        return str(content["text"])
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False, sort_keys=True)


def load_dense_runtime(database_path: Path, case: Mapping[str, Any]) -> dict[str, Any]:
    source_ids = case.get("source_event_ids")
    if not isinstance(source_ids, list) or not source_ids:
        raise ValueError(f"case {case.get('case_id')} has no source_event_ids")
    expected_hashes = {
        str(source["event_id"]): str(source["content_sha256"])
        for source in case.get("sources", [])
        if isinstance(source, Mapping)
    }
    sources: list[dict[str, Any]] = []
    messages: list[dict[str, Any]] = []
    with _open_read_only(database_path) as connection:
        for index, event_id in enumerate(source_ids):
            row = connection.execute("SELECT * FROM events WHERE id=?", (str(event_id),)).fetchone()
            if row is None:
                raise ValueError(f"source event is missing: {event_id}")
            raw = dict(row)
            content_json = str(raw["content_json"])
            if expected_hashes.get(str(event_id)) != sha256_text(content_json):
                raise ValueError(f"source content hash changed: {event_id}")
            content = json.loads(content_json)
            metadata = json.loads(raw["metadata_json"]) if raw.get("metadata_json") else {}
            turn = metadata.get("turn_id", metadata.get("turn_index", index)) if isinstance(metadata, dict) else index
            sources.append({**raw, "event_index": index, "turn": turn, "content": content})
            messages.append(
                {
                    "event_index": index,
                    "speaker": str(raw.get("actor_type") or "unknown"),
                    "turn": turn,
                    "occurred_at": raw.get("occurred_at"),
                    "content": _event_text(content),
                }
            )
    anchor = sources[0]
    content = {"messages": messages}
    context = {
        "occurred_at": anchor.get("occurred_at"),
        "actor_type": "conversation",
        "event_type": "message",
        "session_id": anchor.get("session_id"),
        "recent_events": [],
        "_source_events": sources,
    }
    return _prompt_runtime(content, context)


def _prompt_runtime(content: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    chunks = split_extraction_content(
        content,
        ChunkingPolicy(target_chars=1_000_000, overlap_turns=0, max_split_depth=0),
    )
    if len(chunks) != 1:
        raise ValueError(f"prompt-density protocol requires one chunk, observed {len(chunks)}")
    chunk = chunks[0]
    prompt_context = {key: value for key, value in context.items() if not key.startswith("_")}
    occurred_at = str(context.get("occurred_at") or "未知")
    language = detect_extraction_language(chunk.text)
    if language == "en":
        user_prompt = (
            f"Event occurred at: {occurred_at}\n"
            f"Event context: {json.dumps(prompt_context, ensure_ascii=False)}\n"
            "<context_only>\n"
            f"{chunk.context_prefix}\n"
            "</context_only>\n"
            "Use context_only only to resolve subjects. Do not extract claims from it.\n"
            "<extract_from>\n"
            f"{chunk.text}\n"
            "</extract_from>"
        )
    else:
        user_prompt = (
            f"事件发生时间 occurred_at：{occurred_at}\n"
            f"事件上下文：{json.dumps(prompt_context, ensure_ascii=False)}\n"
            "<context_only>\n"
            f"{chunk.context_prefix}\n"
            "</context_only>\n"
            "context_only 仅用于消解主语，禁止从中提取 claim。\n"
            "<extract_from>\n"
            f"{chunk.text}\n"
            "</extract_from>"
        )
    return {
        "language": language,
        "body_length": len(chunk.text),
        "body_sha256": sha256_text(chunk.text),
        "messages": [{"role": "user", "content": user_prompt}],
    }


def _load_memdaily_sample(sample_path: Path) -> tuple[dict[str, Any], Path]:
    sample = json.loads(sample_path.read_text(encoding="utf-8"))
    source_spec = sample["sources"]["memdaily"]
    source_path = Path(str(source_spec["path"]))
    if file_sha256(source_path) != str(source_spec["sha256"]):
        raise ValueError("MemDaily source hash does not match frozen Chinese E2E sample")
    return sample, source_path


def _short_content(message: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    content = {
        "messages": [
            {
                "event_index": 0,
                "speaker": "user",
                "turn": 0,
                "occurred_at": message.occurred_at,
                "content": message.text,
            }
        ]
    }
    context = {
        "occurred_at": message.occurred_at,
        "actor_type": "conversation",
        "event_type": "message",
        "session_id": None,
        "recent_events": [],
    }
    return content, context


def select_short_cases(sample_path: Path, *, count: int, seed: int) -> tuple[list[dict[str, Any]], Path, str]:
    sample, source_path = _load_memdaily_sample(sample_path)
    selected_trajectory_ids = set(map(str, sample["memdaily"]["case_ids"]))
    trajectories = [
        trajectory
        for trajectory in load_trajectories(source_path, n_per_type=None)
        if trajectory.case_id in selected_trajectory_ids
    ]
    qtypes = sorted({trajectory.qtype for trajectory in trajectories})
    if len(qtypes) != count:
        raise ValueError(f"expected {count} MemDaily question types, observed {len(qtypes)}")
    selected: list[dict[str, Any]] = []
    for qtype in qtypes:
        candidates: list[tuple[int, str, Any, Any]] = []
        for trajectory in trajectories:
            if trajectory.qtype != qtype:
                continue
            for message in trajectory.messages:
                if message.text.strip():
                    rank = _seed_rank(seed, f"{trajectory.case_id}:{message.mid}")
                    candidates.append((len(message.text), rank, trajectory, message))
        if not candidates:
            raise ValueError(f"no non-empty MemDaily messages for {qtype}")
        _, _, trajectory, message = min(candidates, key=lambda item: (item[0], item[1]))
        content, context = _short_content(message)
        runtime = _prompt_runtime(content, context)
        selected.append(
            {
                "case_id": f"short:{trajectory.case_id}:mid:{message.mid}",
                "case_type": "short",
                "qtype": qtype,
                "source_trajectory_id": trajectory.case_id,
                "source_mid": message.mid,
                "source_text_sha256": sha256_text(message.text),
                "language": runtime["language"],
                "body_length": runtime["body_length"],
                "body_sha256": runtime["body_sha256"],
                "selection_order": len(selected),
                "pair_order": list(arm_order(f"short:{trajectory.case_id}:mid:{message.mid}")),
            }
        )
    return selected, source_path, file_sha256(source_path)


def load_short_runtime(source_path: Path, case: Mapping[str, Any]) -> dict[str, Any]:
    wanted = str(case["source_trajectory_id"])
    mid = int(case["source_mid"])
    trajectory = next(
        (item for item in load_trajectories(source_path, n_per_type=None) if item.case_id == wanted), None
    )
    if trajectory is None:
        raise ValueError(f"MemDaily trajectory is missing: {wanted}")
    message = next((item for item in trajectory.messages if item.mid == mid), None)
    if message is None:
        raise ValueError(f"MemDaily message is missing: {wanted}:{mid}")
    if sha256_text(message.text) != str(case["source_text_sha256"]):
        raise ValueError(f"MemDaily message hash changed: {wanted}:{mid}")
    content, context = _short_content(message)
    return _prompt_runtime(content, context)


def prepare_experiment(
    source_manifest_path: Path,
    memdaily_sample_path: Path,
    output_path: Path,
    manual_review_path: Path,
) -> dict[str, Any]:
    source_manifest = json.loads(source_manifest_path.read_text(encoding="utf-8"))
    source_database = Path(str(source_manifest["source_database"]))
    candidates: list[dict[str, Any]] = []
    exclusions: list[dict[str, str]] = []
    source_by_id = {str(case["case_id"]): case for case in source_manifest["cases"]}
    for source_case in source_manifest["cases"]:
        case_id = str(source_case["case_id"])
        try:
            runtime = load_dense_runtime(source_database, source_case)
        except Exception as error:
            exclusions.append({"case_id": case_id, "reason": type(error).__name__, "detail": str(error)[:300]})
            continue
        candidates.append(
            {
                "case_id": case_id,
                "language": runtime["language"],
                "body_length": runtime["body_length"],
                "body_sha256": runtime["body_sha256"],
            }
        )
    selected_dense = select_dense_cases(candidates, count=20, seed=SEED)
    dense_cases: list[dict[str, Any]] = []
    for item in selected_dense:
        source_case = source_by_id[str(item["case_id"])]
        dense_cases.append(
            {
                **item,
                "case_type": "dense",
                "pair_order": list(arm_order(str(item["case_id"]))),
                "source_event_ids": source_case["source_event_ids"],
                "sources": source_case["sources"],
            }
        )
    short_cases, memdaily_source, memdaily_sha = select_short_cases(
        memdaily_sample_path,
        count=6,
        seed=SEED,
    )
    audit_offsets = (0, 4, 8, 12, 16)
    key_fact_case_ids = [dense_cases[index]["case_id"] for index in audit_offsets]
    manifest = {
        "protocol_id": PROTOCOL_ID,
        "prepared_at": utc_now(),
        "seed": SEED,
        "contains_message_bodies": False,
        "source": {
            "dense_manifest": str(source_manifest_path.resolve()),
            "dense_manifest_sha256": file_sha256(source_manifest_path),
            "dense_database": str(source_database.resolve()),
            "source_case_count": len(source_manifest["cases"]),
            "available_case_count": len(candidates),
            "excluded_case_count": len(exclusions),
            "exclusions": exclusions,
            "memdaily_sample_manifest": str(memdaily_sample_path.resolve()),
            "memdaily_sample_manifest_sha256": file_sha256(memdaily_sample_path),
            "memdaily_source": str(memdaily_source.resolve()),
            "memdaily_source_sha256": memdaily_sha,
        },
        "selection": {
            "dense": "language x within-language body-length quartile; preserve all rare-language cases; seeded round-robin",
            "short": "one shortest non-empty event per MemDaily question type; seed breaks equal-length ties",
            "dense_count": 20,
            "short_count": 6,
            "key_fact_case_ids": key_fact_case_ids,
        },
        "configuration": frozen_configuration(),
        "cases": dense_cases + short_cases,
    }
    write_json(output_path, manifest)
    review_template = {
        "protocol_id": PROTOCOL_ID,
        "manifest_sha256": file_sha256(output_path),
        "key_fact_review": {
            "case_ids": key_fact_case_ids,
            "reviewed_case_count": 0,
            "covered": 0,
            "total": 0,
            "hallucinated_claims": 0,
            "cases": [],
        },
        "short_event_review": {
            "case_ids": [case["case_id"] for case in short_cases],
            "reviewed_case_count": 0,
            "padding_claims": 0,
            "cases": [],
        },
    }
    if not manual_review_path.exists():
        write_json(manual_review_path, review_template)
    return manifest


def _env_value(path: Path, key: str) -> str:
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        if name.strip() == key:
            return value.strip().strip('"').strip("'")
    raise ValueError(f"{key} is missing from {path}")


def gold_definition_sha256(review: Mapping[str, Any]) -> str:
    key_review = review.get("key_fact_review") if isinstance(review.get("key_fact_review"), Mapping) else {}
    short_review = review.get("short_event_review") if isinstance(review.get("short_event_review"), Mapping) else {}
    payload = {
        "key_cases": [
            {
                "case_id": case.get("case_id"),
                "facts": [{"id": fact.get("id"), "text": fact.get("text")} for fact in case.get("facts", [])],
            }
            for case in key_review.get("cases", [])
            if isinstance(case, Mapping)
        ],
        "short_cases": [
            {"case_id": case.get("case_id"), "expected": case.get("expected")}
            for case in short_review.get("cases", [])
            if isinstance(case, Mapping)
        ],
    }
    return sha256_text(canonical_json(payload))


def usage_cost_cny(usage: Mapping[str, Any]) -> float:
    input_tokens = int(usage.get("input_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or 0)
    cached_tokens = min(input_tokens, int(usage.get("cached_tokens") or 0))
    return (
        (input_tokens - cached_tokens) * INPUT_PRICE_PER_MILLION
        + cached_tokens * CACHED_INPUT_PRICE_PER_MILLION
        + output_tokens * OUTPUT_PRICE_PER_MILLION
    ) / 1_000_000


def worst_case_request_cost(payload: Mapping[str, Any]) -> float:
    # UTF-8 byte count is a conservative token upper bound for the request body.
    input_upper_bound = len(canonical_json(payload).encode("utf-8"))
    return (input_upper_bound * INPUT_PRICE_PER_MILLION + MAX_TOKENS * OUTPUT_PRICE_PER_MILLION) / 1_000_000


_SPACE_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[^\w\u3400-\u4dbf\u4e00-\u9fff]+", re.UNICODE)


def _normalized_claim(claim: Mapping[str, Any]) -> tuple[str, str, str]:
    subject = _PUNCT_RE.sub("", _SPACE_RE.sub(" ", str(claim.get("subject") or "").casefold())).strip()
    value = _PUNCT_RE.sub("", _SPACE_RE.sub(" ", str(claim.get("value") or "").casefold())).strip()
    kind = str(claim.get("kind") or "").casefold()
    return subject, value, kind


def duplicate_profile(claims: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    accepted: list[tuple[str, str, str]] = []
    duplicates = 0
    for claim in claims:
        current = _normalized_claim(claim)
        duplicate = False
        for prior in accepted:
            if current == prior:
                duplicate = True
                break
            same_coordinate = current[0] == prior[0] and current[2] == prior[2]
            if same_coordinate and SequenceMatcher(None, current[1], prior[1]).ratio() >= 0.92:
                duplicate = True
                break
        if duplicate:
            duplicates += 1
        else:
            accepted.append(current)
    count = len(claims)
    return {
        "claim_count": count,
        "duplicate_count": duplicates,
        "duplicate_rate": duplicates / count if count else 0.0,
    }


def _response_usage(response: Mapping[str, Any]) -> dict[str, int]:
    usage = response.get("usage")
    if not isinstance(usage, Mapping):
        raise ValueError("provider response usage is missing or malformed")

    def token_count(label: str, *keys: str, source: Mapping[str, Any] = usage, required: bool = False) -> int:
        sentinel = object()
        value: Any = sentinel
        for key in keys:
            if key in source:
                value = source[key]
                break
        if value is sentinel:
            if required:
                raise ValueError(f"provider response usage is missing {label}")
            return 0
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"provider response usage has invalid {label}")
        integer = int(value)
        if value != integer or integer < 0:
            raise ValueError(f"provider response usage has invalid {label}")
        return integer

    completion_details = (
        usage.get("completion_tokens_details") if isinstance(usage.get("completion_tokens_details"), Mapping) else {}
    )
    prompt_details = (
        usage.get("prompt_tokens_details") if isinstance(usage.get("prompt_tokens_details"), Mapping) else {}
    )
    input_tokens = token_count("prompt_tokens", "prompt_tokens", "input_tokens", required=True)
    output_tokens = token_count("completion_tokens", "completion_tokens", "output_tokens", required=True)
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": token_count("total_tokens", "total_tokens") or input_tokens + output_tokens,
        "reasoning_tokens": token_count(
            "reasoning_tokens",
            "reasoning_tokens",
            source=completion_details if "reasoning_tokens" in completion_details else usage,
        ),
        "cached_tokens": token_count(
            "cached_tokens",
            "cached_tokens",
            source=prompt_details if "cached_tokens" in prompt_details else usage,
        ),
    }


def _assistant_content(response: Mapping[str, Any]) -> str:
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("provider response has no choices")
    message = choices[0].get("message") if isinstance(choices[0], Mapping) else None
    if not isinstance(message, Mapping):
        raise ValueError("provider response has no assistant message")
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(item.get("text") or "") for item in content if isinstance(item, Mapping))
    raise ValueError("assistant content is not text")


def _validate_assistant(content: str, arm: str) -> tuple[bool, dict[str, Any] | None, list[str]]:
    try:
        payload = json.loads(content)
    except (TypeError, json.JSONDecodeError) as error:
        return False, None, [f"json:{error}"]
    if not isinstance(payload, dict):
        return False, None, ["json:root_not_object"]
    errors = sorted(Draft202012Validator(response_schema(arm)).iter_errors(payload), key=lambda item: list(item.path))
    return not errors, payload, [error.message for error in errors[:20]]


def _execute_arm(
    client: httpx.Client,
    api_key: str,
    budget: BudgetGuard,
    case: Mapping[str, Any],
    runtime: Mapping[str, Any],
    arm: str,
    thinking_budget: int | None = None,
) -> dict[str, Any]:
    case_id = str(case["case_id"])
    request_id = f"{case_id}:{arm}"
    payload = build_payload(
        runtime["messages"], arm=arm, language=str(runtime["language"]), thinking_budget=thinking_budget
    )
    worst_case = worst_case_request_cost(payload)
    base = {
        "protocol_id": PROTOCOL_ID,
        "completed_at": utc_now(),
        "case_id": case_id,
        "case_type": str(case["case_type"]),
        "arm": arm,
        "pair_order": list(arm_order(case_id)),
        "language": str(runtime["language"]),
        "body_length": int(runtime["body_length"]),
        "body_sha256": str(runtime["body_sha256"]),
        "request_fingerprint": sha256_text(canonical_json(payload)),
        "request_started": False,
        "attempt_count": 0,
        "configuration": {
            "model": MODEL,
            "base_url": BASE_URL,
            "enable_thinking": ENABLE_THINKING,
            "strict_json_schema": True,
            "max_tokens": MAX_TOKENS,
            "timeout_seconds": TIMEOUT_SECONDS,
            "max_attempts": MAX_ATTEMPTS,
            "schema_max_items": 20 if arm == "A" else 30,
            "prompt_sha256": sha256_text(system_prompt(str(runtime["language"]), arm)),
            "schema_sha256": sha256_text(canonical_json(response_schema(arm))),
        },
    }
    if thinking_budget is not None:
        base["configuration"].update({"enable_thinking": True, "thinking_budget": thinking_budget})
    if not budget.try_reserve(request_id, worst_case):
        return {
            **base,
            "status": "budget_stopped",
            "latency_seconds": 0.0,
            "schema_valid": False,
            "schema_errors": ["budget guard refused a new request"],
            "claims": [],
            "claim_count": 0,
            "usage": {
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "reasoning_tokens": 0,
                "cached_tokens": 0,
            },
            "cost_cny": 0.0,
            "duplicate_profile": duplicate_profile([]),
            "budget": budget.snapshot(),
        }
    started = time.perf_counter()
    actual_cost = 0.0
    request_dispatched = False
    budget_settled = False
    try:
        request_dispatched = True
        response = client.post(
            ENDPOINT,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=httpx.Timeout(TIMEOUT_SECONDS),
        )
        response.raise_for_status()
        envelope = response.json()
        if not isinstance(envelope, dict):
            raise TypeError("provider response body is not an object")
        usage = _response_usage(envelope)
        actual_cost = usage_cost_cny(usage)
        budget.settle(request_id, actual_cost)
        budget_settled = True
        content = _assistant_content(envelope)
        schema_valid, parsed, schema_errors = _validate_assistant(content, arm)
        claims = parsed.get("claims", []) if isinstance(parsed, dict) and isinstance(parsed.get("claims"), list) else []
        choice = envelope.get("choices", [{}])[0]
        return {
            **base,
            "completed_at": utc_now(),
            "request_started": True,
            "attempt_count": 1,
            "status": "success" if schema_valid else "schema_error",
            "latency_seconds": time.perf_counter() - started,
            "schema_valid": schema_valid,
            "schema_errors": schema_errors,
            "claims": claims,
            "claim_count": len(claims),
            "should_memorize": parsed.get("should_memorize") if isinstance(parsed, dict) else None,
            "finish_reason": choice.get("finish_reason") if isinstance(choice, Mapping) else None,
            "raw_request_id": envelope.get("id") or response.headers.get("x-request-id"),
            "usage": usage,
            "cost_cny": actual_cost,
            "duplicate_profile": duplicate_profile(claims),
            "budget": budget.snapshot(),
        }
    except Exception as error:
        if not budget_settled:
            if request_dispatched:
                try:
                    actual_cost = max(
                        actual_cost,
                        budget.halt_unknown_cost(
                            request_id,
                            f"cost unknown after dispatched request {request_id}: {type(error).__name__}",
                        ),
                    )
                except ValueError:
                    # settle() already removed and truthfully charged an overrun reservation.
                    pass
            else:
                budget.release(request_id)
        status_code = error.response.status_code if isinstance(error, httpx.HTTPStatusError) else None
        provider_body = None
        if isinstance(error, httpx.HTTPStatusError):
            provider_body = error.response.text[:500]
        return {
            **base,
            "completed_at": utc_now(),
            "request_started": True,
            "attempt_count": 1,
            "status": "api_error",
            "latency_seconds": time.perf_counter() - started,
            "schema_valid": False,
            "schema_errors": [f"{type(error).__name__}: {str(error)[:300]}"],
            "claims": [],
            "claim_count": 0,
            "usage": {
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "reasoning_tokens": 0,
                "cached_tokens": 0,
            },
            "cost_cny": actual_cost,
            "duplicate_profile": duplicate_profile([]),
            "error": {"class": type(error).__name__, "status_code": status_code, "provider_body": provider_body},
            "budget": budget.snapshot(),
        }


def _runtime_for_case(manifest: Mapping[str, Any], case: Mapping[str, Any]) -> dict[str, Any]:
    source = manifest["source"]
    if case["case_type"] == "dense":
        runtime = load_dense_runtime(Path(str(source["dense_database"])), case)
    else:
        memdaily_path = Path(str(source["memdaily_source"]))
        if file_sha256(memdaily_path) != str(source["memdaily_source_sha256"]):
            raise ValueError("MemDaily source hash changed after prepare")
        runtime = load_short_runtime(memdaily_path, case)
    if runtime["body_sha256"] != case["body_sha256"]:
        raise ValueError(f"prepared body hash changed: {case['case_id']}")
    return runtime


def _latest_by_arm(rows: Sequence[Mapping[str, Any]]) -> dict[tuple[str, str], Mapping[str, Any]]:
    result: dict[tuple[str, str], Mapping[str, Any]] = {}
    for row in rows:
        key = (str(row.get("case_id")), str(row.get("arm")))
        result[key] = row
    return result


def _was_attempted(row: Mapping[str, Any]) -> bool:
    if "request_started" in row:
        return bool(row["request_started"])
    return str(row.get("status")) not in {"budget_stopped", "runner_error", "missing"}


def _row_duplicate_profile(row: Mapping[str, Any]) -> Mapping[str, Any]:
    profile = row.get("duplicate_profile")
    if isinstance(profile, Mapping):
        return profile
    claims = row.get("claims")
    if isinstance(claims, list):
        return duplicate_profile([claim for claim in claims if isinstance(claim, Mapping)])
    return duplicate_profile([])


def _percentile(values: Sequence[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _early_stop_metrics(first_cases: Sequence[Mapping[str, Any]], rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    latest = _latest_by_arm(rows)
    b_rows = [latest.get((str(case["case_id"]), "B")) for case in first_cases]
    qualifying = sum(bool(row and row.get("schema_valid") and int(row.get("claim_count") or 0) >= 12) for row in b_rows)
    latencies = [float(row["latency_seconds"]) for row in b_rows if row and row.get("request_started")]
    p50 = _percentile(latencies, 0.50)
    passed = qualifying >= 8 and p50 is not None and p50 <= 60.0
    return {
        "paired_cases": len(first_cases),
        "b_cases_at_least_12_claims": qualifying,
        "b_latency_p50_seconds": p50,
        "passed": passed,
        "stop_reasons": [
            reason
            for condition, reason in (
                (qualifying < 8, "fewer_than_8_of_first_10_dense_B_cases_reached_12_claims"),
                (p50 is None or p50 > 60.0, "first_10_dense_B_latency_p50_exceeded_60_seconds"),
            )
            if condition
        ],
    }


def _manual_review_metrics(manifest: Mapping[str, Any], manual_review: Mapping[str, Any]) -> dict[str, Any]:
    """Derive every manual gate value from case/fact evidence, never aggregate counters."""
    cases = [case for case in manifest.get("cases", []) if isinstance(case, Mapping)]
    selection = manifest.get("selection") if isinstance(manifest.get("selection"), Mapping) else {}
    expected_key_ids = [str(value) for value in selection.get("key_fact_case_ids", [])]
    expected_short_ids = [str(case["case_id"]) for case in cases if case.get("case_type") == "short"]
    key_review = (
        manual_review.get("key_fact_review") if isinstance(manual_review.get("key_fact_review"), Mapping) else {}
    )
    short_review = (
        manual_review.get("short_event_review") if isinstance(manual_review.get("short_event_review"), Mapping) else {}
    )

    key_cases = [case for case in key_review.get("cases", []) if isinstance(case, Mapping)]
    key_ids = [str(case.get("case_id") or "") for case in key_cases]
    key_details_valid = (
        len(expected_key_ids) == 5
        and len(key_cases) == 5
        and len(set(key_ids)) == len(key_ids)
        and set(key_ids) == set(expected_key_ids)
    )
    key_reviewed = 0
    key_total = 0
    key_covered = 0
    hallucinations = 0
    for case in key_cases:
        reviewed = case.get("reviewed") is True
        key_reviewed += int(reviewed)
        facts = case.get("facts") if isinstance(case.get("facts"), list) else []
        fact_ids: list[str] = []
        for fact in facts:
            if not isinstance(fact, Mapping):
                key_details_valid = False
                continue
            fact_id = str(fact.get("id") or "")
            fact_ids.append(fact_id)
            if not fact_id or not str(fact.get("text") or "").strip() or not isinstance(fact.get("covered"), bool):
                key_details_valid = False
                continue
            key_total += 1
            key_covered += int(fact["covered"])
        if not facts or len(set(fact_ids)) != len(fact_ids):
            key_details_valid = False
        case_hallucinations = case.get("hallucinations")
        if not isinstance(case_hallucinations, list):
            key_details_valid = False
        else:
            hallucinations += len(case_hallucinations)
    key_coverage = key_covered / key_total if key_total else 0.0
    key_aggregate_consistent = (
        [str(value) for value in key_review.get("case_ids", [])] == expected_key_ids
        and int(key_review.get("reviewed_case_count") or 0) == key_reviewed
        and int(key_review.get("covered") or 0) == key_covered
        and int(key_review.get("total") or 0) == key_total
        and int(key_review.get("hallucinated_claims") or 0) == hallucinations
    )

    short_cases = [case for case in short_review.get("cases", []) if isinstance(case, Mapping)]
    short_ids = [str(case.get("case_id") or "") for case in short_cases]
    short_details_valid = (
        len(expected_short_ids) == 6
        and len(short_cases) == 6
        and len(set(short_ids)) == len(short_ids)
        and set(short_ids) == set(expected_short_ids)
    )
    short_reviewed = 0
    padding_claims = 0
    for case in short_cases:
        short_reviewed += int(case.get("reviewed") is True)
        expected = case.get("expected")
        if not expected or not isinstance(expected, (str, list)):
            short_details_valid = False
        padding_ids = case.get("padding_claim_ids")
        if not isinstance(padding_ids, list):
            short_details_valid = False
        else:
            padding_claims += len(padding_ids)
    short_aggregate_consistent = (
        [str(value) for value in short_review.get("case_ids", [])] == expected_short_ids
        and int(short_review.get("reviewed_case_count") or 0) == short_reviewed
        and int(short_review.get("padding_claims") or 0) == padding_claims
    )
    return {
        "key": {
            "reviewed_cases": key_reviewed,
            "covered": key_covered,
            "total": key_total,
            "coverage": key_coverage,
            "hallucinated_claims": hallucinations,
            "details_valid": key_details_valid,
            "aggregate_consistent": key_aggregate_consistent,
        },
        "short": {
            "reviewed_cases": short_reviewed,
            "padding_claims": padding_claims,
            "details_valid": short_details_valid,
            "aggregate_consistent": short_aggregate_consistent,
        },
    }


def _configuration_mismatches(
    manifest: Mapping[str, Any],
    latest: Mapping[tuple[str, str], Mapping[str, Any]],
    expected_keys: set[tuple[str, str]],
) -> list[dict[str, Any]]:
    configuration = manifest.get("configuration") if isinstance(manifest.get("configuration"), Mapping) else {}
    arms = configuration.get("arms") if isinstance(configuration.get("arms"), Mapping) else {}
    prompt_hashes = (
        configuration.get("prompt_sha256") if isinstance(configuration.get("prompt_sha256"), Mapping) else {}
    )
    schema_hashes = (
        configuration.get("schema_sha256") if isinstance(configuration.get("schema_sha256"), Mapping) else {}
    )
    mismatches: list[dict[str, Any]] = []
    for case_id, arm in sorted(expected_keys):
        row = latest.get((case_id, arm))
        if row is None:
            continue
        language = str(row.get("language") or "")
        arm_configuration = arms.get(arm) if isinstance(arms.get(arm), Mapping) else {}
        arm_prompt_hashes = prompt_hashes.get(arm) if isinstance(prompt_hashes.get(arm), Mapping) else {}
        expected = {
            "model": configuration.get("model"),
            "base_url": configuration.get("base_url"),
            "enable_thinking": configuration.get("enable_thinking"),
            "strict_json_schema": configuration.get("strict_json_schema"),
            "max_tokens": configuration.get("max_tokens"),
            "timeout_seconds": configuration.get("timeout_seconds"),
            "max_attempts": configuration.get("max_attempts"),
            "schema_max_items": arm_configuration.get("max_items"),
            "prompt_sha256": arm_prompt_hashes.get(language),
            "schema_sha256": schema_hashes.get(arm),
        }
        actual = row.get("configuration") if isinstance(row.get("configuration"), Mapping) else {}
        fields = [name for name, expected_value in expected.items() if actual.get(name) != expected_value]
        if fields:
            mismatches.append({"case_id": case_id, "arm": arm, "fields": fields})
    return mismatches


def run_experiment(manifest_path: Path, env_file: Path, output_path: Path, metadata_path: Path) -> dict[str, Any]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("protocol_id") != PROTOCOL_ID:
        raise ValueError("manifest protocol_id does not match")
    validate_frozen_configuration(manifest)
    cases = manifest.get("cases")
    if not isinstance(cases, list) or len(cases) != 26:
        raise ValueError("frozen run requires exactly 20 dense + 6 short cases")
    api_key = _env_value(env_file, "EMBEDDING_API_KEY")
    pre_run_review = json.loads(DEFAULT_MANUAL_REVIEW.read_text(encoding="utf-8"))
    frozen_gold_sha256 = gold_definition_sha256(pre_run_review)
    existing = read_jsonl(output_path)
    completed = {
        (str(row.get("case_id")), str(row.get("arm")))
        for row in existing
        if row.get("request_started") and row.get("status") in {"success", "schema_error", "api_error"}
    }
    spent = sum(float(row.get("cost_cny") or 0.0) for row in existing if row.get("request_started"))
    budget = BudgetGuard(soft_limit_cny=SOFT_BUDGET_CNY, hard_limit_cny=HARD_BUDGET_CNY, spent_cny=spent)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    write_lock = threading.Lock()
    progress_lock = threading.Lock()
    pair_counter = {"done": 0}

    def append(record: Mapping[str, Any]) -> None:
        with write_lock:
            with output_path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
                stream.flush()

    limits = httpx.Limits(max_connections=CONCURRENCY, max_keepalive_connections=CONCURRENCY)
    with httpx.Client(limits=limits) as client:

        def evaluate_pair(case: Mapping[str, Any]) -> list[dict[str, Any]]:
            case_id = str(case["case_id"])
            records: list[dict[str, Any]] = []
            try:
                runtime = _runtime_for_case(manifest, case)
                for arm in arm_order(case_id):
                    if (case_id, arm) in completed:
                        continue
                    record = _execute_arm(client, api_key, budget, case, runtime, arm)
                    append(record)
                    records.append(record)
            except Exception as error:
                for arm in arm_order(case_id):
                    if (case_id, arm) in completed:
                        continue
                    record = {
                        "protocol_id": PROTOCOL_ID,
                        "completed_at": utc_now(),
                        "case_id": case_id,
                        "case_type": case["case_type"],
                        "arm": arm,
                        "pair_order": list(arm_order(case_id)),
                        "request_started": False,
                        "attempt_count": 0,
                        "status": "runner_error",
                        "schema_valid": False,
                        "schema_errors": [f"{type(error).__name__}: {str(error)[:300]}"],
                        "claims": [],
                        "claim_count": 0,
                        "usage": {
                            "input_tokens": 0,
                            "output_tokens": 0,
                            "total_tokens": 0,
                            "reasoning_tokens": 0,
                            "cached_tokens": 0,
                        },
                        "cost_cny": 0.0,
                        "duplicate_profile": duplicate_profile([]),
                        "budget": budget.snapshot(),
                    }
                    append(record)
                    records.append(record)
            with progress_lock:
                pair_counter["done"] += 1
                statuses = ",".join(f"{item['arm']}={item['status']}" for item in records) or "resumed"
                print(
                    f"pair {pair_counter['done']}/26 {case_id} {statuses} budget={budget.snapshot()['spent_cny']:.6f}",
                    flush=True,
                )
            return records

        def run_batch(batch: Sequence[Mapping[str, Any]]) -> None:
            with ThreadPoolExecutor(max_workers=CONCURRENCY) as executor:
                futures = [executor.submit(evaluate_pair, case) for case in batch]
                for future in as_completed(futures):
                    future.result()

        dense_cases = [case for case in cases if case["case_type"] == "dense"]
        first_ten = dense_cases[:10]
        run_batch(first_ten)
        rows_after_first = read_jsonl(output_path)
        early_stop = _early_stop_metrics(first_ten, rows_after_first)
        metadata = {
            "protocol_id": PROTOCOL_ID,
            "updated_at": utc_now(),
            "manifest_sha256": file_sha256(manifest_path),
            "gold_definition_sha256": frozen_gold_sha256,
            "early_stop": early_stop,
            "budget": budget.snapshot(),
            "stopped": not early_stop["passed"],
            "stop_reason": "preflight" if not early_stop["passed"] else None,
        }
        write_json(metadata_path, metadata)
        if not early_stop["passed"]:
            print(json.dumps(metadata, ensure_ascii=False), flush=True)
            return metadata
        remaining = dense_cases[10:] + [case for case in cases if case["case_type"] == "short"]
        run_batch(remaining)
    metadata.update(
        {
            "updated_at": utc_now(),
            "budget": budget.snapshot(),
            "stopped": bool(budget.snapshot()["soft_stop_reached"]),
            "stop_reason": "soft_budget" if budget.snapshot()["soft_stop_reached"] else None,
            "completed_pair_count": len({str(row.get("case_id")) for row in read_jsonl(output_path)}),
        }
    )
    write_json(metadata_path, metadata)
    print(json.dumps(metadata, ensure_ascii=False), flush=True)
    return metadata


def _arm_summary(
    cases: Sequence[Mapping[str, Any]], latest: Mapping[tuple[str, str], Mapping[str, Any]], arm: str
) -> dict[str, Any]:
    rows = [latest.get((str(case["case_id"]), arm)) for case in cases]
    present = [row for row in rows if row is not None]
    valid = [row for row in present if row.get("schema_valid")]
    latencies = [float(row["latency_seconds"]) for row in present if _was_attempted(row)]
    output_tokens = [int(row.get("usage", {}).get("output_tokens") or 0) for row in present if _was_attempted(row)]
    claims = [int(row.get("claim_count") or 0) for row in valid]
    duplicate_claims = sum(int(_row_duplicate_profile(row).get("duplicate_count") or 0) for row in valid)
    total_claims = sum(int(_row_duplicate_profile(row).get("claim_count") or 0) for row in valid)
    return {
        "expected_arm_cases": len(cases),
        "present_arm_cases": len(present),
        "schema_valid_cases": len(valid),
        "schema_success_rate": len(valid) / len(cases) if cases else 0.0,
        "latency_p50_seconds": _percentile(latencies, 0.50),
        "latency_p95_seconds": _percentile(latencies, 0.95),
        "claim_count_mean": sum(claims) / len(claims) if claims else None,
        "claim_count_p50": _percentile([float(value) for value in claims], 0.50),
        "output_tokens_p95": _percentile([float(value) for value in output_tokens], 0.95),
        "cost_total_cny": sum(float(row.get("cost_cny") or 0.0) for row in present),
        "cost_mean_per_expected_case_cny": (
            sum(float(row.get("cost_cny") or 0.0) for row in present) / len(cases) if cases else None
        ),
        "duplicate_count": duplicate_claims,
        "claim_total_for_duplicates": total_claims,
        "duplicate_rate": duplicate_claims / total_claims if total_claims else 0.0,
    }


def score_records(
    manifest: Mapping[str, Any],
    runs: Sequence[Mapping[str, Any]],
    manual_review: Mapping[str, Any],
) -> dict[str, Any]:
    cases = list(manifest.get("cases") or [])
    dense_cases = [case for case in cases if case.get("case_type") == "dense"]
    short_cases = [case for case in cases if case.get("case_type") == "short"]
    latest = _latest_by_arm(runs)
    expected_arm_cases = len(cases) * 2
    expected_keys = {(str(case["case_id"]), arm) for case in cases for arm in ("A", "B")}
    present_keys = expected_keys.intersection(latest)
    attempted_requests = sum(
        1 for row in runs if _was_attempted(row) and (str(row.get("case_id")), str(row.get("arm"))) in expected_keys
    )
    valid_expected = sum(bool(latest[key].get("schema_valid")) for key in present_keys)
    schema_success_rate = valid_expected / expected_arm_cases if expected_arm_cases else 0.0
    b_dense = [latest.get((str(case["case_id"]), "B")) for case in dense_cases]
    dense_qualifying = sum(
        bool(row and row.get("schema_valid") and int(row.get("claim_count") or 0) >= 12) for row in b_dense
    )
    b_rows = [latest.get((str(case["case_id"]), "B")) for case in cases]
    b_attempted = [row for row in b_rows if row and _was_attempted(row)]
    b_latencies = [float(row["latency_seconds"]) for row in b_attempted]
    b_latency_p50 = _percentile(b_latencies, 0.50)
    b_latency_p95 = _percentile(b_latencies, 0.95)
    missing_keys = expected_keys.difference(present_keys)
    nonzero_reasoning = sum(
        int(row.get("usage", {}).get("reasoning_tokens") or 0) != 0
        for key, row in latest.items()
        if key in expected_keys
    )
    b_output_tokens = [float(row.get("usage", {}).get("output_tokens") or 0) for row in b_attempted]
    b_output_p95 = _percentile(b_output_tokens, 0.95)
    b_cost_total = sum(float(row.get("cost_cny") or 0.0) for row in b_attempted)
    b_cost_mean = b_cost_total / len(cases) if cases else None
    duplicate_count = sum(int(_row_duplicate_profile(row).get("duplicate_count") or 0) for row in b_rows if row)
    duplicate_claim_count = sum(int(_row_duplicate_profile(row).get("claim_count") or 0) for row in b_rows if row)
    duplicate_rate = duplicate_count / duplicate_claim_count if duplicate_claim_count else 0.0
    review_metrics = _manual_review_metrics(manifest, manual_review)
    key_metrics = review_metrics["key"]
    short_metrics = review_metrics["short"]
    configuration_mismatches = _configuration_mismatches(manifest, latest, expected_keys)

    def gate(gate_id: str, label: str, measured: Any, threshold: str, passed: bool) -> dict[str, Any]:
        return {
            "gate_id": gate_id,
            "label": label,
            "measured": measured,
            "threshold": threshold,
            "passed": bool(passed),
        }

    gates = [
        gate(
            "dense_12_claims",
            "B 臂高密度案 claim 数",
            {"qualifying_cases": dense_qualifying, "dense_cases": len(dense_cases)},
            "20 案中至少 18 案 >=12 claims",
            len(dense_cases) == 20 and dense_qualifying >= 18,
        ),
        gate(
            "latency_p50",
            "B 臂延迟 p50",
            b_latency_p50,
            "<=60s",
            len(b_attempted) == len(cases) and b_latency_p50 is not None and b_latency_p50 <= 60.0,
        ),
        gate(
            "latency_p95",
            "B 臂延迟 p95",
            b_latency_p95,
            "<=90s",
            len(b_attempted) == len(cases) and b_latency_p95 is not None and b_latency_p95 <= 90.0,
        ),
        gate(
            "requests_per_arm_case",
            "请求数/arm-case",
            attempted_requests / expected_arm_cases if expected_arm_cases else None,
            "<=1.2",
            not missing_keys and attempted_requests / expected_arm_cases <= 1.2,
        ),
        gate(
            "reasoning_tokens",
            "reasoning tokens",
            {"nonzero_records": nonzero_reasoning, "missing_arm_cases": len(missing_keys)},
            "全部为 0",
            not missing_keys and nonzero_reasoning == 0,
        ),
        gate(
            "output_tokens_p95",
            "B 臂输出 tokens p95",
            b_output_p95,
            "<=4000",
            len(b_attempted) == len(cases) and b_output_p95 is not None and b_output_p95 <= 4000,
        ),
        gate(
            "cost_mean",
            "B 臂平均成本/案",
            b_cost_mean,
            "<=¥0.02",
            len(b_attempted) == len(cases) and b_cost_mean is not None and b_cost_mean <= 0.02,
        ),
        gate(
            "schema_success",
            "严格 schema 成功率",
            schema_success_rate,
            ">=95%",
            not missing_keys and schema_success_rate >= 0.95,
        ),
        gate(
            "configuration_integrity",
            "运行记录配置冻结完整性",
            {"mismatch_count": len(configuration_mismatches), "mismatches": configuration_mismatches[:20]},
            "52 arm-cases 全部与 manifest 配置一致",
            not missing_keys and not configuration_mismatches,
        ),
        gate(
            "duplicate_rate",
            "B 臂案内重复率",
            {"duplicate_rate": duplicate_rate, "duplicates": duplicate_count, "claims": duplicate_claim_count},
            "<=2%",
            len(b_rows) == len(cases) and all(row is not None for row in b_rows) and duplicate_rate <= 0.02,
        ),
        gate(
            "key_fact_quality",
            "5 案关键事实覆盖与虚构",
            key_metrics,
            "5 案、coverage>=90%、hallucination=0",
            key_metrics["reviewed_cases"] == 5
            and key_metrics["total"] > 0
            and key_metrics["coverage"] >= 0.90
            and key_metrics["hallucinated_claims"] == 0
            and key_metrics["details_valid"]
            and key_metrics["aggregate_consistent"],
        ),
        gate(
            "short_no_padding",
            "6 个短事件零凑数",
            short_metrics,
            "6 案、padding=0",
            len(short_cases) == 6
            and short_metrics["reviewed_cases"] == 6
            and short_metrics["padding_claims"] == 0
            and short_metrics["details_valid"]
            and short_metrics["aggregate_consistent"],
        ),
    ]
    return {
        "overall_passed": all(item["passed"] for item in gates),
        "expected_cases": len(cases),
        "expected_arm_cases": expected_arm_cases,
        "present_arm_cases": len(present_keys),
        "missing_arm_cases": [{"case_id": case_id, "arm": arm} for case_id, arm in sorted(missing_keys)],
        "gates": gates,
        "arms": {arm: _arm_summary(cases, latest, arm) for arm in ("A", "B")},
        "cost": {
            "total_cny": sum(float(row.get("cost_cny") or 0.0) for row in runs if row.get("request_started")),
            "A_cny": sum(
                float(row.get("cost_cny") or 0.0)
                for row in runs
                if row.get("arm") == "A" and row.get("request_started")
            ),
            "B_cny": sum(
                float(row.get("cost_cny") or 0.0)
                for row in runs
                if row.get("arm") == "B" and row.get("request_started")
            ),
            "pricing_basis": "provider usage × frozen official China (Beijing) list price",
        },
    }


def _format_measured(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.6f}"
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def write_gate_table(path: Path, report: Mapping[str, Any]) -> None:
    lines = [
        "# Prompt 密度 A/B 门禁对照表",
        "",
        f"- 总判定：{'PASS' if report['overall_passed'] else 'FAIL'}",
        f"- usage 原价估算实耗：¥{report['cost']['total_cny']:.6f}",
        "",
        "| 门禁 | 实测值 | 门槛 | 判定 |",
        "|---|---:|---:|:---:|",
    ]
    for gate in report["gates"]:
        lines.append(
            f"| {gate['label']} | {_format_measured(gate['measured'])} | {gate['threshold']} | {'PASS' if gate['passed'] else 'FAIL'} |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_comparison(path: Path, manifest: Mapping[str, Any], runs: Sequence[Mapping[str, Any]]) -> None:
    latest = _latest_by_arm(runs)
    fields = [
        "case_id",
        "case_type",
        "pair_order",
        "a_status",
        "b_status",
        "a_claims",
        "b_claims",
        "claim_delta_b_minus_a",
        "a_latency_seconds",
        "b_latency_seconds",
        "a_output_tokens",
        "b_output_tokens",
        "a_cost_cny",
        "b_cost_cny",
        "a_duplicate_rate",
        "b_duplicate_rate",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for case in manifest["cases"]:
            case_id = str(case["case_id"])
            a = latest.get((case_id, "A"), {})
            b = latest.get((case_id, "B"), {})
            a_claims = int(a.get("claim_count") or 0)
            b_claims = int(b.get("claim_count") or 0)
            writer.writerow(
                {
                    "case_id": case_id,
                    "case_type": case["case_type"],
                    "pair_order": "".join(arm_order(case_id)),
                    "a_status": a.get("status", "missing"),
                    "b_status": b.get("status", "missing"),
                    "a_claims": a_claims,
                    "b_claims": b_claims,
                    "claim_delta_b_minus_a": b_claims - a_claims,
                    "a_latency_seconds": a.get("latency_seconds"),
                    "b_latency_seconds": b.get("latency_seconds"),
                    "a_output_tokens": a.get("usage", {}).get("output_tokens"),
                    "b_output_tokens": b.get("usage", {}).get("output_tokens"),
                    "a_cost_cny": a.get("cost_cny"),
                    "b_cost_cny": b.get("cost_cny"),
                    "a_duplicate_rate": a.get("duplicate_profile", {}).get("duplicate_rate"),
                    "b_duplicate_rate": b.get("duplicate_profile", {}).get("duplicate_rate"),
                }
            )


def run_metadata_integrity(
    metadata: Mapping[str, Any] | None,
    manifest_sha256: str,
    manual_review: Mapping[str, Any],
    gold_sha256: str,
) -> dict[str, Any]:
    """Bind score-time inputs to the exact pre-run manifest and gold definition."""
    mismatches: list[str] = []
    if metadata is None:
        mismatches.append("metadata_missing")
    else:
        if metadata.get("protocol_id") != PROTOCOL_ID:
            mismatches.append("metadata.protocol_id")
        if metadata.get("manifest_sha256") != manifest_sha256:
            mismatches.append("metadata.manifest_sha256")
        if metadata.get("gold_definition_sha256") != gold_sha256:
            mismatches.append("metadata.gold_definition_sha256")
    if manual_review.get("manifest_sha256") != manifest_sha256:
        mismatches.append("manual_review.manifest_sha256")
    return {
        "passed": not mismatches,
        "mismatches": mismatches,
        "expected": {
            "protocol_id": PROTOCOL_ID,
            "manifest_sha256": manifest_sha256,
            "gold_definition_sha256": gold_sha256,
        },
    }


def score_experiment(
    manifest_path: Path,
    runs_path: Path,
    manual_review_path: Path,
    metadata_path: Path,
    output_path: Path,
    table_path: Path,
    comparison_path: Path,
) -> dict[str, Any]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    runs = read_jsonl(runs_path)
    manual_review = json.loads(manual_review_path.read_text(encoding="utf-8"))
    report = score_records(manifest, runs, manual_review)
    manifest_sha = file_sha256(manifest_path)
    gold_sha = gold_definition_sha256(manual_review)
    report.update(
        {
            "protocol_id": PROTOCOL_ID,
            "scored_at": utc_now(),
            "manifest_sha256": manifest_sha,
            "runs_sha256": file_sha256(runs_path) if runs_path.is_file() else None,
            "manual_review_sha256": file_sha256(manual_review_path),
            "gold_definition_sha256": gold_sha,
        }
    )
    metadata: Mapping[str, Any] | None = None
    if metadata_path.is_file():
        raw_metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata = raw_metadata if isinstance(raw_metadata, Mapping) else None
        report["run_metadata"] = raw_metadata
        report["run_metadata_sha256"] = file_sha256(metadata_path)
    metadata_check = run_metadata_integrity(metadata, manifest_sha, manual_review, gold_sha)
    report["gates"].append(
        {
            "gate_id": "run_metadata_integrity",
            "label": "运行 metadata 输入绑定",
            "measured": metadata_check,
            "threshold": "metadata 存在且 protocol/manifest/gold 与本次判分输入一致",
            "passed": metadata_check["passed"],
        }
    )
    frozen_gold = metadata.get("gold_definition_sha256") if metadata is not None else None
    gold_integrity_passed = frozen_gold == gold_sha
    report["gates"].append(
        {
            "gate_id": "gold_freeze_integrity",
            "label": "盲审 gold 冻结完整性",
            "measured": {"pre_run": frozen_gold, "score_time": gold_sha},
            "threshold": "pre-run hash == score-time hash",
            "passed": gold_integrity_passed,
        }
    )
    report["overall_passed"] = bool(report["overall_passed"] and metadata_check["passed"] and gold_integrity_passed)
    if report["overall_passed"]:
        report["production_landing_diff"] = {
            "prompt_additions_zh": list(ZH_DENSITY_LINES),
            "prompt_additions_en": list(EN_DENSITY_LINES),
            "six_scalars": {
                "schema.claims.maxItems": 30,
                "llm.model": MODEL,
                "llm.enable_thinking": False,
                "llm.max_tokens": MAX_TOKENS,
                "llm.timeout": TIMEOUT_SECONDS,
                "llm.max_attempts": MAX_ATTEMPTS,
            },
            "toml_lines": [
                f'model = "{MODEL}"',
                f"enable_thinking = {str(ENABLE_THINKING).lower()}",
                f"max_tokens = {MAX_TOKENS}",
            ],
        }
    write_json(output_path, report)
    write_gate_table(table_path, report)
    write_comparison(comparison_path, manifest, runs)
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare", help="freeze 20 dense + 6 short cases")
    prepare.add_argument("--source-manifest", type=Path, default=DEFAULT_SOURCE_MANIFEST)
    prepare.add_argument("--memdaily-sample", type=Path, default=DEFAULT_MEMDAILY_SAMPLE)
    prepare.add_argument("--output", type=Path, default=DEFAULT_MANIFEST)
    prepare.add_argument("--manual-review", type=Path, default=DEFAULT_MANUAL_REVIEW)

    run = subparsers.add_parser("run", help="run real paired A/B requests")
    run.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    run.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    run.add_argument("--output", type=Path, default=DEFAULT_RUNS)
    run.add_argument("--metadata", type=Path, default=DEFAULT_RUN_METADATA)

    score = subparsers.add_parser("score", help="apply every frozen hard gate")
    score.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    score.add_argument("--runs", type=Path, default=DEFAULT_RUNS)
    score.add_argument("--manual-review", type=Path, default=DEFAULT_MANUAL_REVIEW)
    score.add_argument("--metadata", type=Path, default=DEFAULT_RUN_METADATA)
    score.add_argument("--output", type=Path, default=DEFAULT_REPORT)
    score.add_argument("--table", type=Path, default=DEFAULT_GATE_TABLE)
    score.add_argument("--comparison", type=Path, default=DEFAULT_COMPARISON)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "prepare":
        manifest = prepare_experiment(args.source_manifest, args.memdaily_sample, args.output, args.manual_review)
        print(
            json.dumps(
                {
                    "protocol_id": manifest["protocol_id"],
                    "cases": len(manifest["cases"]),
                    "dense": sum(case["case_type"] == "dense" for case in manifest["cases"]),
                    "short": sum(case["case_type"] == "short" for case in manifest["cases"]),
                    "excluded": manifest["source"]["excluded_case_count"],
                    "output": str(args.output),
                },
                ensure_ascii=False,
            )
        )
        return 0
    if args.command == "run":
        metadata = run_experiment(args.manifest, args.env_file, args.output, args.metadata)
        return 2 if metadata.get("stopped") and metadata.get("stop_reason") == "preflight" else 0
    if args.command == "score":
        report = score_experiment(
            args.manifest,
            args.runs,
            args.manual_review,
            args.metadata,
            args.output,
            args.table,
            args.comparison,
        )
        print(json.dumps({"overall_passed": report["overall_passed"], "cost": report["cost"]}, ensure_ascii=False))
        return 0 if report["overall_passed"] else 1
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
