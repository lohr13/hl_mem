#!/usr/bin/env python
"""Offline scorer for completed C-series raw outputs.

This process is the only C-series component allowed to load gold/rubrics.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import importlib
import json
import re
import sys
import unicodedata
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from statistics import fmean, pstdev
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hl_mem.evaluation.c_series import ARM_IDS  # noqa: E402

_ENTITY_SCORER: Any = importlib.import_module("tests.eval.chinese_e2e")
_MEMDAILY_SCORER: Any = importlib.import_module("evaluation.tools.run_memdaily_benchmark")

_LEAK_KEYS = frozenset(
    {
        "gold",
        "answer_entities",
        "role_action_object",
        "forbidden_entities",
        "forbidden_assertions",
        "accepted_rubrics",
        "rubrics",
        "verdict",
        "reference_answer",
        "answer",
        "answers",
        "scorer_verdict",
    }
)


def _nfc(value: object) -> str:
    return unicodedata.normalize("NFC", str(value))


def audit_leakage(payload: Any) -> list[str]:
    """返回 prompt/input/raw 结构中的评分字段路径。"""
    hits: list[str] = []

    def visit(value: Any, path: str) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                child = f"{path}.{key}"
                normalized_key = str(key).casefold()
                if (
                    normalized_key in _LEAK_KEYS
                    or any(token in normalized_key for token in ("gold", "forbidden", "rubric"))
                    or normalized_key.endswith(("_gold", "_verdict", "_answer_ref"))
                ):
                    hits.append(child)
                visit(item, child)
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            for index, item in enumerate(value):
                visit(item, f"{path}[{index}]")

    visit(payload, "$")
    return hits


def _gold(raw: Mapping[str, Any]) -> Any:
    return _ENTITY_SCORER.AnswerEntityGold(
        answerability=str(raw["answerability"]),
        answer_entities=(
            tuple(_nfc(item) for item in raw["answer_entities"]) if raw.get("answer_entities") is not None else None
        ),
        role_action_object=tuple(
            _ENTITY_SCORER.RoleActionObject(_nfc(item["role"]), _nfc(item["action"]), _nfc(item["object"]))
            for item in raw.get("role_action_object") or []
        ),
        forbidden_entities=tuple(_nfc(item) for item in raw.get("forbidden_entities") or []),
        forbidden_assertions=tuple(_nfc(item) for item in raw.get("forbidden_assertions") or []),
    )


def _semantic_atoms(value: object) -> tuple[str, ...]:
    normalized = _nfc(value).strip()
    atoms = tuple(item.strip() for item in re.split(r"[、,，]|(?:和|与|及|并)", normalized) if item.strip())
    return atoms or ((normalized,) if normalized else ())


def _rao_answer_match(answer: str, gold: Any) -> bool:
    normalized = _nfc(answer)
    answer_entities = set(gold.answer_entities or ())
    for item in gold.role_action_object:
        required_atoms = [
            atom for atom in (*_semantic_atoms(item.role), *_semantic_atoms(item.object)) if atom in answer_entities
        ]
        if item.action not in normalized or not all(atom in normalized for atom in required_atoms):
            return False
    return True


def _rao_packet_match(packet: Sequence[Mapping[str, Any]], gold: Any) -> bool:
    for expected in gold.role_action_object:
        matching = [item for item in packet if _nfc(item.get("action") or "") == expected.action]
        if not matching:
            return False
        roles = "|".join(_nfc(item.get("role") or "") for item in matching)
        objects = "|".join(_nfc(item.get("object") or "") for item in matching)
        entities = "|".join(_nfc(entity) for item in matching for entity in item.get("entities") or [])
        if not all(atom in roles or atom in entities for atom in _semantic_atoms(expected.role)):
            return False
        if not all(atom in objects or atom in entities for atom in _semantic_atoms(expected.object)):
            return False
    return True


def _role_modality_confusion(answer: str, packet: Sequence[Mapping[str, Any]]) -> bool:
    normalized = _nfc(answer)
    actions_by_object: dict[str, set[str]] = {}
    for item in packet:
        obj = _nfc(item.get("object") or "")
        action = _nfc(item.get("action") or "")
        if obj and action:
            actions_by_object.setdefault(obj, set()).add(action)
    for obj, actions in actions_by_object.items():
        if obj not in normalized:
            continue
        if any("推荐" in action for action in actions) and any(
            word in normalized for word in ("采用", "购买", "执行", "报名")
        ):
            if not any(any(word in action for word in ("采用", "购买", "执行", "报名")) for action in actions):
                return True
        if any("报道" in action for action in actions) and "拥有" in normalized:
            if not any("拥有" in action for action in actions):
                return True
    return False


def _identity(value: object) -> str:
    return str(Path(str(value)).resolve()).casefold()


def _after(value: object, boundary: object) -> bool:
    if not value or not boundary:
        return False
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")) > datetime.fromisoformat(
        str(boundary).replace("Z", "+00:00")
    )


def audit_evidence_provenance(
    packet: Sequence[Mapping[str, Any]],
    case: Mapping[str, Any],
    prereg: Mapping[str, Any],
) -> dict[str, list[str]]:
    """按协议 10.2 审计模态、namespace、双时间与冻结 cache/corpus 归属。"""
    modality: list[str] = []
    provenance: list[str] = []
    allowed = {_nfc(item).casefold() for item in case.get("allowed_modalities") or []}
    expected_namespace = str(case.get("namespace") or "")
    expected_cache_identity = _identity(case.get("source_cache_identity") or "")
    expected_cache_sha = str(case.get("source_cache_sha256") or "")
    frozen_caches = {_identity(path): str(digest) for path, digest in (prereg.get("cache_files") or {}).items()}
    expected_corpora = {
        str(item["id"]): str(item["sha256"])
        for item in case.get("source_corpora") or []
        if isinstance(item, Mapping) and item.get("id") and item.get("sha256")
    }
    frozen_corpora = {str(key): str(value) for key, value in (prereg.get("corpora") or {}).items()}
    for item_index, item in enumerate(packet):
        evidence = item.get("evidence_provenance")
        if not isinstance(evidence, Sequence) or isinstance(evidence, (str, bytes)) or not evidence:
            provenance.append(f"packet[{item_index}].evidence_provenance:missing")
            continue
        for evidence_index, raw in enumerate(evidence):
            path = f"packet[{item_index}].evidence_provenance[{evidence_index}]"
            if not isinstance(raw, Mapping):
                provenance.append(f"{path}:not_object")
                continue
            evidence_modality = _nfc(raw.get("modality") or "").casefold()
            if not evidence_modality or evidence_modality not in allowed:
                modality.append(f"{path}.modality:{evidence_modality or 'missing'}")
            if not raw.get("event_id") or not raw.get("content_kind"):
                provenance.append(f"{path}:missing_event_or_content_kind")
            if str(raw.get("namespace") or "") != expected_namespace:
                provenance.append(f"{path}.namespace")
            try:
                if _after(raw.get("occurred_at"), case.get("question_at")):
                    provenance.append(f"{path}.occurred_at")
                if _after(raw.get("recorded_at"), case.get("known_as_of")):
                    provenance.append(f"{path}.recorded_at")
            except (TypeError, ValueError):
                provenance.append(f"{path}:invalid_time")
            cache_identity = _identity(raw.get("source_cache_identity") or "")
            cache_sha = str(raw.get("source_cache_sha256") or "")
            if cache_identity != expected_cache_identity or cache_sha != expected_cache_sha:
                provenance.append(f"{path}.source_cache_case_mismatch")
            if frozen_caches.get(cache_identity) != cache_sha:
                provenance.append(f"{path}.source_cache_not_frozen")
            evidence_corpora = {
                str(entry["id"]): str(entry["sha256"])
                for entry in raw.get("source_corpora") or []
                if isinstance(entry, Mapping) and entry.get("id") and entry.get("sha256")
            }
            if evidence_corpora != expected_corpora:
                provenance.append(f"{path}.source_corpora_case_mismatch")
            if any(frozen_corpora.get(corpus_id) != digest for corpus_id, digest in evidence_corpora.items()):
                provenance.append(f"{path}.source_corpus_not_frozen")
    return {"modality": modality, "provenance": provenance}


def score_visible_case(
    answer_text: str,
    packet: Sequence[Mapping[str, Any]],
    raw_gold: Mapping[str, Any],
) -> dict[str, Any]:
    """用冻结 entity scorer + exact-NFC RAO/安全规则判 visible hard case。"""
    gold = _gold(raw_gold)
    entity = _ENTITY_SCORER.score_answer_entity_packet(packet, gold, answer_text=answer_text)
    normalized_answer = _nfc(answer_text)
    no_answer = gold.answerability == "no_answer"
    if no_answer:
        answer_correct = "信息不足" in normalized_answer and not entity["negative_violation"]
    else:
        answer_entities_in_answer = all(
            answer_entity in normalized_answer for answer_entity in gold.answer_entities or ()
        )
        answer_correct = (
            answer_entities_in_answer and _rao_answer_match(normalized_answer, gold) and _rao_packet_match(packet, gold)
        )
    role_confusion = _role_modality_confusion(normalized_answer, packet)
    negative_violation = bool(entity["negative_violation"] or role_confusion)
    return {
        **entity,
        "answer_correct": bool(answer_correct and not negative_violation),
        "negative_violation": negative_violation,
        "rao_match": _rao_answer_match(normalized_answer, gold),
        "packet_rao_match": _rao_packet_match(packet, gold),
        "role_modality_confusion": role_confusion,
        "modality_violation": False,
        "provenance_violation": False,
        "leakage_violation": False,
    }


def aggregate_visible(scores: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    result = _ENTITY_SCORER.aggregate_answer_entity_scores(scores)
    result.update(
        {
            "accuracy": sum(bool(item["answer_correct"]) for item in scores) / len(scores),
            "modality_violations": sum(bool(item["modality_violation"]) for item in scores),
            "provenance_violations": sum(bool(item["provenance_violation"]) for item in scores),
            "leakage_violations": sum(bool(item["leakage_violation"]) for item in scores),
        }
    )
    return dict(result)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw", type=Path, required=True)
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--sample", type=Path, required=True)
    parser.add_argument("--design", type=Path, required=True)
    parser.add_argument("--prereg", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--markdown", type=Path, required=True)
    return parser.parse_args()


def _read_complete_jsonl(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    rows: list[dict[str, Any]] = []
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            if index == len(lines) - 1 and not text.endswith(("\n", "\r")):
                break
            raise
        if item.get("status") == "complete":
            rows.append(item)
    return rows


def _metadata(sample_path: Path, design_path: Path) -> dict[str, dict[str, Any]]:
    sampled = _ENTITY_SCORER.load_sampled_inputs(_ENTITY_SCORER.load_sample_manifest(sample_path))
    result: dict[str, dict[str, Any]] = {}
    for bundle in sampled.perltqa_bundles:
        for question in bundle.questions:
            result[question.case_id] = {
                "dataset": "chinese_e2e",
                "category": f"perltqa_{question.category}",
                "gold": dataclasses.asdict(question.answer_entity_gold),
                "anchors": question.answer_anchors,
                "accepted_rubrics": question.accepted_rubrics,
                "memdaily": None,
            }
    manifest = _ENTITY_SCORER.load_sample_manifest(sample_path)
    for trajectory in sampled.memdaily_trajectories:
        result[trajectory.case_id] = {
            "dataset": "chinese_e2e",
            "category": f"memdaily_{trajectory.qtype}",
            "gold": dataclasses.asdict(manifest.answer_entity_gold_by_case_id[trajectory.case_id]),
            "anchors": (trajectory.answer,),
            "accepted_rubrics": (),
            "memdaily": trajectory,
        }
    for case in json.loads(design_path.read_text(encoding="utf-8"))["cases"]:
        result[str(case["case_id"])] = {
            "dataset": "relation_design_dev",
            "category": case["category"],
            "gold": case["gold"],
            "anchors": tuple(case["gold"]["answer_entities"] or ("信息不足",)),
            "accepted_rubrics": (),
            "memdaily": None,
        }
    return result


def _score_e2e(answer: str, packet: Sequence[Mapping[str, Any]], info: Mapping[str, Any]) -> dict[str, Any]:
    gold = _gold(info["gold"])
    entity = _ENTITY_SCORER.score_answer_entity_packet(packet, gold, answer_text=answer)
    trajectory = info.get("memdaily")
    if trajectory is None:
        accuracy = _ENTITY_SCORER.score_answer(answer, info["anchors"], info["accepted_rubrics"])["answer_correct"]
    else:
        qa = _MEMDAILY_SCORER.score_qa_accuracy(
            answer,
            trajectory.answer,
            choices=trajectory.choices or None,
            ground_truth_choice=trajectory.ground_truth_choice,
        )
        accuracy = qa["choice_correct"] if trajectory.ground_truth_choice else qa["exact_match"]
    role_confusion = _role_modality_confusion(answer, packet)
    negative_violation = bool(entity["negative_violation"] or role_confusion)
    return {
        **entity,
        "answer_correct": bool(accuracy and not negative_violation),
        "negative_violation": negative_violation,
        "rao_match": None,
        "role_modality_confusion": role_confusion,
        "modality_violation": False,
        "provenance_violation": False,
        "leakage_violation": False,
    }


def _majority(values: Sequence[bool]) -> bool:
    return sum(values) >= 2


def _percentile(values: Sequence[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * fraction))]


def _report(rows: Sequence[Mapping[str, Any]], metadata: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    by_arm: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        by_arm[str(row["arm_id"])].append(row)
    per_arm: dict[str, Any] = {}
    majority: dict[tuple[str, str], bool] = {}
    for arm, arm_rows in by_arm.items():
        by_case: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for row in arm_rows:
            by_case[str(row["case_id"])].append(row)
        for case_id, case_rows in by_case.items():
            majority[(arm, case_id)] = _majority([bool(item["score"]["answer_correct"]) for item in case_rows])
        scores = [item["score"] for item in arm_rows]
        hard_scores = [
            item["score"] for item in arm_rows if metadata[str(item["case_id"])]["dataset"] == "relation_design_dev"
        ]
        no_answer = [
            item["score"] for item in arm_rows if metadata[str(item["case_id"])]["gold"]["answerability"] == "no_answer"
        ]
        entity = _ENTITY_SCORER.aggregate_answer_entity_scores(scores)
        hard_entity = _ENTITY_SCORER.aggregate_answer_entity_scores(hard_scores)
        recall_latencies = [float(item["recall_latency_seconds"]) for item in arm_rows]
        e2e_latencies = [float(item["e2e_latency_seconds"]) for item in arm_rows]
        per_arm[arm] = {
            "accuracy": fmean(float(item["answer_correct"]) for item in scores),
            "accuracy_repeat_stddev": pstdev(
                [
                    fmean(
                        float(item["score"]["answer_correct"])
                        for item in arm_rows
                        if int(item["repeat_index"]) == repeat
                    )
                    for repeat in range(3)
                ]
            ),
            "entity_coverage_at_5": entity["entity_coverage_at_5"],
            "hard_entity_coverage_at_5": hard_entity["entity_coverage_at_5"],
            "no_answer_accuracy": fmean(float(item["answer_correct"]) for item in no_answer),
            "forbidden_violations": sum(bool(item["negative_violation"]) for item in scores),
            "modality_violations": sum(bool(item["modality_violation"]) for item in scores),
            "provenance_violations": sum(bool(item["provenance_violation"]) for item in scores),
            "role_modality_confusions": sum(bool(item["role_modality_confusion"]) for item in scores),
            "leakage_violations": sum(bool(item["leakage_violation"]) for item in scores),
            "mean_total_tokens": fmean(float(item["usage"]["total_tokens"]) for item in arm_rows),
            "mean_packet_tokens": fmean(
                sum(int(packet_item["token_count"]) for packet_item in item["packet"]) for item in arm_rows
            ),
            "recall_latency_p50_seconds": _percentile(recall_latencies, 0.50),
            "recall_latency_p95_seconds": _percentile(recall_latencies, 0.95),
            "e2e_latency_p95_seconds": _percentile(e2e_latencies, 0.95),
            "planner_calls": sum(item.get("rescue") == "planner" for item in arm_rows),
            "planner_failures": sum(bool(item.get("planner_error")) for item in arm_rows),
            "mean_planner_tokens_per_query": fmean(float(item["planner_usage"]["total_tokens"]) for item in arm_rows),
            "raw_records": sum(
                sum(packet_item.get("kind") == "raw_event" for packet_item in item["packet"]) for item in arm_rows
            ),
            "packet_budget_violations": sum(
                len(item["packet"]) > 10
                or sum(int(packet_item["token_count"]) for packet_item in item["packet"]) > 2_000
                or sum(packet_item.get("kind") == "raw_event" for packet_item in item["packet"]) > 6
                or sum(
                    int(packet_item["token_count"])
                    for packet_item in item["packet"]
                    if packet_item.get("kind") == "raw_event"
                )
                > 800
                for item in arm_rows
            ),
        }
    hard_ids = {case_id for case_id, info in metadata.items() if info["dataset"] == "relation_design_dev"}
    c0_correct = {case_id for case_id in metadata if majority[("C0", case_id)]}
    gates: dict[str, Any] = {}
    for arm in ARM_IDS[1:]:
        arm_correct = {case_id for case_id in metadata if majority[(arm, case_id)]}
        hard_net = len((arm_correct - c0_correct) & hard_ids) - len((c0_correct - arm_correct) & hard_ids)
        regressions = len(c0_correct - arm_correct)
        per_arm[arm]["hard_relation_net_gain"] = hard_net
        per_arm[arm]["correct_to_incorrect"] = regressions
        gate = {
            "hard_relation_net_gain_ge_2": hard_net >= 2,
            "paired_zero_regression": regressions == 0,
            "entity_coverage_non_decrease": per_arm[arm]["entity_coverage_at_5"]
            >= per_arm["C0"]["entity_coverage_at_5"],
            "hard_entity_coverage_plus_005": per_arm[arm]["hard_entity_coverage_at_5"]
            >= per_arm["C0"]["hard_entity_coverage_at_5"] + 0.05,
            "no_answer_non_decrease": per_arm[arm]["no_answer_accuracy"] >= per_arm["C0"]["no_answer_accuracy"],
            "forbidden_zero": per_arm[arm]["forbidden_violations"] == 0,
            "modality_zero": per_arm[arm]["modality_violations"] == 0,
            "frozen_evidence_provenance_zero": per_arm[arm]["provenance_violations"] == 0,
            "leakage_zero": per_arm[arm]["leakage_violations"] == 0,
            "packet_budget": per_arm[arm]["packet_budget_violations"] == 0,
        }
        if arm in {"C1", "C2", "C3", "C4", "C5"}:
            gate.update(
                {
                    "no_extra_recall_llm": per_arm[arm]["planner_calls"] == 0,
                    "recall_p50_cost": per_arm[arm]["recall_latency_p50_seconds"]
                    <= per_arm["C0"]["recall_latency_p50_seconds"] * 1.15,
                    "recall_p95_cost": per_arm[arm]["recall_latency_p95_seconds"]
                    <= max(
                        per_arm["C0"]["recall_latency_p95_seconds"] + 0.150,
                        per_arm["C0"]["recall_latency_p95_seconds"] * 1.25,
                    ),
                }
            )
        if arm == "f4":
            count = len(by_arm[arm])
            gate.update(
                {
                    "trigger_rate_le_020": per_arm[arm]["planner_calls"] / count <= 0.20,
                    "planner_failure_rate_le_002": per_arm[arm]["planner_failures"]
                    / max(1, per_arm[arm]["planner_calls"])
                    <= 0.02,
                    "planner_tokens_le_292": per_arm[arm]["mean_planner_tokens_per_query"] <= 292,
                    "e2e_p95_cost": per_arm[arm]["e2e_latency_p95_seconds"]
                    <= per_arm["C0"]["e2e_latency_p95_seconds"] + 2.5,
                }
            )
        gate["passed"] = all(gate.values())
        gates[arm] = gate
    return {"per_arm": per_arm, "gates": gates}


def main() -> int:
    args = parse_args()
    inputs = json.loads(args.inputs.read_text(encoding="utf-8"))
    input_leaks = audit_leakage(inputs)
    if input_leaks:
        raise RuntimeError(f"gold leakage in live inputs: {input_leaks}")
    raw = _read_complete_jsonl(args.raw)
    prereg = json.loads(args.prereg.read_text(encoding="utf-8"))
    expected = len(inputs["cases"]) * 3 * len(ARM_IDS)
    latest = {(str(item["case_id"]), int(item["repeat_index"]), str(item["arm_id"])): item for item in raw}
    if len(latest) != expected:
        raise RuntimeError(f"raw outputs incomplete: {len(latest)}/{expected}")
    metadata = _metadata(args.sample, args.design)
    input_cases = {str(case["case_id"]): case for case in inputs["cases"]}
    scored: list[dict[str, Any]] = []
    for item in latest.values():
        info = metadata[str(item["case_id"])]
        score = (
            score_visible_case(str(item["predicted_answer"]), item["packet"], info["gold"])
            if info["dataset"] == "relation_design_dev"
            else _score_e2e(str(item["predicted_answer"]), item["packet"], info)
        )
        raw_audit = {key: value for key, value in item.items() if key != "predicted_answer"}
        leaks = audit_leakage(raw_audit)
        evidence_audit = audit_evidence_provenance(
            [*item["packet"], *(item.get("top5_seed_packet") or [])],
            input_cases[str(item["case_id"])],
            prereg,
        )
        score["modality_violation"] = bool(evidence_audit["modality"])
        score["provenance_violation"] = bool(evidence_audit["provenance"])
        score["leakage_violation"] = bool(leaks)
        scored.append(
            {
                **item,
                "score": score,
                "leakage_paths": leaks,
                "modality_paths": evidence_audit["modality"],
                "provenance_paths": evidence_audit["provenance"],
            }
        )
    aggregates = _report(scored, metadata)
    output = {
        "schema_version": 2,
        "protocol_version": prereg["protocol_version"],
        "scorer_version": "answer-entity-packet-v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "preregistration_sha256": hashlib.sha256(args.prereg.read_bytes()).hexdigest(),
        "raw_sha256": hashlib.sha256(args.raw.read_bytes()).hexdigest(),
        **aggregates,
        "scored_cases": scored,
    }
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = [
        "# C-series relation experiment",
        "",
        "| arm | accuracy | entity@5 | hard net | regressions | forbidden | modality | tokens | recall p95 | gate |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for arm in ARM_IDS:
        item = output["per_arm"][arm]
        lines.append(
            f"| {arm} | {item['accuracy']:.4f} | {item['entity_coverage_at_5']:.4f} | "
            f"{item.get('hard_relation_net_gain', 0)} | {item.get('correct_to_incorrect', 0)} | "
            f"{item['forbidden_violations']} | {item['modality_violations']} | "
            f"{item['mean_total_tokens']:.1f} | {item['recall_latency_p95_seconds']:.3f} | "
            f"{output['gates'].get(arm, {}).get('passed', 'baseline')} |"
        )
    args.markdown.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
