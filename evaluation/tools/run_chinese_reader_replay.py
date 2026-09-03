"""Validate frozen Chinese E2E reports and reconstruct reader replay cases."""

from __future__ import annotations

import hashlib
import json
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import Field, dataclass, replace
from importlib import import_module
from pathlib import Path
from typing import Any, ClassVar, Protocol, TypeAlias

import httpx

# These evaluation helpers live below namespace-package roots that mypy otherwise
# discovers twice when this file is checked by path (``tools`` and
# ``evaluation.tools``). Runtime lookup preserves the existing public seam while
# keeping the task's exact standalone mypy command scoped to this module.
_chinese_e2e = import_module("tests.eval.chinese_e2e")
_qa_client = import_module("evaluation.tools.longmemeval.qa_client")
load_sample_manifest = _chinese_e2e.load_sample_manifest
load_sampled_inputs = _chinese_e2e.load_sampled_inputs
build_perltqa_ingest_trajectory = _chinese_e2e.build_perltqa_ingest_trajectory
build_perltqa_question_trajectory = _chinese_e2e.build_perltqa_question_trajectory
qa_call_with_retry = _qa_client.qa_call_with_retry

AcceptedRubrics: TypeAlias = tuple[tuple[tuple[str, ...], ...], ...]


class MemDailyTrajectory(Protocol):
    __dataclass_fields__: ClassVar[dict[str, Field[Any]]]
    case_id: str
    qtype: str
    subtype: str
    tid: int
    namespace: str
    question: str
    answer: str
    question_at: str | None
    ground_truth_choice: str | None
    choices: dict[str, str]
    messages: tuple[object, ...]
    gold_event_ids: tuple[str, ...]


class RoleActionObject(Protocol):
    role: str
    action: str
    object: str


class AnswerEntityGold(Protocol):
    answerability: str
    answer_entities: tuple[str, ...] | None
    role_action_object: tuple[RoleActionObject, ...]
    forbidden_entities: tuple[str, ...]
    forbidden_assertions: tuple[str, ...]


EXPECTED_CASE_COUNT = 40
OFFICIAL_BENCHMARK = "chinese_e2e"
OFFICIAL_SCORER_VERSION = "deterministic-rubric-v2"
OFFICIAL_ANSWER_ENTITY_SCORER_VERSION = "answer-entity-packet-v1"
OFFICIAL_SAMPLE_ID = "zh-e2e-v3"
OFFICIAL_READER_MODEL = "qwen3.7-plus"
SUPPORTED_ANSWERABILITY = frozenset({"supported", "low_confidence"})
GLM_THINKING_MODEL = "glm-5.3-flash"
GLM_MAX_ATTEMPTS = 3


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
class ParsedGLMResponse:
    final_answer: str
    input_tokens: int
    output_tokens: int
    reasoning_tokens: int
    total_tokens: int
    thinking_verified: bool


@dataclass(frozen=True)
class ReaderCallMetadata:
    input_tokens: int
    output_tokens: int
    reasoning_tokens: int
    total_tokens: int
    latency_seconds: float
    attempts: int
    thinking_verified: bool


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


def build_glm_thinking_payload(
    model: str,
    system_prompt: str,
    user_prompt: str,
) -> dict[str, Any]:
    """Build the fixed evaluation-only GLM thinking request."""
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.1,
        "max_tokens": 4096,
        "thinking": {"type": "enabled"},
    }


def _token_count(usage: Mapping[object, object], *field_names: str) -> int:
    raw_value: object = 0
    for field_name in field_names:
        if field_name in usage:
            raw_value = usage[field_name]
            break
    invalid_value = isinstance(raw_value, bool)
    value = 0
    if raw_value is not None and isinstance(raw_value, (str, int, float)) and not invalid_value:
        try:
            value = int(raw_value)
        except (ValueError, OverflowError):
            invalid_value = True
    elif raw_value is not None:
        invalid_value = True
    if invalid_value or value < 0:
        raise ValueError("GLM response contains invalid token usage")
    return value


def parse_glm_thinking_response(envelope: Mapping[str, Any]) -> ParsedGLMResponse:
    """Minimize a GLM envelope to the final answer and non-sensitive usage data."""
    choices = envelope.get("choices")
    if not isinstance(choices, Sequence) or isinstance(choices, (str, bytes)) or not choices:
        raise ValueError("GLM response is missing a final answer")
    first_choice = choices[0]
    if not isinstance(first_choice, Mapping):
        raise ValueError("GLM response is missing a final answer")
    message = first_choice.get("message")
    if not isinstance(message, Mapping):
        raise ValueError("GLM response is missing a final answer")
    final_answer = message.get("content")
    if not isinstance(final_answer, str) or not final_answer.strip():
        raise ValueError("GLM response is missing a final answer")

    reasoning_content = message.get("reasoning_content")
    has_reasoning_content = isinstance(reasoning_content, str) and bool(reasoning_content.strip())
    del reasoning_content

    raw_usage = envelope.get("usage")
    usage: Mapping[object, object] = raw_usage if isinstance(raw_usage, Mapping) else {}
    input_tokens = _token_count(usage, "prompt_tokens", "input_tokens")
    output_tokens = _token_count(usage, "completion_tokens", "output_tokens")
    raw_details = usage.get("completion_tokens_details") or usage.get("output_tokens_details") or {}
    if not isinstance(raw_details, Mapping):
        raise ValueError("GLM response contains invalid token usage")
    reasoning_tokens = _token_count(raw_details, "reasoning_tokens")
    total_tokens = _token_count(usage, "total_tokens") or input_tokens + output_tokens

    return ParsedGLMResponse(
        final_answer=final_answer,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        reasoning_tokens=reasoning_tokens,
        total_tokens=total_tokens,
        thinking_verified=has_reasoning_content or reasoning_tokens > 0,
    )


def _safe_status_error(status_code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://provider.invalid/chat/completions")
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError(
        f"GLM request failed with HTTP {status_code}",
        request=request,
        response=response,
    )


def _response_json(response: httpx.Response) -> Mapping[str, Any]:
    invalid_json = False
    try:
        raw_envelope = response.json()
    except (TypeError, ValueError, UnicodeError):
        invalid_json = True
        raw_envelope = None
    if invalid_json or not isinstance(raw_envelope, Mapping):
        raise ValueError("GLM response must be a JSON object")
    return {str(key): value for key, value in raw_envelope.items()}


class GLMThinkingTransport:
    """Evaluation-only QA chat transport that retains metadata, never reasoning."""

    def __init__(
        self,
        client: httpx.Client,
        *,
        max_attempts: int = GLM_MAX_ATTEMPTS,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not 1 <= max_attempts <= GLM_MAX_ATTEMPTS:
            raise ValueError("max_attempts must be between 1 and 3")
        self._client = client
        self._max_attempts = max_attempts
        self._sleep = sleep
        self.last_call: ReaderCallMetadata | None = None

    def _request_once(
        self,
        *,
        url: str,
        api_key: str,
        payload: Mapping[str, Any],
    ) -> ParsedGLMResponse:
        failure: BaseException | None = None
        try:
            response = self._client.post(
                url,
                json=payload,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as error:
            failure = _safe_status_error(error.response.status_code)
        except httpx.ReadTimeout:
            failure = httpx.ReadTimeout("GLM request timed out")
        except httpx.ConnectTimeout:
            failure = httpx.ConnectTimeout("GLM connection timed out")
        except httpx.HTTPError:
            failure = httpx.HTTPError("GLM request failed")
        if failure is not None:
            raise failure
        return parse_glm_thinking_response(_response_json(response))

    def __call__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        system_prompt: str,
        user_prompt: str,
    ) -> tuple[str, int]:
        if model != GLM_THINKING_MODEL:
            raise ValueError("GLM thinking transport requires glm-5.3-flash")
        self.last_call = None
        attempts = 0
        url = f"{base_url.rstrip('/')}/chat/completions"
        payload = build_glm_thinking_payload(model, system_prompt, user_prompt)
        started_at = time.monotonic()

        def request_once() -> ParsedGLMResponse:
            nonlocal attempts
            attempts += 1
            return self._request_once(url=url, api_key=api_key, payload=payload)

        parsed = qa_call_with_retry(
            request_once,
            max_attempts=self._max_attempts,
            sleep=self._sleep,
        )
        self.last_call = ReaderCallMetadata(
            input_tokens=parsed.input_tokens,
            output_tokens=parsed.output_tokens,
            reasoning_tokens=parsed.reasoning_tokens,
            total_tokens=parsed.total_tokens,
            latency_seconds=time.monotonic() - started_at,
            attempts=attempts,
            thinking_verified=parsed.thinking_verified,
        )
        return parsed.final_answer, parsed.total_tokens


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
    if report.get("benchmark") != OFFICIAL_BENCHMARK:
        raise ReplayInputError(f"source report benchmark must be {OFFICIAL_BENCHMARK}")
    if report.get("scorer_version") != OFFICIAL_SCORER_VERSION:
        raise ReplayInputError(f"source report scorer_version must be {OFFICIAL_SCORER_VERSION}")
    if report.get("answer_entity_scorer_version") != OFFICIAL_ANSWER_ENTITY_SCORER_VERSION:
        raise ReplayInputError(
            "source report answer_entity_scorer_version must be " f"{OFFICIAL_ANSWER_ENTITY_SCORER_VERSION}"
        )

    sample = report.get("sample")
    if not isinstance(sample, Mapping) or sample.get("id") != OFFICIAL_SAMPLE_ID:
        raise ReplayInputError(f"source report sample.id must be {OFFICIAL_SAMPLE_ID}")
    run = report.get("run")
    models = run.get("models") if isinstance(run, Mapping) else None
    if not isinstance(models, Mapping) or models.get("qa") != OFFICIAL_READER_MODEL:
        raise ReplayInputError(f"source report run.models.qa must be {OFFICIAL_READER_MODEL}")

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
            raise ReplayInputError(f"source report case {case_id} answerability must be supported or low_confidence")

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
