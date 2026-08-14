"""Fixed-sample Chinese extraction -> recall -> QA evaluation helpers."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, fields, replace
from datetime import datetime, timezone
from pathlib import Path
from statistics import fmean
from typing import Any

from evaluation.tools import run_memdaily_benchmark as memdaily_benchmark
from evaluation.tools.run_memdaily_benchmark import MemDailyMessage, MemDailyTrajectory, load_trajectories
from hl_mem import __version__
from hl_mem.components import initialize_process, make_embedder, make_reranker
from hl_mem.config_loader import load_settings
from hl_mem.evaluation.perltqa import CATEGORIES, PerLTQAAdapter, PerLTQAQuestion
from hl_mem.settings import Settings
from hl_mem.storage.database import Database

AnswerRubric = tuple[tuple[str, ...], ...]
AcceptedRubrics = tuple[AnswerRubric, ...]

SCORER_VERSION = "deterministic-rubric-v1"
OVERALL_QA_ACCURACY_MINIMUM = 0.90

# ── Monkey-patch QA to enable thinking for multi-hop reasoning ──────────────
# The upstream _qa_dashscope_chat sends a plain completion without thinking.
# PerLTQA social_relationship and dialogues questions need reasoning steps.


def _qa_dashscope_with_thinking(
    api_key: str,
    base_url: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
) -> tuple[str, int]:
    """QA with thinking enabled for multi-hop reasoning (budget 2048, body 512)."""
    import httpx

    url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.1,
        "max_tokens": 512,
        "enable_thinking": True,
        "thinking_budget": 2048,
    }
    with httpx.Client(timeout=120.0) as client:
        resp = client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    answer_text = ""
    choices = data.get("choices") or []
    if choices:
        answer_text = (choices[0].get("message") or {}).get("content") or ""
    return answer_text, (data.get("usage") or {}).get("total_tokens", 0)


# Replace _qa_dashscope_chat *inside* run_memdaily_benchmark's namespace so
# that _run_qa (which calls it by bare name) picks up thinking.
memdaily_benchmark.__dict__["_qa_dashscope_chat"] = _qa_dashscope_with_thinking


class SampleManifestError(ValueError):
    """The fixed paid sample is malformed or cannot be resolved."""


class SourceHashMismatch(SampleManifestError):
    """An upstream private dataset changed after sample selection."""


@dataclass(frozen=True)
class DatasetThresholds:
    qa_accuracy: float
    qa_f1: float
    recall_at_5: float
    mrr: float
    extraction_coverage: float


DEFAULT_THRESHOLDS: dict[str, DatasetThresholds] = {
    "perltqa": DatasetThresholds(0.85, 0.28, 0.78, 0.65, 0.75),
    "memdaily": DatasetThresholds(0.75, 0.75, 0.75, 0.70, 0.75),
}

SLICE_MINIMUMS: dict[str, float] = {
    "recall_at_5": 0.50,
    "extraction_coverage": 0.50,
}


@dataclass(frozen=True)
class GateFailure:
    dataset: str
    metric: str
    actual: float
    minimum: float


@dataclass(frozen=True)
class E2EQuestion:
    case_id: str
    category: str
    question: str
    answer: str
    answer_anchors: tuple[str, ...]
    accepted_rubrics: AcceptedRubrics
    namespace: str
    gold_event_ids: tuple[str, ...]


@dataclass(frozen=True)
class PerLTQABundle:
    name: str
    namespace: str
    evaluation_as_of: str
    messages: tuple[MemDailyMessage, ...]
    questions: tuple[E2EQuestion, ...]


@dataclass(frozen=True)
class SampledInputs:
    perltqa_bundles: tuple[PerLTQABundle, ...]
    memdaily_trajectories: tuple[MemDailyTrajectory, ...]


@dataclass(frozen=True)
class E2ESampleManifest:
    schema_version: int
    sample_id: str
    sources: dict[str, dict[str, str]]
    perltqa: dict[str, Any]
    memdaily: dict[str, Any]
    accepted_rubrics_by_question_hash: dict[str, AcceptedRubrics]

    @property
    def perltqa_question_count(self) -> int:
        return sum(
            len(category["question_sha256"])
            for persona in self.perltqa["personas"]
            for category in persona["categories"].values()
        )

    @property
    def memdaily_question_count(self) -> int:
        return len(self.memdaily["case_ids"])

    @property
    def slice_counts(self) -> dict[str, int]:
        counts: Counter[str] = Counter()
        for persona in self.perltqa["personas"]:
            for category, specification in persona["categories"].items():
                counts[f"perltqa_{category}"] += len(specification["question_sha256"])
        for case_id in self.memdaily["case_ids"]:
            parts = case_id.split(":")
            if len(parts) != 4 or parts[0] != "memdaily":
                raise SampleManifestError(f"invalid MemDaily case_id: {case_id!r}")
            counts[f"memdaily_{parts[1]}"] += 1
        return dict(sorted(counts.items()))

    @property
    def expected_case_ids_by_slice(self) -> dict[str, tuple[str, ...]]:
        cases: dict[str, list[str]] = {}
        for persona in self.perltqa["personas"]:
            name_digest = _sha256_text(str(persona["name"]))[:12]
            for category, specification in persona["categories"].items():
                slice_name = f"perltqa_{category}"
                cases.setdefault(slice_name, []).extend(
                    f"perltqa:{name_digest}:{category}:{str(question_hash)[:12]}"
                    for question_hash in specification["question_sha256"]
                )
        for case_id in self.memdaily["case_ids"]:
            parts = str(case_id).split(":")
            if len(parts) != 4 or parts[0] != "memdaily":
                raise SampleManifestError(f"invalid MemDaily case_id: {case_id!r}")
            cases.setdefault(f"memdaily_{parts[1]}", []).append(str(case_id))
        return {name: tuple(case_ids) for name, case_ids in sorted(cases.items())}

    @property
    def expected_case_ids(self) -> dict[str, tuple[str, ...]]:
        cases: dict[str, list[str]] = {"perltqa": [], "memdaily": []}
        for slice_name, case_ids in self.expected_case_ids_by_slice.items():
            dataset = slice_name.split("_", 1)[0]
            cases[dataset].extend(case_ids)
        return {dataset: tuple(case_ids) for dataset, case_ids in cases.items()}


def _required_mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise SampleManifestError(f"{label} must be an object")
    return {str(key): item for key, item in value.items()}


def _required_list(value: object, label: str) -> list[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise SampleManifestError(f"{label} must be a list")
    return list(value)


def _parse_accepted_rubrics(
    value: object,
    *,
    schema_version: int,
    selected_question_hashes: set[str],
) -> dict[str, AcceptedRubrics]:
    if value is None:
        return {}
    if schema_version < 2:
        raise SampleManifestError("accepted_rubrics requires manifest schema_version 2")

    parsed: dict[str, AcceptedRubrics] = {}
    for question_hash, raw_rubrics in _required_mapping(value, "perltqa.accepted_rubrics").items():
        normalized_hash = question_hash.lower()
        if not re.fullmatch(r"[0-9a-f]{64}", normalized_hash):
            raise SampleManifestError(f"accepted_rubrics has invalid question SHA-256: {question_hash!r}")
        if normalized_hash not in selected_question_hashes:
            raise SampleManifestError(f"accepted_rubrics references an unselected question: {question_hash}")

        rubric_items = _required_list(raw_rubrics, f"accepted_rubrics.{question_hash}")
        if not rubric_items:
            raise SampleManifestError(f"accepted_rubrics.{question_hash} must contain at least one rubric")
        rubrics: list[AnswerRubric] = []
        for rubric_index, raw_rubric in enumerate(rubric_items):
            label = f"accepted_rubrics.{question_hash}[{rubric_index}]"
            rubric = _required_mapping(raw_rubric, label)
            if set(rubric) != {"required_concepts"}:
                raise SampleManifestError(f"{label} must contain only required_concepts")
            concept_items = _required_list(rubric["required_concepts"], f"{label}.required_concepts")
            if not concept_items:
                raise SampleManifestError(f"{label}.required_concepts must not be empty")
            concepts: list[tuple[str, ...]] = []
            for concept_index, raw_concept in enumerate(concept_items):
                concept_label = f"{label}.required_concepts[{concept_index}]"
                expressions = tuple(
                    dict.fromkeys(str(item).strip() for item in _required_list(raw_concept, concept_label))
                )
                if not expressions or any(not expression for expression in expressions):
                    raise SampleManifestError(f"{concept_label} must contain non-empty synonymous expressions")
                concepts.append(expressions)
            rubrics.append(tuple(concepts))
        parsed[normalized_hash] = tuple(rubrics)
    return parsed


def load_sample_manifest(path: Path) -> E2ESampleManifest:
    raw = _required_mapping(json.loads(path.read_text(encoding="utf-8")), "manifest")
    schema_version = raw.get("schema_version")
    if schema_version not in {1, 2}:
        raise SampleManifestError("Chinese E2E manifest schema_version must be 1 or 2")
    sources_raw = _required_mapping(raw.get("sources"), "sources")
    sources: dict[str, dict[str, str]] = {}
    for name, value in sources_raw.items():
        source = _required_mapping(value, f"sources.{name}")
        path_text = str(source.get("path") or "").strip()
        digest = str(source.get("sha256") or "").strip().lower()
        if not path_text or len(digest) != 64:
            raise SampleManifestError(f"sources.{name} requires path and SHA-256")
        sources[name] = {"path": path_text, "sha256": digest}
    if set(sources) != {"perltqa_memory", "perltqa_qa", "memdaily"}:
        raise SampleManifestError("manifest must declare all three Chinese E2E sources")

    perltqa = _required_mapping(raw.get("perltqa"), "perltqa")
    memdaily = _required_mapping(raw.get("memdaily"), "memdaily")
    personas = _required_list(perltqa.get("personas"), "perltqa.personas")
    evaluation_as_of = str(perltqa.get("evaluation_as_of") or "").strip()
    try:
        parsed_evaluation_as_of = datetime.fromisoformat(evaluation_as_of.replace("Z", "+00:00"))
    except ValueError as error:
        raise SampleManifestError("perltqa.evaluation_as_of must be an ISO-8601 timestamp") from error
    if parsed_evaluation_as_of.tzinfo is None:
        raise SampleManifestError("perltqa.evaluation_as_of must include a timezone")
    case_ids = [str(item) for item in _required_list(memdaily.get("case_ids"), "memdaily.case_ids")]
    if len(case_ids) != len(set(case_ids)):
        raise SampleManifestError("MemDaily case_ids must be unique")
    names: set[str] = set()
    selected_question_hashes: set[str] = set()
    for index, raw_persona in enumerate(personas):
        persona = _required_mapping(raw_persona, f"perltqa.personas[{index}]")
        name = str(persona.get("name") or "").strip()
        if not name or name in names:
            raise SampleManifestError("PerLTQA persona names must be non-empty and unique")
        names.add(name)
        categories = _required_mapping(persona.get("categories"), f"perltqa persona {name}.categories")
        if tuple(categories) != CATEGORIES:
            raise SampleManifestError(f"PerLTQA persona {name} must preserve all four category slices")
        for category, raw_specification in categories.items():
            specification = _required_mapping(raw_specification, f"{name}.{category}")
            target = str(specification.get("target_source") or "").strip()
            distractor = str(specification.get("distractor_source") or "").strip()
            question_hashes = [
                str(item).lower()
                for item in _required_list(specification.get("question_sha256"), f"{name}.{category}.question_sha256")
            ]
            if not target or not distractor or target == distractor:
                raise SampleManifestError(f"{name}.{category} requires distinct target and distractor sources")
            if not question_hashes or any(len(item) != 64 for item in question_hashes):
                raise SampleManifestError(f"{name}.{category} requires full question SHA-256 values")
            if len(question_hashes) != len(set(question_hashes)):
                raise SampleManifestError(f"{name}.{category} question hashes must be unique")
            selected_question_hashes.update(question_hashes)

    accepted_rubrics = _parse_accepted_rubrics(
        perltqa.get("accepted_rubrics"),
        schema_version=int(schema_version),
        selected_question_hashes=selected_question_hashes,
    )

    manifest = E2ESampleManifest(
        schema_version=int(schema_version),
        sample_id=str(raw.get("sample_id") or "").strip(),
        sources=sources,
        perltqa={**perltqa, "personas": personas},
        memdaily={**memdaily, "case_ids": case_ids},
        accepted_rubrics_by_question_hash=accepted_rubrics,
    )
    if not manifest.sample_id:
        raise SampleManifestError("sample_id is required")
    if len(personas) != int(perltqa.get("expected_personas", -1)):
        raise SampleManifestError("PerLTQA persona count does not match expected_personas")
    if manifest.perltqa_question_count != int(perltqa.get("expected_questions", -1)):
        raise SampleManifestError("PerLTQA question count does not match expected_questions")
    if manifest.memdaily_question_count != int(memdaily.get("expected_questions", -1)):
        raise SampleManifestError("MemDaily question count does not match expected_questions")
    expected_case_ids = [case_id for case_ids in manifest.expected_case_ids.values() for case_id in case_ids]
    if len(expected_case_ids) != len(set(expected_case_ids)):
        raise SampleManifestError("derived Chinese E2E case IDs must be unique")
    return manifest


def verify_source_hashes(sources: Mapping[str, Mapping[str, str]]) -> None:
    for name, specification in sources.items():
        path = Path(str(specification["path"]))
        if not path.is_file():
            raise FileNotFoundError(f"Chinese E2E source does not exist: {path}")
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        expected = str(specification["sha256"]).lower()
        if actual != expected:
            raise SourceHashMismatch(f"{name} SHA-256 changed: expected {expected}, got {actual}")


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _perltqa_event_id(name: str, category: str, source_key: str) -> str:
    identity = _sha256_text(f"{name}\0{category}\0{source_key}")[:24]
    return f"perltqa:e2e:{identity}"


def _render_perltqa_message(name: str, category: str, source_key: str, text: str) -> str:
    labels = {
        "profile": "个人资料",
        "social_relationship": "社会关系",
        "events": "经历事件",
        "dialogues": "历史对话",
    }
    return f"关于{name}的{labels[category]}（{source_key}）：\n{text}"


def _resolve_perltqa_bundles(manifest: E2ESampleManifest) -> tuple[PerLTQABundle, ...]:
    adapter = PerLTQAAdapter()
    characters = adapter.load(
        Path(manifest.sources["perltqa_memory"]["path"]),
        Path(manifest.sources["perltqa_qa"]["path"]),
        per_character=None,
        qa_per_category=None,
    )
    by_name = {character.name: character for character in characters}
    anchors_by_question = _load_perltqa_answer_anchors(Path(manifest.sources["perltqa_qa"]["path"]))
    bundles: list[PerLTQABundle] = []
    for raw_persona in manifest.perltqa["personas"]:
        persona = _required_mapping(raw_persona, "PerLTQA persona")
        name = str(persona["name"])
        character = by_name.get(name)
        if character is None:
            raise SampleManifestError(f"PerLTQA persona no longer exists: {name}")
        claims = {(claim.category, claim.source_key): claim for claim in character.claims}
        questions: dict[tuple[str, str], list[PerLTQAQuestion]] = {}
        for question in character.questions:
            if len(question.reference_keys) == 1:
                questions.setdefault((question.category, question.reference_keys[0]), []).append(question)

        namespace = f"eval:perltqa:e2e:{_sha256_text(name)[:16]}"
        messages: list[MemDailyMessage] = []
        selected_questions: list[E2EQuestion] = []
        seen_events: set[str] = set()
        categories = _required_mapping(persona["categories"], f"{name}.categories")
        for category in CATEGORIES:
            specification = _required_mapping(categories[category], f"{name}.{category}")
            target = str(specification["target_source"])
            distractor = str(specification["distractor_source"])
            target_event_id = _perltqa_event_id(name, category, target)
            for source_key in (target, distractor):
                claim = claims.get((category, source_key))
                if claim is None:
                    raise SampleManifestError(f"PerLTQA source no longer exists: {name}/{category}/{source_key}")
                event_id = _perltqa_event_id(name, category, source_key)
                if event_id in seen_events:
                    raise SampleManifestError(f"duplicate PerLTQA event in bundle: {event_id}")
                seen_events.add(event_id)
                messages.append(
                    MemDailyMessage(
                        mid=len(messages),
                        event_id=event_id,
                        occurred_at="2024-01-01T00:00:00+00:00",
                        text=_render_perltqa_message(name, category, source_key, claim.text),
                        place="PerLTQA",
                    )
                )

            available = {
                _sha256_text(question.question): question for question in questions.get((category, target), [])
            }
            for question_hash in specification["question_sha256"]:
                question = available.get(str(question_hash))
                if question is None:
                    raise SampleManifestError(
                        f"PerLTQA question no longer exists: {name}/{category}/{target}/{question_hash}"
                    )
                anchor_identity = (
                    name,
                    category,
                    target,
                    str(question_hash),
                    _sha256_text(question.answer),
                )
                anchor_options = anchors_by_question.get(anchor_identity, set())
                if len(anchor_options) != 1:
                    raise SampleManifestError(
                        f"PerLTQA anchors do not resolve uniquely: {name}/{category}/{target}/{question_hash}"
                    )
                selected_questions.append(
                    E2EQuestion(
                        case_id=f"perltqa:{_sha256_text(name)[:12]}:{category}:{str(question_hash)[:12]}",
                        category=category,
                        question=question.question,
                        answer=question.answer,
                        answer_anchors=next(iter(anchor_options)),
                        accepted_rubrics=manifest.accepted_rubrics_by_question_hash.get(str(question_hash), ()),
                        namespace=namespace,
                        gold_event_ids=(target_event_id,),
                    )
                )
        bundles.append(
            PerLTQABundle(
                name=name,
                namespace=namespace,
                evaluation_as_of=str(manifest.perltqa["evaluation_as_of"]),
                messages=tuple(messages),
                questions=tuple(selected_questions),
            )
        )
    return tuple(bundles)


def _resolve_memdaily_trajectories(manifest: E2ESampleManifest) -> tuple[MemDailyTrajectory, ...]:
    source = Path(manifest.sources["memdaily"]["path"])
    available = {trajectory.case_id: trajectory for trajectory in load_trajectories(source, n_per_type=None)}
    selected: list[MemDailyTrajectory] = []
    for case_id in manifest.memdaily["case_ids"]:
        trajectory = available.get(str(case_id))
        if trajectory is None:
            raise SampleManifestError(f"MemDaily trajectory no longer exists: {case_id}")
        selected.append(trajectory)
    return tuple(selected)


def _load_perltqa_answer_anchors(
    path: Path,
) -> dict[tuple[str, str, str, str, str], set[tuple[str, ...]]]:
    anchors: dict[tuple[str, str, str, str, str], set[tuple[str, ...]]] = {}

    def add(persona: str, category: str, source_key: str, raw_question: object) -> None:
        if not isinstance(raw_question, Mapping):
            return
        question = str(raw_question.get("Question") or "").strip()
        answer = str(raw_question.get("Answer") or "").strip()
        if not question:
            return
        terms: list[str] = []
        raw_anchors = raw_question.get("Memory Anchors")
        if isinstance(raw_anchors, Sequence) and not isinstance(raw_anchors, (str, bytes)):
            for item in raw_anchors:
                if isinstance(item, Mapping):
                    terms.extend(str(key).strip() for key in item if str(key).strip())
        identity = (persona, category, source_key, _sha256_text(question), _sha256_text(answer))
        anchors.setdefault(identity, set()).add(tuple(dict.fromkeys(terms)))

    root = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(root, Sequence) or isinstance(root, (str, bytes)):
        raise SampleManifestError("PerLTQA QA root must be a list")
    for raw_persona in root:
        if not isinstance(raw_persona, Mapping):
            continue
        for name, categories in raw_persona.items():
            if not isinstance(categories, Mapping):
                continue
            for category in CATEGORIES:
                raw_category = categories.get(category)
                if category == "profile":
                    if isinstance(raw_category, Sequence) and not isinstance(raw_category, (str, bytes)):
                        for item in raw_category:
                            if isinstance(item, Mapping):
                                source_key = str(item.get("Reference Memory") or "").strip()
                                add(str(name), category, source_key, item)
                    continue
                if not isinstance(raw_category, Mapping):
                    continue
                for source_key, raw_questions in raw_category.items():
                    if isinstance(raw_questions, Sequence) and not isinstance(raw_questions, (str, bytes)):
                        for item in raw_questions:
                            add(str(name), category, str(source_key), item)
    return anchors


def load_sampled_inputs(manifest: E2ESampleManifest) -> SampledInputs:
    verify_source_hashes(manifest.sources)
    return SampledInputs(
        perltqa_bundles=_resolve_perltqa_bundles(manifest),
        memdaily_trajectories=_resolve_memdaily_trajectories(manifest),
    )


def build_perltqa_ingest_trajectory(bundle: PerLTQABundle) -> MemDailyTrajectory:
    """Represent one persona's selected sources as one cacheable extraction unit."""
    return MemDailyTrajectory(
        case_id=f"perltqa:e2e:ingest:{_sha256_text(bundle.name)[:16]}",
        qtype="perltqa",
        subtype="mixed",
        tid=0,
        namespace=bundle.namespace,
        question="",
        answer="",
        question_at=bundle.evaluation_as_of,
        ground_truth_choice=None,
        choices={},
        messages=bundle.messages,
        gold_event_ids=(),
    )


def build_perltqa_question_trajectory(
    ingest: MemDailyTrajectory,
    question: E2EQuestion,
) -> MemDailyTrajectory:
    """Reuse a persona extraction while swapping only question-time fields."""
    if question.namespace != ingest.namespace:
        raise ValueError("PerLTQA question namespace does not match its ingest bundle")
    return replace(
        ingest,
        case_id=question.case_id,
        question=question.question,
        answer=question.answer,
        gold_event_ids=question.gold_event_ids,
    )


def score_retrieved_evidence(
    retrieved: Sequence[Mapping[str, Any]],
    gold_event_ids: Sequence[str],
    *,
    k: int = 5,
) -> dict[str, float]:
    """Score evidence provenance without depending on generated claim IDs."""
    gold = {str(item) for item in gold_event_ids}
    first_rank: int | None = None
    for fallback_rank, item in enumerate(retrieved, start=1):
        rank = int(item.get("rank") or fallback_rank)
        evidence = {str(value) for value in item.get("evidence_event_ids", [])}
        if evidence & gold:
            first_rank = rank
            break
    return {
        "recall_at_5": 1.0 if first_rank is not None and first_rank <= k else 0.0,
        "mrr": 1.0 / first_rank if first_rank is not None else 0.0,
    }


def _normalized_answer(text: str) -> str:
    return "".join(character.lower() for character in text if character.isalnum())


def score_answer_anchors(predicted: str, anchors: Sequence[str]) -> float:
    """Require every official PerLTQA answer anchor in the concise reader output."""
    predicted_text = _normalized_answer(predicted)
    terms = [term for anchor in anchors for term in re.split(r"[、,，；;]+", str(anchor)) if _normalized_answer(term)]
    if not predicted_text or not terms:
        return 0.0
    return float(all(_normalized_answer(term) in predicted_text for term in terms))


def score_answer(
    predicted: str,
    anchors: Sequence[str],
    accepted_rubrics: AcceptedRubrics = (),
) -> dict[str, float | str]:
    """Score official anchors first, then deterministic reviewed rubrics."""
    if score_answer_anchors(predicted, anchors):
        return {
            "answer_correct": 1.0,
            "verdict_basis": "official_anchor",
            "scorer_version": SCORER_VERSION,
        }

    predicted_text = _normalized_answer(predicted)
    rubric_match = bool(predicted_text) and any(
        bool(rubric)
        and all(
            bool(concept)
            and any(
                normalized_expression in predicted_text
                for expression in concept
                if (normalized_expression := _normalized_answer(expression))
            )
            for concept in rubric
        )
        for rubric in accepted_rubrics
    )
    return {
        "answer_correct": float(rubric_match),
        "verdict_basis": "reviewed_rubric" if accepted_rubrics else "official_anchor",
        "scorer_version": SCORER_VERSION,
    }


def covered_gold_events(connection: Any, gold_event_ids: Sequence[str]) -> tuple[str, ...]:
    """Return gold events that produced at least one stored claim evidence link."""
    ordered = tuple(dict.fromkeys(str(item) for item in gold_event_ids))
    if not ordered:
        return ()
    placeholders = ",".join("?" for _ in ordered)
    rows = connection.execute(
        "SELECT DISTINCT evidence_id FROM evidence_links "
        "WHERE derived_type='claim' AND evidence_type='event' "
        f"AND evidence_id IN ({placeholders})",
        ordered,
    ).fetchall()
    covered = {str(row[0]) for row in rows}
    return tuple(event_id for event_id in ordered if event_id in covered)


def _mean(items: Sequence[float]) -> float | None:
    return fmean(items) if items else None


def _aggregate_group(cases: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    successful = [case for case in cases if not case.get("error")]
    qa_accuracy = [
        float(case["qa"].get("answer_correct", case["qa"]["exact_match"]))
        for case in successful
        if case.get("qa") is not None
    ]
    qa_f1 = [float(case["qa"]["f1"]) for case in successful if case.get("qa") is not None]
    recall_at_5 = [
        float(case["retrieval"]["recall_at_5"])
        for case in successful
        if case.get("retrieval") is not None and case["retrieval"].get("recall_at_5") is not None
    ]
    mrr = [
        float(case["retrieval"]["mrr"])
        for case in successful
        if case.get("retrieval") is not None and case["retrieval"].get("mrr") is not None
    ]
    gold_units = {str(item) for case in cases for item in case.get("gold_extraction_units", [])}
    covered_units = {str(item) for case in cases for item in case.get("covered_extraction_units", [])} & gold_units
    return {
        "cases": len(cases),
        "successful_cases": len(successful),
        "failed_cases": len(cases) - len(successful),
        "qa_accuracy": _mean(qa_accuracy),
        "qa_f1": _mean(qa_f1),
        "recall_at_5": _mean(recall_at_5),
        "mrr": _mean(mrr),
        "extraction_coverage": len(covered_units) / len(gold_units) if gold_units else None,
        "gold_extraction_units": len(gold_units),
        "covered_extraction_units": len(covered_units),
    }


def aggregate_results(cases: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    datasets = sorted({str(case.get("dataset") or "unknown") for case in cases})
    slices = sorted({str(case.get("slice") or "unknown") for case in cases})
    return {
        "overall": _aggregate_group(cases),
        "by_dataset": {
            dataset: _aggregate_group([case for case in cases if str(case.get("dataset")) == dataset])
            for dataset in datasets
        },
        "by_slice": {
            slice_name: _aggregate_group([case for case in cases if str(case.get("slice")) == slice_name])
            for slice_name in slices
        },
    }


def evaluate_gate(
    metrics_by_dataset: Mapping[str, Mapping[str, Any]],
    thresholds: Mapping[str, DatasetThresholds] = DEFAULT_THRESHOLDS,
) -> tuple[GateFailure, ...]:
    failures: list[GateFailure] = []
    for dataset, threshold in thresholds.items():
        metrics = metrics_by_dataset.get(dataset)
        if metrics is None:
            failures.append(GateFailure(dataset, "dataset_present", 0.0, 1.0))
            continue
        failed_cases = float(metrics.get("failed_cases", math.inf))
        if failed_cases != 0.0:
            failures.append(GateFailure(dataset, "failed_cases", failed_cases, 0.0))
        for field in fields(threshold):
            metric = field.name
            actual = float(metrics.get(metric, math.nan))
            minimum = float(getattr(threshold, metric))
            if not math.isfinite(actual) or actual < minimum:
                failures.append(GateFailure(dataset, metric, actual, minimum))
    return tuple(failures)


def evaluate_overall_gate(overall_metrics: Mapping[str, Any]) -> tuple[GateFailure, ...]:
    actual = float(overall_metrics.get("qa_accuracy", math.nan))
    if not math.isfinite(actual) or actual < OVERALL_QA_ACCURACY_MINIMUM:
        return (GateFailure("overall", "qa_accuracy", actual, OVERALL_QA_ACCURACY_MINIMUM),)
    return ()


def _case_contract(
    manifest: E2ESampleManifest,
    cases: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    expected_by_dataset = manifest.expected_case_ids
    actual_by_dataset: dict[str, list[str]] = {}
    for case in cases:
        dataset = str(case.get("dataset") or "unknown")
        actual_by_dataset.setdefault(dataset, []).append(str(case.get("case_id") or ""))

    contract: dict[str, dict[str, Any]] = {}
    for dataset in sorted(set(expected_by_dataset) | set(actual_by_dataset)):
        expected = tuple(expected_by_dataset.get(dataset, ()))
        expected_set = set(expected)
        actual = actual_by_dataset.get(dataset, [])
        actual_counts = Counter(actual)
        missing = [case_id for case_id in expected if actual_counts[case_id] == 0]
        unexpected = sorted(case_id for case_id in actual_counts if case_id not in expected_set)
        duplicates = sorted(case_id for case_id, count in actual_counts.items() if count > 1)
        contract[dataset] = {
            "expected_cases": len(expected),
            "actual_rows": len(actual),
            "missing_case_ids": missing,
            "unexpected_case_ids": unexpected,
            "duplicate_case_ids": duplicates,
            "exact": not missing and not unexpected and not duplicates and len(actual) == len(expected),
        }
    return contract


def _slice_case_contract(
    manifest: E2ESampleManifest,
    cases: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    expected_by_slice = manifest.expected_case_ids_by_slice
    actual_by_slice: dict[str, list[str]] = {}
    for case in cases:
        slice_name = str(case.get("slice") or "unknown")
        actual_by_slice.setdefault(slice_name, []).append(str(case.get("case_id") or ""))

    contract: dict[str, dict[str, Any]] = {}
    for slice_name in sorted(set(expected_by_slice) | set(actual_by_slice)):
        expected = tuple(expected_by_slice.get(slice_name, ()))
        expected_set = set(expected)
        actual = actual_by_slice.get(slice_name, [])
        actual_counts = Counter(actual)
        missing = [case_id for case_id in expected if actual_counts[case_id] == 0]
        unexpected = sorted(case_id for case_id in actual_counts if case_id not in expected_set)
        duplicates = sorted(case_id for case_id, count in actual_counts.items() if count > 1)
        contract[slice_name] = {
            "expected_cases": len(expected),
            "actual_rows": len(actual),
            "missing_case_ids": missing,
            "unexpected_case_ids": unexpected,
            "duplicate_case_ids": duplicates,
            "exact": not missing and not unexpected and not duplicates and len(actual) == len(expected),
        }
    return contract


def _evaluate_slice_gate(
    metrics_by_slice: Mapping[str, Mapping[str, Any]],
    expected_slice_counts: Mapping[str, int],
) -> tuple[GateFailure, ...]:
    """Guard broad slice collapse without brittle high thresholds on 2-8 samples."""
    failures: list[GateFailure] = []
    for slice_name, expected_count in expected_slice_counts.items():
        dataset = slice_name.split("_", 1)[0]
        metrics = metrics_by_slice.get(slice_name)
        if metrics is None:
            failures.append(GateFailure(dataset, f"{slice_name}.cases", 0.0, float(expected_count)))
            continue
        actual_count = float(metrics.get("cases", math.nan))
        if not math.isfinite(actual_count) or actual_count != float(expected_count):
            failures.append(GateFailure(dataset, f"{slice_name}.cases", actual_count, float(expected_count)))
        for metric, minimum in SLICE_MINIMUMS.items():
            actual = float(metrics.get(metric, math.nan))
            if not math.isfinite(actual) or actual < minimum:
                failures.append(GateFailure(dataset, f"{slice_name}.{metric}", actual, minimum))
    return tuple(failures)


def normalize_benchmark_case(
    *,
    dataset: str,
    slice_name: str,
    ingest_unit_id: str,
    raw: Mapping[str, Any],
    covered_event_ids: Sequence[str],
) -> dict[str, Any]:
    """Convert existing benchmark output into the shared E2E case schema."""
    gold_event_ids = [str(item) for item in raw.get("gold_event_ids", [])]
    retrieved = [dict(item) for item in raw.get("retrieved", []) if isinstance(item, Mapping)]
    provenance_scores = score_retrieved_evidence(retrieved, gold_event_ids, k=5)
    retrieval_raw = raw.get("retrieval") if isinstance(raw.get("retrieval"), Mapping) else {}
    recall_at_5 = retrieval_raw.get("recall_at_5", provenance_scores["recall_at_5"])
    return {
        "dataset": dataset,
        "case_id": str(raw.get("case_id") or ""),
        "slice": slice_name,
        "question": str(raw.get("question") or ""),
        "answer": str(raw.get("answer") or ""),
        "answer_anchors": [str(item) for item in raw.get("answer_anchors", [])],
        "accepted_rubrics": list(raw.get("accepted_rubrics", [])),
        "ingest_unit_id": ingest_unit_id,
        "ingest": dict(raw["ingest"]) if isinstance(raw.get("ingest"), Mapping) else None,
        "retrieval": {
            "recall_at_5": float(recall_at_5) if recall_at_5 is not None else None,
            "mrr": provenance_scores["mrr"],
        },
        "retrieved": retrieved,
        "qa": dict(raw["qa"]) if isinstance(raw.get("qa"), Mapping) else None,
        "gold_extraction_units": gold_event_ids,
        "covered_extraction_units": [str(item) for item in covered_event_ids],
        "error": raw.get("error"),
        "elapsed_seconds": raw.get("elapsed_seconds"),
    }


def _run_accounting(cases: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    unique_ingests: dict[str, Mapping[str, Any]] = {}
    for case in cases:
        ingest = case.get("ingest")
        if isinstance(ingest, Mapping):
            unique_ingests.setdefault(str(case.get("ingest_unit_id") or case.get("case_id")), ingest)
    cache_counts = Counter(str(ingest.get("cache_status") or "unknown") for ingest in unique_ingests.values())
    return {
        "cache_status_counts": dict(sorted(cache_counts.items())),
        "usage": {
            "extraction_input_tokens": sum(int(ingest.get("input_tokens", 0)) for ingest in unique_ingests.values()),
            "extraction_output_tokens": sum(int(ingest.get("output_tokens", 0)) for ingest in unique_ingests.values()),
            "extraction_total_tokens": sum(int(ingest.get("total_tokens", 0)) for ingest in unique_ingests.values()),
            "qa_total_tokens": sum(
                int(case["qa"].get("usage", {}).get("total_tokens", 0))
                for case in cases
                if isinstance(case.get("qa"), Mapping)
            ),
        },
    }


def build_e2e_report(
    *,
    manifest: E2ESampleManifest,
    cases: Sequence[Mapping[str, Any]],
    models: Mapping[str, str],
    status: str,
    refresh: bool,
    ingest_config_fingerprint: str | None = None,
) -> dict[str, Any]:
    """Build an auditable report used both by pytest output and its gate."""
    metrics = aggregate_results(cases)
    case_contract = _case_contract(manifest, cases)
    slice_contract = _slice_case_contract(manifest, cases)
    failures = list(evaluate_gate(metrics["by_dataset"]))
    failures.extend(evaluate_overall_gate(metrics["overall"]))
    failures.extend(
        GateFailure(dataset, "case_set_exact", 0.0, 1.0)
        for dataset, contract in case_contract.items()
        if not contract["exact"]
    )
    failures.extend(
        GateFailure(slice_name.split("_", 1)[0], f"{slice_name}.case_set_exact", 0.0, 1.0)
        for slice_name, contract in slice_contract.items()
        if not contract["exact"]
    )
    failures.extend(_evaluate_slice_gate(metrics["by_slice"], manifest.slice_counts))
    accounting = _run_accounting(cases)
    return {
        "schema_version": 2,
        "benchmark": "chinese_e2e",
        "scorer_version": SCORER_VERSION,
        "status": status,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sample": {
            "id": manifest.sample_id,
            "sources": manifest.sources,
            "perltqa_questions": manifest.perltqa_question_count,
            "memdaily_questions": manifest.memdaily_question_count,
            "perltqa_evaluation_as_of": manifest.perltqa["evaluation_as_of"],
            "slice_counts": manifest.slice_counts,
        },
        "run": {
            "refresh_extraction": refresh,
            "package_version": f"v{__version__}",
            "ingest_config_fingerprint": ingest_config_fingerprint,
            "models": dict(models),
            **accounting,
        },
        "metrics": metrics,
        "gate": {
            "passed": not failures,
            "thresholds": {name: asdict(value) for name, value in DEFAULT_THRESHOLDS.items()},
            "overall_thresholds": {"qa_accuracy": OVERALL_QA_ACCURACY_MINIMUM},
            "slice_minimums": SLICE_MINIMUMS,
            "case_contract": case_contract,
            "slice_contract": slice_contract,
            "failures": [asdict(failure) for failure in failures],
        },
        "cases": list(cases),
    }


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _cache_paths(cache_root: Path, dataset: str, identity: str) -> tuple[Path, Path]:
    digest = _sha256_text(identity)[:24]
    database_path = cache_root / dataset / f"{digest}.db"
    return database_path, database_path.with_suffix(".manifest.json")


def _remove_cache_artifacts(cache_root: Path, database_path: Path, manifest_path: Path) -> None:
    root = cache_root.resolve()
    paths = (
        database_path,
        Path(f"{database_path}-wal"),
        Path(f"{database_path}-shm"),
        manifest_path,
        manifest_path.with_suffix(f"{manifest_path.suffix}.tmp"),
    )
    for path in paths:
        resolved = path.resolve()
        if not resolved.is_relative_to(root):
            raise ValueError(f"refusing to remove cache artifact outside {root}: {resolved}")
        path.unlink(missing_ok=True)


def _runtime_settings(config_path: Path, env_path: Path) -> Settings:
    import dataclasses

    configured = load_settings(config_path, env_path)
    if configured.extractor_mode != "llm" or not configured.llm_api_key:
        raise RuntimeError("Chinese E2E requires extractor.mode='llm' and LLM_API_KEY")
    if configured.embedder_mode != "real" or not configured.embedding_api_key:
        raise RuntimeError("Chinese E2E requires embedding.mode='real' and EMBEDDING_API_KEY")
    if configured.reranker_mode not in {"on", "real"} or not configured.reranker_api_key:
        raise RuntimeError("Chinese E2E requires a real reranker and RERANKER_API_KEY")
    runtime = dataclasses.replace(
        configured,
        vector_backend="sqlite_scan",
        query_expansion_mode="off",
        relation_discovery_mode="off",
        procedure_recall_mode="off",
        recall_side_effect_backoff_seconds=0.0,
    )
    runtime.validate()
    return runtime


def _models(settings: Settings) -> dict[str, str]:
    return {
        "extractor": settings.llm_model,
        "embedder": settings.embedding_model,
        "reranker": settings.reranker_model,
        "qa": os.getenv("HL_MEM_EVAL_QA_MODEL", memdaily_benchmark.QA_FALLBACK_MODEL),
    }


def _base_raw_question(question: E2EQuestion, ingest: Mapping[str, Any] | None) -> dict[str, Any]:
    return {
        "case_id": question.case_id,
        "question": question.question,
        "answer": question.answer,
        "answer_anchors": list(question.answer_anchors),
        "accepted_rubrics": [
            {"required_concepts": [list(concept) for concept in rubric]} for rubric in question.accepted_rubrics
        ],
        "gold_event_ids": list(question.gold_event_ids),
        "ingest": dict(ingest) if isinstance(ingest, Mapping) else None,
        "retrieval": None,
        "retrieved": [],
        "qa": None,
        "error": None,
    }


def _open_coverage(
    database_path: Path,
    settings: Settings,
    gold_event_ids: Sequence[str],
) -> tuple[str, ...]:
    if not database_path.is_file():
        return ()
    database = Database(database_path, settings=settings)
    connection = database.open()
    try:
        return covered_gold_events(connection, gold_event_ids)
    finally:
        database.close()


def remaining_bundle_questions(
    bundle: PerLTQABundle,
    results: Sequence[Mapping[str, Any]],
) -> tuple[E2EQuestion, ...]:
    """Return only questions not already represented in a partial bundle result."""
    completed = {str(result.get("case_id") or "") for result in results}
    return tuple(question for question in bundle.questions if question.case_id not in completed)


def _run_perltqa_bundle(
    bundle: PerLTQABundle,
    settings: Settings,
    embedder: Any,
    reranker: Any,
    cache_root: Path,
    *,
    refresh: bool,
    case_number: int,
    total: int,
) -> list[dict[str, Any]]:
    ingest_trajectory = build_perltqa_ingest_trajectory(bundle)
    database_path, manifest_path = _cache_paths(cache_root, "perltqa", ingest_trajectory.case_id)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    database: Database | None = None
    ingest_result: dict[str, Any] | None = None
    covered: tuple[str, ...] = ()
    results: list[dict[str, Any]] = []
    try:
        reuse_cache = False
        cache_reason: str | None = "refresh_requested" if refresh else None
        if not refresh and database_path.is_file():
            reuse_cache, cache_reason = memdaily_benchmark._validate_cached_ingest(
                manifest_path,
                ingest_trajectory,
                settings,
            )
        elif not refresh:
            cache_reason = "database_missing"
        if not reuse_cache:
            _remove_cache_artifacts(cache_root, database_path, manifest_path)

        database = Database(database_path, settings=settings)
        connection = database.open()
        if reuse_cache:
            reuse_cache, database_reason = memdaily_benchmark._validate_cached_ingest(
                manifest_path,
                ingest_trajectory,
                settings,
                connection,
            )
            if not reuse_cache:
                cache_reason = database_reason
                database.close()
                database = None
                _remove_cache_artifacts(cache_root, database_path, manifest_path)
                database = Database(database_path, settings=settings)
                connection = database.open()

        if reuse_cache:
            ingest_result = {
                "skipped": True,
                "cache_status": "reused",
                "cache_reason": "cache_valid",
                "cache_manifest": str(manifest_path),
            }
        else:
            ingest_result = memdaily_benchmark._ingest_trajectory(
                connection,
                ingest_trajectory,
                settings,
                embedder,
                case_number=case_number,
                total=total,
            )
            ingest_result["cache_status"] = "fresh_ingest" if refresh else "stale_reingested"
            ingest_result["cache_reason"] = cache_reason
            ingest_result["cache_manifest"] = str(manifest_path)
            _write_json_atomic(manifest_path, memdaily_benchmark._cache_identity(ingest_trajectory, settings))

        all_gold = tuple(
            dict.fromkeys(event_id for question in bundle.questions for event_id in question.gold_event_ids)
        )
        covered = covered_gold_events(connection, all_gold)
        print(
            f"[{case_number}/{total}] PerLTQA {bundle.name}: messages={len(bundle.messages)} "
            f"questions={len(bundle.questions)} cache={ingest_result['cache_status']} "
            f"extracted={len(covered)}/{len(all_gold)}",
            flush=True,
        )
        for question in bundle.questions:
            raw = _base_raw_question(question, ingest_result)
            question_trajectory = build_perltqa_question_trajectory(ingest_trajectory, question)
            try:
                raw["retrieval"], raw["retrieved"] = memdaily_benchmark._recall_trajectory(
                    connection,
                    question_trajectory,
                    settings,
                    embedder,
                    reranker,
                )
                raw["qa"] = memdaily_benchmark._run_qa(
                    connection,
                    question_trajectory,
                    raw["retrieved"],
                    settings,
                    answerability=raw["retrieval"]["answerability"],
                )
                raw["qa"].update(
                    score_answer(
                        str(raw["qa"].get("predicted_answer") or ""),
                        question.answer_anchors,
                        question.accepted_rubrics,
                    )
                )
            except Exception as error:  # every paid case must remain visible in the report
                raw["error"] = f"{type(error).__name__}: {error}"
            results.append(
                normalize_benchmark_case(
                    dataset="perltqa",
                    slice_name=f"perltqa_{question.category}",
                    ingest_unit_id=ingest_trajectory.case_id,
                    raw=raw,
                    covered_event_ids=tuple(item for item in question.gold_event_ids if item in set(covered)),
                )
            )
    except Exception as error:
        message = f"{type(error).__name__}: {error}"
        for question in remaining_bundle_questions(bundle, results):
            raw = _base_raw_question(question, ingest_result)
            raw["error"] = message
            results.append(
                normalize_benchmark_case(
                    dataset="perltqa",
                    slice_name=f"perltqa_{question.category}",
                    ingest_unit_id=ingest_trajectory.case_id,
                    raw=raw,
                    covered_event_ids=(),
                )
            )
    finally:
        if database is not None:
            database.close()
    return results


def _run_memdaily_cases(
    trajectories: Sequence[MemDailyTrajectory],
    settings: Settings,
    embedder: Any,
    reranker: Any,
    cache_root: Path,
    *,
    refresh: bool,
    start_number: int,
    total: int,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    previous_root = memdaily_benchmark.DATABASE_ROOT
    memdaily_root = cache_root / "memdaily"
    memdaily_benchmark.DATABASE_ROOT = memdaily_root
    try:
        for offset, trajectory in enumerate(trajectories):
            case_number = start_number + offset
            raw = memdaily_benchmark._run_case(
                trajectory,
                settings,
                embedder,
                reranker,
                skip_ingest=not refresh,
                run_qa=True,
                clean=False,
                case_number=case_number,
                total=total,
            )
            database_path = memdaily_benchmark._case_db_path(trajectory.case_id)
            covered = _open_coverage(database_path, settings, trajectory.gold_event_ids)
            result = normalize_benchmark_case(
                dataset="memdaily",
                slice_name=f"memdaily_{trajectory.qtype}",
                ingest_unit_id=trajectory.case_id,
                raw=raw,
                covered_event_ids=covered,
            )
            results.append(result)
            retrieval = result.get("retrieval") or {}
            qa = result.get("qa") or {}
            print(
                f"[{case_number}/{total}] {trajectory.case_id}: "
                f"R@5={retrieval.get('recall_at_5')} MRR={retrieval.get('mrr')} "
                f"QA={qa.get('exact_match')} error={result.get('error')}",
                flush=True,
            )
    finally:
        memdaily_benchmark.DATABASE_ROOT = previous_root
    return results


def run_chinese_e2e(
    *,
    manifest_path: Path,
    cache_root: Path,
    report_path: Path,
    refresh: bool,
    config_path: Path | None = None,
    env_path: Path | None = None,
) -> dict[str, Any]:
    """Run the fixed paid sample through production extraction, recall and QA."""
    root = Path(__file__).resolve().parents[2]
    manifest = load_sample_manifest(manifest_path)
    sampled = load_sampled_inputs(manifest)
    settings = _runtime_settings(config_path or root / "hl_mem.toml", env_path or root / ".env")
    initialize_process(settings)
    embedder = make_embedder(settings)
    reranker = make_reranker(settings)
    if reranker is None:
        raise RuntimeError("Chinese E2E requires a real reranker instance")

    cache_root = cache_root.resolve()
    cache_root.mkdir(parents=True, exist_ok=True)
    models = _models(settings)
    config_fingerprint = memdaily_benchmark.ingest_config_fingerprint(settings)
    total_units = len(sampled.perltqa_bundles) + len(sampled.memdaily_trajectories)
    cases: list[dict[str, Any]] = []

    def write_progress(status: str) -> dict[str, Any]:
        report = build_e2e_report(
            manifest=manifest,
            cases=cases,
            models=models,
            status=status,
            refresh=refresh,
            ingest_config_fingerprint=config_fingerprint,
        )
        _write_json_atomic(report_path, report)
        return report

    try:
        for index, bundle in enumerate(sampled.perltqa_bundles, start=1):
            cases.extend(
                _run_perltqa_bundle(
                    bundle,
                    settings,
                    embedder,
                    reranker,
                    cache_root,
                    refresh=refresh,
                    case_number=index,
                    total=total_units,
                )
            )
            write_progress("running")
        cases.extend(
            _run_memdaily_cases(
                sampled.memdaily_trajectories,
                settings,
                embedder,
                reranker,
                cache_root,
                refresh=refresh,
                start_number=len(sampled.perltqa_bundles) + 1,
                total=total_units,
            )
        )
    except BaseException:
        write_progress("aborted")
        raise
    return write_progress("completed")
