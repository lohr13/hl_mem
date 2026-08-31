"""构建不含生产数据的确定性 recall_v2 CI fixture。"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from hl_mem.application.ingest import claim_text, compute_fact_hash
from hl_mem.domain.claims.claim import build_index_text
from hl_mem.ingest.embedder import FakeEmbedder
from hl_mem.ingest.extractors import FakeExtractor
from hl_mem.settings import Settings
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.database import Database
from hl_mem.storage.events import EventRepository
from hl_mem.storage.evidence import EvidenceRepository
from tests.eval.dataset import EvalCase, load_cases
from tests.eval.eval_runner import _sha256_utf8_lf

FIXTURE_ID = "core-recall-public-v1"
FIXED_TIME = "2026-01-01T00:00:00+00:00"


def _unique_text(parts: Iterable[str]) -> str:
    return "；".join(dict.fromkeys(part.strip() for part in parts if part.strip()))


def _case_text(case: EvalCase) -> str:
    assert case.binding is not None
    binding_terms = [term for group in case.binding.claim_keyword_groups for term in group]
    return _unique_text((case.query, *case.expected_keywords, *binding_terms))


def _claim_specs(cases: list[EvalCase]) -> list[dict[str, str]]:
    extractor = FakeExtractor()
    specs: list[dict[str, str]] = []
    for case in cases:
        if case.expected_type != "claim":
            continue
        extracted = extractor.extract({"text": f"记住 {_case_text(case)}"})
        if len(extracted) != 1:
            raise RuntimeError(f"{case.case_id}: fake extractor 未生成唯一 claim")
        specs.append(
            {
                "case_id": case.case_id,
                "claim_id": f"ci-fixture-claim-{case.case_id.lower()}",
                "event_id": f"ci-fixture-event-{case.case_id.lower()}",
                "predicate": extracted[0].predicate,
                "subject": extracted[0].subject,
                "value": extracted[0].value,
            }
        )
    return specs


def _fixture_sha256(dataset: Path, specs: list[dict[str, str]], embedding_dim: int) -> str:
    payload = {
        "fixture_id": FIXTURE_ID,
        "dataset_sha256": _sha256_utf8_lf(dataset),
        "embedding": {"provider": "fake", "model": "fake", "dim": embedding_dim},
        "extractor": {"provider": "fake", "model": "fake-v1"},
        "reranker": {"mode": "off", "model": "gte-rerank-v2"},
        "claims": specs,
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _insert_fixture_claim(
    connection: Any,
    spec: dict[str, str],
    embedder: FakeEmbedder,
) -> None:
    event_content = {"text": f"记住 {spec['value']}"}
    EventRepository(connection).insert_event(
        {
            "id": spec["event_id"],
            "idempotency_key": f"ci-fixture:{spec['case_id']}",
            "tenant_id": "default",
            "event_type": "explicit_memory",
            "actor_type": "user",
            "content": event_content,
            "occurred_at": FIXED_TIME,
            "recorded_at": FIXED_TIME,
            "sensitivity": "normal",
        }
    )
    claim = {
        "id": spec["claim_id"],
        "namespace_key": "default",
        "subject_entity_id": spec["subject"],
        "predicate": spec["predicate"],
        "value": spec["value"],
        "qualifiers": {},
        "fact_hash": compute_fact_hash(spec["subject"], spec["predicate"], spec["value"]),
        "conflict_key": None,
        "conflict_key_version": 3,
        "legacy_conflict_key": None,
        "valid_from": FIXED_TIME,
        "recorded_from": FIXED_TIME,
        "observed_at": FIXED_TIME,
        "volatility": "stable",
        "status": "active",
        "confidence": 0.95,
        "importance": 0.8,
        "scope": "permanent",
        "access_count": 0,
        "source_authority": "medium",
        "extractor_version": "fake-v1",
        "embedding_model": embedder.model,
        "embedding_dim": embedder.dim,
        "canonical_attribute": "memory.explicit",
        "canonical_slot": None,
        "topic_tags_json": "[]",
    }
    claim["index_text"] = build_index_text({**claim, "topic_tags": []}, mode="legacy")
    claim["embedding_dense"] = embedder.embed_one(claim_text(claim))
    ClaimRepository(connection).insert_claim(claim)
    EvidenceRepository(connection).add_link(
        {
            "id": f"ci-fixture-link-{spec['case_id'].lower()}",
            "derived_type": "claim",
            "derived_id": spec["claim_id"],
            "evidence_type": "event",
            "evidence_id": spec["event_id"],
            "relation": "derived_from",
            "weight": 1.0,
        }
    )


def build_ci_snapshot(target: Path, dataset: Path) -> dict[str, Any]:
    """从冻结查询集构建可在任意 CI 环境重建的合成 snapshot。"""
    target = target.resolve()
    dataset = dataset.resolve()
    if target.exists():
        raise FileExistsError(f"CI fixture 目标已存在: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    cases = load_cases(dataset)
    specs = _claim_specs(cases)
    settings = Settings(
        database_path=str(target),
        embedder_mode="fake",
        embedding_dim=2048,
        extractor_mode="fake",
        reranker_mode="off",
        index_text_mode="legacy",
    )
    embedder = FakeEmbedder(settings.embedding_dim)
    fixture_sha256 = _fixture_sha256(dataset, specs, settings.embedding_dim)
    database = Database(settings=settings)
    connection = database.open()
    try:
        for spec in specs:
            _insert_fixture_claim(connection, spec, embedder)
        connection.execute("CREATE TABLE eval_fixture_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        connection.executemany(
            "INSERT INTO eval_fixture_metadata(key,value) VALUES (?,?)",
            (
                ("fixture_id", FIXTURE_ID),
                ("fixture_sha256", fixture_sha256),
                ("dataset_sha256", _sha256_utf8_lf(dataset)),
                ("embedding_provider", "fake"),
                ("embedding_model", "fake"),
                ("embedding_dim", str(settings.embedding_dim)),
                ("extractor_provider", "fake"),
                ("extractor_model", "fake-v1"),
                ("reranker_mode", "off"),
                ("index_text_mode", "legacy"),
            ),
        )
        connection.execute("UPDATE schema_migrations SET applied_at=?", (FIXED_TIME,))
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("VACUUM")
    finally:
        database.close()
    return {
        "fixture_id": FIXTURE_ID,
        "fixture_sha256": fixture_sha256,
        "dataset_sha256": _sha256_utf8_lf(dataset),
        "case_count": len(cases),
        "claim_count": len(specs),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="构建 recall_v2 确定性 CI snapshot")
    parser.add_argument("--target", required=True, type=Path)
    parser.add_argument(
        "--dataset",
        type=Path,
        default=Path(__file__).parents[1] / "datasets" / "recall_v2.jsonl",
    )
    arguments = parser.parse_args()
    print(json.dumps(build_ci_snapshot(arguments.target, arguments.dataset), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
