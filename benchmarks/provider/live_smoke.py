"""Disposable, explicitly invoked Provider live-smoke harness."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import tomllib
from collections.abc import Callable, Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, cast
from urllib.parse import urlsplit

import httpx
import tomli_w
from jsonschema import Draft202012Validator, FormatChecker

from hl_mem import __version__
from hl_mem.application.ingest import IngestService
from hl_mem.application.recall import RecallService
from hl_mem.components import (
    create_provider_runtime,
    initialize_process,
    make_embedder,
    make_extractor,
    make_reranker,
)
from hl_mem.config_loader import load_settings
from hl_mem.errors import UsageLimitExceededError
from hl_mem.ingest.embedder import FakeEmbedder
from hl_mem.ingest.extractors import FakeExtractor
from hl_mem.ingest.llm_extractor import LLMExtractor
from hl_mem.observability.pricing import UsagePriceBook
from hl_mem.observability.usage_types import UsageAmount, UsageIdentity
from hl_mem.plugins.contracts import ProviderCapability, ProviderKey
from hl_mem.recall.reranker import FakeReranker
from hl_mem.storage.database import Database
from hl_mem.storage.events import EventRepository

_FIXTURE_PATH = Path(__file__).with_name("fixture.json")
_RESULT_SCHEMA_PATH = Path(__file__).with_name("result_schema.json")
_SAFE_LABEL = "controlled_reranker_failure"
_CORE_COMMIT_PATTERN = re.compile(r"[0-9a-f]{40,64}")


class LiveSmokeSafetyError(RuntimeError):
    """Raised before Provider work when the disposable-run contract is unsafe."""


class LiveSmokeBudgetError(LiveSmokeSafetyError):
    """Raised when the preflight or final ledger exceeds a smoke limit."""


@dataclass(frozen=True, slots=True)
class LiveSmokeLimits:
    llm_requests: int = 10
    embedding_items: int = 30
    rerank_documents: int = 100
    cost_microunits: int = 20_000_000

    def __post_init__(self) -> None:
        hard_caps = {
            "llm_requests": 10,
            "embedding_items": 30,
            "rerank_documents": 100,
            "cost_microunits": 20_000_000,
        }
        for name, hard_cap in hard_caps.items():
            value = getattr(self, name)
            if type(value) is not int or value < 0 or value > hard_cap:
                raise ValueError(f"{name} must be a non-negative integer within the hard cap {hard_cap}")


class _LiveSmokeReservationGuard:
    """Monotonically reserve the smoke's worst-case Provider allowance."""

    def __init__(self, limits: LiveSmokeLimits) -> None:
        self._limits = limits
        self._lock = threading.Lock()
        self._reserved = {
            "llm_requests": 0,
            "embedding_items": 0,
            "rerank_documents": 0,
            "cost_microunits": 0,
        }

    def reserve(self, identity: UsageIdentity, amount: UsageAmount) -> None:
        if amount.cost_microunits is None:
            raise LiveSmokeBudgetError("cost_microunits cannot be reserved safely")
        delta = {
            "llm_requests": amount.requests if identity.capability is ProviderCapability.LLM else 0,
            "embedding_items": (amount.embedding_items if identity.capability is ProviderCapability.EMBEDDING else 0),
            "rerank_documents": (amount.rerank_documents if identity.capability is ProviderCapability.RERANKER else 0),
            "cost_microunits": amount.cost_microunits,
        }
        with self._lock:
            proposed = {name: self._reserved[name] + value for name, value in delta.items()}
            for name, value in proposed.items():
                if value > getattr(self._limits, name):
                    raise LiveSmokeBudgetError(f"{name} would exceed the live smoke limit")
            self._reserved = proposed


@dataclass(frozen=True, slots=True)
class _LiveSmokeDependencies:
    entry_points: Iterable[Any] | None = None
    client_factory: Callable[[], httpx.Client] = httpx.Client
    temp_parent: Path | None = None


@dataclass(frozen=True, slots=True)
class _FileIdentity:
    size: int
    mtime_ns: int
    sha256: str


class _ControlledFailureReranker:
    def rerank(self, _query: str, _documents: list[str], top_n: int = 20) -> list[tuple[int, float]]:
        del top_n
        raise RuntimeError(_SAFE_LABEL)


def _file_identity(path: Path) -> _FileIdentity:
    stat = path.stat()
    return _FileIdentity(stat.st_size, stat.st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest())


def _read_template(config: Path) -> dict[str, Any]:
    try:
        with config.open("rb") as stream:
            raw = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise LiveSmokeSafetyError("live smoke config must be a readable TOML file") from error
    if not isinstance(raw, dict):
        raise LiveSmokeSafetyError("live smoke config must contain a TOML document")
    return raw


def _database_filename(raw: dict[str, Any]) -> str:
    database = raw.get("database")
    value = database.get("path") if isinstance(database, dict) else None
    if not isinstance(value, str) or not value.strip():
        raise LiveSmokeSafetyError("database.path must name one database inside the temporary root")
    windows = PureWindowsPath(value)
    posix = PurePosixPath(value)
    if (
        windows.is_absolute()
        or posix.is_absolute()
        or windows.drive
        or len(windows.parts) != 1
        or len(posix.parts) != 1
        or value in {".", ".."}
    ):
        raise LiveSmokeSafetyError("database.path must be a simple filename inside the temporary root")
    return value


def _reject_fake_components(raw: dict[str, Any]) -> None:
    for label in ("extraction", "embedding", "reranker"):
        section = raw.get(label)
        if isinstance(section, dict) and str(section.get("mode", "")).casefold() == "fake":
            raise LiveSmokeSafetyError(f"Fake {label} components are forbidden in the live smoke")


def _require_explicit_file(path: Path | None, label: str) -> Path:
    if path is None:
        raise LiveSmokeSafetyError(f"an explicit {label} is required")
    candidate = Path(path)
    is_junction = getattr(os.path, "isjunction", lambda _path: False)
    absolute_candidate = Path(os.path.abspath(candidate))
    path_components = (absolute_candidate, *absolute_candidate.parents)
    if ".." in candidate.parts or any(
        component.is_symlink() or is_junction(component) for component in path_components
    ):
        raise LiveSmokeSafetyError(f"the explicit {label} uses an unsafe symlink, junction, or escape path")
    if not candidate.is_file():
        raise LiveSmokeSafetyError(f"the explicit {label} must be a readable regular file")
    return candidate


def _verify_input_identities(identities: Mapping[Path, _FileIdentity]) -> None:
    try:
        unchanged = all(_file_identity(path) == identity for path, identity in identities.items())
    except OSError as error:
        raise LiveSmokeSafetyError("an explicit input changed or disappeared during the live smoke") from error
    if not unchanged:
        raise LiveSmokeSafetyError("an explicit input changed during the live smoke")


def _copy_verified_input(source: Path, destination: Path, identity: _FileIdentity) -> None:
    shutil.copyfile(source, destination)
    copied = _file_identity(destination)
    if copied.size != identity.size or copied.sha256 != identity.sha256:
        raise LiveSmokeSafetyError("an explicit input changed while it was copied for the live smoke")
    _verify_input_identities({source: identity})


def _core_commit() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        check=False,
    )
    commit = completed.stdout.strip().casefold()
    if completed.returncode != 0 or _CORE_COMMIT_PATTERN.fullmatch(commit) is None:
        raise LiveSmokeSafetyError("the repository core commit could not be resolved safely")
    return commit


def _safe_source_urls(price_document: Mapping[str, Any]) -> list[str]:
    source_urls = price_document.get("source_urls", [])
    if not isinstance(source_urls, list):
        raise LiveSmokeSafetyError("price book source URLs must be a list")
    for source_url in source_urls:
        raw_url = str(source_url)
        if "?" in raw_url or "#" in raw_url:
            raise LiveSmokeSafetyError("price book source URL credentials or metadata are forbidden")
        try:
            parsed = urlsplit(raw_url)
            hostname = parsed.hostname
        except ValueError as error:
            raise LiveSmokeSafetyError("price book source URL is invalid") from error
        if (
            parsed.scheme.casefold() != "https"
            or not hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise LiveSmokeSafetyError("price book source URL credentials or metadata are forbidden")
    return [str(source_url) for source_url in source_urls]


def _forecast(fixture: Mapping[str, Any]) -> dict[str, int]:
    claim_count = int(fixture["expected_claim_count"])
    recall_count = len(fixture["recalls"]) + 1
    embedding_items = claim_count + recall_count
    rerank_documents = claim_count * recall_count
    return {
        "llm_requests": 1,
        "embedding_requests": math.ceil(embedding_items / 10),
        "embedding_items": embedding_items,
        "reranker_requests": recall_count,
        "rerank_documents": rerank_documents,
    }


def _accepted_claim_count(actual: int, expected_minimum: int) -> bool:
    """Keep Provider smoke focused on pipeline health, not exact extraction granularity."""
    return actual >= expected_minimum


def _preflight(
    settings: Any,
    price_book: UsagePriceBook,
    fixture: Mapping[str, Any],
    limits: LiveSmokeLimits,
) -> dict[str, int]:
    forecast = _forecast(fixture)
    for name in ("llm_requests", "embedding_items", "rerank_documents"):
        if forecast[name] > getattr(limits, name):
            raise LiveSmokeBudgetError(f"preflight {name} would exceed the configured limit")
    estimates = (
        (
            UsageIdentity(ProviderCapability.LLM, "extract", "preflight", settings.llm_provider, settings.llm_model),
            UsageAmount(requests=forecast["llm_requests"], input_tokens=2048, output_tokens=4096),
        ),
        (
            UsageIdentity(
                ProviderCapability.EMBEDDING,
                "embed",
                "preflight",
                settings.embedding_provider,
                settings.embedding_model,
            ),
            UsageAmount(
                requests=forecast["embedding_requests"],
                input_tokens=4096,
                embedding_items=forecast["embedding_items"],
            ),
        ),
        (
            UsageIdentity(
                ProviderCapability.RERANKER,
                "rerank",
                "preflight",
                settings.reranker_provider,
                settings.reranker_model,
            ),
            UsageAmount(
                requests=forecast["reranker_requests"],
                input_tokens=8192,
                output_tokens=1024,
                rerank_documents=forecast["rerank_documents"],
            ),
        ),
    )
    total_cost = 0
    for identity, estimate in estimates:
        priced = price_book.price(identity, estimate, phase="reserve")
        if priced.cost_microunits is None:
            raise LiveSmokeBudgetError("preflight has unknown cost because a configured model price rule is missing")
        total_cost += priced.cost_microunits
    forecast["cost_microunits"] = total_cost
    if total_cost > limits.cost_microunits:
        raise LiveSmokeBudgetError("preflight cost_microunits would exceed the configured limit")
    return forecast


def _temporary_document(raw: dict[str, Any], database_name: str, price_name: str, limits: LiveSmokeLimits) -> str:
    document = deepcopy(raw)
    document.setdefault("database", {})["path"] = database_name
    usage = document.setdefault("usage", {})
    usage["price_book_path"] = price_name
    usage["daily_request_limit"] = 0
    usage["daily_cost_limit_microunits"] = limits.cost_microunits
    return tomli_w.dumps(document)


def _atomic_write_json(output: Path, result: Mapping[str, object]) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{output.name}.", suffix=".tmp", dir=output.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(result, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)


def _safe_counts(connection: sqlite3.Connection) -> dict[str, int]:
    return {
        "claims": int(connection.execute("SELECT count(*) FROM claims").fetchone()[0]),
        "evidence_links": int(connection.execute("SELECT count(*) FROM evidence_links").fetchone()[0]),
        "canonical_entities": int(connection.execute("SELECT count(*) FROM canonical_entities").fetchone()[0]),
        "claim_entity_links": int(connection.execute("SELECT count(*) FROM claim_entity_links").fetchone()[0]),
        "vectors": int(
            connection.execute("SELECT count(*) FROM claims WHERE embedding_dense IS NOT NULL").fetchone()[0]
        ),
    }


def _active_reservations(path: Path) -> int:
    connection = sqlite3.connect(path)
    try:
        return int(connection.execute("SELECT count(*) FROM usage_reservations WHERE state='active'").fetchone()[0])
    finally:
        connection.close()


def _assert_final_limits(counters: Mapping[str, int], limits: LiveSmokeLimits) -> None:
    for name in ("llm_requests", "embedding_items", "rerank_documents", "cost_microunits"):
        if counters[name] > getattr(limits, name):
            raise LiveSmokeBudgetError(f"final ledger {name} exceeded the configured limit")
    if counters["active_reservations"] != 0:
        raise LiveSmokeBudgetError("final ledger contains active reservations")


def _failed_pipeline_evidence(
    runtime: Any,
    connection: sqlite3.Connection | None,
    latencies: Mapping[str, float],
    execution_counts: Mapping[str, int],
) -> tuple[dict[str, int], dict[str, float], list[str], dict[str, bool]] | None:
    usage = runtime.governor.snapshot()
    settled = usage["settled"]
    reserved = usage["reserved"]
    ledger = sqlite3.connect(runtime.governor.path)
    ledger.row_factory = sqlite3.Row
    try:
        active = ledger.execute(
            "SELECT COUNT(*) reservations,COALESCE(SUM(attempts),0) attempts,"
            "COALESCE(SUM(CASE WHEN capability='llm' THEN reserved_requests ELSE 0 END),0) llm_requests "
            "FROM usage_reservations WHERE state='active'"
        ).fetchone()
    finally:
        ledger.close()
    if int(settled["requests"]) + int(active["attempts"]) == 0:
        return None

    persisted = {
        "claims": 0,
        "evidence_links": 0,
        "canonical_entities": 0,
        "claim_entity_links": 0,
        "vectors": 0,
    }
    if connection is not None:
        try:
            persisted = _safe_counts(connection)
        except sqlite3.Error:
            pass
    settled_cost = settled["cost_microunits"]
    reserved_cost = reserved["cost_microunits"]
    counters = {
        "llm_requests": int(usage["counts_by_capability"].get("llm", 0)) + int(active["llm_requests"]),
        "embedding_items": int(settled["embedding_items"]) + int(reserved["embedding_items"]),
        "rerank_documents": int(settled["rerank_documents"]) + int(reserved["rerank_documents"]),
        "cost_microunits": int(settled_cost or 0) + int(reserved_cost or 0),
        "active_reservations": int(active["reservations"]),
        **persisted,
        **execution_counts,
    }
    checks = {
        "extract_ingest": False,
        "claim_persistence": False,
        "evidence_persistence": False,
        "entity_persistence": False,
        "vector_persistence": False,
        "ordinary_recall": False,
        "entity_recall": False,
        "temporal_recall": False,
        "preference_recall": False,
        "reranker_success": False,
        "reranker_failure_fallback": False,
        "usage_settled": int(usage["unknown_cost_count"]) == 0,
        "reservations_released": int(active["reservations"]) == 0,
    }
    fixed_latencies = {name: max(0.0, float(latencies.get(name, 0.0))) for name in ("ingest", "recall")}
    return counters, fixed_latencies, ["provider_pipeline_failure"], checks


def _close_all(resources: Iterable[Any]) -> bool:
    failed = False
    for resource in resources:
        close = getattr(resource, "close", None)
        if not callable(close):
            continue
        try:
            close()
        except Exception:
            failed = True
    return failed


def _run_pipeline(
    settings: Any,
    fixture: Mapping[str, Any],
    dependencies: _LiveSmokeDependencies,
    reservation_guard: Callable[[UsageIdentity, UsageAmount], None],
) -> tuple[dict[str, int], dict[str, float], list[str], dict[str, bool], tuple[str, ...], str]:
    client: httpx.Client | None = None
    runtime = None
    database: Database | None = None
    connection: sqlite3.Connection | None = None
    extractor: LLMExtractor | None = None
    embedder: Any = None
    reranker: Any = None
    latencies = {"ingest": 0.0, "recall": 0.0}
    execution_counts = {
        "recall_results": 0,
        "reranker_successful_recalls": 0,
        "ordinary_recall_results": 0,
        "entity_recall_results": 0,
        "temporal_recall_results": 0,
        "preference_recall_results": 0,
    }
    plugins: tuple[str, ...] = ()
    provider_kind = "mixed"
    active_stage: str | None = None
    stage_started = 0.0
    outcome: tuple[dict[str, int], dict[str, float], list[str], dict[str, bool], tuple[str, ...], str] | None = None
    pending_error: Exception | None = None
    try:
        client = dependencies.client_factory()
        runtime = create_provider_runtime(
            settings,
            entry_points=dependencies.entry_points,
            client=client,
            _reservation_guard=reservation_guard,
        )
        plugins = tuple(item["plugin_id"] for item in runtime.registry.health_snapshot())
        selected_plugins = {
            runtime.registry.plugin_id_for(ProviderKey(ProviderCapability.LLM, settings.llm_provider)),
            runtime.registry.plugin_id_for(ProviderKey(ProviderCapability.EMBEDDING, settings.embedding_provider)),
            runtime.registry.plugin_id_for(ProviderKey(ProviderCapability.RERANKER, settings.reranker_provider)),
        }
        provider_kinds = {
            "builtin" if plugin_id == "hl-mem.builtin" else "external_plugin" for plugin_id in selected_plugins
        }
        provider_kind = next(iter(provider_kinds)) if len(provider_kinds) == 1 else "mixed"
        initialize_process(settings)
        embedder = make_embedder(settings, runtime=runtime)
        reranker = make_reranker(settings, runtime=runtime)
        built_extractor = make_extractor(settings, require_real=True, runtime=runtime)
        if not isinstance(built_extractor, LLMExtractor):
            raise LiveSmokeSafetyError("production composition did not create LLMExtractor")
        extractor = built_extractor
        if (
            isinstance(extractor, FakeExtractor)
            or isinstance(embedder, FakeEmbedder)
            or isinstance(reranker, FakeReranker)
        ):
            raise LiveSmokeSafetyError("Fake components are forbidden in the live smoke")

        database = Database(settings=settings)
        connection = database.open_worker()
        event = {
            "tenant_id": "provider-smoke",
            "event_type": "message",
            "actor_type": "user",
            "content": {"text": str(fixture["text"])},
            "occurred_at": str(fixture["occurred_at"]),
            "sensitivity": "normal",
        }
        active_stage = "ingest"
        stage_started = time.perf_counter()
        event_result = IngestService(connection).ingest_event(event, idempotency_key="provider-live-smoke-v1")
        stored_event = EventRepository(connection, settings).get_event(str(event_result["id"]))
        if stored_event is None:
            raise RuntimeError("smoke event was not persisted")
        claims = extractor.extract(
            {"text": str(fixture["text"])},
            {"occurred_at": str(fixture["occurred_at"])},
        )
        now = datetime.now(timezone.utc).isoformat()
        stored = [IngestService.store_extracted(connection, claim, stored_event, now, embedder) for claim in claims]
        latencies["ingest"] = (time.perf_counter() - stage_started) * 1000

        active_stage = "recall"
        stage_started = time.perf_counter()
        recall_total = 0
        recall_counts: dict[str, int] = {}
        reranker_successful_recalls = 0
        for recall in fixture["recalls"]:
            response = RecallService(connection, embedder, reranker, settings=settings).recall(
                str(recall["query"]),
                limit=int(fixture["expected_claim_count"]),
                intent=str(recall["intent"]),
                as_of=str(recall["as_of"]) if "as_of" in recall else None,
                debug=True,
                namespace="provider-smoke",
            )
            label = str(recall["label"])
            recall_counts[label] = int(response["total"])
            recall_total += recall_counts[label]
            execution_counts[f"{label}_recall_results"] = recall_counts[label]
            execution_counts["recall_results"] = recall_total
            trace = response["search_trace"]
            if trace["reranker_error_class"] is None and any(
                candidate["rerank_rank"] is not None for candidate in trace["candidates"].values()
            ):
                reranker_successful_recalls += 1
        fallback = RecallService(connection, embedder, _ControlledFailureReranker(), settings=settings).recall(
            "Atlas Hub",
            limit=int(fixture["expected_claim_count"]),
            intent="current_state",
            debug=True,
            namespace="provider-smoke",
        )
        fallback_total = int(fallback["total"])
        recall_total += fallback_total
        execution_counts["recall_results"] = recall_total
        execution_counts["reranker_successful_recalls"] = reranker_successful_recalls
        latencies["recall"] = (time.perf_counter() - stage_started) * 1000
        active_stage = None

        persisted = _safe_counts(connection)
        usage = runtime.governor.snapshot()
        settled = cast(Mapping[str, Any], usage["settled"])
        counts_by_capability = cast(Mapping[str, Any], usage["counts_by_capability"])
        budget_path = runtime.governor.path
        counters = {
            "llm_requests": int(counts_by_capability.get("llm", 0)),
            "embedding_items": int(settled["embedding_items"]),
            "rerank_documents": int(settled["rerank_documents"]),
            "cost_microunits": int(settled["cost_microunits"] or 0),
            "active_reservations": _active_reservations(budget_path),
            "claims": persisted["claims"],
            "evidence_links": persisted["evidence_links"],
            "canonical_entities": persisted["canonical_entities"],
            "claim_entity_links": persisted["claim_entity_links"],
            "vectors": persisted["vectors"],
            "recall_results": recall_total,
            "reranker_successful_recalls": reranker_successful_recalls,
            **{f"{label}_recall_results": count for label, count in recall_counts.items()},
        }
        fallback_trace = fallback["search_trace"]
        checks = {
            "extract_ingest": _accepted_claim_count(len(claims), int(fixture["expected_claim_count"]))
            and all(item.status == "stored" for item in stored),
            "claim_persistence": persisted["claims"] >= int(fixture["expected_claim_count"]),
            "evidence_persistence": persisted["evidence_links"] >= int(fixture["expected_claim_count"]),
            "entity_persistence": persisted["canonical_entities"] > 0 and persisted["claim_entity_links"] > 0,
            "vector_persistence": persisted["vectors"] >= int(fixture["expected_claim_count"]),
            "ordinary_recall": recall_counts.get("ordinary", 0) > 0,
            "entity_recall": recall_counts.get("entity", 0) > 0,
            "temporal_recall": recall_counts.get("temporal", 0) > 0,
            "preference_recall": recall_counts.get("preference", 0) > 0,
            "reranker_success": reranker_successful_recalls > 0 and int(settled["rerank_documents"]) > 0,
            "reranker_failure_fallback": fallback_total > 0
            and fallback_trace["reranker_error_class"] == "RuntimeError",
            "usage_settled": usage["unknown_cost_count"] == 0,
            "reservations_released": counters["active_reservations"] == 0,
        }
        outcome = counters, latencies, [_SAFE_LABEL], checks, plugins, provider_kind
    except Exception as error:
        if active_stage is not None:
            latencies[active_stage] = (time.perf_counter() - stage_started) * 1000
        if runtime is None:
            pending_error = error
        else:
            failure = _failed_pipeline_evidence(runtime, connection, latencies, execution_counts)
            if failure is None:
                pending_error = error
            else:
                counters, failed_latencies, error_categories, checks = failure
                outcome = counters, failed_latencies, error_categories, checks, plugins, provider_kind
    finally:
        cleanup_failed = _close_all(
            (
                extractor.llm_client if extractor is not None else None,
                reranker,
                embedder,
                database,
                runtime,
                client,
            )
        )
    if outcome is None:
        if pending_error is None:
            raise RuntimeError("live smoke pipeline ended without an outcome")
        raise pending_error
    if cleanup_failed:
        counters, completed_latencies, _errors, checks, plugins, provider_kind = outcome
        return counters, completed_latencies, ["provider_pipeline_failure"], checks, plugins, provider_kind
    return outcome


def _run_live_smoke(
    config: Path,
    output: Path,
    *,
    env_file: Path | None,
    price_book: Path | None,
    limits: LiveSmokeLimits,
    dependencies: _LiveSmokeDependencies,
) -> dict[str, object]:
    config_path = _require_explicit_file(Path(config), "config")
    env_path = _require_explicit_file(env_file, "env file")
    price_path = _require_explicit_file(price_book, "price book")
    output_path = Path(output)
    if output_path.resolve() in {config_path.resolve(), env_path.resolve(), price_path.resolve()}:
        raise LiveSmokeSafetyError("output must not overwrite the config, env file, or price book")
    input_identities = {
        config_path: _file_identity(config_path),
        env_path: _file_identity(env_path),
        price_path: _file_identity(price_path),
    }
    try:
        result, failure_written = _run_live_smoke_impl(
            config_path,
            env_path=env_path,
            price_path=price_path,
            input_identities=input_identities,
            limits=limits,
            dependencies=dependencies,
            failure_output=output_path,
        )
        _verify_input_identities(input_identities)
        if not failure_written:
            _atomic_write_json(output_path, result)
    finally:
        _verify_input_identities(input_identities)
    return result


def _run_live_smoke_impl(
    config: Path,
    *,
    env_path: Path,
    price_path: Path,
    input_identities: Mapping[Path, _FileIdentity],
    limits: LiveSmokeLimits,
    dependencies: _LiveSmokeDependencies,
    failure_output: Path,
) -> tuple[dict[str, object], bool]:
    result: dict[str, object]
    with tempfile.TemporaryDirectory(
        prefix="hl-mem-provider-smoke-",
        dir=dependencies.temp_parent,
    ) as temporary_name:
        root = Path(temporary_name).resolve()
        copied_config = root / "source-config.toml"
        copied_env = root / "source-secrets.env"
        copied_price = root / "source-price-book.json"
        for source, destination in (
            (config, copied_config),
            (env_path, copied_env),
            (price_path, copied_price),
        ):
            _copy_verified_input(source, destination, input_identities[source])
        _verify_input_identities(input_identities)

        raw = _read_template(copied_config)
        database_name = _database_filename(raw)
        _reject_fake_components(raw)
        try:
            price_document = json.loads(copied_price.read_text(encoding="utf-8"))
            fixture = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise LiveSmokeSafetyError("fixture and price book must be readable JSON") from error
        source_urls = _safe_source_urls(price_document)
        loaded_price_book = UsagePriceBook.load(copied_price)
        config_fingerprint = input_identities[config].sha256
        fixture_sha = hashlib.sha256(_FIXTURE_PATH.read_bytes()).hexdigest()
        core_commit = _core_commit()

        database_path = root / database_name
        if database_path.parent.resolve() != root or database_path.exists():
            raise LiveSmokeSafetyError("database path escaped or pre-existed outside the new temporary root")
        temporary_config = root / "hl_mem.toml"
        temporary_config.write_text(
            _temporary_document(raw, database_name, copied_price.name, limits),
            encoding="utf-8",
            newline="\n",
        )
        settings = load_settings(
            temporary_config,
            copied_env,
            environ={},
            validate_runtime=True,
        )
        if Path(settings.database_path).resolve() != database_path:
            raise LiveSmokeSafetyError("loaded database path is not owned by the new temporary root")
        if Path(settings.usage_price_book_path or "").resolve() != copied_price.resolve():
            raise LiveSmokeSafetyError("loaded price book path is not owned by the new temporary root")
        _preflight(settings, loaded_price_book, fixture, limits)
        reservation_guard = _LiveSmokeReservationGuard(limits)
        try:
            counters, latency, error_categories, checks, plugins, provider_kind = _run_pipeline(
                settings,
                fixture,
                dependencies,
                reservation_guard.reserve,
            )
        except UsageLimitExceededError as error:
            if "cost estimate" in str(error):
                raise LiveSmokeBudgetError("provider ledger has unknown cost under the active money limit") from error
            raise LiveSmokeBudgetError("provider ledger rejected a call within the smoke limits") from error
        try:
            _assert_final_limits(counters, limits)
            final_budget = True
        except LiveSmokeBudgetError:
            final_budget = False
            error_categories = ["provider_pipeline_failure"]
        checks = {**checks, "final_budget": final_budget, "temporary_database": True}
        result = {
            "schema_version": 1,
            "passed": all(checks.values()) and "provider_pipeline_failure" not in error_categories,
            "provider_kind": provider_kind,
            "core_commit": core_commit,
            "run_at_utc": datetime.now(timezone.utc).isoformat(),
            "fixture_sha256": fixture_sha,
            "config_fingerprint": config_fingerprint,
            "labels": {
                "package_version": __version__,
                "plugins": sorted(set(plugins)),
                "models": {
                    "llm": settings.llm_model,
                    "embedding": settings.embedding_model,
                    "reranker": settings.reranker_model,
                },
            },
            "counters": counters,
            "latency_ms": latency,
            "price_book": {
                "sha256": input_identities[price_path].sha256,
                "fingerprint": loaded_price_book.fingerprint,
                "effective_date": str(price_document["effective_date"]),
                "source_urls": source_urls,
            },
            "error_categories": error_categories,
            "checks": checks,
        }
        schema = json.loads(_RESULT_SCHEMA_PATH.read_text(encoding="utf-8"))
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(result)
        failure_written = False
        if not result["passed"]:
            _verify_input_identities(input_identities)
            _atomic_write_json(failure_output, result)
            failure_written = True

    return result, failure_written


def run_live_smoke(
    config: Path,
    output: Path,
    *,
    env_file: Path,
    price_book: Path,
    limits: LiveSmokeLimits,
) -> dict[str, object]:
    """Run one governed smoke against resources owned by a fresh temporary root."""
    return _run_live_smoke(
        config,
        output,
        env_file=env_file,
        price_book=price_book,
        limits=limits,
        dependencies=_LiveSmokeDependencies(),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--env-file", required=True, type=Path)
    parser.add_argument("--price-book", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    result = run_live_smoke(
        args.config,
        args.output,
        env_file=args.env_file,
        price_book=args.price_book,
        limits=LiveSmokeLimits(),
    )
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["LiveSmokeBudgetError", "LiveSmokeLimits", "LiveSmokeSafetyError", "run_live_smoke"]
