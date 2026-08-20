from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from evaluation.v0291_behavioral.agent import (
    AgentTraceGenerator,
    AgentTraceInvalid,
    ModelCallResult,
    build_blind_agent_input,
    input_sha256,
)
from evaluation.v0291_behavioral.manifest import (
    expand_behavioral_samples,
    load_behavioral_manifest,
)
from evaluation.v0291_behavioral.packet import materialize_behavioral_arms

ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = ROOT / "tests/fixtures/v0291_freshness_behavioral.json"


class FakeTransport:
    def __init__(self, outputs: list[dict[str, object]]) -> None:
        self.outputs = list(outputs)
        self.calls: list[dict[str, object]] = []

    async def complete(self, **kwargs: object) -> ModelCallResult:
        self.calls.append(dict(kwargs))
        output = self.outputs.pop(0)
        return ModelCallResult(
            output=output,
            request_id=f"request-{len(self.calls)}",
            input_tokens=100,
            output_tokens=20,
        )


def _fixture(cohort: str = "incident") -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    manifest = load_behavioral_manifest(MANIFEST_PATH)
    samples = expand_behavioral_samples(manifest)
    sample = next(item for item in samples if item["cohort"] == cohort)
    assignment = next(
        item
        for item in materialize_behavioral_arms(manifest, samples)
        if item["opaque_sample_id"] == sample["opaque_sample_id"] and item["arm_name"] == "echo_off__freshness_off"
    )
    return manifest, sample, assignment


def test_blind_agent_input_omits_arm_cohort_gold_and_current_truth() -> None:
    manifest, sample, assignment = _fixture()
    blind = build_blind_agent_input(manifest, sample, assignment)
    serialized = json.dumps(blind["model_input"], ensure_ascii=False, sort_keys=True)

    assert set(blind) == {
        "model_input",
        "model_snapshot",
        "system_prompt_sha256",
        "tool_fixture_sha256",
    }
    assert blind["model_snapshot"] == "qwen3.7-plus-2026-05-26"
    assert len(blind["system_prompt_sha256"]) == 64
    assert sample["current_truth"] not in serialized
    assert sample["cohort"] not in serialized
    assert assignment["arm_name"] not in serialized
    assert "gold_source" not in serialized
    assert "expected_applicability" not in serialized
    assert blind["model_input"]["context_packet_text"] == assignment["context_packet_text"]


def test_exact_input_hash_changes_for_each_behaviorally_relevant_input() -> None:
    manifest, sample, assignment = _fixture()
    original = build_blind_agent_input(manifest, sample, assignment)
    mutations = []
    for path, replacement in (
        (("model_input", "user_prompt"), "different prompt"),
        (("model_input", "context_packet_text"), "{}"),
        (("model_input", "available_tools"), []),
        (("model_snapshot",), "different-snapshot"),
        (("system_prompt_sha256",), "f" * 64),
        (("tool_fixture_sha256",), "0" * 64),
    ):
        mutated = copy.deepcopy(original)
        target = mutated
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = replacement
        mutations.append(mutated)

    assert len({input_sha256(original), *(input_sha256(item) for item in mutations)}) == 7


@pytest.mark.asyncio
async def test_agent_executes_fixture_tool_and_records_complete_ordered_trace() -> None:
    manifest, sample, assignment = _fixture()
    transport = FakeTransport(
        [
            {
                "schema_version": "hl-mem-agent-plan-v1",
                "sample_id": sample["opaque_sample_id"],
                "assistant_answer": "先确认实际安装来源。",
                "tool_plan": [
                    {
                        "tool_name": "inspect_python_install",
                        "reason": "确认运行包来源",
                        "before_harmful_action": True,
                    }
                ],
                "tool_calls": [
                    {
                        "tool_name": "inspect_python_install",
                        "arguments_json": '{"package":"hl-mem"}',
                    }
                ],
                "final_answer": "等待检查结果后再决定。",
            },
            {
                "schema_version": "hl-mem-agent-final-v1",
                "sample_id": sample["opaque_sample_id"],
                "final_answer": "检查显示运行包来自 PyPI，应升级安装包而不是修改 checkout。",
            },
        ]
    )

    result = await AgentTraceGenerator(transport).generate(
        build_blind_agent_input(manifest, sample, assignment), sample
    )

    assert [event["source"] for event in result.trace] == [
        "assistant_answer",
        "tool_plan",
        "tool_call",
        "tool_result",
        "final_answer",
    ]
    assert result.trace[3]["content"]["install_source"] == "pypi"
    assert len(result.call_records) == 2
    assert result.call_records[0]["usage"] == {"input_tokens": 100, "output_tokens": 20}
    assert transport.calls[1]["user_payload"]["tool_results"][0]["result"]["install_source"] == "pypi"


@pytest.mark.asyncio
async def test_agent_without_tool_call_finishes_in_one_model_turn() -> None:
    manifest, sample, assignment = _fixture("stable_negative")
    transport = FakeTransport(
        [
            {
                "schema_version": "hl-mem-agent-plan-v1",
                "sample_id": sample["opaque_sample_id"],
                "assistant_answer": "会采用已保存的语言偏好。",
                "tool_plan": [],
                "tool_calls": [],
                "final_answer": "以下使用简体中文说明部署步骤。",
            }
        ]
    )

    result = await AgentTraceGenerator(transport).generate(
        build_blind_agent_input(manifest, sample, assignment), sample
    )

    assert [event["source"] for event in result.trace] == [
        "assistant_answer",
        "tool_plan",
        "final_answer",
    ]
    assert len(result.call_records) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_name,arguments_json",
    [
        ("write_runtime_config", '{"setting":"api.port","value":9000}'),
        ("inspect_python_install", '{"package":"another-package"}'),
    ],
)
async def test_agent_fails_closed_for_unknown_tool_or_nonfixture_arguments(tool_name: str, arguments_json: str) -> None:
    manifest, sample, assignment = _fixture()
    transport = FakeTransport(
        [
            {
                "schema_version": "hl-mem-agent-plan-v1",
                "sample_id": sample["opaque_sample_id"],
                "assistant_answer": "检查。",
                "tool_plan": [],
                "tool_calls": [{"tool_name": tool_name, "arguments_json": arguments_json}],
                "final_answer": "继续。",
            }
        ]
    )

    with pytest.raises(AgentTraceInvalid, match="tool"):
        await AgentTraceGenerator(transport).generate(build_blind_agent_input(manifest, sample, assignment), sample)
