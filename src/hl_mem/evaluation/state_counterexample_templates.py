"""Versioned v0.30 state counterexample bundle templates."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from hl_mem.domain.claims.state_coordinates import StateCoordinate
from hl_mem.evaluation.state_protocol import coordinate_mapping

SCHEMA_VERSION = 1
_CATEGORY_SHORT = {
    "software_version": "version",
    "non_version_state": "state",
    "compound_claim": "compound",
    "counterexample": "counter",
    "non_state_control": "control",
}
_VERSION_SUBTYPES = ("upgrade", "rollback", "delayed_recording", "subject_drift", "predicate_drift")
_STATE_SUBTYPES = ("service_health", "process", "deployment", "connectivity", "job")
_COMPOUND_SUBTYPES = ("health_process", "deployment_connectivity", "version_job", "two_services")
_COUNTER_SUBTYPES = (
    "historical_narrative",
    "plan",
    "requirement",
    "quotation",
    "negation",
    "multi_deployment",
    "multi_instance",
)
_CONTROL_SUBTYPES = ("preference", "identity", "architecture", "ordinary_fact")


def _coordinate(
    subject: str,
    slot: str,
    qualifiers: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return coordinate_mapping(StateCoordinate("default", subject, slot, qualifiers))


def _event(bundle_id: str, index: int, text: str, *, role: str = "user") -> dict[str, Any]:
    return {
        "event_index": index,
        "event_id": f"{bundle_id}:e{index}",
        "role": role,
        "content": {"text": text},
        "occurred_at": f"2026-06-{index + 1:02d}T00:00:00Z",
    }


def _atomic(
    bundle_id: str,
    index: int,
    source_event_index: int,
    coordinate: Mapping[str, Any] | None,
    state_value: str,
    *,
    source_claim_index: int | None = None,
    atomic_index: int = 0,
) -> dict[str, Any]:
    claim_index = index if source_claim_index is None else source_claim_index
    return {
        "assertion_id": f"{bundle_id}:c{claim_index}:a{atomic_index}",
        "source_claim_index": claim_index,
        "atomic_index": atomic_index,
        "source_event_indices": [source_event_index],
        "coordinate": dict(coordinate) if coordinate is not None else None,
        "atomicity": "atomic",
        "state_value": state_value,
    }


def _gold(
    bundle_id: str,
    split: str,
    category: str,
    claims: Sequence[Mapping[str, Any]],
    edges: Sequence[Sequence[str]],
    current: Sequence[str],
    historical: Sequence[str],
    *,
    counterexample: bool = False,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "bundle_id": bundle_id,
        "split": split,
        "category": category,
        "atomic_claims": [dict(claim) for claim in claims],
        "expected_supersede_edges": [list(edge) for edge in edges],
        "counterexample_zero_supersede": counterexample,
        "current_assertion_ids": list(current),
        "historical_assertion_ids": list(historical),
    }


def _version_bundle(
    bundle_id: str, split: str, subtype: str, variant: int
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    subject = f"component-{variant % 12:02d}"
    coordinate = _coordinate(subject, "config.version")
    versions = ("v1.0", "v1.1", "v1.2") if subtype != "rollback" else ("v1.0", "v1.1", "v1.0")
    surfaces = (subject, subject, subject)
    texts: tuple[str, ...]
    if subtype == "subject_drift":
        surfaces = (subject, f"{subject} 服务", f"{subject} 的 API 服务")
    if subtype == "delayed_recording":
        texts = (
            f"补录：2026 年 6 月 1 日时 {subject} 当前版本是 {versions[0]}",
            f"{subject} 当前版本是 {versions[1]}",
            f"{subject} 当前版本是 {versions[2]}",
        )
    elif subtype == "predicate_drift":
        texts = (
            f"{subject} 的配置版本是 {versions[0]}",
            f"事实记录：{subject} 当前 version 为 {versions[1]}",
            f"{subject} 现在运行 release {versions[2]}",
        )
    elif subtype == "rollback":
        texts = (
            f"{subject} 当前版本是 {versions[0]}",
            f"{subject} 已升级，当前版本是 {versions[1]}",
            f"{subject} 已回滚，当前版本是 {versions[2]}",
        )
    else:
        texts = tuple(f"{surface} 当前版本是 {version}" for surface, version in zip(surfaces, versions, strict=True))
    events = [_event(bundle_id, index, text) for index, text in enumerate(texts)]
    claims = [_atomic(bundle_id, index, index, coordinate, version) for index, version in enumerate(versions)]
    ids = [str(claim["assertion_id"]) for claim in claims]
    return events, _gold(
        bundle_id,
        split,
        "software_version",
        claims,
        ((ids[0], ids[1]), (ids[1], ids[2])),
        (ids[2],),
        (ids[0], ids[1]),
    )


def _state_bundle(
    bundle_id: str, split: str, subtype: str, variant: int
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    subject = f"service-{variant % 16:02d}"
    definitions = {
        "service_health": ("state.service_health", {"service": "api"}, ("healthy", "unhealthy", "healthy")),
        "process": ("state.process", {"process": "worker"}, ("running", "stopped", "running")),
        "deployment": ("state.deployment", {"deployment": "blue"}, ("ready", "failed", "ready")),
        "connectivity": ("state.connectivity", {"service": "api"}, ("reachable", "timeout", "reachable")),
        "job": ("state.job", {"job": "sync"}, ("queued", "running", "completed")),
    }
    slot, qualifiers, values = definitions[subtype]
    coordinate = _coordinate(subject, slot, qualifiers)
    noun = {
        "service_health": "API 服务",
        "process": "worker 进程",
        "deployment": "blue 部署",
        "connectivity": "API 连接",
        "job": "sync 任务",
    }[subtype]
    events = [_event(bundle_id, index, f"{subject} 的 {noun} 当前状态为 {value}") for index, value in enumerate(values)]
    claims = [_atomic(bundle_id, index, index, coordinate, value) for index, value in enumerate(values)]
    ids = [str(claim["assertion_id"]) for claim in claims]
    return events, _gold(
        bundle_id,
        split,
        "non_version_state",
        claims,
        ((ids[0], ids[1]), (ids[1], ids[2])),
        (ids[2],),
        (ids[0], ids[1]),
    )


def _compound_bundle(
    bundle_id: str,
    split: str,
    subtype: str,
    variant: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    subject = f"compound-{variant % 20:02d}"
    choices = {
        "health_process": (
            _coordinate(subject, "state.service_health", {"service": "api"}),
            _coordinate(subject, "state.process", {"process": "worker"}),
            "API 服务",
            "worker 进程",
        ),
        "deployment_connectivity": (
            _coordinate(subject, "state.deployment", {"deployment": "blue"}),
            _coordinate(subject, "state.connectivity", {"service": "api"}),
            "blue 部署",
            "API 连接",
        ),
        "version_job": (
            _coordinate(subject, "config.version"),
            _coordinate(subject, "state.job", {"job": "sync"}),
            "当前版本",
            "sync 任务",
        ),
        "two_services": (
            _coordinate(subject, "state.service_health", {"service": "api"}),
            _coordinate(subject, "state.service_health", {"service": "worker"}),
            "API 服务",
            "worker 服务",
        ),
    }
    first_coordinate, second_coordinate, first_noun, second_noun = choices[subtype]
    values = {
        "health_process": (("healthy", "running"), ("unhealthy", "stopped")),
        "deployment_connectivity": (("ready", "reachable"), ("failed", "unreachable")),
        "version_job": (("v1.0", "queued"), ("v1.1", "completed")),
        "two_services": (("healthy", "unhealthy"), ("unhealthy", "healthy")),
    }[subtype]
    events = [
        _event(
            bundle_id,
            event_index,
            f"{subject} 的 {first_noun} 当前 {first_value}；{second_noun} 当前 {second_value}",
        )
        for event_index, (first_value, second_value) in enumerate(values)
    ]
    claims = [
        _atomic(bundle_id, 0, 0, first_coordinate, values[0][0], source_claim_index=0, atomic_index=0),
        _atomic(bundle_id, 1, 0, second_coordinate, values[0][1], source_claim_index=0, atomic_index=1),
        _atomic(bundle_id, 2, 1, first_coordinate, values[1][0], source_claim_index=1, atomic_index=0),
        _atomic(bundle_id, 3, 1, second_coordinate, values[1][1], source_claim_index=1, atomic_index=1),
    ]
    ids = [str(claim["assertion_id"]) for claim in claims]
    return events, _gold(
        bundle_id,
        split,
        "compound_claim",
        claims,
        ((ids[0], ids[2]), (ids[1], ids[3])),
        (ids[2], ids[3]),
        (ids[0], ids[1]),
    )


def _counter_bundle(
    bundle_id: str,
    split: str,
    subtype: str,
    variant: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    subject = f"counter-{variant % 24:02d}"
    texts: tuple[str, str]
    coordinates: tuple[dict[str, Any] | None, dict[str, Any] | None]
    historical: tuple[str, ...]
    if subtype == "historical_narrative":
        coordinate = _coordinate(subject, "config.version")
        texts = (
            f"历史记录：2024 年时 {subject} 当前版本是 v0.8",
            f"回顾材料提到 2025 年时 {subject} 当前版本是 v0.9",
        )
        coordinates = (coordinate, coordinate)
        historical = ()
    elif subtype == "multi_deployment":
        texts = (
            f"{subject} 的 blue 部署当前版本是 v2.0",
            f"{subject} 的 green 部署当前版本是 v1.9",
        )
        coordinates = (
            _coordinate(subject, "config.version", {"deployment": "blue"}),
            _coordinate(subject, "config.version", {"deployment": "green"}),
        )
        historical = ()
    elif subtype == "multi_instance":
        texts = (
            f"{subject} 实例 node-a 当前版本是 v2.0",
            f"{subject} 实例 node-b 当前版本是 v1.9",
        )
        coordinates = (
            _coordinate(subject, "config.version", {"instance": "node-a"}),
            _coordinate(subject, "config.version", {"instance": "node-b"}),
        )
        historical = ()
    else:
        templates = {
            "plan": f"计划下周把 {subject} 升级到 v2.0",
            "requirement": f"要求 {subject} 必须保持 v2.0",
            "quotation": f"文档写道：{subject} 当前版本是 v2.0",
            "negation": f"{subject} 并不是 running 状态",
        }
        texts = (templates[subtype], f"这条{subtype}描述不得改变 {subject} 的当前状态")
        coordinates = (None, None)
        historical = ()
    events = [_event(bundle_id, index, text) for index, text in enumerate(texts)]
    claims = [_atomic(bundle_id, index, index, coordinates[index], f"counter-{subtype}-{index}") for index in range(2)]
    ids = [str(claim["assertion_id"]) for claim in claims]
    if subtype == "historical_narrative":
        historical = tuple(ids)
    current: Sequence[str] = ids if subtype in {"multi_deployment", "multi_instance"} else ()
    return events, _gold(
        bundle_id,
        split,
        "counterexample",
        claims,
        (),
        current,
        historical,
        counterexample=True,
    )


def _control_bundle(
    bundle_id: str,
    split: str,
    subtype: str,
    variant: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    subject = f"control-{variant % 12:02d}"
    texts = {
        "preference": (f"{subject} 偏好简洁回复", f"{subject} 喜欢深色主题"),
        "identity": (f"{subject} 是测试账号", f"{subject} 的角色是维护者"),
        "architecture": (f"{subject} 采用本地优先架构", f"{subject} 使用 SQLite 存储"),
        "ordinary_fact": (f"{subject} 包含三个模块", f"{subject} 的文档使用中文"),
    }[subtype]
    events = [_event(bundle_id, index, text) for index, text in enumerate(texts)]
    claims = [_atomic(bundle_id, index, index, None, f"non-state-{subtype}-{index}") for index in range(2)]
    return events, _gold(bundle_id, split, "non_state_control", claims, (), (), ())


def build_bundle_payload(
    *,
    split: str,
    category: str,
    category_index: int,
    global_index: int,
    source_kind: str,
    seed: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build one deterministic v0.30 corpus/gold pair."""

    bundle_id = f"{split}-{_CATEGORY_SHORT[category]}-{category_index + 1:03d}"
    subtype_choices = {
        "software_version": _VERSION_SUBTYPES,
        "non_version_state": _STATE_SUBTYPES,
        "compound_claim": _COMPOUND_SUBTYPES,
        "counterexample": _COUNTER_SUBTYPES,
        "non_state_control": _CONTROL_SUBTYPES,
    }[category]
    subtype = subtype_choices[category_index % len(subtype_choices)]
    builders = {
        "software_version": _version_bundle,
        "non_version_state": _state_bundle,
        "compound_claim": _compound_bundle,
        "counterexample": _counter_bundle,
        "non_state_control": _control_bundle,
    }
    events, gold = builders[category](bundle_id, split, subtype, global_index)
    provenance: dict[str, Any]
    if source_kind == "real_deidentified":
        if seed is None:
            raise ValueError("real_deidentified bundle requires a redacted seed")
        provenance = {
            "source_kind": source_kind,
            "redaction": "irreversible_structural_v1",
            "composition": "redacted_real_context_plus_controlled_assertion_v1",
            "seed": dict(seed),
        }
        controlled_text = str(events[0]["content"]["text"])
        events[0] = {
            **events[0],
            "content": {
                "text": (
                    "【去标识真实上下文，仅保留结构，不作为事实证据】\n"
                    f"{seed['redacted_skeleton']}\n"
                    "【当前评测事件】\n"
                    f"{controlled_text}"
                )
            },
            "context_only": {
                "redacted_text": str(seed["redacted_skeleton"]),
                "source_hash": str(seed["source_hash"]),
            },
        }
    else:
        provenance = {
            "source_kind": source_kind,
            "generator": "adversarial_templates_v1",
            "seed_id": f"synthetic-{global_index:03d}",
        }
    corpus = {
        "schema_version": SCHEMA_VERSION,
        "bundle_id": bundle_id,
        "split": split,
        "category": category,
        "subtype": subtype,
        "source_kind": source_kind,
        "provenance": provenance,
        "events": events,
    }
    return corpus, gold
