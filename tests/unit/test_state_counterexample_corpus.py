import hashlib
import json
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from hl_mem.evaluation.state_counterexample_corpus import (
    aggregate_dev_statistics,
    generate_corpus,
    open_readonly_event_database,
    sample_redacted_seeds,
    verify_sealed_manifest,
)
from hl_mem.evaluation.state_experiment_arms import make_arm_sample, run_arm
from hl_mem.evaluation.state_experiment_scoring import score_protocol


def _source_database(path: Path, count: int = 205) -> None:
    connection = sqlite3.connect(path)
    connection.execute("""
        CREATE TABLE events(
            id TEXT PRIMARY KEY,
            event_type TEXT,
            actor_type TEXT,
            content_json TEXT,
            occurred_at TEXT,
            content_hash TEXT,
            sensitivity TEXT
        )
        """)
    for index in range(count):
        private_text = (
            f"张三{index} 的邮箱 private-{index}@example.com，项目密钥 SECRET-{index}；"
            f"API 服务当前版本是 v{index}.0。"
        )
        connection.execute(
            "INSERT INTO events VALUES(?,?,?,?,?,?,?)",
            (
                f"event-{index:03d}",
                "message",
                "user" if index % 2 == 0 else "assistant",
                json.dumps({"text": private_text}, ensure_ascii=False),
                f"2026-01-{index % 28 + 1:02d}T00:00:00Z",
                hashlib.sha256(private_text.encode()).hexdigest(),
                "normal",
            ),
        )
    connection.commit()
    connection.close()


def _insert_invalid_source_events(path: Path) -> None:
    connection = sqlite3.connect(path)
    for suffix, content_json in (
        ("malformed", "not-json"),
        ("missing", json.dumps({"other": "value"})),
        ("empty", json.dumps({"text": "   "})),
    ):
        connection.execute(
            "INSERT INTO events VALUES(?,?,?,?,?,?,?)",
            (
                f"event-{suffix}",
                "message",
                "user",
                content_json,
                "2026-01-01T00:00:00Z",
                hashlib.sha256(content_json.encode()).hexdigest(),
                "normal",
            ),
        )
    connection.commit()
    connection.close()


def _seed(index: int) -> dict[str, Any]:
    return {
        "seed_id": f"real-{index:03d}",
        "source_hash": hashlib.sha256(f"source-{index}".encode()).hexdigest(),
        "actor_class": "user" if index % 2 == 0 else "assistant",
        "language_profile": "zh",
        "length_bucket": "medium",
        "punctuation_profile": {"comma": index % 3, "period": 1, "question": 0},
        "state_signals": ["version"] if index % 2 == 0 else [],
        "structure_runs": ["han:8", "ascii:4", "punct:2"],
        "redacted_skeleton": "<HAN:8>服务当前版本是<ASCII:4>",
    }


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_real_sampler_is_readonly_deterministic_and_irreversibly_redacted(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    _source_database(source)
    before = hashlib.sha256(source.read_bytes()).hexdigest()

    first = sample_redacted_seeds(source, limit=200, seed="v0300-batch2")
    second = sample_redacted_seeds(source, limit=200, seed="v0300-batch2")

    assert first == second
    assert len(first) == 200
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    serialized = json.dumps(first, ensure_ascii=False)
    assert "张三" not in serialized
    assert "private-" not in serialized
    assert "SECRET-" not in serialized
    assert "example.com" not in serialized
    assert set(first[0]) == {
        "seed_id",
        "source_hash",
        "actor_class",
        "language_profile",
        "length_bucket",
        "punctuation_profile",
        "state_signals",
        "structure_runs",
        "redacted_skeleton",
    }
    assert all("服务" in row["redacted_skeleton"] for row in first)

    connection = open_readonly_event_database(source)
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        connection.execute("INSERT INTO events(id,event_type,content_json) VALUES('write','message','{}')")
    connection.close()


def test_real_sampler_filters_malformed_missing_and_empty_text_before_quota(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    _source_database(source)
    _insert_invalid_source_events(source)

    with pytest.raises(ValueError, match="205 eligible events; 206 required"):
        sample_redacted_seeds(source, limit=206, seed="v0300-batch2")


def test_generator_freezes_exact_protocol_quotas_and_separates_gold(tmp_path: Path) -> None:
    manifest = generate_corpus([_seed(index) for index in range(200)], tmp_path)

    assert manifest["totals"] == {
        "bundles": 400,
        "events": 1000,
        "gold_records": 400,
        "gold_coverage": 1.0,
    }
    assert manifest["splits"] == {
        "dev": {"bundles": 280, "events": 700},
        "sealed": {"bundles": 120, "events": 300},
    }
    assert manifest["categories"] == {
        "software_version": {"dev": 84, "sealed": 36, "total": 120},
        "non_version_state": {"dev": 56, "sealed": 24, "total": 80},
        "compound_claim": {"dev": 56, "sealed": 24, "total": 80},
        "counterexample": {"dev": 56, "sealed": 24, "total": 80},
        "non_state_control": {"dev": 28, "sealed": 12, "total": 40},
    }
    assert manifest["sources"] == {
        "real_deidentified": {"dev": 140, "sealed": 60, "total": 200},
        "synthetic_adversarial": {"dev": 140, "sealed": 60, "total": 200},
    }

    dev_corpus = _jsonl(tmp_path / "v0300_state_dev_corpus.jsonl")
    dev_gold = _jsonl(tmp_path / "v0300_state_dev_gold.jsonl")
    assert len(dev_corpus) == len(dev_gold) == 280
    assert {row["bundle_id"] for row in dev_corpus} == {row["bundle_id"] for row in dev_gold}
    assert all("events" in row and "atomic_claims" not in row for row in dev_corpus)
    assert all("atomic_claims" in row and "events" not in row for row in dev_gold)
    assert all("context_only" in row["events"][0] for row in dev_corpus if row["source_kind"] == "real_deidentified")
    assert all(
        "context_only" not in row["events"][0] for row in dev_corpus if row["source_kind"] == "synthetic_adversarial"
    )
    assert Counter(row["category"] for row in dev_corpus) == {
        "software_version": 84,
        "non_version_state": 56,
        "compound_claim": 56,
        "counterexample": 56,
        "non_state_control": 28,
    }
    assert all(
        set(gold)
        == {
            "schema_version",
            "bundle_id",
            "split",
            "category",
            "atomic_claims",
            "expected_supersede_edges",
            "counterexample_zero_supersede",
            "current_assertion_ids",
            "historical_assertion_ids",
        }
        for gold in dev_gold
    )
    assert all(
        claim["assertion_id"] == f"{gold['bundle_id']}:c{claim['source_claim_index']}:a{claim['atomic_index']}"
        for gold in dev_gold
        for claim in gold["atomic_claims"]
    )
    compound = next(row for row in dev_gold if row["category"] == "compound_claim")
    assert [claim["assertion_id"] for claim in compound["atomic_claims"]] == [
        f"{compound['bundle_id']}:c0:a0",
        f"{compound['bundle_id']}:c0:a1",
        f"{compound['bundle_id']}:c1:a0",
        f"{compound['bundle_id']}:c1:a1",
    ]
    compound_corpus = {row["subtype"]: row for row in dev_corpus if row["category"] == "compound_claim"}
    assert "v1.0" in compound_corpus["version_job"]["events"][0]["content"]["text"]
    assert "queued" in compound_corpus["version_job"]["events"][0]["content"]["text"]
    assert "ready" in compound_corpus["deployment_connectivity"]["events"][0]["content"]["text"]
    assert "reachable" in compound_corpus["deployment_connectivity"]["events"][0]["content"]["text"]
    dev_by_id = {row["bundle_id"]: row for row in dev_corpus}
    assert all(
        claim["state_value"]
        in dev_by_id[gold["bundle_id"]]["events"][claim["source_event_indices"][0]]["content"]["text"]
        for gold in dev_gold
        if gold["category"] == "compound_claim"
        for claim in gold["atomic_claims"]
    )


def test_sealed_verification_returns_aggregates_without_records(tmp_path: Path) -> None:
    generate_corpus([_seed(index) for index in range(200)], tmp_path)

    result = verify_sealed_manifest(tmp_path / "v0300_state_corpus_manifest.json")

    assert result == {
        "sealed_bundles": 120,
        "sealed_events": 300,
        "sealed_gold_records": 120,
        "hashes_valid": True,
    }
    assert "records" not in result
    assert "contents" not in result


def test_corpus_generation_is_byte_deterministic(tmp_path: Path) -> None:
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"

    first = generate_corpus([_seed(index) for index in range(200)], first_dir)
    second = generate_corpus([_seed(index) for index in range(200)], second_dir)

    assert first["files"] == second["files"]


def test_real_seed_changes_model_visible_input_not_only_provenance(tmp_path: Path) -> None:
    first_seeds = [_seed(index) for index in range(200)]
    second_seeds = [_seed(index) for index in range(200)]
    second_seeds[0]["redacted_skeleton"] = "<HAN:5>service<ASCII:4>"

    generate_corpus(first_seeds, tmp_path / "first")
    generate_corpus(second_seeds, tmp_path / "second")
    first_real = next(
        row
        for row in _jsonl(tmp_path / "first" / "v0300_state_dev_corpus.jsonl")
        if row["source_kind"] == "real_deidentified"
    )
    second_real = next(
        row
        for row in _jsonl(tmp_path / "second" / "v0300_state_dev_corpus.jsonl")
        if row["source_kind"] == "real_deidentified"
    )

    assert first_real["events"][0]["content"]["text"] != second_real["events"][0]["content"]["text"]


def test_dev_bundle_runs_through_fake_extractor_boundary_b1_and_scorer(tmp_path: Path) -> None:
    generate_corpus([_seed(index) for index in range(200)], tmp_path)
    dev_corpus = _jsonl(tmp_path / "v0300_state_dev_corpus.jsonl")
    dev_gold = _jsonl(tmp_path / "v0300_state_dev_gold.jsonl")
    bundle = next(
        row for row in dev_corpus if row["category"] == "compound_claim" and row["subtype"] == "health_process"
    )
    gold = next(row for row in dev_gold if row["bundle_id"] == bundle["bundle_id"])
    raw_claims = []
    for event in bundle["events"]:
        controlled_text = event["content"]["text"].rsplit("\n", 1)[-1]
        raw_claims.append(
            {
                "subject": controlled_text.split(" 的 ", 1)[0],
                "value": controlled_text,
                "kind": "fact",
                "confidence": 0.95,
                "notability": "medium",
                "evidence_quote": controlled_text,
                "source_event_indices": [event["event_index"]],
            }
        )
    sample = make_arm_sample(bundle, {"claims": raw_claims, "should_memorize": True})
    candidate = run_arm([sample], arm="B1")
    expected_edges = {tuple(edge) for edge in gold["expected_supersede_edges"]}
    current_ids = gold["current_assertion_ids"]
    historical_ids = gold["historical_assertion_ids"]

    report = score_protocol(
        [gold],
        baseline_predictions={"claim_count": 4, "non_state_assertion_ids": []},
        candidate_runs=[candidate, candidate, candidate],
        persisted_edges=expected_edges,
        baseline_observations={"current_injected_assertion_ids": [*current_ids, historical_ids[0]]},
        candidate_observations={
            "current_injected_assertion_ids": current_ids,
            "historical_retrieved_assertion_ids": historical_ids,
        },
        corpus_records=[bundle],
    )

    assert [claim["assertion_id"] for claim in candidate[0]["claims"]] == [
        claim["assertion_id"] for claim in gold["atomic_claims"]
    ]
    assert report["passed"] is True


def test_generator_rejects_seed_fields_that_could_bypass_redaction(tmp_path: Path) -> None:
    seeds = [_seed(index) for index in range(200)]
    seeds[0]["raw_text"] = "private source content"

    with pytest.raises(ValueError, match="redacted seed schema"):
        generate_corpus(seeds, tmp_path)

    seeds = [_seed(index) for index in range(200)]
    seeds[0]["redacted_skeleton"] = "张三的服务当前正常"
    with pytest.raises(ValueError, match="redacted skeleton"):
        generate_corpus(seeds, tmp_path)


def test_dev_statistics_never_require_sealed_files(tmp_path: Path) -> None:
    generate_corpus([_seed(index) for index in range(200)], tmp_path)
    (tmp_path / "v0300_state_sealed_corpus.jsonl").unlink()
    (tmp_path / "v0300_state_sealed_gold.jsonl").unlink()

    statistics = aggregate_dev_statistics(
        tmp_path / "v0300_state_dev_corpus.jsonl",
        tmp_path / "v0300_state_dev_gold.jsonl",
    )

    assert statistics["bundles"] == 280
    assert statistics["events"] == 700
    assert statistics["gold_coverage"] == 1.0
