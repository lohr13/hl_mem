import json

from hl_mem.llm.types import LLMResponse
from hl_mem.workers.dedup_judge import DedupJudge


class _Client:
    model = "fake-model"

    def __init__(self, content=None) -> None:
        self.requests = []
        self.content = (
            content if content is not None else {"decision": "distinct", "confidence": 0.99, "reason": "different task"}
        )

    def complete(self, request):
        self.requests.append(request)
        return LLMResponse(
            content=self.content,
            finish_reason="stop",
            usage_total_tokens=10,
        )


def test_dedup_judge_receives_identity_and_valid_time_fields() -> None:
    client = _Client()
    claim = {
        "subject_entity_id": "hl_mem",
        "predicate": "uses",
        "value": "qwen",
        "canonical_slot": "choice.model",
        "canonical_attribute": "choice.model",
        "qualifiers": {"task": "embedding"},
        "valid_from": "2026-01-01T00:00:00+00:00",
        "valid_to": None,
    }

    decision = DedupJudge(client).judge(claim, {**claim, "qualifiers": {"task": "chat"}})
    payload = json.loads(client.requests[0].messages[1].content)
    prompt = client.requests[0].messages[0].content

    assert decision == ("distinct", 0.99, "different task")
    assert payload["left"]["canonical_slot"] == "choice.model"
    assert payload["left"]["canonical_attribute"] == "choice.model"
    assert payload["left"]["valid_from"] == "2026-01-01T00:00:00+00:00"
    assert payload["left"]["valid_to"] is None
    assert "配置键" in prompt
    assert "task" in prompt
    assert "版本" in prompt


def test_dedup_judge_degrades_empty_or_unknown_decision_to_uncertain() -> None:
    claim = {"subject_entity_id": "user", "predicate": "uses", "value": "SQLite"}

    empty = DedupJudge(_Client({"decision": "", "confidence": 0.9, "reason": "missing"})).judge(claim, claim)
    unknown = DedupJudge(_Client({"decision": "same", "confidence": 0.9, "reason": "unknown enum"})).judge(claim, claim)

    assert empty == ("uncertain", 0.0, "invalid_output:decision")
    assert unknown == ("uncertain", 0.0, "invalid_output:decision")


def test_dedup_judge_degrades_malformed_json_and_fields_to_uncertain() -> None:
    claim = {"subject_entity_id": "user", "predicate": "uses", "value": "SQLite"}
    malformed_client = _Client("not-json")

    malformed_json = DedupJudge(malformed_client).judge(claim, claim)
    invalid_confidence = DedupJudge(_Client({"decision": "equivalent", "confidence": "high", "reason": "same"})).judge(
        claim, claim
    )

    assert malformed_json == ("uncertain", 0.0, "invalid_output:json")
    assert invalid_confidence == ("uncertain", 0.0, "invalid_output:confidence")
    assert "decision、confidence、reason" in malformed_client.requests[0].messages[0].content
