"""Deterministic, dry-run-first migration from the v0.36.1 config to schema v1."""

from __future__ import annotations

import copy
import hashlib
import os
import tempfile
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tomli_w

from hl_mem.config.loader import load_settings_data
from hl_mem.config.models import CONFIG_SCHEMA_VERSION, Settings
from hl_mem.errors import ConfigurationError
from hl_mem.storage.backup import validate_upgrade_recovery_set

_REMOVED_PATHS = (
    "extraction.pre_filter",
    "recall.tag_channel_enabled",
    "recall.tag_channel_weight",
    "recall.tag_candidate_limit",
    "relation.auto_apply_confidence",
    "relation.conflict_confidence",
)


@dataclass(frozen=True)
class MigrationChange:
    path: str
    before: object
    after: object
    reason: str


@dataclass(frozen=True)
class MigrationPlan:
    source: Path
    source_sha256: str
    source_version: int | None
    target_version: int
    document: str = field(repr=False)
    changes: tuple[MigrationChange, ...] = ()
    removed: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()
    database_path: Path | None = None
    recovery_required: bool = False

    @property
    def no_op(self) -> bool:
        return self.source_version == self.target_version and not self.changes and not self.removed


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _table(data: dict[str, Any], name: str) -> dict[str, Any] | None:
    value = data.get(name)
    if value is None:
        value = {}
        data[name] = value
    return value if isinstance(value, dict) else None


def _remove_path(data: dict[str, Any], path: str) -> bool:
    table_name, key = path.split(".", 1)
    table = data.get(table_name)
    if not isinstance(table, dict) or key not in table:
        return False
    del table[key]
    return True


def _set_mode(
    table: dict[str, Any] | None,
    *,
    table_name: str,
    key: str,
    old_default: object,
    from_value: object,
    to_value: object,
    reason: str,
    changes: list[MigrationChange],
) -> None:
    if table is None:
        return
    before = table.get(key, old_default)
    if before != from_value:
        return
    table[key] = to_value
    changes.append(MigrationChange(f"{table_name}.{key}", before, to_value, reason))


def _set_default(
    table: dict[str, Any] | None,
    *,
    table_name: str,
    key: str,
    value: object,
    reason: str,
    changes: list[MigrationChange],
) -> None:
    if table is None or key in table:
        return
    table[key] = value
    changes.append(MigrationChange(f"{table_name}.{key}", None, value, reason))


def _candidate_settings(
    data: Mapping[str, Any],
    *,
    source: Path,
    env_path: Path,
    environ: Mapping[str, str] | None,
    validate_runtime: bool,
) -> Settings:
    return load_settings_data(
        data,
        source_path=source,
        env_path=env_path,
        environ=environ,
        validate_runtime=validate_runtime,
    )


def plan_config_migration(
    config_path: Path,
    *,
    env_path: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> MigrationPlan:
    """Build and validate a migration plan without writing any file."""
    source = Path(config_path).expanduser().resolve()
    resolved_env = Path(env_path) if env_path is not None else source.parent / ".env"
    try:
        original = source.read_bytes()
    except OSError as error:
        raise ConfigurationError(f"{source}: failed to read configuration: {error}") from error
    try:
        parsed = tomllib.loads(original.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ConfigurationError(f"{source}: invalid TOML: {error}") from error

    raw_version = parsed.get("schema_version")
    source_version = raw_version if type(raw_version) is int else None
    changes: list[MigrationChange] = []
    removed: list[str] = []
    blockers: list[str] = []

    if raw_version is not None and raw_version != CONFIG_SCHEMA_VERSION:
        blockers.append(
            f"unsupported source schema_version {raw_version!r}; only unversioned v0.36.1 and schema_version 1 are supported"
        )
        document = original.decode("utf-8")
        return MigrationPlan(
            source=source,
            source_sha256=_sha256_bytes(original),
            source_version=source_version,
            target_version=CONFIG_SCHEMA_VERSION,
            document=document,
            blockers=tuple(blockers),
        )

    if raw_version == CONFIG_SCHEMA_VERSION:
        candidate = copy.deepcopy(parsed)
    else:
        candidate = {"schema_version": CONFIG_SCHEMA_VERSION, **copy.deepcopy(parsed)}
        changes.append(
            MigrationChange("schema_version", None, CONFIG_SCHEMA_VERSION, "adopt the versioned Core 1.0 schema")
        )
        extraction = _table(candidate, "extraction")
        embedding = _table(candidate, "embedding")
        recall = _table(candidate, "recall")
        relation = _table(candidate, "relation")
        dedup = _table(candidate, "dedup")
        worker = _table(candidate, "worker")
        plugins = _table(candidate, "plugins")

        if extraction is not None:
            extraction_mode = extraction.get("mode")
            if extraction_mode in {None, "fake"}:
                blockers.append("extraction.mode must be replaced with 'llm' and a configured production LLM service")
        if embedding is not None:
            embedding_mode = embedding.get("mode")
            if embedding_mode in {None, "fake"}:
                blockers.append(
                    "embedding.mode must be replaced with 'real' and a configured production embedding service"
                )

        _set_mode(
            recall,
            table_name="recall",
            key="query_expansion_mode",
            old_default="auto",
            from_value="auto",
            to_value="off",
            reason="make paid automatic query expansion opt-in",
            changes=changes,
        )
        _set_mode(
            recall,
            table_name="recall",
            key="resurrection_mode",
            old_default="auto",
            from_value="auto",
            to_value="off",
            reason="make state-changing resurrection opt-in",
            changes=changes,
        )
        _set_mode(
            relation,
            table_name="relation",
            key="discovery_mode",
            old_default="off",
            from_value="auto",
            to_value="audit",
            reason="require review before relation proposals affect memory",
            changes=changes,
        )
        _set_default(
            dedup,
            table_name="dedup",
            key="llm_enabled",
            value=False,
            reason="make paid semantic deduplication opt-in",
            changes=changes,
        )
        _set_default(
            worker,
            table_name="worker",
            key="semantic_conflict_consolidation_enabled",
            value=False,
            reason="make paid semantic conflict review opt-in",
            changes=changes,
        )
        _set_default(
            worker,
            table_name="worker",
            key="policy_induction_enabled",
            value=False,
            reason="make automatic policy publication opt-in",
            changes=changes,
        )
        _set_default(
            worker,
            table_name="worker",
            key="reclassify_enabled",
            value=False,
            reason="make paid semantic reclassification opt-in",
            changes=changes,
        )
        if plugins is not None and "enabled" not in plugins:
            plugins["enabled"] = []
            changes.append(MigrationChange("plugins.enabled", None, (), "declare an explicit plugin allowlist"))
        for path in _REMOVED_PATHS:
            if _remove_path(candidate, path):
                removed.append(path)

    document = tomli_w.dumps(candidate)
    try:
        emitted = tomllib.loads(document)
        structural = _candidate_settings(
            emitted,
            source=source,
            env_path=resolved_env,
            environ=environ,
            validate_runtime=False,
        )
    except (ConfigurationError, tomllib.TOMLDecodeError) as error:
        blockers.append(str(error))
        structural = None

    if not blockers:
        try:
            _candidate_settings(
                emitted,
                source=source,
                env_path=resolved_env,
                environ=environ,
                validate_runtime=True,
            )
        except ConfigurationError as error:
            blockers.append(str(error))

    database_path = Path(structural.database_path) if structural is not None else None
    return MigrationPlan(
        source=source,
        source_sha256=_sha256_bytes(original),
        source_version=source_version,
        target_version=CONFIG_SCHEMA_VERSION,
        document=document,
        changes=tuple(changes),
        removed=tuple(removed),
        blockers=tuple(dict.fromkeys(blockers)),
        database_path=database_path,
        recovery_required=database_path.is_file() if database_path is not None else False,
    )


def _write_exclusive(path: Path, content: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def apply_config_migration(
    plan: MigrationPlan,
    *,
    backup_path: Path | None = None,
    manifest_path: Path | None = None,
    env_path: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Apply a previously validated plan after recovery and stale-plan checks."""
    if plan.blockers:
        raise ValueError("configuration migration is blocked: " + "; ".join(plan.blockers))
    if plan.no_op:
        raise ValueError(f"configuration already uses schema_version {plan.target_version}")

    try:
        original = plan.source.read_bytes()
    except OSError as error:
        raise ConfigurationError(f"{plan.source}: failed to reread configuration: {error}") from error
    if _sha256_bytes(original) != plan.source_sha256:
        raise ValueError("configuration changed since migration was planned")

    resolved_env = Path(env_path) if env_path is not None else plan.source.parent / ".env"
    candidate = tomllib.loads(plan.document)
    _candidate_settings(
        candidate,
        source=plan.source,
        env_path=resolved_env,
        environ=environ,
        validate_runtime=True,
    )

    if plan.recovery_required:
        if backup_path is None or manifest_path is None:
            raise ValueError("an existing database requires both backup and manifest paths")
        if plan.database_path is None:
            raise RuntimeError("migration plan lost its database path")
        validate_upgrade_recovery_set(plan.database_path, backup_path, manifest_path)

    config_backup = plan.source.with_name(f"{plan.source.name}.v0.bak")
    if config_backup.exists():
        raise FileExistsError(f"configuration backup already exists: {config_backup}")

    temporary: Path | None = None
    backup_created = False
    try:
        _write_exclusive(config_backup, original)
        backup_created = True
        descriptor, raw_temporary = tempfile.mkstemp(
            prefix=f".{plan.source.name}.",
            suffix=".tmp",
            dir=plan.source.parent,
        )
        temporary = Path(raw_temporary)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(plan.document.encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        with temporary.open("rb") as stream:
            reloaded = tomllib.load(stream)
        _candidate_settings(
            reloaded,
            source=temporary,
            env_path=resolved_env,
            environ=environ,
            validate_runtime=True,
        )
        os.replace(temporary, plan.source)
        temporary = None
    except Exception:
        if backup_created:
            config_backup.unlink(missing_ok=True)
        raise
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return config_backup
