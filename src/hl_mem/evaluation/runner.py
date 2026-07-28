"""Extraction、retrieval、lifecycle 三层 benchmark 编排。"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any

from hl_mem.application.ingest import IngestService
from hl_mem.application.recall import RecallService
from hl_mem.domain.recall import RecallIntent
from hl_mem.domain.temporal import claim_is_visible
from hl_mem.evaluation.longmemeval import LongMemEvalAdapter
from hl_mem.evaluation.metrics import (
    bootstrap_ci,
    evidence_precision_recall,
    mrr,
    ndcg_at_k,
    recall_at_k,
    temporal_correctness,
)
from hl_mem.evaluation.models import BenchmarkCase
from hl_mem.evaluation.reporting import generate_json_report, generate_markdown_summary
from hl_mem.ingest.embedder import FakeEmbedder
from hl_mem.ingest.extractors import FakeExtractor
from hl_mem.settings import Settings
from hl_mem.storage.database import Database
from hl_mem.workers.decay import decay_claims
from hl_mem.workers.ttl import expire_claims


class BenchmarkRunner:
    """在 runner 自建 SQLite 文件上执行可重复、离线优先的 benchmark。"""

    SEED = 42
    SUPPORTED_LAYERS = frozenset({"extraction", "retrieval", "lifecycle"})

    def __init__(
        self,
        adapter: LongMemEvalAdapter | None = None,
        settings: Settings | None = None,
        limit: int | None = None,
    ) -> None:
        self.adapter = adapter or LongMemEvalAdapter()
        self.settings = settings or Settings()
        self.limit = limit
        self.embedder = FakeEmbedder(self.settings.embedding_dim)
        self.extractor = FakeExtractor()

    def run(
        self,
        source: Path,
        subset: str,
        layers: Sequence[str],
        output: Path,
        keep_db: bool,
    ) -> dict[str, Any]:
        """执行评测、写入 JSON/Markdown，并返回报告对象。"""
        selected_layers = tuple(dict.fromkeys(layers))
        unknown = set(selected_layers) - self.SUPPORTED_LAYERS
        if unknown:
            raise ValueError(
                f"unsupported benchmark layers: {', '.join(sorted(unknown))}"
            )
        source = source.resolve()
        output = output.resolve()
        cases = list(self.adapter.load(source, subset))
        if self.limit is not None:
            cases = cases[: self.limit]
        source_hash = _sha256_file(source)
        git_revision = _git_revision()
        config = self._config_payload(source_hash, subset, git_revision)
        config_hash = hashlib.sha256(_canonical_json(config)).hexdigest()
        case_results: list[dict[str, Any]] = []
        temporary_root = Path(tempfile.mkdtemp(prefix="hl-mem-benchmark-"))
        database_root = output / "databases" if keep_db else temporary_root
        database_root.mkdir(parents=True, exist_ok=True)
        try:
            for case in cases:
                database_path = database_root / f"{_safe_name(case.case_id)}.sqlite3"
                case_results.append(
                    self._run_case(case, selected_layers, database_path)
                )
            result = {
                "schema_version": 1,
                "benchmark": "longmemeval",
                "subset": subset,
                "config_hash": config_hash,
                "run": {
                    "started_at": datetime.now(timezone.utc).isoformat(),
                    "git_revision": git_revision,
                    "source_hash": source_hash,
                    "models": {
                        "extractor": self.settings.extractor_mode,
                        "embedder": self.embedder.model,
                        "judge": "not_run",
                    },
                    "seed": self.SEED,
                    "standard": self.limit is None,
                },
                "metrics": self._aggregate(case_results, selected_layers),
                "categories": self._categories(case_results, selected_layers),
                "cases": case_results,
            }
            generate_json_report(result, output)
            generate_markdown_summary(result, output)
            return result
        finally:
            if not keep_db:
                shutil.rmtree(temporary_root, ignore_errors=True)

    def _run_case(
        self,
        case: BenchmarkCase,
        layers: Sequence[str],
        database_path: Path,
    ) -> dict[str, Any]:
        database = Database(database_path)
        connection = database.open()
        result: dict[str, Any] = {
            "case_id": case.case_id,
            "category": case.category,
            "event_count": len(case.events),
            "metrics": {},
            "errors": [],
        }
        try:
            self._ingest_case(connection, case)
            if "extraction" in layers:
                result["metrics"]["extraction"] = self._extraction_metrics(
                    connection, case
                )
            if "retrieval" in layers:
                result["metrics"]["retrieval"] = self._retrieval_metrics(
                    connection, case
                )
            if "lifecycle" in layers:
                result["metrics"]["lifecycle"] = self._lifecycle_metrics(
                    connection, case
                )
        except Exception as error:
            result["errors"].append(f"{type(error).__name__}: {error}")
        finally:
            database.close()
        return result

    def _ingest_case(self, connection: Any, case: BenchmarkCase) -> None:
        service = IngestService(connection)
        for raw_event in case.events:
            event = dict(raw_event)
            service.ingest_event(event)
            content = event.get("content", {})
            if not isinstance(content, (str, dict)):
                raise TypeError(
                    f"benchmark event content must be text or an object, got {type(content).__name__}"
                )
            for extracted in self.extractor.extract(content):
                IngestService.store_extracted(
                    connection,
                    extracted,
                    event,
                    str(event.get("recorded_at") or event["occurred_at"]),
                    self.embedder,
                )

    @staticmethod
    def _claims_and_evidence(
        connection: Any,
    ) -> tuple[list[dict[str, Any]], dict[str, set[str]]]:
        claims = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM claims ORDER BY recorded_from,id"
            )
        ]
        evidence_by_claim: dict[str, set[str]] = defaultdict(set)
        for row in connection.execute(
            "SELECT derived_id,evidence_id FROM evidence_links "
            "WHERE derived_type='claim' AND evidence_type='event'"
        ):
            evidence_by_claim[row["derived_id"]].add(row["evidence_id"])
        return claims, evidence_by_claim

    def _extraction_metrics(
        self, connection: Any, case: BenchmarkCase
    ) -> dict[str, Any]:
        claims, evidence = self._claims_and_evidence(connection)
        extracted_ids = {event_id for ids in evidence.values() for event_id in ids}
        evidence_scores = evidence_precision_recall(
            extracted_ids, case.gold_evidence_event_ids
        )
        gold_yield = len(extracted_ids & set(case.gold_evidence_event_ids))
        return {
            **evidence_scores,
            "claim_yield": gold_yield / len(case.gold_evidence_event_ids)
            if case.gold_evidence_event_ids
            else 0.0,
            "claims": len(claims),
            "judge": "not_run",
        }

    def _retrieval_metrics(
        self, connection: Any, case: BenchmarkCase
    ) -> dict[str, Any]:
        response = RecallService(
            connection, self.embedder, settings=self.settings
        ).recall(
            case.query,
            limit=10,
            as_of=case.as_of,
            known_as_of=case.known_as_of,
            namespace=f"eval:{case.case_id}",
            debug=True,
        )
        results = response["results"]
        temporal = temporal_correctness(results, case.gold_temporal)
        return {
            "recall_at_1": recall_at_k(results, case.gold_evidence_event_ids, 1),
            "recall_at_5": recall_at_k(results, case.gold_evidence_event_ids, 5),
            "recall_at_10": recall_at_k(results, case.gold_evidence_event_ids, 10),
            "mrr": mrr(results, case.gold_evidence_event_ids),
            "ndcg_at_10": ndcg_at_k(results, case.gold_evidence_event_ids, 10),
            "temporal_correctness": temporal["overall"],
            "temporal_details": temporal,
            "judge": "not_run",
        }

    def _lifecycle_metrics(
        self, connection: Any, case: BenchmarkCase
    ) -> dict[str, Any]:
        claims, evidence = self._claims_and_evidence(connection)
        assertions: list[dict[str, Any]] = []
        for checkpoint in case.lifecycle_checkpoints:
            if checkpoint.worker_action == "expire_ttl":
                expire_claims(connection, checkpoint.at)
            elif checkpoint.worker_action == "decay_access":
                decay_claims(connection, checkpoint.at)
            claims, evidence = self._claims_and_evidence(connection)
            actual_visible: set[str] = set()
            status_by_event: dict[str, str] = {}
            for claim in claims:
                event_ids = evidence.get(claim["id"], set())
                if claim_is_visible(
                    claim,
                    checkpoint.at,
                    checkpoint.known_as_of,
                    RecallIntent.CURRENT_STATE,
                ):
                    actual_visible.update(event_ids)
                for event_id in event_ids:
                    status_by_event[event_id] = claim["status"]
            expected_visible = set(checkpoint.expected_visible_event_ids)
            expected_hidden = set(checkpoint.expected_hidden_event_ids)
            visibility_ok = expected_visible <= actual_visible and not (
                expected_hidden & actual_visible
            )
            status_ok = all(
                status_by_event.get(event_id) == status
                for event_id, status in checkpoint.expected_status_by_event_id.items()
            )
            assertions.append(
                {
                    "at": checkpoint.at,
                    "worker_action": checkpoint.worker_action,
                    "passed": visibility_ok and status_ok,
                    "expected_visible_ids": sorted(expected_visible),
                    "actual_visible_ids": sorted(actual_visible),
                    "expected_status_by_event_id": checkpoint.expected_status_by_event_id,
                    "actual_status_by_event_id": status_by_event,
                }
            )
        passed = sum(bool(item["passed"]) for item in assertions)
        return {
            "accuracy": passed / len(assertions) if assertions else 1.0,
            "passed": passed,
            "total": len(assertions),
            "assertions": assertions,
        }

    def _config_payload(
        self, source_hash: str, subset: str, git_revision: str
    ) -> dict[str, Any]:
        snapshot = self.settings.snapshot()
        whitelist = {
            key: snapshot[key]
            for key in (
                "embedder_mode",
                "embedding_dim",
                "reranker_mode",
                "llm_model",
                "llm_provider",
                "tag_boost_enabled",
                "tag_boost_weight",
            )
            if key in snapshot
        }
        return {
            "git_revision": git_revision,
            "prompt_hash": hashlib.sha256(b"hl_mem.production.extractor").hexdigest(),
            "adapter_version": self.adapter.VERSION,
            "model": self.embedder.model,
            "settings": whitelist,
            "subset": subset,
            "subset_hash": source_hash,
            "seed": self.SEED,
        }

    @staticmethod
    def _aggregate(
        case_results: Sequence[Mapping[str, Any]], layers: Iterable[str]
    ) -> dict[str, Any]:
        aggregated: dict[str, Any] = {}
        for layer in layers:
            layer_metrics = [
                case["metrics"][layer]
                for case in case_results
                if not case["errors"] and layer in case["metrics"]
            ]
            names = sorted(
                {
                    name
                    for metrics in layer_metrics
                    for name, value in metrics.items()
                    if isinstance(value, (int, float)) and not isinstance(value, bool)
                }
            )
            values: dict[str, Any] = {}
            for name in names:
                samples = [
                    float(metrics[name]) for metrics in layer_metrics if name in metrics
                ]
                values[name] = mean(samples) if samples else 0.0
                if name in {"f1", "recall_at_5", "accuracy"}:
                    interval = bootstrap_ci(samples, seed=BenchmarkRunner.SEED)
                    values[f"{name}_ci95"] = dict(zip(("low", "high"), interval))
            if any(metrics.get("judge") == "not_run" for metrics in layer_metrics):
                values["judge"] = "not_run"
            aggregated[layer] = values
        return aggregated

    @classmethod
    def _categories(
        cls,
        case_results: Sequence[Mapping[str, Any]],
        layers: Iterable[str],
    ) -> dict[str, Any]:
        grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for case in case_results:
            grouped[str(case["category"])].append(case)
        return {
            category: cls._aggregate(cases, layers)
            for category, cases in sorted(grouped.items())
        }


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_revision() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    return completed.stdout.strip() if completed.returncode == 0 else "unknown"


def _safe_name(value: str) -> str:
    return "".join(
        character if character.isalnum() or character in "-_." else "_"
        for character in value
    )
