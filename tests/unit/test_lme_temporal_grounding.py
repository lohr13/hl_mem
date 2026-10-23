from dataclasses import replace

import pytest

from evaluation.tools import run_longmemeval_benchmark as runner
from tests.unit.test_longmemeval_batching import _record


@pytest.mark.parametrize(
    "question",
    [
        "What major project milestone did I reach four weeks ago?",
        "Where did I travel most recently?",
        "What time did I wake up on Mondays before the schedule changed?",
    ],
)
def test_temporal_rules_do_not_depend_on_dataset_question_type(question):
    case = replace(runner.normalize_case(_record("arbitrary")), question=question)
    prompts = {
        runner._reader_system_prompt(replace(case, question_type=kind))
        for kind in (
            "multi-session",
            "knowledge-update",
            "temporal-reasoning",
            "unknown",
        )
    }
    assert len(prompts) == 1


def test_temporal_event_selection_precedes_coarse_date_arithmetic():
    case = runner.normalize_case(_record("arbitrary"))
    prompt = runner._reader_system_prompt(case)
    assert "first match the requested event, relation, and completion state" in prompt
    assert "occurrence time from the statement's valid time" in prompt
    assert "coarse relative periods" in prompt
    assert "unless the question explicitly requires an exact date or interval" in prompt
    assert "do not turn a plan into a completed event" in prompt
    assert "separate trips or transactions do not automatically replace each other" in prompt


def test_temporal_prompt_change_does_not_change_exact_question_time():
    case = runner.normalize_case(_record("arbitrary"))
    prompt = runner._build_reader_user_prompt(None, case, [])
    assert f"Current Date: {case.question_at}" in prompt
    assert "23:59:59" not in prompt
