"""Validate frozen Chinese E2E reports and reconstruct reader replay cases."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import Field, asdict, dataclass, replace
from datetime import datetime, timezone
from importlib import import_module
from pathlib import Path
from typing import Any, ClassVar, Iterator, Protocol, TypeAlias

import httpx

# These evaluation helpers live below namespace-package roots that mypy otherwise
# discovers twice when this file is checked by path (``tools`` and
# ``evaluation.tools``). Runtime lookup preserves the existing public seam while
# keeping the task's exact standalone mypy command scoped to this module.
_chinese_e2e = import_module("tests.eval.chinese_e2e")
_memdaily_benchmark = import_module("evaluation.tools.run_memdaily_benchmark")
_qa_client = import_module("evaluation.tools.longmemeval.qa_client")
_http_utils = import_module("hl_mem.http_utils")
_secrets = import_module("hl_mem.config.secrets")
load_sample_manifest = _chinese_e2e.load_sample_manifest
load_sampled_inputs = _chinese_e2e.load_sampled_inputs
build_perltqa_ingest_trajectory = _chinese_e2e.build_perltqa_ingest_trajectory
build_perltqa_question_trajectory = _chinese_e2e.build_perltqa_question_trajectory
score_answer = _chinese_e2e.score_answer
score_answer_entity_packet = _chinese_e2e.score_answer_entity_packet
aggregate_results = _chinese_e2e.aggregate_results
_run_qa = _memdaily_benchmark._run_qa
qa_call_with_retry = _qa_client.qa_call_with_retry
retry_after_seconds = _http_utils.retry_after_seconds
read_secret_values = _secrets.read_secret_values
is_placeholder_secret = _secrets.is_placeholder_secret

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


class ThinkingTransport(Protocol):
    last_call: ReaderCallMetadata | None

    def __call__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        system_prompt: str,
        user_prompt: str,
    ) -> tuple[str, int]: ...


EXPECTED_CASE_COUNT = 40
OFFICIAL_BENCHMARK = "chinese_e2e"
OFFICIAL_SCORER_VERSION = "deterministic-rubric-v2"
OFFICIAL_ANSWER_ENTITY_SCORER_VERSION = "answer-entity-packet-v1"
OFFICIAL_SAMPLE_ID = "zh-e2e-v3"
OFFICIAL_READER_MODEL = "qwen3.7-plus"
SUPPORTED_ANSWERABILITY = frozenset({"supported", "low_confidence"})
GLM_THINKING_MODEL = "glm-5.3-flash"
GLM_MAX_ATTEMPTS = 3
DEFAULT_MANIFEST = Path("tests/eval/fixtures/chinese_e2e_sample.json")
DEFAULT_SOURCES = {
    "qwen37": Path("var/eval/v114/candidate/full40/qwen37/run1/report.json"),
    "glm53": Path("var/eval/v114/candidate/full40/glm53/run1/report.json"),
    "qwen38-27b": Path("var/eval/v114/candidate/full40/qwen38-27b/recovery1/report.json"),
}
DEFAULT_OUTPUT_ROOT = Path("var/eval/v114/cross_reader/glm53-thinking")
DEFAULT_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"
MODEL = GLM_THINKING_MODEL
CANARY_CASE_ID = "perltqa:23d905b73c57:dialogues:836f6182a0a9"
ARM_LABELS = ("qwen37", "glm53", "qwen38-27b")
REPLAY_SCHEMA_VERSION = 1
READER_PROMPT_VERSION = "memdaily-qa-prompt-v1"
THINKING = {"type": "enabled"}
ORIGINAL_QWEN_READER_IDENTITY = {
    "model": OFFICIAL_READER_MODEL,
    "enable_thinking": True,
    "thinking_budget": 2048,
    "answer_budget": 512,
}
_MISSING = object()
_SAFE_RESPONSE_ERRORS = frozenset(
    {
        "GLM response must be a JSON object",
        "GLM response is missing a final answer",
        "GLM response contains invalid token usage",
    }
)


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
class _SanitizedFailure:
    kind: str
    message: str
    status_code: int | None = None


@dataclass(frozen=True)
class _TrajectoryCase:
    dataset: str
    slice_name: str
    trajectory: MemDailyTrajectory
    answer_anchors: tuple[str, ...]
    accepted_rubrics: AcceptedRubrics
    answer_entity_gold: AnswerEntityGold


@dataclass(frozen=True)
class _QASettings:
    llm_api_key: str | None = None
    llm_base_url: str | None = None


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
    selected_value: int | None = None
    for field_name in field_names:
        if field_name not in usage:
            continue
        raw_value = usage[field_name]
        if isinstance(raw_value, bool) or not isinstance(raw_value, int) or raw_value < 0:
            raise ValueError("GLM response contains invalid token usage")
        if selected_value is None:
            selected_value = raw_value
    return selected_value if selected_value is not None else 0


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

    raw_usage = envelope.get("usage", _MISSING)
    if raw_usage is _MISSING:
        usage: Mapping[object, object] = {}
    elif isinstance(raw_usage, Mapping):
        usage = raw_usage
    else:
        raise ValueError("GLM response contains invalid token usage")
    input_tokens = _token_count(usage, "prompt_tokens", "input_tokens")
    output_tokens = _token_count(usage, "completion_tokens", "output_tokens")
    detail_variants: list[Mapping[object, object]] = []
    for field_name in ("completion_tokens_details", "output_tokens_details"):
        if field_name not in usage:
            continue
        raw_details = usage[field_name]
        if not isinstance(raw_details, Mapping):
            raise ValueError("GLM response contains invalid token usage")
        detail_variants.append(raw_details)
    reasoning_tokens = 0
    reasoning_selected = False
    for details in detail_variants:
        variant_reasoning_tokens = _token_count(details, "reasoning_tokens")
        if not reasoning_selected and "reasoning_tokens" in details:
            reasoning_tokens = variant_reasoning_tokens
            reasoning_selected = True
    total_tokens = _token_count(usage, "total_tokens") or input_tokens + output_tokens

    return ParsedGLMResponse(
        final_answer=final_answer,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        reasoning_tokens=reasoning_tokens,
        total_tokens=total_tokens,
        thinking_verified=has_reasoning_content or reasoning_tokens > 0,
    )


def _canonical_retry_after(value: str | None) -> str | None:
    if value is None:
        return None
    seconds = retry_after_seconds(value)
    if seconds is None or not math.isfinite(seconds):
        return None
    return f"{seconds:g}"


def _safe_status_error(
    status_code: int,
    *,
    retry_after: str | None = None,
) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://provider.invalid/chat/completions")
    headers = {"Retry-After": retry_after} if retry_after is not None else None
    response = httpx.Response(status_code, request=request, headers=headers)
    return httpx.HTTPStatusError(
        f"GLM request failed with HTTP {status_code}",
        request=request,
        response=response,
    )


def _describe_failure(error: Exception) -> _SanitizedFailure:
    if isinstance(error, httpx.HTTPStatusError):
        return _SanitizedFailure(
            kind="status",
            message=f"GLM request failed with HTTP {error.response.status_code}",
            status_code=error.response.status_code,
        )
    if isinstance(error, httpx.ReadTimeout):
        return _SanitizedFailure(kind="read_timeout", message="GLM request timed out")
    if isinstance(error, httpx.ConnectTimeout):
        return _SanitizedFailure(kind="connect_timeout", message="GLM connection timed out")
    if isinstance(error, httpx.HTTPError):
        return _SanitizedFailure(kind="http", message="GLM request failed")
    if isinstance(error, ValueError):
        message = str(error)
        if message not in _SAFE_RESPONSE_ERRORS:
            message = "GLM response is invalid"
        return _SanitizedFailure(kind="value", message=message)
    return _SanitizedFailure(kind="runtime", message="GLM request failed")


def _clear_exception_chain(error: BaseException) -> None:
    pending = [error]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)
        current.__traceback__ = None
        current.__cause__ = None
        current.__context__ = None


def _fresh_exception(failure: _SanitizedFailure) -> Exception:
    if failure.kind == "status" and failure.status_code is not None:
        return _safe_status_error(failure.status_code)
    if failure.kind == "read_timeout":
        return httpx.ReadTimeout(failure.message)
    if failure.kind == "connect_timeout":
        return httpx.ConnectTimeout(failure.message)
    if failure.kind == "http":
        return httpx.HTTPError(failure.message)
    if failure.kind == "value":
        return ValueError(failure.message)
    return RuntimeError(failure.message)


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
            failure = _safe_status_error(
                error.response.status_code,
                retry_after=_canonical_retry_after(error.response.headers.get("Retry-After")),
            )
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
        self.last_call = None
        if model != GLM_THINKING_MODEL:
            del api_key, base_url, model, system_prompt, user_prompt, self
            raise ValueError("GLM thinking transport requires glm-5.3-flash")
        attempts = 0
        url = f"{base_url.rstrip('/')}/chat/completions"
        payload = build_glm_thinking_payload(model, system_prompt, user_prompt)
        started_at = time.monotonic()

        def request_once() -> ParsedGLMResponse:
            nonlocal attempts
            attempts += 1
            return self._request_once(url=url, api_key=api_key, payload=payload)

        sanitized_failure: _SanitizedFailure | None = None
        try:
            parsed = qa_call_with_retry(
                request_once,
                max_attempts=self._max_attempts,
                sleep=self._sleep,
            )
        except Exception as error:
            sanitized_failure = _describe_failure(error)
            _clear_exception_chain(error)
        if sanitized_failure is not None:
            del api_key, base_url, model, system_prompt, user_prompt
            del payload, request_once, url, self
            raise _fresh_exception(sanitized_failure)
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


def qa_correct(qa: Mapping[str, Any]) -> bool:
    """Return the frozen paired-comparison correctness value."""
    return bool(qa.get("answer_correct", qa.get("exact_match", False)))


def classify_flip(before: bool, after: bool) -> str:
    """Name one paired correctness transition."""
    if before and after:
        return "unchanged_correct"
    if not before and not after:
        return "unchanged_wrong"
    if after:
        return "wrong_to_right"
    return "right_to_wrong"


@contextmanager
def _scoped_environment(values: Mapping[str, str]) -> Iterator[None]:
    previous = {name: os.environ.get(name) for name in values}
    try:
        os.environ.update(values)
        yield
    finally:
        for name, old_value in previous.items():
            if old_value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = old_value


def score_replayed_case(
    replay_case: ReplayCase,
    transport: ThinkingTransport,
    *,
    api_key: str = "injected-transport",
    base_url: str = DEFAULT_BASE_URL,
) -> dict[str, Any]:
    """Replay exactly the recorded evidence through the frozen QA prompt/scorers."""
    source_qa = replay_case.source_case.get("qa")
    if not isinstance(source_qa, Mapping):
        raise ReplayInputError(f"source case {replay_case.case_id} requires QA output")
    answerability = source_qa.get("answerability")
    if not isinstance(answerability, str):
        raise ReplayInputError(f"source case {replay_case.case_id} requires QA answerability")

    with _scoped_environment(
        {
            "HL_MEM_EVAL_QA_API_KEY": api_key,
            "HL_MEM_EVAL_QA_BASE_URL": base_url,
            "HL_MEM_EVAL_QA_MODEL": MODEL,
        }
    ):
        qa = _run_qa(
            None,
            replay_case.trajectory,
            replay_case.retrieved,
            _QASettings(),
            answerability=answerability,
            qa_chat=transport,
        )

    predicted = str(qa.get("predicted_answer") or "")
    if replay_case.dataset == "perltqa":
        qa.update(score_answer(predicted, replay_case.answer_anchors, replay_case.accepted_rubrics))
    metadata = transport.last_call
    if metadata is None:
        raise RuntimeError("reader transport returned no call metadata")

    result = deepcopy(replay_case.source_case)
    result.pop("messages", None)
    result["qa"] = qa
    result["answer_entity"] = score_answer_entity_packet(
        replay_case.retrieved,
        replay_case.answer_entity_gold,
        answer_text=predicted,
        k=5,
    )
    result["reader_call"] = asdict(metadata)
    return result


def _qa_pair_metrics(cases: Sequence[Mapping[str, Any]]) -> dict[str, float | None]:
    overall = aggregate_results(cases)["overall"]
    return {
        "qa_accuracy": overall["qa_accuracy"],
        "qa_f1": overall["qa_f1"],
    }


def summarize_arm(
    source_cases: Sequence[ReplayCase],
    replay_cases: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Build paired reader metrics without invoking any release gate."""
    original = _qa_pair_metrics([case.source_case for case in source_cases])
    replayed = _qa_pair_metrics(replay_cases)
    replayed_by_id = {str(case.get("case_id")): case for case in replay_cases}
    flips: dict[str, list[str]] = {
        "unchanged_correct": [],
        "unchanged_wrong": [],
        "wrong_to_right": [],
        "right_to_wrong": [],
    }
    for replay_case in source_cases:
        result = replayed_by_id.get(replay_case.case_id)
        if result is None or result.get("error"):
            continue
        before_qa = replay_case.source_case.get("qa")
        after_qa = result.get("qa")
        if not isinstance(before_qa, Mapping) or not isinstance(after_qa, Mapping):
            continue
        flips[classify_flip(qa_correct(before_qa), qa_correct(after_qa))].append(replay_case.case_id)

    reader_calls = [case["reader_call"] for case in replay_cases if isinstance(case.get("reader_call"), Mapping)]
    totals = {
        "input_tokens": sum(int(call.get("input_tokens", 0)) for call in reader_calls),
        "output_tokens": sum(int(call.get("output_tokens", 0)) for call in reader_calls),
        "reasoning_tokens": sum(int(call.get("reasoning_tokens", 0)) for call in reader_calls),
        "total_tokens": sum(int(call.get("total_tokens", 0)) for call in reader_calls),
        "latency_seconds": sum(float(call.get("latency_seconds", 0.0)) for call in reader_calls),
        "attempts": sum(int(call.get("attempts", 0)) for call in reader_calls),
    }
    successful_ids = {
        str(case.get("case_id"))
        for case in replay_cases
        if not case.get("error") and isinstance(case.get("qa"), Mapping)
    }
    paired_original = _qa_pair_metrics([case.source_case for case in source_cases if case.case_id in successful_ids])
    paired_replayed = _qa_pair_metrics([case for case in replay_cases if str(case.get("case_id")) in successful_ids])
    paired_delta: dict[str, float | None] = {}
    for key in ("qa_accuracy", "qa_f1"):
        original_value = paired_original[key]
        replayed_value = paired_replayed[key]
        paired_delta[key] = (
            None if original_value is None or replayed_value is None else replayed_value - original_value
        )
    return {
        "original": original,
        "replay": replayed,
        "paired_delta": paired_delta,
        "flips": flips,
        "reader_totals": totals,
        "failed_case_ids": [str(case.get("case_id")) for case in replay_cases if case.get("error")],
    }


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _reader_identity(base_url: str) -> dict[str, str]:
    return {"model": MODEL, "base_url": base_url.rstrip("/")}


def _versions() -> dict[str, str]:
    return {
        "prompt": READER_PROMPT_VERSION,
        "qa_scorer": OFFICIAL_SCORER_VERSION,
        "answer_entity_scorer": OFFICIAL_ANSWER_ENTITY_SCORER_VERSION,
    }


def _validate_sources(sources: Mapping[str, Sequence[ReplayCase]]) -> None:
    if set(sources) != set(ARM_LABELS):
        raise ReplayInputError(f"replay sources must contain exactly {list(ARM_LABELS)}")
    reference_ids: set[str] | None = None
    for label in ARM_LABELS:
        cases = sources[label]
        case_ids = [case.case_id for case in cases]
        if len(cases) != EXPECTED_CASE_COUNT or len(set(case_ids)) != EXPECTED_CASE_COUNT:
            raise ReplayInputError(f"source arm {label} must contain exactly {EXPECTED_CASE_COUNT} unique cases")
        if case_ids.count(CANARY_CASE_ID) != 1:
            raise ReplayInputError(f"source arm {label} must contain the canary case exactly once")
        if any(case.arm.label != label for case in cases):
            raise ReplayInputError(f"source arm {label} contains a mismatched arm label")
        actual_ids = set(case_ids)
        if reference_ids is None:
            reference_ids = actual_ids
        elif actual_ids != reference_ids:
            raise ReplayInputError("source arm case sets do not match")


def _checkpoint_path(output_root: Path, label: str) -> Path:
    return output_root / f"{label}.json"


def _load_checkpoint(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ReplayInputError(f"cannot read replay checkpoint {path}") from error
    if not isinstance(raw, Mapping):
        raise ReplayInputError(f"replay checkpoint must be an object: {path}")
    return {str(key): value for key, value in raw.items()}


def _expected_checkpoint_identity(
    label: str,
    cases: Sequence[ReplayCase],
    *,
    base_url: str,
) -> dict[str, Any]:
    arm = cases[0].arm
    return {
        "schema_version": REPLAY_SCHEMA_VERSION,
        "arm": label,
        "source": {"path": str(arm.report_path), "sha256": arm.report_sha256},
        "reader": _reader_identity(base_url),
        "thinking": dict(THINKING),
        "versions": _versions(),
        "case_ids": [case.case_id for case in cases],
    }


def _validate_checkpoint(
    checkpoint: Mapping[str, Any],
    expected: Mapping[str, Any],
) -> None:
    if checkpoint.get("schema_version") != expected["schema_version"] or checkpoint.get("arm") != expected["arm"]:
        raise ReplayInputError("replay checkpoint identity does not match")
    source = checkpoint.get("source")
    expected_source = expected["source"]
    if not isinstance(source, Mapping) or source.get("sha256") != expected_source["sha256"]:
        raise ReplayInputError("replay checkpoint source hash does not match")
    if checkpoint.get("reader") != expected["reader"]:
        raise ReplayInputError("replay checkpoint reader identity does not match")
    if checkpoint.get("thinking") != expected["thinking"]:
        raise ReplayInputError("replay checkpoint thinking object does not match")
    if checkpoint.get("versions") != expected["versions"]:
        raise ReplayInputError("replay checkpoint prompt/scorer versions do not match")
    if checkpoint.get("case_ids") != expected["case_ids"]:
        raise ReplayInputError("replay checkpoint case set does not match")

    completed = checkpoint.get("completed_case_ids")
    replay_cases = checkpoint.get("cases")
    if not isinstance(completed, list) or not isinstance(replay_cases, list):
        raise ReplayInputError("replay checkpoint completed cases are invalid")
    completed_ids = [str(item) for item in completed]
    result_ids = [str(item.get("case_id")) for item in replay_cases if isinstance(item, Mapping)]
    if (
        len(result_ids) != len(replay_cases)
        or completed_ids != result_ids
        or len(set(completed_ids)) != len(completed_ids)
        or not set(completed_ids).issubset(set(expected["case_ids"]))
    ):
        raise ReplayInputError("replay checkpoint completed case set is invalid")
    if checkpoint.get("metrics") != aggregate_results(replay_cases):
        raise ReplayInputError("replay checkpoint metrics do not match its cases")


def _arm_status(
    label: str,
    replay_cases: Sequence[Mapping[str, Any]],
    total_cases: int,
    overall_status: str,
) -> str:
    if overall_status in {"mode_unverified", "canary_failed"} and label == "qwen37":
        return overall_status
    if overall_status == "canary_completed":
        return "canary_completed" if label == "qwen37" else "pending"
    if len(replay_cases) == total_cases:
        return "completed_with_failures" if any(case.get("error") for case in replay_cases) else "completed"
    return "running" if replay_cases else "pending"


def _ranking(arms: Mapping[str, Mapping[str, Any]], metric_group: str) -> list[str]:
    def key(label: str) -> tuple[float, float, str]:
        metrics = arms[label][metric_group]
        accuracy = metrics.get("qa_accuracy")
        f1 = metrics.get("qa_f1")
        return (
            -(float(accuracy) if accuracy is not None else -math.inf),
            -(float(f1) if f1 is not None else -math.inf),
            label,
        )

    return sorted(ARM_LABELS, key=key)


def _build_summary(
    sources: Mapping[str, Sequence[ReplayCase]],
    states: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    base_url: str,
    status: str,
    execution_order: Sequence[str],
    started_at: str,
    updated_at: str,
) -> dict[str, Any]:
    arms = {label: summarize_arm(sources[label], states[label]) for label in ARM_LABELS}
    terminal = status != "running"
    return {
        "schema_version": REPLAY_SCHEMA_VERSION,
        "status": status,
        "started_at": started_at,
        "completed_at": updated_at if terminal else None,
        "updated_at": updated_at,
        "reader": _reader_identity(base_url),
        "thinking": dict(THINKING),
        "versions": _versions(),
        "original_qwen_reader": dict(ORIGINAL_QWEN_READER_IDENTITY),
        "source_hashes": {label: sources[label][0].arm.report_sha256 for label in ARM_LABELS},
        "case_sets": {label: [case.case_id for case in sources[label]] for label in ARM_LABELS},
        "logical_calls": len(execution_order),
        "execution_order": list(execution_order),
        "failed_case_ids": [
            f"{label}:{case.get('case_id')}" for label in ARM_LABELS for case in states[label] if case.get("error")
        ],
        "arms": arms,
        "original_ranking": _ranking(arms, "original"),
        "replay_ranking": _ranking(arms, "replay"),
    }


def _persist_replay(
    sources: Mapping[str, Sequence[ReplayCase]],
    states: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    output_root: Path,
    base_url: str,
    status: str,
    execution_order: Sequence[str],
    started_at: str,
) -> dict[str, Any]:
    updated_at = datetime.now(timezone.utc).isoformat()
    for label in ARM_LABELS:
        replay_cases = states[label]
        arm_status = _arm_status(label, replay_cases, len(sources[label]), status)
        checkpoint = {
            **_expected_checkpoint_identity(label, sources[label], base_url=base_url),
            "status": arm_status,
            "started_at": started_at,
            "updated_at": updated_at,
            "completed_at": (
                updated_at
                if arm_status
                in {"completed", "completed_with_failures", "canary_completed", "mode_unverified", "canary_failed"}
                else None
            ),
            "completed_case_ids": [str(case.get("case_id")) for case in replay_cases],
            "metrics": aggregate_results(replay_cases),
            "cases": list(replay_cases),
        }
        _write_json_atomic(_checkpoint_path(output_root, label), checkpoint)
    summary = _build_summary(
        sources,
        states,
        base_url=base_url,
        status=status,
        execution_order=execution_order,
        started_at=started_at,
        updated_at=updated_at,
    )
    _write_json_atomic(output_root / "summary.json", summary)
    return summary


def _resume_state(
    sources: Mapping[str, Sequence[ReplayCase]],
    *,
    output_root: Path,
    base_url: str,
) -> tuple[dict[str, list[dict[str, Any]]], list[str], str, dict[str, Any]]:
    checkpoints: dict[str, dict[str, Any]] = {}
    for label in ARM_LABELS:
        checkpoint = _load_checkpoint(_checkpoint_path(output_root, label))
        _validate_checkpoint(
            checkpoint,
            _expected_checkpoint_identity(label, sources[label], base_url=base_url),
        )
        checkpoints[label] = checkpoint
    summary = _load_checkpoint(output_root / "summary.json")
    if summary.get("schema_version") != REPLAY_SCHEMA_VERSION:
        raise ReplayInputError("replay summary identity does not match")
    if summary.get("reader") != _reader_identity(base_url):
        raise ReplayInputError("replay summary reader identity does not match")
    if summary.get("thinking") != THINKING:
        raise ReplayInputError("replay summary thinking object does not match")
    if summary.get("versions") != _versions():
        raise ReplayInputError("replay summary prompt/scorer versions do not match")
    expected_hashes = {label: sources[label][0].arm.report_sha256 for label in ARM_LABELS}
    if summary.get("source_hashes") != expected_hashes:
        raise ReplayInputError("replay summary source hashes do not match")
    expected_sets = {label: [case.case_id for case in sources[label]] for label in ARM_LABELS}
    if summary.get("case_sets") != expected_sets:
        raise ReplayInputError("replay summary case sets do not match")
    execution_order = summary.get("execution_order")
    if not isinstance(execution_order, list) or summary.get("logical_calls") != len(execution_order):
        raise ReplayInputError("replay summary execution order is invalid")

    states = {label: [dict(case) for case in checkpoints[label]["cases"]] for label in ARM_LABELS}
    completed_physical = {f"{label}:{case.get('case_id')}" for label in ARM_LABELS for case in states[label]}
    if len(completed_physical) != len(execution_order) or set(execution_order) != completed_physical:
        raise ReplayInputError("replay summary and arm checkpoints do not match")
    status = str(summary.get("status") or "")
    if status in {"mode_unverified", "canary_failed"}:
        raise ReplayInputError(f"cannot resume replay with status {status}")
    started_at = summary.get("started_at")
    if not isinstance(started_at, str) or not started_at:
        raise ReplayInputError("replay summary start timestamp is invalid")
    return states, [str(item) for item in execution_order], started_at, summary


def _failed_replay_case(replay_case: ReplayCase) -> dict[str, Any]:
    result = deepcopy(replay_case.source_case)
    result.pop("messages", None)
    result["qa"] = None
    result["answer_entity"] = None
    result["reader_call"] = None
    result["error"] = {"type": "reader_call_failed", "message": "reader replay failed"}
    return result


def run_replay(
    sources: Mapping[str, Sequence[ReplayCase]],
    transport: ThinkingTransport,
    *,
    output_root: Path,
    canary_only: bool,
    resume: bool,
    api_key: str = "injected-transport",
    base_url: str = DEFAULT_BASE_URL,
) -> dict[str, Any]:
    """Run or resume all three fixed reader arms with a physical Qwen canary first."""
    _validate_sources(sources)
    output_root = Path(output_root)
    if resume:
        states, execution_order, started_at, previous_summary = _resume_state(
            sources,
            output_root=output_root,
            base_url=base_url,
        )
    else:
        states = {label: [] for label in ARM_LABELS}
        execution_order = []
        started_at = datetime.now(timezone.utc).isoformat()
        previous_summary = {}

    completed = {(label, str(case.get("case_id"))) for label in ARM_LABELS for case in states[label]}
    qwen_cases = list(sources["qwen37"])
    canary = next(case for case in qwen_cases if case.case_id == CANARY_CASE_ID)
    ordered = [("qwen37", canary)]
    ordered.extend(("qwen37", case) for case in qwen_cases if case.case_id != CANARY_CASE_ID)
    ordered.extend((label, case) for label in ARM_LABELS[1:] for case in sources[label])
    remaining = [(label, case) for label, case in ordered if (label, case.case_id) not in completed]

    if not remaining or (canary_only and ("qwen37", CANARY_CASE_ID) in completed):
        return previous_summary

    summary: dict[str, Any] = previous_summary
    for remaining_index, (label, replay_case) in enumerate(remaining):
        physical_id = f"{label}:{replay_case.case_id}"
        execution_order.append(physical_id)
        failed = False
        try:
            result = score_replayed_case(
                replay_case,
                transport,
                api_key=api_key,
                base_url=base_url,
            )
        except Exception:
            result = _failed_replay_case(replay_case)
            failed = True
        states[label].append(result)

        status = "running"
        if label == "qwen37" and replay_case.case_id == CANARY_CASE_ID:
            reader_call = result.get("reader_call")
            verified = isinstance(reader_call, Mapping) and reader_call.get("thinking_verified") is True
            if failed:
                status = "canary_failed"
            elif not verified:
                status = "mode_unverified"
            elif canary_only:
                status = "canary_completed"
        if status == "running" and remaining_index == len(remaining) - 1:
            status = (
                "completed_with_failures"
                if any(case.get("error") for arm_cases in states.values() for case in arm_cases)
                else "completed"
            )
        summary = _persist_replay(
            sources,
            states,
            output_root=output_root,
            base_url=base_url,
            status=status,
            execution_order=execution_order,
            started_at=started_at,
        )
        if status in {"canary_completed", "mode_unverified", "canary_failed"}:
            return summary
    return summary


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Replay frozen Chinese QA evidence through GLM thinking mode")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument(
        "--qwen37-report",
        "--qwen37-source",
        dest="qwen37_report",
        type=Path,
        default=DEFAULT_SOURCES["qwen37"],
    )
    parser.add_argument(
        "--glm53-report",
        "--glm53-source",
        dest="glm53_report",
        type=Path,
        default=DEFAULT_SOURCES["glm53"],
    )
    parser.add_argument(
        "--qwen38-27b-report",
        "--qwen38-27b-source",
        dest="qwen38_27b_report",
        type=Path,
        default=DEFAULT_SOURCES["qwen38-27b"],
    )
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--canary-only", action="store_true")
    parser.add_argument("--resume", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        secrets = read_secret_values(args.env_file, {"LLM_API_KEY"}, os.environ)
        api_key = secrets.get("LLM_API_KEY")
        if is_placeholder_secret(api_key):
            raise ReplayInputError("LLM_API_KEY is missing or is a placeholder")
        sources = load_replay_cases(
            args.manifest,
            {
                "qwen37": args.qwen37_report,
                "glm53": args.glm53_report,
                "qwen38-27b": args.qwen38_27b_report,
            },
        )
        with httpx.Client() as client:
            summary = run_replay(
                sources,
                GLMThinkingTransport(client),
                output_root=args.output_root,
                canary_only=args.canary_only,
                resume=args.resume,
                api_key=api_key or "",
                base_url=args.base_url,
            )
    except (OSError, ReplayInputError, RuntimeError, httpx.HTTPError, ValueError) as error:
        print(f"reader replay failed: {error}", file=sys.stderr)
        return 2
    print(f"reader replay status: {summary['status']}")
    return 0 if summary["status"] in {"completed", "canary_completed"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
