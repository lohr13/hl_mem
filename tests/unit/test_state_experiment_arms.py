import copy
from functools import partial
from typing import Any

import pytest

from hl_mem.evaluation.state_experiment_arms import (
    apply_atomicity_gate,
    canonicalize_claim,
    make_projection_sample,
    preserve_atomic_claim,
    preserve_compact_claim,
    project_compact_claim,
    project_response,
)


def _claim(*, subject: str, value: str, kind: str = "fact") -> dict[str, Any]:
    return {
        "subject": subject,
        "value": value,
        "kind": kind,
        "confidence": 0.93,
        "notability": "medium",
        "evidence_quote": value,
        "source_event_indices": [0],
    }


def _response(*claims: dict[str, Any]) -> dict[str, Any]:
    return {"claims": list(claims), "should_memorize": bool(claims)}


def test_canonicalization_projects_version_coordinate_with_canonical_entity() -> None:
    projection = canonicalize_claim(_claim(subject=" HL_MEM 服务 ", value="HL_MEM 当前版本是 v0.30.0", kind="config"))

    assert projection == {
        "predicate": "配置",
        "canonical_subject": "hl_mem",
        "canonical_slot": "config.version",
        "coordinate_qualifiers": {},
        "coordinate": {
            "namespace": "default",
            "canonical_subject": "hl_mem",
            "canonical_slot": "config.version",
            "coordinate_qualifiers": {},
        },
        "state_context": "current",
        "reason_codes": ["state_slot:config.version"],
    }


def test_canonicalization_routes_non_version_state_and_builds_qualifiers() -> None:
    projection = canonicalize_claim(_claim(subject="HL_MEM 服务", value="生产环境的 API 服务当前 healthy"))

    assert projection["predicate"] == "状态"
    assert projection["canonical_slot"] == "state.service_health"
    assert projection["coordinate"] == {
        "namespace": "default",
        "canonical_subject": "hl_mem",
        "canonical_slot": "state.service_health",
        "coordinate_qualifiers": {"environment": "production", "service": "api"},
    }


@pytest.mark.parametrize(
    ("claim", "expected_coordinate"),
    [
        (
            _claim(subject="compound-00 的 API 服务", value="当前 healthy"),
            {
                "namespace": "default",
                "canonical_subject": "compound-00",
                "canonical_slot": "state.service_health",
                "coordinate_qualifiers": {"service": "api"},
            },
        ),
        (
            _claim(
                subject="compound-02",
                value="compound-02 的当前版本当前 v1.0",
                kind="config",
            ),
            {
                "namespace": "default",
                "canonical_subject": "compound-02",
                "canonical_slot": "config.version",
                "coordinate_qualifiers": {},
            },
        ),
    ],
)
def test_z1_routes_state_from_controlled_subject_and_value(
    claim: dict[str, Any],
    expected_coordinate: dict[str, Any],
) -> None:
    projection = canonicalize_claim(claim)

    assert projection["coordinate"] == expected_coordinate
    assert projection["state_context"] == "current"


@pytest.mark.parametrize(
    ("subject", "value"),
    [
        ("worker 进程", "当前 running"),
        ("worker 服务", "当前 healthy"),
        ("sync 任务", "当前 queued"),
        ("API 连接", "当前 reachable"),
    ],
)
def test_z1_does_not_invent_coordinate_when_subject_has_no_stable_owner(
    subject: str,
    value: str,
) -> None:
    projection = canonicalize_claim(_claim(subject=subject, value=value))

    assert projection["coordinate"] is None
    assert projection["state_context"] == "non_state"


@pytest.mark.parametrize(
    ("claim", "expected_qualifiers"),
    [
        (
            _claim(
                subject="service-07",
                value="service-07 的 API 连接 当前状态为 reachable",
            ),
            {"service": "api"},
        ),
        (
            _claim(
                subject="service-08",
                value="service-08 的 sync 任务 当前状态为 queued",
            ),
            {"job": "sync"},
        ),
    ],
)
def test_z2_builds_connectivity_and_job_qualifiers_from_surface_order(
    claim: dict[str, Any],
    expected_qualifiers: dict[str, str],
) -> None:
    projection = canonicalize_claim(claim)

    assert projection["coordinate_qualifiers"] == expected_qualifiers
    assert projection["coordinate"]["coordinate_qualifiers"] == expected_qualifiers


@pytest.mark.parametrize(
    ("claim", "expected_subject", "expected_qualifiers"),
    [
        (
            _claim(
                subject="counter-10 实例 node-a",
                value="当前版本是 v2.0",
                kind="config",
            ),
            "counter-10",
            {"instance": "node-a"},
        ),
        (
            _claim(
                subject="service-10 的 worker 进程",
                value="service-10 的 worker 进程 当前状态为 running",
            ),
            "service-10",
            {"process": "worker"},
        ),
        (
            _claim(
                subject="service-06 sync 任务",
                value="service-06 的 sync 任务 当前状态为 queued",
            ),
            "service-06",
            {"job": "sync"},
        ),
        (
            _claim(
                subject="compound-05 的 blue 部署",
                value="compound-05 的 blue 部署 当前 ready",
            ),
            "compound-05",
            {"deployment": "blue"},
        ),
    ],
)
def test_z3_separates_coordinate_bearing_subject_suffixes(
    claim: dict[str, Any],
    expected_subject: str,
    expected_qualifiers: dict[str, str],
) -> None:
    projection = canonicalize_claim(claim)

    assert projection["canonical_subject"] == expected_subject
    assert projection["coordinate"]["canonical_subject"] == expected_subject
    assert projection["coordinate_qualifiers"] == expected_qualifiers


def test_z4_repaired_endpoint_coordinates_share_lifecycle_bucket() -> None:
    recovered_from_none = [
        canonicalize_claim(
            _claim(
                subject="compound-00 的 API 服务",
                value=f"当前 {state}",
            )
        )["coordinate"]
        for state in ("healthy", "unhealthy")
    ]
    recovered_from_drift = [
        canonicalize_claim(
            _claim(
                subject=subject,
                value=value,
                kind="config",
            )
        )["coordinate"]
        for subject, value in (
            ("component-03", "component-03 当前版本是 v1.0"),
            ("component-03 服务", "component-03 服务 当前版本是 v1.1"),
            ("component-03 的 API 服务", "component-03 的 API 服务 当前版本是 v1.2"),
        )
    ]

    assert recovered_from_none[0] is not None
    assert recovered_from_none[0] == recovered_from_none[1]
    assert recovered_from_drift[0] is not None
    assert recovered_from_drift == [recovered_from_drift[0]] * 3


def test_predicate_drift_does_not_change_state_coordinate() -> None:
    config_projection = canonicalize_claim(_claim(subject="HL_MEM", value="HL_MEM 当前版本为 v0.30.0", kind="config"))
    fact_projection = canonicalize_claim(_claim(subject="HL_MEM", value="HL_MEM 当前版本为 v0.30.0", kind="fact"))

    assert config_projection["predicate"] == "配置"
    assert fact_projection["predicate"] == "事实"
    assert config_projection["coordinate"] == fact_projection["coordinate"]


def test_subject_drift_strips_generic_service_surfaces_from_coordinate() -> None:
    coordinates = [
        canonicalize_claim(_claim(subject=subject, value="component-01 当前版本为 v1.2", kind="config"))["coordinate"]
        for subject in ("component-01", "component-01 服务", "component-01 的 API 服务")
    ]

    assert (
        coordinates
        == [
            {
                "namespace": "default",
                "canonical_subject": "component-01",
                "canonical_slot": "config.version",
                "coordinate_qualifiers": {},
            }
        ]
        * 3
    )


@pytest.mark.parametrize(
    ("subject", "alias_subject", "value"),
    [
        ("orion-core", "orion-core 服务", "orion-core 服务 当前版本是 v4.2"),
        ("orion-core", "orion-core 的 API 服务", "orion-core 的 API 服务 当前版本是 v4.2"),
        ("lumen-stack", "lumen-stack service", "lumen-stack service current version: v3.1"),
        ("lumen-stack", "lumen-stack API service", "lumen-stack API service current version: v3.1"),
    ],
)
def test_z5_version_owner_alias_is_invariant_to_subject_or_value_position(
    subject: str,
    alias_subject: str,
    value: str,
) -> None:
    value_alias = canonicalize_claim(_claim(subject=subject, value=value, kind="config"))["coordinate"]
    subject_alias = canonicalize_claim(_claim(subject=alias_subject, value=value, kind="config"))["coordinate"]

    assert (
        value_alias
        == subject_alias
        == {
            "namespace": "default",
            "canonical_subject": subject,
            "canonical_slot": "config.version",
            "coordinate_qualifiers": {},
        }
    )


@pytest.mark.parametrize(
    ("value", "expected_service"),
    [
        ("payments 服务 当前版本是 v2.0", "payments"),
        ("orion-core 的 billing 服务 当前版本是 v2.0", "billing"),
        ("orion-core 服务与 billing 服务 当前版本是 v2.0", "orion-core"),
    ],
)
def test_z5_version_value_alias_does_not_swallow_distinct_service(
    value: str,
    expected_service: str,
) -> None:
    projection = canonicalize_claim(_claim(subject="orion-core", value=value, kind="config"))

    assert projection["coordinate_qualifiers"] == {"service": expected_service}


def test_z5_version_value_alias_preserves_independent_qualifiers() -> None:
    projection = canonicalize_claim(
        _claim(
            subject="orion-core",
            value=("orion-core 的 API 服务在生产环境 blue 部署实例 node-a 的 Linux " "当前版本是 v4.2"),
            kind="config",
        )
    )

    assert projection["coordinate_qualifiers"] == {
        "deployment": "blue",
        "environment": "production",
        "instance": "node-a",
        "platform": "linux",
    }


def test_coordinate_qualifiers_keep_instances_on_separate_coordinates() -> None:
    node_a = canonicalize_claim(_claim(subject="Gateway", value="Gateway 实例 node-a 当前版本是 v3.1", kind="config"))
    node_b = canonicalize_claim(_claim(subject="Gateway", value="Gateway 实例 node-b 当前版本是 v3.1", kind="config"))

    assert node_a["coordinate_qualifiers"] == {"instance": "node-a"}
    assert node_b["coordinate_qualifiers"] == {"instance": "node-b"}
    assert node_a["coordinate"] != node_b["coordinate"]


@pytest.mark.parametrize(
    ("kind", "value"),
    [
        ("plan", "计划下周把 API 升级到 v2.0"),
        ("fact", "文档写道：API 当前版本是 v1.0"),
        ("fact", "API 并不是 running"),
    ],
)
def test_counterexamples_do_not_project_current_state(kind: str, value: str) -> None:
    projection = canonicalize_claim(_claim(subject="Gateway", value=value, kind=kind))

    assert projection["canonical_slot"] is None
    assert projection["coordinate"] is None
    assert projection["state_context"] == "non_asserted"
    assert "non_current_context" in projection["reason_codes"]


def test_historical_snapshot_keeps_coordinate_but_is_not_current() -> None:
    projection = canonicalize_claim(_claim(subject="Gateway", value="2025 年时 Gateway 当前版本是 v1.0"))

    assert projection["coordinate"] == {
        "namespace": "default",
        "canonical_subject": "gateway",
        "canonical_slot": "config.version",
        "coordinate_qualifiers": {},
    }
    assert projection["state_context"] == "historical"
    assert projection["reason_codes"] == ["state_slot:config.version", "historical_context"]


def test_coordinate_serialization_decodes_frozen_qualifier_json_once() -> None:
    projection = canonicalize_claim(_claim(subject="Gateway", value="生产环境的 API 服务当前 healthy"))

    qualifiers = projection["coordinate"]["coordinate_qualifiers"]
    assert qualifiers == {"environment": "production", "service": "api"}
    assert not any(isinstance(value, str) and value.startswith('"') for value in qualifiers.values())


def test_atomicity_gate_passes_one_state_assertion_without_mutating_input() -> None:
    claim = _claim(subject="Gateway", value="API 服务当前 healthy")
    original = copy.deepcopy(claim)

    result = apply_atomicity_gate(claim, strategy="split")

    assert result == {"decision": "atomic", "detected_state_assertions": 1, "claims": [claim]}
    assert claim == original


@pytest.mark.parametrize(
    ("value", "detected", "fragments"),
    [
        (
            "API 服务现在 healthy；worker 进程已经 stopped",
            2,
            ["API 服务现在 healthy", "worker 进程已经 stopped"],
        ),
        (
            "API service is healthy and worker process is stopped. sync job is completed",
            3,
            ["API service is healthy", "worker process is stopped", "sync job is completed"],
        ),
        (
            "API 服务现在 healthy；架构仍使用 SQLite；worker 进程已经 stopped",
            2,
            ["API 服务现在 healthy", "架构仍使用 SQLite", "worker 进程已经 stopped"],
        ),
    ],
    ids=("zh-source-order", "english-boundaries", "mixed-non-state"),
)
def test_atomicity_split_preserves_boundaries_order_and_grounding(
    value: str,
    detected: int,
    fragments: list[str],
) -> None:
    result = apply_atomicity_gate(_claim(subject="Gateway", value=value), strategy="split")

    assert result["decision"] == "split"
    assert result["detected_state_assertions"] == detected
    assert [item["value"] for item in result["claims"]] == fragments
    assert [item["evidence_quote"] for item in result["claims"]] == fragments


def test_atomicity_split_rejects_when_value_fragments_are_not_grounded_in_quote() -> None:
    claim = _claim(
        subject="Gateway",
        value="API 服务现在 healthy；worker 进程已经 stopped",
    )
    claim["evidence_quote"] = "原文只说两个组件发生了变化"

    result = apply_atomicity_gate(claim, strategy="split")

    assert result == {
        "decision": "rejected",
        "detected_state_assertions": 2,
        "claims": [],
        "reason": "ungrounded_split",
    }


def test_atomicity_gate_can_reject_multiple_state_assertions() -> None:
    claim = _claim(subject="Gateway", value="API 服务现在 healthy，worker 进程已经 stopped")

    result = apply_atomicity_gate(claim, strategy="reject")

    assert result == {"decision": "rejected", "detected_state_assertions": 2, "claims": []}


def test_project_response_composes_passthrough_and_structured_projection_policies() -> None:
    raw = _response(_claim(subject="Gateway", value="API 服务现在 healthy；worker 进程已经 stopped"))
    sample = {"sample_id": "compound-001", "raw_llm_json": raw}

    passthrough = project_response(
        sample,
        projector=preserve_compact_claim,
        atomicity_policy=preserve_atomic_claim,
    )
    first = project_response(
        sample,
        projector=partial(project_compact_claim, namespace="default"),
        atomicity_policy=partial(apply_atomicity_gate, strategy="split"),
    )
    second = project_response(
        copy.deepcopy(sample),
        projector=partial(project_compact_claim, namespace="default"),
        atomicity_policy=partial(apply_atomicity_gate, strategy="split"),
    )

    assert passthrough == {
        "sample_id": "compound-001",
        "raw_llm_json": raw,
        "input_claim_count": 1,
        "output_claim_count": 1,
        "claims": raw["claims"],
        "rejections": [],
    }
    assert first == second
    assert [item["assertion_id"] for item in first["claims"]] == [
        "compound-001:c0:a0",
        "compound-001:c0:a1",
    ]
    assert [item["projection"]["canonical_slot"] for item in first["claims"]] == [
        "state.service_health",
        "state.process",
    ]


def test_corpus_bundle_and_frozen_response_have_a_generic_projection_contract() -> None:
    bundle = {
        "bundle_id": "dev-version-001",
        "events": [{"event_index": 0, "content": {"text": "Gateway 当前版本是 v2.0"}}],
    }
    raw = _response(_claim(subject="Gateway", value="Gateway 当前版本是 v2.0", kind="config"))

    sample = make_projection_sample(bundle, raw)
    result = project_response(
        sample,
        projector=partial(project_compact_claim, namespace="default"),
        atomicity_policy=partial(apply_atomicity_gate, strategy="split"),
        metadata={"label": "B1 historical replay"},
    )

    assert sample == {"sample_id": "dev-version-001", "raw_llm_json": raw}
    assert result["sample_id"] == bundle["bundle_id"]
    assert result["claims"][0]["assertion_id"] == "dev-version-001:c0:a0"
    assert result["metadata"] == {"label": "B1 historical replay"}


@pytest.mark.parametrize(
    "bundle",
    [
        {"bundle_id": "", "events": []},
        {"bundle_id": "dev-001"},
        {"bundle_id": "dev-001", "events": "not-an-array"},
    ],
)
def test_projection_sample_adapter_rejects_malformed_corpus_bundles(bundle: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="corpus bundle"):
        make_projection_sample(bundle, _response())


@pytest.mark.parametrize(
    "mutate",
    [
        lambda claim: claim.pop("evidence_quote"),
        lambda claim: claim.update({"extra": "not-compact-v1"}),
        lambda claim: claim.update({"kind": "state"}),
        lambda claim: claim.update({"kind": ["fact"]}),
        lambda claim: claim.update({"confidence": True}),
        lambda claim: claim.update({"notability": "urgent"}),
        lambda claim: claim.update({"notability": ["medium"]}),
        lambda claim: claim.update({"source_event_indices": []}),
        lambda claim: claim.update({"subject": "x" * 201}),
    ],
)
def test_project_response_rejects_malformed_seven_field_claims(mutate: Any) -> None:
    claim = _claim(subject="Gateway", value="API 服务当前 healthy")
    mutate(claim)

    with pytest.raises(ValueError, match="compact seven-field contract"):
        project_response(
            {"sample_id": "bad-001", "raw_llm_json": _response(claim)},
            projector=partial(project_compact_claim, namespace="default"),
            atomicity_policy=partial(apply_atomicity_gate, strategy="split"),
        )


def test_project_response_rejects_more_than_twenty_compact_claims() -> None:
    claim = _claim(subject="Gateway", value="API 服务当前 healthy")

    with pytest.raises(ValueError, match="at most 20 claims"):
        project_response(
            {"sample_id": "dense-001", "raw_llm_json": _response(*[claim] * 21)},
            projector=partial(project_compact_claim, namespace="default"),
            atomicity_policy=partial(apply_atomicity_gate, strategy="split"),
        )
