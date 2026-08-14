"""Evaluate Q0-Q4 embeddings on frozen LongMemEval claim corpora.

This is an evaluation-only tool.  It reads benchmark databases in SQLite
read-only mode, reuses stored Q1 document vectors, and performs dense cosine
ranking without importing or invoking the HL-Mem recall pipeline.
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import math
import re
import sqlite3
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any, Iterable, Mapping, Sequence

import httpx
import numpy as np
from run_embedding_ablation import (
    CONFIGS,
    Cost,
    DashScopeEmbeddingClient,
    EmbeddingOutput,
    _load_env_value,
    embed_remote,
)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CACHE = ROOT / "evaluation" / "cache" / "lme_embedding_sel_v1"
DEFAULT_CANARY_REPORT_GLOB = "longmemeval_final_canary_o*.json"
DEFAULT_SOURCE_DATASET = ROOT / "evaluation" / "longmemeval" / "longmemeval_s_cleaned.json"
HARD_CASE_IDS = (
    "gpt4_2ba83207",
    "gpt4_731e37d7",
    "gpt4_7abb270c",
    "gpt4_1916e0ea",
    "6a1eabeb",
    "07741c45",
)
CONFIG_CODES = ("Q0", "Q1", "Q2", "Q3", "Q4")
METRIC_NAMES = ("recall_at_5", "recall_at_10", "hit_at_1", "hit_at_5", "mrr")
PAIR_THRESHOLDS = (0.82, 0.92)
DEFAULT_LLM_BASE_URL = "https://coding.dashscope.aliyuncs.com/v1"


@dataclass
class Claim:
    case_id: str
    claim_id: str
    index_text: str
    value: str
    status: str
    subject_entity_id: str | None
    predicate: str | None
    canonical_slot: str | None
    conflict_key: str | None
    fact_hash: str | None
    valid_from: str | None
    valid_to: str | None
    recorded_from: str | None
    recorded_to: str | None
    supersedes_id: str | None
    superseded_by_id: str | None
    evidence_event_ids: list[str]
    evidence_session_ids: list[str]
    q1_dense: np.ndarray | None = field(repr=False)

    @property
    def in_retrieval_corpus(self) -> bool:
        return self.status == "active"

    def frozen_record(self) -> dict[str, Any]:
        return {
            "claim_id": self.claim_id,
            "index_text": self.index_text,
            "value": self.value,
            "status": self.status,
            "in_retrieval_corpus": self.in_retrieval_corpus,
            "subject_entity_id": self.subject_entity_id,
            "predicate": self.predicate,
            "canonical_slot": self.canonical_slot,
            "conflict_key": self.conflict_key,
            "fact_hash": self.fact_hash,
            "valid_from": self.valid_from,
            "valid_to": self.valid_to,
            "recorded_from": self.recorded_from,
            "recorded_to": self.recorded_to,
            "supersedes_id": self.supersedes_id,
            "superseded_by_id": self.superseded_by_id,
            "evidence_event_ids": self.evidence_event_ids,
            "evidence_session_ids": self.evidence_session_ids,
        }


@dataclass
class CaseCorpus:
    case_id: str
    question_type: str
    question: str
    answer: str
    gold_session_ids: list[str]
    database: Path
    claims: list[Claim]

    @property
    def active_claims(self) -> list[Claim]:
        return [claim for claim in self.claims if claim.in_retrieval_corpus]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=False) + "\n")
    temporary.replace(path)


def _canonical_sha256(payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def resolve_llm_base_url(env_path: Path) -> str:
    """Use the same coding-plan endpoint default as ``hl_mem.settings``."""
    return _load_env_value(env_path, "LLM_BASE_URL") or DEFAULT_LLM_BASE_URL


def _decode_value(raw: Any) -> str:
    text = str(raw or "")
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return text
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)


def _fallback_index_text(row: sqlite3.Row) -> str:
    return " ".join(
        str(value) for value in (row["subject_entity_id"], row["predicate"], _decode_value(row["value_json"])) if value
    )


def _decode_q1_vector(blob: bytes | None, model: Any, dim: Any) -> np.ndarray | None:
    if blob is None or model != "qwen3.7-text-embedding" or int(dim or 0) != 2048 or len(blob) != 8192:
        return None
    return np.frombuffer(blob, dtype="<f4").astype(np.float32, copy=True)


def _session_from_event(source_uri: Any, content_json: Any) -> str | None:
    source = str(source_uri or "")
    if source.startswith("longmemeval:") and ":" in source:
        return source.rsplit(":", 1)[-1]
    try:
        content = json.loads(str(content_json or "{}"))
    except json.JSONDecodeError:
        return None
    locator = content.get("benchmark_locator") if isinstance(content, dict) else None
    if isinstance(locator, dict) and locator.get("session_id") is not None:
        return str(locator["session_id"])
    return None


def _load_case(case_record: Mapping[str, Any]) -> CaseCorpus:
    case_id = str(case_record["case_id"])
    database_value = Path(str(case_record["database"]))
    database = database_value if database_value.is_absolute() else ROOT / database_value
    if not database.is_file():
        raise FileNotFoundError(f"case {case_id}: database does not exist: {database}")
    if case_record.get("error"):
        raise ValueError(f"case {case_id}: runner reported error: {case_record['error']}")

    uri = f"file:{database.resolve().as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        event_sessions = {
            str(row["id"]): _session_from_event(row["source_uri"], row["content_json"])
            for row in connection.execute("SELECT id,source_uri,content_json FROM events")
        }
        evidence: dict[str, list[str]] = defaultdict(list)
        for row in connection.execute(
            "SELECT derived_id,evidence_id FROM evidence_links "
            "WHERE derived_type='claim' AND evidence_type='event' ORDER BY derived_id,evidence_id"
        ):
            evidence[str(row["derived_id"])].append(str(row["evidence_id"]))
        rows = connection.execute(
            "SELECT id,subject_entity_id,predicate,value_json,status,index_text,"
            "canonical_slot,conflict_key,fact_hash,valid_from,valid_to,recorded_from,recorded_to,"
            "supersedes_id,superseded_by_id,embedding_dense,embedding_model,embedding_dim "
            "FROM claims ORDER BY id"
        ).fetchall()
    finally:
        connection.close()

    claims: list[Claim] = []
    for row in rows:
        claim_id = str(row["id"])
        event_ids = evidence.get(claim_id, [])
        session_ids = sorted(
            {session for event_id in event_ids if (session := event_sessions.get(event_id)) is not None}
        )
        claims.append(
            Claim(
                case_id=case_id,
                claim_id=claim_id,
                index_text=str(row["index_text"] or _fallback_index_text(row)),
                value=_decode_value(row["value_json"]),
                status=str(row["status"]),
                subject_entity_id=row["subject_entity_id"],
                predicate=row["predicate"],
                canonical_slot=row["canonical_slot"],
                conflict_key=row["conflict_key"],
                fact_hash=row["fact_hash"],
                valid_from=row["valid_from"],
                valid_to=row["valid_to"],
                recorded_from=row["recorded_from"],
                recorded_to=row["recorded_to"],
                supersedes_id=row["supersedes_id"],
                superseded_by_id=row["superseded_by_id"],
                evidence_event_ids=event_ids,
                evidence_session_ids=session_ids,
                q1_dense=_decode_q1_vector(row["embedding_dense"], row["embedding_model"], row["embedding_dim"]),
            )
        )
    return CaseCorpus(
        case_id=case_id,
        question_type=str(case_record.get("question_type") or "uncategorized"),
        question=str(case_record.get("question") or ""),
        answer=str(case_record.get("answer") or ""),
        gold_session_ids=[str(value) for value in case_record.get("gold_session_ids") or []],
        database=database,
        claims=claims,
    )


def load_cases(report_paths: Sequence[Path]) -> list[CaseCorpus]:
    records: dict[str, Mapping[str, Any]] = {}
    for report_path in report_paths:
        report = json.loads(report_path.read_text(encoding="utf-8-sig"))
        if report.get("status") != "completed":
            raise ValueError(f"runner report is not completed: {report_path}")
        for case in report.get("cases") or []:
            case_id = str(case.get("case_id") or "")
            if not case_id:
                raise ValueError(f"runner report contains case without case_id: {report_path}")
            if case_id in records:
                raise ValueError(f"duplicate case_id across reports: {case_id}")
            records[case_id] = case
    if not records:
        raise ValueError("no cases found in runner reports")
    return [_load_case(records[case_id]) for case_id in records]


def freeze_corpus(cases: Sequence[CaseCorpus], path: Path) -> dict[str, Any]:
    case_payloads = []
    for case in cases:
        claims = [claim.frozen_record() for claim in case.claims]
        case_payloads.append(
            {
                "case_id": case.case_id,
                "question_type": case.question_type,
                "question": case.question,
                "answer": case.answer,
                "gold_session_ids": case.gold_session_ids,
                "database": str(case.database.relative_to(ROOT)),
                "claims_total": len(claims),
                "claims_active": sum(bool(item["in_retrieval_corpus"]) for item in claims),
                "claims": claims,
            }
        )
    frozen = {
        "schema_version": 1,
        "generated_at": _utc_now(),
        "method": {
            "claim_order": "case input order, then claim id",
            "retrieval_filter": "status == active",
            "gold": "claim evidence session intersects answer_session_ids",
            "embedding_vectors_included": False,
        },
        "case_count": len(cases),
        "claims_total": sum(len(case.claims) for case in cases),
        "claims_active": sum(len(case.active_claims) for case in cases),
        "cases": case_payloads,
    }
    frozen["fingerprint"] = _canonical_sha256(
        {
            "method": frozen["method"],
            "cases": case_payloads,
        }
    )
    _write_json(path, frozen)
    return frozen


def _normalize_rows(matrix: np.ndarray) -> np.ndarray:
    values = np.asarray(matrix, dtype=np.float32)
    norms = np.linalg.norm(values, axis=1, keepdims=True)
    output = np.zeros_like(values)
    np.divide(values, norms, out=output, where=norms != 0.0)
    return output


def gold_claim_ids(claims: Sequence[Mapping[str, Any]], gold_session_ids: set[str]) -> set[str]:
    return {
        str(claim["claim_id"])
        for claim in claims
        if set(str(value) for value in claim.get("evidence_session_ids") or []) & gold_session_ids
    }


def compute_query_metrics(relevant_ids: set[str], ranked_ids: Sequence[str]) -> dict[str, Any]:
    if not relevant_ids:
        return {
            "eligible": False,
            "relevant_claims": 0,
            "first_relevant_rank": None,
            **{name: None for name in METRIC_NAMES},
            "utility": None,
        }
    ranked = list(ranked_ids)
    first_rank = next(
        (rank for rank, claim_id in enumerate(ranked, start=1) if claim_id in relevant_ids),
        None,
    )
    found_at_5 = relevant_ids & set(ranked[:5])
    found_at_10 = relevant_ids & set(ranked[:10])
    metrics: dict[str, Any] = {
        "eligible": True,
        "relevant_claims": len(relevant_ids),
        "first_relevant_rank": first_rank,
        "recall_at_5": len(found_at_5) / len(relevant_ids),
        "recall_at_10": len(found_at_10) / len(relevant_ids),
        "hit_at_1": float(bool(relevant_ids & set(ranked[:1]))),
        "hit_at_5": float(bool(found_at_5)),
        "mrr": 1.0 / first_rank if first_rank is not None else 0.0,
    }
    metrics["utility"] = mean(float(metrics[name]) for name in METRIC_NAMES)
    return metrics


def pairwise_win_loss_tie(
    per_config: Mapping[str, Mapping[str, Mapping[str, Any]]],
    *,
    metric: str,
    tolerance: float = 1e-12,
) -> dict[str, Any]:
    output: dict[str, Any] = {}
    codes = [code for code in CONFIG_CODES if code in per_config]
    for left, right in itertools.combinations(codes, 2):
        per_query: dict[str, str] = {}
        wins = losses = ties = 0
        common = sorted(set(per_config[left]) & set(per_config[right]))
        for case_id in common:
            left_value = per_config[left][case_id].get(metric)
            right_value = per_config[right][case_id].get(metric)
            if left_value is None or right_value is None:
                continue
            difference = float(left_value) - float(right_value)
            if abs(difference) <= tolerance:
                outcome = "tie"
                ties += 1
            elif difference > 0:
                outcome = "win"
                wins += 1
            else:
                outcome = "loss"
                losses += 1
            per_query[case_id] = outcome
        output[f"{left}_vs_{right}"] = {
            "left": left,
            "right": right,
            "metric": metric,
            "wins": wins,
            "losses": losses,
            "ties": ties,
            "per_query": per_query,
        }
    return output


def _wilson_interval(successes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    if total == 0:
        return 0.0, 0.0
    proportion = successes / total
    denominator = 1.0 + z * z / total
    center = (proportion + z * z / (2.0 * total)) / denominator
    margin = z * math.sqrt(proportion * (1.0 - proportion) / total + z * z / (4.0 * total * total)) / denominator
    return max(0.0, center - margin), min(1.0, center + margin)


def fixed_threshold_false_merges(
    pair_scores: Mapping[str, float],
    negative_pair_ids: set[str],
    *,
    thresholds: Sequence[float] = PAIR_THRESHOLDS,
) -> dict[str, Any]:
    output: dict[str, Any] = {}
    available = sorted(negative_pair_ids & set(pair_scores))
    for threshold in thresholds:
        false_ids = [pair_id for pair_id in available if float(pair_scores[pair_id]) >= threshold]
        low, high = _wilson_interval(len(false_ids), len(available))
        output[f"{threshold:g}"] = {
            "threshold": float(threshold),
            "negative_pairs": len(available),
            "false_merge_count": len(false_ids),
            "false_merge_rate": len(false_ids) / len(available) if available else None,
            "wilson_low": low if available else None,
            "wilson_high": high if available else None,
            "false_merge_pair_ids": false_ids,
        }
    return output


def support_coverage(
    ranked: Sequence[Mapping[str, Any]],
    gold_session_ids: set[str],
    *,
    ks: Sequence[int] = (5, 10),
) -> dict[str, Any]:
    output: dict[str, Any] = {"gold_sessions": len(gold_session_ids)}
    for k in ks:
        found = {
            str(session)
            for claim in ranked[:k]
            for session in claim.get("evidence_session_ids") or []
            if str(session) in gold_session_ids
        }
        output[f"coverage_at_{k}"] = len(found) / len(gold_session_ids) if gold_session_ids else None
        output[f"all_support_at_{k}"] = float(found == gold_session_ids) if gold_session_ids else None
        output[f"found_sessions_at_{k}"] = sorted(found)
    return output


def update_stale_metrics(
    ranked: Sequence[Mapping[str, Any]],
    *,
    ks: Sequence[int] = (1, 5),
) -> dict[str, Any]:
    stale_ranks = [
        rank for rank, claim in enumerate(ranked, start=1) if claim.get("update_label") == "stale-conflicting"
    ]
    current_ranks = [
        rank for rank, claim in enumerate(ranked, start=1) if claim.get("update_label") == "support-current"
    ]
    output: dict[str, Any] = {
        "stale_first_rank": stale_ranks[0] if stale_ranks else None,
        "current_first_rank": current_ranks[0] if current_ranks else None,
        "current_before_stale": bool(current_ranks and (not stale_ranks or current_ranks[0] < stale_ranks[0])),
    }
    for k in ks:
        output[f"stale_at_{k}"] = float(any(rank <= k for rank in stale_ranks))
        output[f"current_at_{k}"] = float(any(rank <= k for rank in current_ranks))
    return output


def _embedding_matrices(
    cases: Sequence[CaseCorpus],
    client: DashScopeEmbeddingClient,
    cache_dir: Path,
    *,
    use_cache: bool,
) -> tuple[
    list[Claim],
    dict[str, np.ndarray],
    dict[str, np.ndarray],
    dict[str, dict[str, Any]],
    list[str],
]:
    all_claims = [claim for case in cases for claim in case.claims]
    texts = [claim.index_text for claim in all_claims]
    questions = [case.question for case in cases]
    documents: dict[str, np.ndarray] = {}
    queries: dict[str, np.ndarray] = {}
    costs: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []

    for code in CONFIG_CODES:
        config = CONFIGS[code]
        if code == "Q1":
            dense = np.empty((len(all_claims), config.dim), dtype=np.float32)
            missing_positions: list[int] = []
            missing_texts: list[str] = []
            for position, claim in enumerate(all_claims):
                if claim.q1_dense is None:
                    missing_positions.append(position)
                    missing_texts.append(claim.index_text)
                else:
                    dense[position] = claim.q1_dense
            remote = embed_remote(
                client,
                config,
                "document",
                missing_texts,
                cache_dir=cache_dir,
                use_cache=use_cache,
            )
            for remote_index, destination in enumerate(missing_positions):
                dense[destination] = remote.dense[remote_index]
            remote.cost.db_cached_vectors += len(all_claims) - len(missing_positions)
            if missing_positions:
                active_missing = sum(all_claims[index].in_retrieval_corpus for index in missing_positions)
                if active_missing:
                    raise ValueError(f"Q1 reuse gate failed: {active_missing} active claims lacked production vectors")
                warnings.append(
                    f"Q1 had {len(missing_positions)} non-active claims without stored vectors; "
                    "remote native vectors were used only for all-status diagnostics"
                )
            document_output = EmbeddingOutput(dense=dense, sparse=None, cost=remote.cost)
        else:
            document_output = embed_remote(
                client,
                config,
                "document",
                texts,
                cache_dir=cache_dir,
                use_cache=use_cache,
            )
        query_output = embed_remote(
            client,
            config,
            "query",
            questions,
            cache_dir=cache_dir,
            use_cache=use_cache,
        )
        total_cost = Cost()
        total_cost.add(document_output.cost)
        total_cost.add(query_output.cost)
        documents[code] = _normalize_rows(document_output.dense)
        queries[code] = _normalize_rows(query_output.dense)
        costs[code] = total_cost.as_dict()
    return all_claims, documents, queries, costs, warnings


def _aggregate_per_config(per_query: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    eligible = [metrics for metrics in per_query.values() if metrics.get("eligible")]
    return {
        "queries": len(per_query),
        "eligible_queries": len(eligible),
        "ineligible_queries": len(per_query) - len(eligible),
        **{
            metric: mean(float(item[metric]) for item in eligible) if eligible else None
            for metric in (*METRIC_NAMES, "utility")
        },
    }


def _event_sequence(event_id: str) -> int:
    match = re.search(r":session:(\d+):", event_id)
    return int(match.group(1)) if match else -1


def _provisional_update_labels(
    case: CaseCorpus,
    case_claims: Sequence[Claim],
    q1_documents: np.ndarray,
    q1_query: np.ndarray,
) -> tuple[dict[str, str], dict[str, Any]]:
    gold = set(case.gold_session_ids)
    gold_claims = [claim for claim in case_claims if set(claim.evidence_session_ids) & gold]
    gold_sequences = [
        _event_sequence(event_id)
        for claim in gold_claims
        for event_id in claim.evidence_event_ids
        if _event_sequence(event_id) >= 0
    ]
    if not gold_sequences:
        return {}, {"method": "heuristic-v1", "warning": "gold event sequence unavailable"}
    latest = max(gold_sequences)

    def has_latest(claim: Claim) -> bool:
        return any(_event_sequence(event_id) == latest for event_id in claim.evidence_event_ids)

    current_positions = [
        index for index, claim in enumerate(case_claims) if claim.status == "active" and has_latest(claim)
    ]
    old_positions = [
        index
        for index, claim in enumerate(case_claims)
        if set(claim.evidence_session_ids) & gold and not has_latest(claim)
    ]
    if not current_positions or not old_positions:
        return {}, {
            "method": "heuristic-v1",
            "warning": "current or historical gold candidates unavailable",
            "latest_gold_event_sequence": latest,
        }
    query_scores = q1_documents @ q1_query
    current_positions.sort(key=lambda index: (-float(query_scores[index]), case_claims[index].claim_id))
    selected_current = current_positions[:5]
    current_slots = {
        case_claims[index].canonical_slot
        for index in selected_current
        if case_claims[index].canonical_slot not in {None, "", "custom.unknown"}
    }
    current_conflicts = {
        case_claims[index].conflict_key for index in selected_current if case_claims[index].conflict_key
    }
    current_matrix = q1_documents[selected_current]
    stale_positions: list[int] = []
    for index in old_positions:
        claim = case_claims[index]
        semantic = float(np.max(current_matrix @ q1_documents[index]))
        same_key = bool(claim.conflict_key and claim.conflict_key in current_conflicts)
        same_slot = bool(claim.canonical_slot and claim.canonical_slot in current_slots)
        linked = bool(
            claim.superseded_by_id
            or claim.supersedes_id
            or any(
                case_claims[current].supersedes_id == claim.claim_id
                or claim.superseded_by_id == case_claims[current].claim_id
                for current in selected_current
            )
        )
        if linked or same_key or same_slot or (semantic >= 0.72 and query_scores[index] >= 0.25):
            stale_positions.append(index)
    fallback = False
    if not stale_positions:
        fallback = True
        stale_positions = sorted(
            old_positions,
            key=lambda index: (-float(query_scores[index]), case_claims[index].claim_id),
        )[:3]
    labels = {case_claims[index].claim_id: "support-current" for index in selected_current}
    labels.update({case_claims[index].claim_id: "stale-conflicting" for index in stale_positions})
    return labels, {
        "method": "heuristic-v1",
        "provisional": True,
        "warning": "not manually adjudicated; inspect candidate texts before production use",
        "latest_gold_event_sequence": latest,
        "fallback_to_top_historical_query_scores": fallback,
        "support_current_ids": [case_claims[index].claim_id for index in selected_current],
        "stale_conflicting_ids": [case_claims[index].claim_id for index in stale_positions],
    }


def evaluate(
    cases: Sequence[CaseCorpus],
    client: DashScopeEmbeddingClient,
    cache_dir: Path,
    *,
    use_cache: bool,
) -> tuple[dict[str, Any], dict[str, np.ndarray], list[Claim]]:
    all_claims, documents, queries, costs, warnings = _embedding_matrices(cases, client, cache_dir, use_cache=use_cache)
    offsets: dict[str, tuple[int, int]] = {}
    position = 0
    for case in cases:
        offsets[case.case_id] = (position, position + len(case.claims))
        position += len(case.claims)

    per_config_case_metrics: dict[str, dict[str, dict[str, Any]]] = {code: {} for code in CONFIG_CODES}
    config_payloads: dict[str, Any] = {}
    for code in CONFIG_CODES:
        per_query: list[dict[str, Any]] = []
        for query_index, case in enumerate(cases):
            start, end = offsets[case.case_id]
            case_claims = all_claims[start:end]
            active_local = [index for index, claim in enumerate(case_claims) if claim.in_retrieval_corpus]
            scores = documents[code][start:end][active_local] @ queries[code][query_index]
            order = np.argsort(-scores, kind="stable")
            ranked_claims = [case_claims[active_local[int(index)]] for index in order]
            ranked_ids = [claim.claim_id for claim in ranked_claims]
            relevant = gold_claim_ids([claim.frozen_record() for claim in ranked_claims], set(case.gold_session_ids))
            metrics = compute_query_metrics(relevant, ranked_ids)
            per_config_case_metrics[code][case.case_id] = metrics
            ranked_records = []
            for rank, ordered_index in enumerate(order[:10], start=1):
                claim = case_claims[active_local[int(ordered_index)]]
                ranked_records.append(
                    {
                        "rank": rank,
                        "claim_id": claim.claim_id,
                        "text": claim.index_text,
                        "value": claim.value,
                        "status": claim.status,
                        "score": float(scores[int(ordered_index)]),
                        "relevant": claim.claim_id in relevant,
                        "evidence_event_ids": claim.evidence_event_ids,
                        "evidence_session_ids": claim.evidence_session_ids,
                    }
                )
            query_payload: dict[str, Any] = {
                "case_id": case.case_id,
                "question_type": case.question_type,
                "question": case.question,
                "gold_session_ids": case.gold_session_ids,
                "corpus_claims": len(active_local),
                "metrics": metrics,
                "top_10": ranked_records,
            }
            if "temporal" in case.question_type.casefold():
                query_payload["temporal_support"] = support_coverage(ranked_records, set(case.gold_session_ids))
            per_query.append(query_payload)
        config_payloads[code] = {
            "definition": {
                "api_kind": CONFIGS[code].api_kind,
                "dimension": CONFIGS[code].dim,
                "text_type": CONFIGS[code].use_text_type,
                "instruct": CONFIGS[code].use_instruct,
                "sparse_requested": CONFIGS[code].use_sparse,
                "ranking_component": "dense only",
            },
            "cost": costs[code],
            "metrics": _aggregate_per_config(per_config_case_metrics[code]),
            "per_query": per_query,
        }

    update_cases: dict[str, Any] = {}
    for query_index, case in enumerate(cases):
        if "knowledge-update" not in case.question_type.casefold():
            continue
        start, end = offsets[case.case_id]
        case_claims = all_claims[start:end]
        labels, label_metadata = _provisional_update_labels(
            case,
            case_claims,
            documents["Q1"][start:end],
            queries["Q1"][query_index],
        )
        by_config: dict[str, Any] = {}
        for code in CONFIG_CODES:
            scores = documents[code][start:end] @ queries[code][query_index]
            order = np.argsort(-scores, kind="stable")
            ranked = [
                {
                    "rank": rank,
                    "claim_id": case_claims[int(index)].claim_id,
                    "text": case_claims[int(index)].index_text,
                    "value": case_claims[int(index)].value,
                    "status": case_claims[int(index)].status,
                    "score": float(scores[int(index)]),
                    "update_label": labels.get(case_claims[int(index)].claim_id, "irrelevant"),
                    "evidence_session_ids": case_claims[int(index)].evidence_session_ids,
                }
                for rank, index in enumerate(order, start=1)
            ]
            by_config[code] = {
                "all_status_metrics": update_stale_metrics(ranked),
                "labeled_candidates": [item for item in ranked if item["update_label"] != "irrelevant"],
                "active_top_5": [item for item in ranked if item["status"] == "active"][:5],
            }
        update_cases[case.case_id] = {
            "label_metadata": label_metadata,
            "by_config": by_config,
        }

    report = {
        "schema_version": 1,
        "benchmark": "LongMemEval-S embedding selection",
        "status": "completed",
        "generated_at": _utc_now(),
        "method": {
            "configs": list(CONFIG_CODES),
            "retrieval": "per-case dense cosine over active claims only",
            "disabled": ["FTS", "RRF", "tag", "reranker", "Q4 sparse fusion"],
            "gold": "active claim evidence session intersects answer_session_ids",
            "metrics": list(METRIC_NAMES),
            "utility": "equal-weight mean of Recall@5/10, Hit@1/5, and MRR",
            "pairwise_tolerance": 1e-12,
            "q1_documents": "stored production native vectors; missing non-active vectors may be fetched only for diagnostics",
        },
        "case_count": len(cases),
        "claims_total": len(all_claims),
        "claims_active": sum(claim.in_retrieval_corpus for claim in all_claims),
        "warnings": warnings,
        "configs": config_payloads,
        "pairwise": {
            metric: pairwise_win_loss_tie(per_config_case_metrics, metric=metric)
            for metric in (*METRIC_NAMES, "utility")
        },
        "knowledge_update_diagnostics": update_cases,
    }
    return report, documents, all_claims


def _english_enough(text: str) -> bool:
    letters = [character for character in text if character.isalpha()]
    if len(letters) < 8:
        return False
    ascii_letters = sum(character.isascii() for character in letters)
    return ascii_letters / len(letters) >= 0.9


def mine_pair_candidates(
    cases: Sequence[CaseCorpus],
    all_claims: Sequence[Claim],
    documents: Mapping[str, np.ndarray],
    *,
    pairs_per_config_case: int,
    minimum_score: float,
) -> list[dict[str, Any]]:
    offsets: dict[str, tuple[int, int]] = {}
    position = 0
    for case in cases:
        offsets[case.case_id] = (position, position + len(case.claims))
        position += len(case.claims)
    candidates: dict[tuple[str, str, str], dict[str, Any]] = {}
    for case in cases:
        start, end = offsets[case.case_id]
        case_claims = all_claims[start:end]
        active = [index for index, claim in enumerate(case_claims) if claim.in_retrieval_corpus]
        if len(active) < 2:
            continue
        upper = np.triu_indices(len(active), 1)
        for code in CONFIG_CODES:
            matrix = documents[code][start:end][active]
            similarities = matrix @ matrix.T
            pair_scores = similarities[upper]
            order = np.argsort(-pair_scores, kind="stable")[:pairs_per_config_case]
            for pair_position in order:
                score = float(pair_scores[int(pair_position)])
                if score < minimum_score:
                    continue
                left = case_claims[active[int(upper[0][int(pair_position)])]]
                right = case_claims[active[int(upper[1][int(pair_position)])]]
                if not (_english_enough(left.value) and _english_enough(right.value)):
                    continue
                left, right = sorted((left, right), key=lambda claim: claim.claim_id)
                key = (case.case_id, left.claim_id, right.claim_id)
                if key not in candidates:
                    candidates[key] = {
                        "pair_id": hashlib.sha256("|".join(key).encode("utf-8")).hexdigest()[:16],
                        "case_id": case.case_id,
                        "left": {
                            "claim_id": left.claim_id,
                            "subject_entity_id": left.subject_entity_id,
                            "predicate": left.predicate,
                            "canonical_slot": left.canonical_slot,
                            "value": left.value,
                        },
                        "right": {
                            "claim_id": right.claim_id,
                            "subject_entity_id": right.subject_entity_id,
                            "predicate": right.predicate,
                            "canonical_slot": right.canonical_slot,
                            "value": right.value,
                        },
                        "scores": {},
                        "selected_by": [],
                    }
                candidates[key]["selected_by"].append(code)
    for candidate in candidates.values():
        case_id = str(candidate["case_id"])
        start, _ = offsets[case_id]
        case_claims = all_claims[offsets[case_id][0] : offsets[case_id][1]]
        by_id = {claim.claim_id: index for index, claim in enumerate(case_claims)}
        left_index = start + by_id[str(candidate["left"]["claim_id"])]
        right_index = start + by_id[str(candidate["right"]["claim_id"])]
        candidate["scores"] = {
            code: float(documents[code][left_index] @ documents[code][right_index]) for code in CONFIG_CODES
        }
        candidate["selected_by"] = sorted(set(candidate["selected_by"]))
        candidate["max_score"] = max(candidate["scores"].values())
    return sorted(
        candidates.values(),
        key=lambda item: (-float(item["max_score"]), str(item["pair_id"])),
    )


def _extract_json_object(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    try:
        value = json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start < 0 or end <= start:
            raise
        value = json.loads(stripped[start : end + 1])
    if not isinstance(value, dict):
        raise ValueError("judge response is not a JSON object")
    return value


def _judge_pair_batch(
    client: httpx.Client,
    *,
    base_url: str,
    model: str,
    batch: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    compact = [
        {
            "pair_id": item["pair_id"],
            "left": item["left"],
            "right": item["right"],
        }
        for item in batch
    ]
    system = (
        "You label English memory-claim pairs for deduplication. Label equivalent only when both claims "
        "state the same atomic fact and merging them loses no distinction. Label non_equivalent when "
        "they are compatible/complementary facts, different attributes, historical versus current values, "
        "conflicting values, or unrelated. Be conservative: uncertain means non_equivalent. Return JSON only."
    )
    user = (
        'Judge every pair. Return {"judgments":[{"pair_id":str,'
        '"label":"equivalent"|"non_equivalent","rationale":str,'
        '"confidence":number}]}. Pairs:\n' + json.dumps(compact, ensure_ascii=False)
    )
    response: httpx.Response | None = None
    for attempt in range(1, 4):
        try:
            response = client.post(
                f"{base_url.rstrip('/')}/chat/completions",
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "temperature": 0,
                    "response_format": {"type": "json_object"},
                },
            )
            response.raise_for_status()
            break
        except (httpx.HTTPError, httpx.TimeoutException):
            if attempt == 3:
                raise
            time.sleep(2 ** (attempt - 1))
    if response is None:
        raise RuntimeError("pair judge request produced no response")
    payload = response.json()
    content = payload["choices"][0]["message"]["content"]
    parsed = _extract_json_object(str(content))
    judgments = parsed.get("judgments")
    if not isinstance(judgments, list):
        raise ValueError("pair judge response lacks judgments array")
    by_id = {str(item["pair_id"]): item for item in judgments if isinstance(item, dict)}
    expected = {str(item["pair_id"]) for item in batch}
    if set(by_id) != expected:
        raise ValueError(
            f"pair judge IDs mismatch: missing={sorted(expected - set(by_id))} "
            f"extra={sorted(set(by_id) - expected)}"
        )
    output = []
    for pair_id in [str(item["pair_id"]) for item in batch]:
        judgment = by_id[pair_id]
        label = str(judgment.get("label") or "")
        if label not in {"equivalent", "non_equivalent"}:
            raise ValueError(f"invalid pair label for {pair_id}: {label}")
        output.append(
            {
                "pair_id": pair_id,
                "label": label,
                "rationale": str(judgment.get("rationale") or ""),
                "confidence": float(judgment.get("confidence") or 0.0),
            }
        )
    return output


def adjudicate_negative_pairs(
    candidates: Sequence[Mapping[str, Any]],
    *,
    api_key: str,
    base_url: str,
    model: str,
    minimum_negatives: int,
    maximum_candidates: int,
    batch_size: int,
) -> list[dict[str, Any]]:
    selected = list(candidates[:maximum_candidates])
    rows: list[dict[str, Any]] = []
    negatives = 0
    with httpx.Client(
        timeout=httpx.Timeout(180.0, connect=15.0),
        trust_env=False,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    ) as client:
        for start in range(0, len(selected), batch_size):
            batch = selected[start : start + batch_size]
            judgments = _judge_pair_batch(client, base_url=base_url, model=model, batch=batch)
            candidate_by_id = {str(item["pair_id"]): item for item in batch}
            for judgment in judgments:
                candidate = candidate_by_id[str(judgment["pair_id"])]
                row = {
                    **judgment,
                    "case_id": candidate["case_id"],
                    "left": candidate["left"],
                    "right": candidate["right"],
                    "scores": candidate["scores"],
                    "selected_by": candidate["selected_by"],
                    "label_method": f"model-assisted:{model}",
                    "provisional": True,
                }
                rows.append(row)
                negatives += judgment["label"] == "non_equivalent"
            print(
                f"pair labels={len(rows)} negatives={negatives}/{minimum_negatives}",
                file=sys.stderr,
                flush=True,
            )
            if negatives >= minimum_negatives:
                break
    if negatives < minimum_negatives:
        raise ValueError(f"only {negatives} negative pairs were adjudicated; required {minimum_negatives}")
    negative_rows = [row for row in rows if row["label"] == "non_equivalent"]
    return negative_rows[:minimum_negatives]


def build_pair_report(
    negative_rows: Sequence[Mapping[str, Any]],
    *,
    candidate_count: int,
) -> dict[str, Any]:
    negative_ids = {str(row["pair_id"]) for row in negative_rows}
    configs: dict[str, Any] = {}
    for code in CONFIG_CODES:
        scores = {str(row["pair_id"]): float(row["scores"][code]) for row in negative_rows}
        configs[code] = fixed_threshold_false_merges(scores, negative_ids)
    return {
        "schema_version": 1,
        "status": "completed",
        "generated_at": _utc_now(),
        "method": {
            "candidate_source": "union of per-case top dense-similarity pairs from Q0-Q4",
            "language": "English natural claim values",
            "negative_definition": "not safe to merge as one atomic fact",
            "label_method": "model-assisted provisional adjudication",
            "warning": "not human gold; manually audit before using absolute false-merge rates",
            "thresholds": list(PAIR_THRESHOLDS),
            "threshold_comparison": "cosine >= threshold",
        },
        "candidate_count": candidate_count,
        "negative_pairs": len(negative_rows),
        "negative_pair_ids": sorted(negative_ids),
        "configs": configs,
    }


def make_hard_dataset(source: Path, output: Path) -> dict[str, Any]:
    from run_longmemeval_benchmark import iter_case_records

    wanted = set(HARD_CASE_IDS)
    found: dict[str, dict[str, Any]] = {}
    for record in iter_case_records(source):
        case_id = str(record.get("question_id") or record.get("case_id") or record.get("id") or "")
        if case_id in wanted:
            found[case_id] = record
            if len(found) == len(wanted):
                break
    missing = wanted - set(found)
    if missing:
        raise ValueError(f"hard dataset source is missing case IDs: {sorted(missing)}")
    ordered = [found[case_id] for case_id in HARD_CASE_IDS]
    _write_json(output, ordered)
    return {
        "output": str(output),
        "case_ids": [str(record["question_id"]) for record in ordered],
        "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "bytes": output.stat().st_size,
    }


def _resolve_report_paths(arguments: argparse.Namespace) -> list[Path]:
    if arguments.reports:
        return [Path(path) for path in arguments.reports]
    if arguments.phase == "canary":
        return sorted((ROOT / "evaluation" / "results").glob(DEFAULT_CANARY_REPORT_GLOB))
    if arguments.report:
        return [arguments.report]
    raise ValueError("hard evaluation requires --report")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("canary", "hard", "make-hard-dataset"), required=True)
    parser.add_argument("--reports", nargs="*")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--corpus-output", type=Path)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--source-dataset", type=Path, default=DEFAULT_SOURCE_DATASET)
    parser.add_argument("--hard-dataset-output", type=Path)
    parser.add_argument("--pair-candidates-output", type=Path)
    parser.add_argument("--pair-gold-output", type=Path)
    parser.add_argument("--pair-output", type=Path)
    parser.add_argument("--label-pairs", action="store_true")
    parser.add_argument("--pairs-per-config-case", type=int, default=80)
    parser.add_argument("--pair-minimum-score", type=float, default=0.82)
    parser.add_argument("--minimum-negative-pairs", type=int, default=100)
    parser.add_argument("--maximum-judge-candidates", type=int, default=500)
    parser.add_argument("--judge-batch-size", type=int, default=10)
    parser.add_argument("--judge-model", default="qwen3.7-plus")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parse_args(argv)
    if arguments.phase == "make-hard-dataset":
        if arguments.hard_dataset_output is None:
            raise ValueError("make-hard-dataset requires --hard-dataset-output")
        payload = make_hard_dataset(arguments.source_dataset, arguments.hard_dataset_output)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if arguments.output is None or arguments.corpus_output is None:
        raise ValueError("evaluation requires --output and --corpus-output")
    report_paths = _resolve_report_paths(arguments)
    cases = load_cases(report_paths)
    if arguments.phase == "canary" and len(cases) != 15:
        raise ValueError(f"canary phase requires 15 cases, found {len(cases)}")
    if arguments.phase == "hard":
        actual = {case.case_id for case in cases}
        if actual != set(HARD_CASE_IDS):
            raise ValueError(
                f"hard phase case IDs mismatch: missing={sorted(set(HARD_CASE_IDS) - actual)} "
                f"extra={sorted(actual - set(HARD_CASE_IDS))}"
            )
    frozen = freeze_corpus(cases, arguments.corpus_output)
    api_key = _load_env_value(ROOT / ".env", "EMBEDDING_API_KEY")
    if not api_key:
        raise ValueError("EMBEDDING_API_KEY is required")
    embedding_client = DashScopeEmbeddingClient(api_key=api_key)
    try:
        report, documents, all_claims = evaluate(
            cases,
            embedding_client,
            arguments.cache_dir,
            use_cache=not arguments.no_cache,
        )
    finally:
        embedding_client.close()
    report["corpus"] = {
        "path": str(arguments.corpus_output),
        "fingerprint": frozen["fingerprint"],
        "case_count": frozen["case_count"],
        "claims_total": frozen["claims_total"],
        "claims_active": frozen["claims_active"],
    }
    _write_json(arguments.output, report)

    pair_requested = any(
        path is not None
        for path in (
            arguments.pair_candidates_output,
            arguments.pair_gold_output,
            arguments.pair_output,
        )
    )
    if pair_requested:
        if arguments.phase != "canary":
            raise ValueError("pair stress evaluation is supported only in canary phase")
        if arguments.pair_candidates_output is None:
            raise ValueError("pair evaluation requires --pair-candidates-output")
        candidates = mine_pair_candidates(
            cases,
            all_claims,
            documents,
            pairs_per_config_case=arguments.pairs_per_config_case,
            minimum_score=arguments.pair_minimum_score,
        )
        _write_json(
            arguments.pair_candidates_output,
            {
                "schema_version": 1,
                "generated_at": _utc_now(),
                "minimum_score": arguments.pair_minimum_score,
                "pairs_per_config_case": arguments.pairs_per_config_case,
                "candidate_count": len(candidates),
                "candidates": candidates,
            },
        )
        if arguments.label_pairs:
            if arguments.pair_gold_output is None or arguments.pair_output is None:
                raise ValueError("--label-pairs requires --pair-gold-output and --pair-output")
            llm_api_key = _load_env_value(ROOT / ".env", "LLM_API_KEY")
            if not llm_api_key:
                raise ValueError("LLM_API_KEY is required to label pairs")
            llm_base_url = resolve_llm_base_url(ROOT / ".env")
            negative_rows = adjudicate_negative_pairs(
                candidates,
                api_key=llm_api_key,
                base_url=llm_base_url,
                model=arguments.judge_model,
                minimum_negatives=arguments.minimum_negative_pairs,
                maximum_candidates=arguments.maximum_judge_candidates,
                batch_size=arguments.judge_batch_size,
            )
            _write_jsonl(arguments.pair_gold_output, negative_rows)
            _write_json(
                arguments.pair_output,
                build_pair_report(negative_rows, candidate_count=len(candidates)),
            )

    print(
        f"completed phase={arguments.phase} cases={len(cases)} active_claims={frozen['claims_active']} "
        f"output={arguments.output}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
