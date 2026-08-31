from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
from collections import Counter
from pathlib import Path

from benchmarks.archive.v030.v030_corpus import build_manifest, load_manifest, write_manifest


def _case(case_id: str, *, source: str, category: str, decision: str) -> dict:
    return {
        "case_id": case_id,
        "source": source,
        "category": category,
        "input": {"placeholder": source == "volcano"},
        "gold": {"decision": decision},
    }


def _write_base_manifests(root: Path) -> None:
    e1_local_decisions = ["keep_left"] * 28 + ["keep_right"] * 14 + ["coexist"] * 11
    e1_local_decisions += ["select_candidate"] * 5 + ["reject"]
    e1_cases = [
        _case(f"local:{index:03}", source="local", category="conflict", decision=decision)
        for index, decision in enumerate(e1_local_decisions)
    ]
    e1_cases.extend(
        _case(f"volcano:{index:02}", source="volcano", category="conflict", decision="coexist") for index in range(11)
    )
    e2_cases = [
        _case(f"local:eq:{index:03}", source="local", category="equivalent", decision="equivalent")
        for index in range(188)
    ]
    e2_cases.extend(
        _case(f"local:distinct:{index:03}", source="local", category="distinct", decision="distinct")
        for index in range(203)
    )
    e2_cases.extend(
        _case(
            f"volcano:dedup:{index:02}",
            source="volcano",
            category="equivalent",
            decision="equivalent",
        )
        for index in range(15)
    )
    snapshots = [
        {"source_id": "local_sqlite", "sha256": "a" * 64, "reconstructable": True},
        {"source_id": "volcano_placeholder", "sha256": "b" * 64, "reconstructable": False},
    ]
    write_manifest(
        root / "e1.json",
        build_manifest(
            "E1",
            e1_cases,
            source_snapshots=snapshots,
            source_audit={"volcano_raw_case_ids": "REMOTE_PULL_REQUIRED"},
        ),
    )
    write_manifest(
        root / "e2.json",
        build_manifest(
            "E2",
            e2_cases,
            source_snapshots=snapshots,
            source_audit={"volcano_pair_columns": "REMOTE_PULL_REQUIRED"},
        ),
    )


def _raw_claim(claim_id: str, *, authority: str = "low") -> dict:
    return {
        "id": claim_id,
        "namespace_key": "default",
        "subject_entity_id": "subject",
        "predicate": "配置",
        "value_json": json.dumps(f"value:{claim_id}", ensure_ascii=False),
        "qualifiers_json": json.dumps({"target": "runner"}, ensure_ascii=False),
        "conflict_key": None,
        "valid_from": "2026-08-20T00:00:00+00:00",
        "valid_to": None,
        "recorded_from": "2026-08-20T00:01:00+00:00",
        "recorded_to": None,
        "observed_at": None,
        "expires_at": None,
        "refresh_after": None,
        "volatility": "stable",
        "status": "active",
        "confidence": 1.0,
        "importance": 0.5,
        "source_authority": authority,
        "supersedes_id": None,
        "extractor_version": "fixture",
        "embedding_dense": "b'fixture-vector'",
        "embedding_sparse": None,
        "embedding_model": "fixture",
        "embedding_dim": 2,
        "fact_hash": claim_id,
        "scope": "permanent",
        "access_count": 0,
        "last_accessed_at": None,
        "last_decayed_at": None,
        "canonical_attribute": "config.other",
        "conflict_key_version": 3,
        "legacy_conflict_key": None,
        "superseded_by_id": None,
        "canonical_slot": "config.other",
        "topic_tags_json": "[]",
        "occurred_start": None,
        "occurred_end": None,
        "entities_json": json.dumps(["subject"], ensure_ascii=False),
        "index_text": "fixture",
        "activation_base": 1.0,
        "activation": 1.0,
        "decay_below_since": None,
        "assertion_kind": "unknown",
    }


def _write_remote_evidence(root: Path) -> tuple[Path, Path]:
    e1_cases: list[dict] = []
    e1_claims: list[dict] = []
    for index in range(13):
        case_id = f"included-{index:02}" if index < 11 else f"adjacent-{index - 11:02}"
        left_id = f"e1-left-{index:02}"
        right_id = f"e1-right-{index:02}"
        resolved_at = "2026-08-20T12:33:12.609724+00:00" if index < 11 else "2026-08-21T02:13:42.725926+00:00"
        decision = "keep_left" if index < 4 else "keep_right"
        e1_cases.append(
            {
                "id": case_id,
                "pair_key": f"pair-{index:02}",
                "left_claim_id": left_id,
                "right_claim_id": right_id,
                "status": "resolved",
                "decision": decision,
                "rationale": "temporal_update_uncertain",
                "confidence": 1.0,
                "created_at": "2026-08-20T12:00:00+00:00",
                "resolved_at": resolved_at,
                "namespace_key": None,
                "group_key": None,
                "generation": 1,
                "revision": 0,
                "overflow": 0,
            }
        )
        e1_claims.extend(
            (
                _raw_claim(left_id, authority="medium"),
                _raw_claim(right_id, authority="low"),
            )
        )
    e1_path = root / "e1_remote.json"
    e1_path.write_text(
        json.dumps(
            {"cases": list(reversed(e1_cases)), "claims": e1_claims, "candidate_members": [], "case_candidates": []},
            ensure_ascii=False,
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    e2_pairs: list[dict] = []
    e2_claims: list[dict] = []
    for index in range(15):
        pair_id = f"pair-{index:02}"
        left_id = f"e2-left-{index:02}"
        right_id = f"e2-right-{index:02}"
        e2_pairs.append(
            {
                "id": pair_id,
                "left_claim_id": left_id,
                "right_claim_id": right_id,
                "similarity": 0.95,
                "policy_version": "v2",
                "predicate": "配置",
                "decision": "equivalent",
                "judge_confidence": 0.99,
                "judge_model": "fixture-judge",
                "judge_reason": f"same fact {index}",
                "reviewed_at": "2026-08-21T00:00:00+00:00",
            }
        )
        e2_claims.extend((_raw_claim(left_id), _raw_claim(right_id)))
    e2_path = root / "e2_remote.json"
    e2_path.write_text(
        json.dumps({"pairs": list(reversed(e2_pairs)), "claims": e2_claims}, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    return e1_path, e2_path


def _load_refreeze_function():
    module_name = "benchmarks.archive.v030.refreeze_remote_evidence"
    assert importlib.util.find_spec(module_name) is not None, "v030 remote refreeze tool is missing"
    return importlib.import_module(module_name).refreeze_remote_evidence


def test_refreeze_replaces_remote_placeholders_and_is_byte_reproducible(tmp_path: Path) -> None:
    manifest_dir = tmp_path / "manifests"
    manifest_dir.mkdir()
    _write_base_manifests(manifest_dir)
    e1_evidence, e2_evidence = _write_remote_evidence(tmp_path)
    refreeze = _load_refreeze_function()

    first = refreeze(manifest_dir, e1_evidence, e2_evidence)
    first_bytes = {name: (manifest_dir / name).read_bytes() for name in ("e1.json", "e2.json")}
    second = refreeze(manifest_dir, e1_evidence, e2_evidence)

    assert first == second
    assert first_bytes == {name: (manifest_dir / name).read_bytes() for name in ("e1.json", "e2.json")}
    assert first["case_counts"] == {"E1": 70, "E2": 406}

    e1 = load_manifest(manifest_dir / "e1.json")
    e1_remote = [case for case in e1["cases"] if case["source"] == "volcano"]
    assert {case["case_id"] for case in e1_remote} == {f"volcano:included-{index:02}" for index in range(11)}
    assert Counter(case["gold"]["decision"] for case in e1_remote) == {"keep_left": 4, "keep_right": 7}
    assert e1["source_audit"]["excluded_with_reason"] == [
        {
            "case_id": "adjacent-00",
            "reason": "resolved_at_on_or_after_2026-08-21T00:00:00+00:00_adjacent_date_outside_exact_11_case_batch",
            "resolved_at": "2026-08-21T02:13:42.725926+00:00",
        },
        {
            "case_id": "adjacent-01",
            "reason": "resolved_at_on_or_after_2026-08-21T00:00:00+00:00_adjacent_date_outside_exact_11_case_batch",
            "resolved_at": "2026-08-21T02:13:42.725926+00:00",
        },
    ]
    first_claim = e1_remote[0]["input"]["claims"][0]
    assert first_claim["embedding_dense"] == "b'fixture-vector'"
    assert first_claim["value"].startswith("value:e1-")
    assert first_claim["qualifiers"] == {"target": "runner"}
    assert first_claim["entities"] == ["subject"]

    e2 = load_manifest(manifest_dir / "e2.json")
    e2_remote = [case for case in e2["cases"] if case["source"] == "volcano"]
    assert {case["case_id"] for case in e2_remote} == {f"volcano:dedup:pair-{index:02}" for index in range(15)}
    assert e2_remote[0]["gold"]["judge_confidence"] == 0.99
    assert e2_remote[0]["gold"]["judge_reason"].startswith("same fact ")

    snapshots = {item["source_id"]: item for item in e1["source_snapshots"]}
    evidence_sha = hashlib.sha256(e1_evidence.read_bytes()).hexdigest()
    assert snapshots["volcano_remote_evidence_e1"]["sha256"] == evidence_sha
    assert snapshots["volcano_remote_evidence_e1"]["reconstructable"] is True
