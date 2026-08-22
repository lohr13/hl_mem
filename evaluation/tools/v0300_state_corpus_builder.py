"""Versioned CLI builder for the frozen v0.30 state counterexample corpus."""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from hl_mem.domain.claims.state_coordinates import StateCoordinate
from hl_mem.evaluation.state_counterexample_corpus import (
    file_sha256,
    load_redacted_seeds,
    validate_redacted_seed,
)
from hl_mem.evaluation.state_protocol import coordinate_mapping

SCHEMA_VERSION = 1
CORPUS_PREFIX = "v0300_state"
_CATEGORY_QUOTAS = {
    "software_version": {"dev": 84, "sealed": 36, "events": 3},
    "non_version_state": {"dev": 56, "sealed": 24, "events": 3},
    "compound_claim": {"dev": 56, "sealed": 24, "events": 2},
    "counterexample": {"dev": 56, "sealed": 24, "events": 2},
    "non_state_control": {"dev": 28, "sealed": 12, "events": 2},
}
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


def _write_jsonl(path: Path, records: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        for record in records:
            stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
            stream.write("\n")


def generate_corpus(redacted_seeds: Sequence[Mapping[str, Any]], output_dir: str | Path) -> dict[str, Any]:
    """Generate the exact frozen 400-bundle v0.30 corpus and separated gold."""

    if len(redacted_seeds) != 200:
        raise ValueError("exactly 200 redacted real-event seeds are required")
    for index, seed in enumerate(redacted_seeds):
        validate_redacted_seed(seed, index)
    seed_ids = [str(seed.get("seed_id") or "") for seed in redacted_seeds]
    if any(not seed_id for seed_id in seed_ids) or len(set(seed_ids)) != 200:
        raise ValueError("redacted seed ids must be non-blank and unique")

    target = Path(output_dir).resolve()
    split_corpus: dict[str, list[dict[str, Any]]] = {"dev": [], "sealed": []}
    split_gold: dict[str, list[dict[str, Any]]] = {"dev": [], "sealed": []}
    real_seed_index = 0
    global_index = 0
    for split in ("dev", "sealed"):
        for category, quota in _CATEGORY_QUOTAS.items():
            for category_index in range(int(quota[split])):
                source_kind = "real_deidentified" if category_index % 2 == 0 else "synthetic_adversarial"
                bundle_seed = redacted_seeds[real_seed_index] if source_kind == "real_deidentified" else None
                if bundle_seed is not None:
                    real_seed_index += 1
                corpus, gold = build_bundle_payload(
                    split=split,
                    category=category,
                    category_index=category_index,
                    global_index=global_index,
                    source_kind=source_kind,
                    seed=bundle_seed,
                )
                split_corpus[split].append(corpus)
                split_gold[split].append(gold)
                global_index += 1
    if real_seed_index != 200:
        raise RuntimeError(f"generator consumed {real_seed_index} real seeds instead of 200")

    paths = {
        "dev_corpus": target / f"{CORPUS_PREFIX}_dev_corpus.jsonl",
        "dev_gold": target / f"{CORPUS_PREFIX}_dev_gold.jsonl",
        "sealed_corpus": target / f"{CORPUS_PREFIX}_sealed_corpus.jsonl",
        "sealed_gold": target / f"{CORPUS_PREFIX}_sealed_gold.jsonl",
    }
    _write_jsonl(paths["dev_corpus"], split_corpus["dev"])
    _write_jsonl(paths["dev_gold"], split_gold["dev"])
    _write_jsonl(paths["sealed_corpus"], split_corpus["sealed"])
    _write_jsonl(paths["sealed_gold"], split_gold["sealed"])

    category_counts = {
        category: {
            "dev": int(quota["dev"]),
            "sealed": int(quota["sealed"]),
            "total": int(quota["dev"]) + int(quota["sealed"]),
        }
        for category, quota in _CATEGORY_QUOTAS.items()
    }
    source_counts = {
        source: {
            "dev": sum(row["source_kind"] == source for row in split_corpus["dev"]),
            "sealed": sum(row["source_kind"] == source for row in split_corpus["sealed"]),
            "total": sum(row["source_kind"] == source for rows in split_corpus.values() for row in rows),
        }
        for source in ("real_deidentified", "synthetic_adversarial")
    }
    split_counts = {
        split: {
            "bundles": len(split_corpus[split]),
            "events": sum(len(row["events"]) for row in split_corpus[split]),
        }
        for split in ("dev", "sealed")
    }
    file_manifest = {
        name: {
            "path": path.name,
            "sha256": file_sha256(path),
            "records": (
                len(split_corpus["dev"] if name == "dev_corpus" else split_gold["dev"])
                if name.startswith("dev_")
                else len(split_corpus["sealed"] if name == "sealed_corpus" else split_gold["sealed"])
            ),
        }
        for name, path in paths.items()
    }
    all_corpus = [*split_corpus["dev"], *split_corpus["sealed"]]
    all_gold = [*split_gold["dev"], *split_gold["sealed"]]
    corpus_ids = {row["bundle_id"] for row in all_corpus}
    gold_ids = {row["bundle_id"] for row in all_gold}
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "protocol": "v0.30.0-batch2-state-counterexamples",
        "totals": {
            "bundles": len(all_corpus),
            "events": sum(len(row["events"]) for row in all_corpus),
            "gold_records": len(all_gold),
            "gold_coverage": len(corpus_ids & gold_ids) / len(all_corpus),
        },
        "splits": split_counts,
        "categories": category_counts,
        "sources": source_counts,
        "files": file_manifest,
    }
    manifest_path = target / f"{CORPUS_PREFIX}_corpus_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return manifest


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed-source", type=Path, required=True, help="JSONL produced by sample_state_events.py")
    parser.add_argument("--output-dir", type=Path, required=True)
    arguments = parser.parse_args(argv)
    manifest = generate_corpus(load_redacted_seeds(arguments.seed_source), arguments.output_dir)
    print(
        json.dumps(
            {
                "bundles": manifest["totals"]["bundles"],
                "events": manifest["totals"]["events"],
                "dev_bundles": manifest["splits"]["dev"]["bundles"],
                "sealed_bundles": manifest["splits"]["sealed"]["bundles"],
                "manifest": str((arguments.output_dir / f"{CORPUS_PREFIX}_corpus_manifest.json").resolve()),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
