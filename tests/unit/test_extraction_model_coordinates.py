import json

import pytest

from hl_mem.ingest.chunking import ChunkingPolicy
from hl_mem.ingest.llm_extractor import LLMExtractor
from hl_mem.llm.types import LLMRequest, LLMResponse


class _Client:
    class _Provider:
        name = "fake"

    provider = _Provider()
    model = "test-model"

    def __init__(self, payload: str) -> None:
        self.payload = payload

    def complete(self, request: LLMRequest) -> LLMResponse:
        del request
        return LLMResponse(self.payload, "stop", 1)


def _extract(*, subject: str, value: str, evidence: str):
    payload = json.dumps(
        {
            "claims": [
                {
                    "subject": subject,
                    "value": value,
                    "kind": "choice",
                    "confidence": 1.0,
                    "notability": "high",
                    "evidence_quote": evidence,
                    "source_event_indices": [0],
                }
            ],
            "should_memorize": True,
        },
        ensure_ascii=False,
    )
    return LLMExtractor(_Client(payload), ChunkingPolicy(10_000, 0, 2)).extract(value)[0]


def test_current_hl_mem_extraction_model_gets_stable_coordinate() -> None:
    statement = "hl-mem 本地提取当前实际使用 glm-5.3-flash"

    claim = _extract(subject="hl-mem 本地提取", value=statement, evidence=statement)

    assert claim.subject == "hl_mem"
    assert claim.canonical_attribute == "choice.model"
    assert claim.canonical_slot == "choice.model"
    assert claim.qualifiers == {"task": "extraction", "state_change": True}


def test_subject_task_without_evidence_does_not_create_slot() -> None:
    claim = _extract(
        subject="hl-mem 本地提取",
        value="hl-mem 当前使用 glm-5.3-flash",
        evidence="hl-mem 当前使用 glm-5.3-flash",
    )

    assert claim.subject == "hl-mem 本地提取"
    assert claim.canonical_attribute == "choice.model"
    assert claim.canonical_slot is None
    assert claim.qualifiers == {}


def test_non_hl_mem_extraction_subject_keeps_named_subject() -> None:
    statement = "Acme 提取任务当前使用 glm-5.3-flash"

    claim = _extract(subject="Acme 提取任务", value=statement, evidence=statement)

    assert claim.subject == "Acme 提取任务"
    assert claim.canonical_slot == "choice.model"
    assert claim.qualifiers == {"task": "extraction", "state_change": True}


def test_multiple_model_tasks_fail_closed() -> None:
    statement = "HL-Mem 的提取和 judge 当前都使用 glm-5.3-flash"

    claim = _extract(subject="HL-Mem", value=statement, evidence=statement)

    assert claim.subject == "hl_mem"
    assert claim.canonical_attribute == "choice.model"
    assert claim.canonical_slot is None
    assert claim.qualifiers == {}


@pytest.mark.parametrize(
    ("subject", "statement", "task"),
    [
        (
            "HL-Mem answering model",
            "HL-Mem answering model currently uses glm-5.3-flash",
            "answering",
        ),
        (
            "HL-Mem embedding model",
            "HL-Mem embedding model currently uses text-embedding-v4",
            "embedding",
        ),
        (
            "HL-Mem reranker",
            "HL-Mem reranker currently uses qwen3-reranker",
            "reranker",
        ),
    ],
)
def test_hl_mem_model_tasks_get_distinct_typed_coordinates(
    subject: str,
    statement: str,
    task: str,
) -> None:
    claim = _extract(subject=subject, value=statement, evidence=statement)

    assert claim.subject == "hl_mem"
    assert claim.canonical_attribute == "choice.model"
    assert claim.canonical_slot == "choice.model"
    assert claim.qualifiers == {"task": task, "state_change": True}


def test_answering_and_extraction_meanings_fail_closed() -> None:
    statement = "HL-Mem extraction and answering models currently use glm-5.3-flash"

    claim = _extract(subject="HL-Mem model", value=statement, evidence=statement)

    assert claim.canonical_attribute == "choice.model"
    assert claim.canonical_slot is None
    assert claim.qualifiers == {}


def test_arbitrary_task_decorated_subject_is_not_normalized() -> None:
    statement = "Acme answering model currently uses glm-5.3-flash"

    claim = _extract(subject="Acme answering model", value=statement, evidence=statement)

    assert claim.subject == "Acme answering model"
    assert claim.canonical_slot == "choice.model"
    assert claim.qualifiers == {"task": "answering", "state_change": True}


@pytest.mark.parametrize("model", ["text-embedding-v4", "qwen3-reranker"])
def test_model_identifier_alone_does_not_prove_the_operational_task(model: str) -> None:
    statement = f"HL-Mem currently uses {model}"

    claim = _extract(subject="HL-Mem", value=statement, evidence=statement)

    assert claim.subject == "hl_mem"
    assert claim.canonical_attribute == "choice.model"
    assert claim.canonical_slot is None
    assert claim.qualifiers == {}
