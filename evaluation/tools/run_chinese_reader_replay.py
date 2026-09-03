"""Validate frozen Chinese E2E reports and reconstruct reader replay cases."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, replace
from importlib import import_module
from pathlib import Path
from typing import Any, TypeAlias

# These evaluation helpers live below namespace-package roots that mypy otherwise
# discovers twice when this file is checked by path (``tools`` and
# ``evaluation.tools``). Runtime lookup preserves the existing public seam while
# keeping the task's exact standalone mypy command scoped to this module.
_chinese_e2e = import_module("tests.eval.chinese_e2e")
load_sample_manifest = _chinese_e2e.load_sample_manifest
load_sampled_inputs = _chinese_e2e.load_sampled_inputs
build_perltqa_ingest_trajectory = _chinese_e2e.build_perltqa_ingest_trajectory
build_perltqa_question_trajectory = _chinese_e2e.build_perltqa_question_trajectory

MemDailyTrajectory: TypeAlias = Any
AcceptedRubrics: TypeAlias = tuple[tuple[tuple[str, ...], ...], ...]
AnswerEntityGold: TypeAlias = Any

EXPECTED_CASE_COUNT = 40
OFFICIAL_READER_MODEL = "qwen3.7-plus"
SUPPORTED_ANSWERABILITY = frozenset({"supported", "low_confidence"})


@dataclass(frozen=True)
class SourceArm:
    label: str
    report_path: Path
    report_sha256: str
    extractor_model: str


@dataclass(frozen=True)
class ReplayCase:
    arm: SourceArm
    case_id: str
    dataset: str
    slice_name: str
    trajectory: MemDailyTrajectory
    answer_anchors: tuple[str, ...]
    accepted_rubrics: AcceptedRubrics
    answer_entity_gold: AnswerEntityGold
    retrieved: tuple[dict[str, Any], ...]
    source_case: dict[str, Any]


@dataclass(frozen=True)
class _TrajectoryCase:
    dataset: str
    slice_name: str
    trajectory: MemDailyTrajectory
    answer_anchors: tuple[str, ...]
    accepted_rubrics: AcceptedRubrics
    answer_entity_gold: AnswerEntityGold


class ReplayInputError(ValueError):
    """A frozen source report cannot be used for reader replay."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_source_case(raw_case: Mapping[object, object]) -> dict[str, Any]:
    copied = {str(key): value for key, value in raw_case.items()}
    raw_qa = copied.get("qa")
    if isinstance(raw_qa, Mapping):
        copied["qa"] = {str(key): value for key, value in raw_qa.items()}
    raw_retrieved = copied.get("retrieved")
    if isinstance(raw_retrieved, list):
        copied["retrieved"] = [dict(item) for item in raw_retrieved]
    return copied


def validate_source_report(
    report: object,
    *,
    expected_case_ids: set[str],
) -> tuple[dict[str, Any], ...]:
    """Validate one complete official source report without truncating evidence."""
    if not isinstance(report, Mapping):
        raise ReplayInputError("source report must be an object")
    if report.get("schema_version") != 3:
        raise ReplayInputError("source report schema_version must be 3")
    if report.get("status") != "completed":
        raise ReplayInputError("source report status must be completed")

    raw_cases = report.get("cases")
    if not isinstance(raw_cases, list):
        raise ReplayInputError("source report cases must be a list")

    cases: list[dict[str, Any]] = []
    case_ids: list[str] = []
    for index, raw_case in enumerate(raw_cases):
        if not isinstance(raw_case, Mapping):
            raise ReplayInputError(f"source report cases[{index}] must be an object")
        case_id = str(raw_case.get("case_id") or "").strip()
        if not case_id:
            raise ReplayInputError(f"source report cases[{index}].case_id is required")
        case_ids.append(case_id)

        error = raw_case.get("error")
        if error is not None and error != "":
            raise ReplayInputError(f"source report contains case errors: {case_id}")

        raw_qa = raw_case.get("qa")
        if not isinstance(raw_qa, Mapping):
            raise ReplayInputError(f"source report case {case_id} requires QA output")
        if raw_qa.get("model") != OFFICIAL_READER_MODEL:
            raise ReplayInputError(f"source report case {case_id} QA model must be {OFFICIAL_READER_MODEL}")
        if raw_qa.get("answerability") not in SUPPORTED_ANSWERABILITY:
            raise ReplayInputError(
                f"source report case {case_id} answerability must be supported or low_confidence"
            )

        raw_retrieved = raw_case.get("retrieved")
        if not isinstance(raw_retrieved, list):
            raise ReplayInputError(f"source report case {case_id} retrieved must be a list")
        if any(not isinstance(item, Mapping) for item in raw_retrieved):
            raise ReplayInputError(f"source report case {case_id} retrieved entries must be objects")

        cases.append(_copy_source_case(raw_case))

    counts = Counter(case_ids)
    duplicates = sorted(case_id for case_id, count in counts.items() if count > 1)
    actual_case_ids = set(case_ids)
    missing = sorted(expected_case_ids - actual_case_ids)
    unexpected = sorted(actual_case_ids - expected_case_ids)
    if duplicates or missing or unexpected:
        raise ReplayInputError(
            "source report case IDs do not match the replay sample: "
            f"duplicate={duplicates}, missing={missing}, unexpected={unexpected}"
        )
    return tuple(cases)


def _add_trajectory_case(index: dict[str, _TrajectoryCase], item: _TrajectoryCase) -> None:
    case_id = item.trajectory.case_id
    if case_id in index:
        raise ReplayInputError(f"duplicate reconstructed trajectory ID: {case_id}")
    index[case_id] = item


def build_trajectory_index(manifest_path: Path) -> dict[str, _TrajectoryCase]:
    """Rebuild the exact sampled trajectories through the hash-validating loaders."""
    manifest = load_sample_manifest(manifest_path)
    sampled = load_sampled_inputs(manifest)
    index: dict[str, _TrajectoryCase] = {}

    for bundle in sampled.perltqa_bundles:
        ingest = build_perltqa_ingest_trajectory(bundle)
        for question in bundle.questions:
            _add_trajectory_case(
                index,
                _TrajectoryCase(
                    dataset="perltqa",
                    slice_name=f"perltqa_{question.category}",
                    trajectory=build_perltqa_question_trajectory(ingest, question),
                    answer_anchors=question.answer_anchors,
                    accepted_rubrics=question.accepted_rubrics,
                    answer_entity_gold=question.answer_entity_gold,
                ),
            )

    for trajectory in sampled.memdaily_trajectories:
        answer_entity_gold = manifest.answer_entity_gold_by_case_id.get(trajectory.case_id)
        if answer_entity_gold is None:
            raise ReplayInputError(f"manifest is missing AnswerEntityGold for {trajectory.case_id}")
        _add_trajectory_case(
            index,
            _TrajectoryCase(
                dataset="memdaily",
                slice_name=f"memdaily_{trajectory.qtype}",
                trajectory=trajectory,
                answer_anchors=(),
                accepted_rubrics=(),
                answer_entity_gold=answer_entity_gold,
            ),
        )

    if len(index) != EXPECTED_CASE_COUNT:
        raise ReplayInputError(
            f"reconstructed trajectory index must contain exactly {EXPECTED_CASE_COUNT} IDs, got {len(index)}"
        )
    return index


def _load_report(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ReplayInputError(f"cannot read source report {path}: {error}") from error
    if not isinstance(raw, Mapping):
        raise ReplayInputError(f"source report must be an object: {path}")
    return {str(key): value for key, value in raw.items()}


def _extractor_model(report: Mapping[str, Any]) -> str:
    run = report.get("run")
    models = run.get("models") if isinstance(run, Mapping) else None
    extractor = models.get("extractor") if isinstance(models, Mapping) else None
    if not isinstance(extractor, str) or not extractor.strip():
        raise ReplayInputError("source report run.models.extractor is required")
    return extractor


def load_replay_cases(
    manifest_path: Path,
    source_paths: Mapping[str, Path],
) -> dict[str, tuple[ReplayCase, ...]]:
    """Join validated source reports to reconstructed cases in report order."""
    trajectories = build_trajectory_index(manifest_path)
    expected_case_ids = set(trajectories)
    loaded: dict[str, tuple[ReplayCase, ...]] = {}

    for label, report_path in source_paths.items():
        report = _load_report(report_path)
        source_cases = validate_source_report(report, expected_case_ids=expected_case_ids)
        arm = SourceArm(
            label=label,
            report_path=report_path,
            report_sha256=sha256_file(report_path),
            extractor_model=_extractor_model(report),
        )
        replay_cases: list[ReplayCase] = []
        for source_case in source_cases:
            case_id = str(source_case["case_id"])
            reconstructed = trajectories[case_id]
            trajectory = reconstructed.trajectory
            if source_case.get("question") != trajectory.question:
                raise ReplayInputError(f"source report question does not match reconstructed trajectory: {case_id}")
            qa = source_case["qa"]
            source_answer = source_case.get("answer")
            qa_gold_answer = qa.get("gold_answer") if isinstance(qa, Mapping) else None
            if source_answer != trajectory.answer or qa_gold_answer != trajectory.answer:
                raise ReplayInputError(f"source report gold answer does not match reconstructed trajectory: {case_id}")

            retrieved = tuple(dict(item) for item in source_case["retrieved"])
            safe_source_case = {key: value for key, value in source_case.items() if key != "messages"}
            replay_cases.append(
                ReplayCase(
                    arm=arm,
                    case_id=case_id,
                    dataset=reconstructed.dataset,
                    slice_name=reconstructed.slice_name,
                    trajectory=replace(trajectory, messages=()),
                    answer_anchors=reconstructed.answer_anchors,
                    accepted_rubrics=reconstructed.accepted_rubrics,
                    answer_entity_gold=reconstructed.answer_entity_gold,
                    retrieved=retrieved,
                    source_case=safe_source_case,
                )
            )
        loaded[label] = tuple(replay_cases)
    return loaded
