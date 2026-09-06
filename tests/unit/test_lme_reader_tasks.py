"""Reader task policy follows question semantics, independently of dataset labels."""

from dataclasses import replace

import pytest

from evaluation.tools import run_longmemeval_benchmark as runner
from tests.unit.test_longmemeval_batching import _record


@pytest.mark.parametrize(
    ("question", "task"),
    [
        ("Can you recommend a quiet place for my next trip?", "recommendation"),
        ("Which route should I choose?", "recommendation"),
        ("Can you recommend a laptop and explain why it fits my needs?", "recommendation"),
        ("I liked the trip you suggested before. Can you recommend another?", "recommendation"),
        ("Can you recommend a trip like the one you suggested last year?", "recommendation"),
        ("What resources do I already have for learning Spanish?", "fact"),
        ("What options have I considered for my trip?", "fact"),
        ("请推荐符合我兴趣的活动。", "recommendation"),
        ("Could there be a reason my laptop runs faster now?", "explanation"),
        ("Why did my plants recover?", "explanation"),
        ("解释一下我最近效率提高的原因。", "explanation"),
        ("What did you recommend for my trip last time?", "fact"),
        ("What options did I pick?", "fact"),
        ("Which editor do I prefer?", "fact"),
        ("你上次推荐了什么？", "fact"),
    ],
)
def test_reader_task_is_semantic(question, task):
    case = runner.normalize_case(_record("arbitrary"))
    for label in ("single-session-preference", "multi-session", "unknown"):
        assert runner._reader_task(replace(case, question=question, question_type=label)) == task


def test_recommendation_does_not_inherit_closed_catalog_or_single_answer_rules():
    case = replace(runner.normalize_case(_record("arbitrary")), question="Can you suggest places to visit?")
    prompt = runner._reader_system_prompt(case)
    assert "allow only deterministic" not in prompt
    assert "Do not invent missing proper nouns" not in prompt
    assert "synthesize only the one" not in prompt
    assert "explicitly use the known preferences" in prompt
    assert "do not claim unverified amenities" in prompt
    assert "specific proper noun is absent" in prompt


def test_explanation_connects_evidence_without_asserting_speculative_causation():
    case = replace(runner.normalize_case(_record("arbitrary")), question="Why is my device faster now?")
    prompt = runner._reader_system_prompt(case)
    assert "connect the supported cause or change to the observed outcome" in prompt
    assert "plausible explanation from an established cause" in prompt
    assert "Do not invent missing proper nouns" in prompt
    assert "synthesize a recommendation" not in prompt
