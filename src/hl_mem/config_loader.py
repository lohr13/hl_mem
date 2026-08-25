"""从单个 TOML 文件和独立密钥边界加载不可变配置快照。"""

from __future__ import annotations

import os
import sys
import tomllib
import types
from collections.abc import Mapping
from dataclasses import Field, fields
from enum import StrEnum
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Literal, Union, get_args, get_origin, get_type_hints

from hl_mem.errors import ConfigurationError
from hl_mem.settings import Settings


def _resolve_database_path(raw_path: str, resolved_config_path: Path, platform: str) -> str:
    """按配置文件真实目录解析数据库路径，并拒绝异平台绝对路径。"""
    windows_absolute = PureWindowsPath(raw_path).is_absolute()
    posix_absolute = PurePosixPath(raw_path).is_absolute()
    if platform == "win32":
        if windows_absolute:
            return raw_path
        if posix_absolute:
            raise ConfigurationError(
                f"{resolved_config_path}: database.path: POSIX absolute path is not valid on Windows: {raw_path}"
            )
    else:
        if posix_absolute:
            return raw_path
        if windows_absolute:
            raise ConfigurationError(
                f"{resolved_config_path}: database.path: Windows absolute path is not valid on POSIX: {raw_path}"
            )
    return str((resolved_config_path.resolve().parent / raw_path).resolve())


def _read_env_file(path: Path, secret_names: frozenset[str]) -> dict[str, str]:
    """读取简单 dotenv 赋值，只保留 schema 声明的密钥。"""
    if not path.is_file():
        return {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise ConfigurationError(f"{path}: failed to read secret file: {error}") from error

    values: dict[str, str] = {}
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        name, raw_value = line.split("=", 1)
        name = name.strip()
        if name not in secret_names:
            continue
        value = raw_value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].rstrip()
        values[name] = value
    return values


def _expected_type(annotation: Any) -> str:
    origin = get_origin(annotation)
    if origin is Literal:
        return "one of " + ", ".join(repr(value) for value in get_args(annotation))
    if origin in {tuple, list}:
        arguments = get_args(annotation)
        item_type = _expected_type(arguments[0]) if arguments else "value"
        return f"array of {item_type}"
    if origin in {types.UnionType, Union}:
        return " or ".join(_expected_type(item) for item in get_args(annotation))
    if isinstance(annotation, type) and issubclass(annotation, StrEnum):
        return "one of " + ", ".join(repr(item.value) for item in annotation)
    return getattr(annotation, "__name__", str(annotation))


def _type_error(path: Path, key_path: str, annotation: Any) -> ConfigurationError:
    return ConfigurationError(f"{path}: {key_path}: expected {_expected_type(annotation)}")


def _coerce_toml_value(value: Any, annotation: Any, path: Path, key_path: str) -> Any:
    """校验 TOML 原生类型，并执行 array->tuple 与字符串枚举转换。"""
    origin = get_origin(annotation)
    arguments = get_args(annotation)
    if origin in {types.UnionType, Union}:
        non_none = tuple(item for item in arguments if item is not type(None))
        for item in non_none:
            try:
                return _coerce_toml_value(value, item, path, key_path)
            except ConfigurationError:
                continue
        raise _type_error(path, key_path, annotation)
    if origin is Literal:
        if value not in arguments or isinstance(value, bool) != any(
            isinstance(item, bool) for item in arguments if item == value
        ):
            raise _type_error(path, key_path, annotation)
        return value
    if origin is tuple:
        if not isinstance(value, list):
            raise _type_error(path, key_path, annotation)
        item_annotation = arguments[0] if arguments else Any
        return tuple(
            _coerce_toml_value(item, item_annotation, path, f"{key_path}[{index}]") for index, item in enumerate(value)
        )
    if isinstance(annotation, type) and issubclass(annotation, StrEnum):
        if not isinstance(value, str):
            raise _type_error(path, key_path, annotation)
        try:
            return annotation(value)
        except ValueError as error:
            raise _type_error(path, key_path, annotation) from error
    if annotation is Any:
        return value
    if annotation is bool:
        if type(value) is not bool:
            raise _type_error(path, key_path, annotation)
        return value
    if annotation is int:
        if type(value) is not int:
            raise _type_error(path, key_path, annotation)
        return value
    if annotation is float:
        if type(value) not in {int, float}:
            raise _type_error(path, key_path, annotation)
        return float(value)
    if annotation is str:
        if not isinstance(value, str):
            raise _type_error(path, key_path, annotation)
        return value
    if not isinstance(value, annotation):
        raise _type_error(path, key_path, annotation)
    return value


def _toml_schema() -> tuple[dict[str, Field[Any]], frozenset[str], dict[str, Field[Any]]]:
    toml_fields: dict[str, Field[Any]] = {}
    table_paths: set[str] = set()
    secret_fields: dict[str, Field[Any]] = {}
    for settings_field in fields(Settings):
        if set(settings_field.metadata) not in ({"toml"}, {"secret_env"}):
            raise RuntimeError(
                f"Settings.{settings_field.name} must declare exactly one supported configuration source"
            )
        toml_path = settings_field.metadata.get("toml")
        secret_env = settings_field.metadata.get("secret_env")
        if toml_path is not None:
            if str(toml_path) in toml_fields:
                raise RuntimeError(f"duplicate Settings TOML path: {toml_path}")
            toml_fields[str(toml_path)] = settings_field
            parts = str(toml_path).split(".")
            table_paths.update(".".join(parts[:index]) for index in range(1, len(parts)))
        else:
            if str(secret_env) in secret_fields:
                raise RuntimeError(f"duplicate Settings secret environment name: {secret_env}")
            secret_fields[str(secret_env)] = settings_field
    return toml_fields, frozenset(table_paths), secret_fields


def _flatten_toml(
    data: Mapping[str, Any],
    path: Path,
    toml_fields: Mapping[str, Field[Any]],
    table_paths: frozenset[str],
    secret_fields: Mapping[str, Field[Any]],
) -> dict[str, Any]:
    flattened: dict[str, Any] = {}
    forbidden_paths = {
        "llm.api_key",
        "embedding.api_key",
        "reranker.api_key",
        "image_describer.api_key",
        *(settings_field.name for settings_field in secret_fields.values()),
        *(name for name in secret_fields),
    }

    def visit(table: Mapping[str, Any], prefix: str = "") -> None:
        for key, value in table.items():
            key_path = f"{prefix}.{key}" if prefix else key
            if key_path in forbidden_paths:
                raise ConfigurationError(f"{path}: {key_path}: secrets must not appear in TOML")
            settings_field = toml_fields.get(key_path)
            if isinstance(value, dict):
                if settings_field is not None:
                    flattened[key_path] = value
                elif key_path not in table_paths:
                    raise ConfigurationError(f"{path}: {key_path}: unknown TOML table")
                else:
                    visit(value, key_path)
            elif settings_field is None:
                raise ConfigurationError(f"{path}: {key_path}: unknown TOML key")
            else:
                flattened[key_path] = value

    visit(data)
    return flattened


def load_settings(
    config_path: Path | None = None,
    env_path: Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> Settings:
    """加载并校验唯一配置快照，不创建组件或修改进程环境。"""
    resolved_config_path = Path(config_path) if config_path is not None else Path.cwd() / "hl_mem.toml"
    resolved_env_path = Path(env_path) if env_path is not None else Path.cwd() / ".env"
    if not resolved_config_path.is_file():
        raise ConfigurationError(f"{resolved_config_path}: configuration file does not exist")

    try:
        with resolved_config_path.open("rb") as stream:
            toml_data = tomllib.load(stream)
    except tomllib.TOMLDecodeError as error:
        raise ConfigurationError(f"{resolved_config_path}: invalid TOML: {error}") from error
    except OSError as error:
        raise ConfigurationError(f"{resolved_config_path}: failed to read configuration: {error}") from error

    toml_fields, table_paths, secret_fields = _toml_schema()
    flattened = _flatten_toml(
        toml_data,
        resolved_config_path,
        toml_fields,
        table_paths,
        secret_fields,
    )
    annotations = get_type_hints(Settings)
    values: dict[str, Any] = {}
    for key_path, value in flattened.items():
        settings_field = toml_fields[key_path]
        values[settings_field.name] = _coerce_toml_value(
            value,
            annotations[settings_field.name],
            resolved_config_path,
            key_path,
        )

    database_field = toml_fields["database.path"]
    values[database_field.name] = _resolve_database_path(
        values.get(database_field.name, database_field.default),
        resolved_config_path,
        sys.platform,
    )

    secret_names = frozenset(secret_fields)
    secret_values = _read_env_file(resolved_env_path, secret_names)
    process_environment = environ if environ is not None else os.environ
    for secret_name, settings_field in secret_fields.items():
        if secret_name in process_environment:
            secret_values[secret_name] = process_environment[secret_name]
        if secret_name in secret_values:
            values[settings_field.name] = secret_values[secret_name]

    settings = Settings(**values)
    try:
        settings.validate()
    except ConfigurationError as error:
        raise ConfigurationError(f"{resolved_config_path}: {error}") from error
    return settings
