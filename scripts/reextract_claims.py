"""重新提取全部事件的 claim，并回填 canonical_slot 与 topic_tags。

单实例锁机制
-----------
本脚本通过文件锁确保同一时间只有一个 reextract 实例运行，避免多个进程
同时写入 SQLite 导致 "database is locked" 错误。

锁文件位于 ``var/reextract.lock``，内容为持有锁的进程 PID。
- 在 Unix 上，使用 ``fcntl.flock(LOCK_EX | LOCK_NB)`` 实现原子排他锁。
- 在 Windows 上（无 fcntl），退化为 PID 文件 + 进程存活检查。
- 启动时若发现锁文件存在，先检查其中 PID 对应的进程是否存活：
  - 若进程已不存在（崩溃残留），自动清理过期锁文件并继续。
  - 若进程仍存活，打印错误并退出（exit code 1）。
- 脚本正常或异常退出时，通过 try/finally 自动释放锁并清理锁文件。
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import sqlite3
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, TextIO

try:
    import fcntl as _fcntl

    _HAS_FCNTL = True
except ImportError:
    _HAS_FCNTL = False

import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from hl_mem.application.ingest import IngestService, claim_text  # noqa: E402
from hl_mem.components import make_embedder, make_extractor  # noqa: E402
from hl_mem.domain.claims.attributes import normalize_topic_tags, validate_slot_instance  # noqa: E402
from hl_mem.domain.claims.claim import build_index_text  # noqa: E402
from hl_mem.domain.claims.conflicts import compute_conflict_key  # noqa: E402
from hl_mem.domain.entity import normalize_entity_id  # noqa: E402
from hl_mem.ingest.extractors import ExtractedClaim  # noqa: E402
from hl_mem.settings import Settings  # noqa: E402
from hl_mem.storage._shared import decode_json  # noqa: E402
from hl_mem.storage.database import Database  # noqa: E402

PLAN_SCHEMA_VERSION = 1
DEFAULT_BATCH_SIZE = 50
DEFAULT_PROGRESS_EVERY = 25


def load_project_env(path: Path) -> None:
    """从项目环境文件补充未显式设置的运行时配置。"""
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        os.environ.setdefault(name.strip(), value.strip().strip('"').strip("'"))


def parse_args() -> argparse.Namespace:
    """解析数据重提取命令行参数。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="仅提取并生成执行计划，不修改 claims")
    parser.add_argument("--database", type=Path, help="数据库路径；默认读取 HL_MEM_DB_PATH/Settings")
    parser.add_argument("--plan", type=Path, help="dry-run 计划路径；默认与数据库位于同一目录")
    parser.add_argument("--limit", type=int, help="最多处理的事件数")
    parser.add_argument(
        "--batch-size",
        type=int,
        default=int(os.getenv("HL_MEM_REEXTRACT_BATCH_SIZE", str(DEFAULT_BATCH_SIZE))),
        help="数据库游标每批读取的事件数",
    )
    parser.add_argument(
        "--progress-every",
        type=int,
        default=int(os.getenv("HL_MEM_REEXTRACT_PROGRESS_EVERY", str(DEFAULT_PROGRESS_EVERY))),
        help="每处理多少个事件输出一次进度",
    )
    args = parser.parse_args()
    if args.batch_size < 1 or args.progress_every < 1 or (args.limit is not None and args.limit < 1):
        parser.error("--batch-size, --progress-every, and --limit must be positive")
    return args


def iter_events(connection: sqlite3.Connection, batch_size: int) -> Iterator[dict[str, Any]]:
    """用 fetchmany 分批流式读取全部事件。"""
    cursor = connection.execute("SELECT * FROM events ORDER BY occurred_at,id")
    while rows := cursor.fetchmany(batch_size):
        for row in rows:
            event = dict(row)
            event["content"] = decode_json(event["content_json"])
            yield event


def extraction_context(connection: sqlite3.Connection, event: dict[str, Any]) -> dict[str, Any]:
    """按在线 worker 契约构造事件时间锚点与最近会话上下文。"""
    context: dict[str, Any] = {"occurred_at": event["occurred_at"], "recent_events": []}
    if not event.get("session_id"):
        return context
    rows = connection.execute(
        "SELECT * FROM events WHERE session_id=? AND "
        "(occurred_at<? OR (occurred_at=? AND id<?)) "
        "ORDER BY occurred_at DESC,id DESC LIMIT 3",
        (event["session_id"], event["occurred_at"], event["occurred_at"], event["id"]),
    ).fetchall()
    context["recent_events"] = [{**dict(row), "content": decode_json(row["content_json"])} for row in reversed(rows)]
    return context


def claim_state_hash(connection: sqlite3.Connection) -> str:
    """计算重提取相关 claim 字段的稳定哈希，防止应用陈旧计划。"""
    digest = hashlib.sha256()
    rows = connection.execute(
        "SELECT id,subject_entity_id,predicate,value_json,canonical_slot,topic_tags_json " "FROM claims ORDER BY id"
    )
    for row in rows:
        digest.update(json.dumps(list(row), ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def find_matching_claim(
    connection: sqlite3.Connection,
    event_id: str,
    extracted: ExtractedClaim,
) -> dict[str, Any] | None:
    """按 subject、predicate、value 精确匹配，并优先选择当前事件已有证据链接的 claim。"""
    subject = normalize_entity_id(extracted.subject)
    candidates = connection.execute(
        "SELECT c.*,CASE WHEN el.derived_id IS NULL THEN 1 ELSE 0 END AS evidence_priority "
        "FROM claims c LEFT JOIN evidence_links el ON el.derived_type='claim' AND el.derived_id=c.id "
        "AND el.evidence_type='event' AND el.evidence_id=? "
        "WHERE c.subject_entity_id=? AND c.predicate=? "
        "ORDER BY evidence_priority,c.recorded_from DESC,c.id",
        (event_id, subject, extracted.predicate),
    ).fetchall()
    for row in candidates:
        if decode_json(row["value_json"]) == extracted.value:
            return dict(row)
    return None


def normalized_classification(extracted: ExtractedClaim) -> tuple[str | None, list[str]]:
    """按持久化边界规则规范化 operational slot 与 topic tags。"""
    qualifiers = extracted.qualifiers or {}
    slot = validate_slot_instance(extracted.canonical_slot, qualifiers)
    tags = normalize_topic_tags(extracted.topic_tags)
    return slot, tags


def classify_action(existing: dict[str, Any] | None, extracted: ExtractedClaim) -> str:
    """判断提取结果应新增、更新还是保持不变。"""
    if existing is None:
        return "add"
    slot, tags = normalized_classification(extracted)
    old_tags = decode_json(existing.get("topic_tags_json") or "[]")
    return "unchanged" if existing.get("canonical_slot") == slot and old_tags == tags else "update"


def write_json_line(stream: TextIO, payload: dict[str, Any]) -> None:
    """以 UTF-8 JSONL 格式写入一条可恢复计划记录。"""
    stream.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    stream.flush()


def dry_run(
    connection: sqlite3.Connection,
    settings: Settings,
    plan_path: Path,
    batch_size: int,
    progress_every: int,
    limit: int | None,
) -> dict[str, int]:
    """执行真实提取并生成不修改 claims 的可复用计划。"""
    extractor = make_extractor(settings, require_real=True, connection=connection)
    counts = {"events": 0, "extracted": 0, "add": 0, "update": 0, "unchanged": 0, "errors": 0}
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = plan_path.with_suffix(plan_path.suffix + ".tmp")
    with temporary_path.open("w", encoding="utf-8", newline="\n") as stream:
        write_json_line(
            stream,
            {
                "type": "header",
                "schema_version": PLAN_SCHEMA_VERSION,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "database": str(Path(settings.database_path).resolve()),
                "claim_state_hash": claim_state_hash(connection),
            },
        )
        for event in iter_events(connection, batch_size):
            if limit is not None and counts["events"] >= limit:
                break
            counts["events"] += 1
            try:
                extracted_claims = extractor.extract(event["content"], extraction_context(connection, event))
                records: list[dict[str, Any]] = []
                for extracted in extracted_claims:
                    existing = find_matching_claim(connection, event["id"], extracted)
                    action = classify_action(existing, extracted)
                    counts["extracted"] += 1
                    counts[action] += 1
                    records.append(
                        {
                            "action": action,
                            "existing_claim_id": existing["id"] if existing else None,
                            "claim": asdict(extracted),
                        }
                    )
                write_json_line(stream, {"type": "event", "event_id": event["id"], "claims": records})
            except Exception as error:  # 逐事件报告，最终以非零状态阻止应用不完整计划。
                counts["errors"] += 1
                write_json_line(
                    stream,
                    {
                        "type": "error",
                        "event_id": event["id"],
                        "error_class": type(error).__name__,
                        "error": str(error),
                    },
                )
                if isinstance(error, httpx.HTTPStatusError) and error.response.status_code in {401, 403, 429}:
                    raise RuntimeError(
                        f"LLM provider rejected extraction with HTTP {error.response.status_code}; "
                        "check credentials and quota before retrying"
                    ) from error
            if counts["events"] % progress_every == 0:
                print(f"dry-run progress: {json.dumps(counts, ensure_ascii=False)}", flush=True)
        write_json_line(stream, {"type": "summary", **counts})
    temporary_path.replace(plan_path)
    print(f"dry-run report: {json.dumps(counts, ensure_ascii=False)}")
    print(f"plan: {plan_path}")
    return counts


def update_existing_claim(
    connection: sqlite3.Connection,
    existing_claim_id: str,
    extracted: ExtractedClaim,
    embedder: Any,
) -> bool:
    """原位更新分类与检索表示，不触碰证据、反馈和生命周期字段。"""
    row = connection.execute("SELECT * FROM claims WHERE id=?", (existing_claim_id,)).fetchone()
    if row is None:
        raise RuntimeError(f"planned claim disappeared: {existing_claim_id}")
    current = dict(row)
    slot, tags = normalized_classification(extracted)
    old_tags = decode_json(current.get("topic_tags_json") or "[]")
    if current.get("canonical_slot") == slot and old_tags == tags:
        return False
    qualifiers = decode_json(current.get("qualifiers_json") or "{}")
    topic_tags_json = json.dumps(tags, ensure_ascii=False, separators=(",", ":"))
    index_claim = {
        "subject_entity_id": current.get("subject_entity_id"),
        "predicate": current.get("predicate"),
        "value": decode_json(current.get("value_json")),
        "canonical_slot": slot,
        "topic_tags": tags,
    }
    index_text = build_index_text(index_claim)
    embedding_dense = embedder.embed_one(claim_text({**index_claim, "index_text": index_text}))
    conflict_key = compute_conflict_key(
        current["namespace_key"],
        current["subject_entity_id"],
        current["predicate"],
        slot,
        qualifiers,
    )
    cursor = connection.execute(
        "UPDATE claims SET canonical_slot=?,topic_tags_json=?,index_text=?,embedding_dense=?,"
        "embedding_model=?,embedding_dim=?,conflict_key=?,conflict_key_version=3 WHERE id=?",
        (
            slot,
            topic_tags_json,
            index_text,
            embedding_dense,
            getattr(embedder, "model", current.get("embedding_model")),
            embedder.dim,
            conflict_key,
            existing_claim_id,
        ),
    )
    connection.commit()
    return cursor.rowcount == 1


def apply_plan(
    connection: sqlite3.Connection,
    settings: Settings,
    plan_path: Path,
    progress_every: int,
    limit: int | None,
) -> dict[str, int]:
    """校验并应用 dry-run 计划。"""
    if not plan_path.is_file():
        raise FileNotFoundError(f"dry-run plan not found: {plan_path}")
    counts = {"events": 0, "extracted": 0, "add": 0, "update": 0, "unchanged": 0, "errors": 0}
    embedder = make_embedder(settings)
    with plan_path.open("r", encoding="utf-8") as stream:
        header = json.loads(stream.readline())
        if header.get("type") != "header" or header.get("schema_version") != PLAN_SCHEMA_VERSION:
            raise RuntimeError("unsupported or malformed dry-run plan")
        if Path(header["database"]) != Path(settings.database_path).resolve():
            raise RuntimeError("dry-run plan targets a different database")
        if header.get("claim_state_hash") != claim_state_hash(connection):
            raise RuntimeError("claim data changed after dry-run; generate a fresh plan")
        for line in stream:
            record = json.loads(line)
            if record["type"] == "summary":
                if record.get("errors"):
                    raise RuntimeError(f"dry-run had {record['errors']} extraction errors; refusing partial apply")
                continue
            if record["type"] == "error":
                continue
            if record["type"] != "event":
                raise RuntimeError(f"unknown plan record type: {record['type']}")
            if limit is not None and counts["events"] >= limit:
                continue
            event_row = connection.execute("SELECT * FROM events WHERE id=?", (record["event_id"],)).fetchone()
            if event_row is None:
                raise RuntimeError(f"planned event disappeared: {record['event_id']}")
            event = dict(event_row)
            event["content"] = decode_json(event["content_json"])
            counts["events"] += 1
            for item in record["claims"]:
                extracted = ExtractedClaim(**item["claim"])
                counts["extracted"] += 1
                action = item["action"]
                if action == "unchanged":
                    counts["unchanged"] += 1
                elif action == "update":
                    changed = update_existing_claim(connection, item["existing_claim_id"], extracted, embedder)
                    counts["update" if changed else "unchanged"] += 1
                elif action == "add":
                    result = IngestService.store_extracted(
                        connection,
                        extracted,
                        {**event, "extractor": "llm"},
                        datetime.now(timezone.utc).isoformat(),
                        embedder,
                        policy=settings.retention_policy(),
                        relation_discovery_mode="off",
                    )
                    if result.status == "skipped":
                        counts["unchanged"] += 1
                    elif result.reason == "inserted":
                        counts["add"] += 1
                    else:
                        counts["unchanged"] += 1
                else:
                    raise RuntimeError(f"unknown planned action: {action}")
            if counts["events"] % progress_every == 0:
                print(f"apply progress: {json.dumps(counts, ensure_ascii=False)}", flush=True)
    print(f"apply report: {json.dumps(counts, ensure_ascii=False)}")
    return counts


def _pid_is_alive(pid: int) -> bool:
    """检查指定 PID 的进程是否存活。"""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # 进程存在但无权限发送信号
        return True
    except OSError:
        return False
    return True


def _acquire_lock(lock_path: Path) -> int | None:
    """获取排他锁，返回锁文件描述符（Unix）或 None（Windows PID-only 模式）。

    如果锁已被其他存活进程持有，打印错误并 sys.exit(1)。
    如果锁文件存在但持有进程已死亡，自动清理过期锁文件。
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    # 检查是否存在残留锁文件
    if lock_path.exists():
        try:
            old_pid = int(lock_path.read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            old_pid = 0

        if old_pid and _pid_is_alive(old_pid):
            print(
                f"error: another reextract process is already running (PID {old_pid}). "
                f"Lock file: {lock_path}",
                file=sys.stderr,
            )
            sys.exit(1)
        else:
            # 进程已不存在，清理过期锁文件
            if old_pid:
                print(
                    f"warning: stale lock file found (PID {old_pid} no longer running), removing",
                    file=sys.stderr,
                )
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass

    # 写入当前 PID
    lock_path.write_text(str(os.getpid()), encoding="utf-8")

    # Unix: 使用 fcntl.flock 获取原子排他锁
    if _HAS_FCNTL:
        fd = os.open(str(lock_path), os.O_RDWR)
        try:
            _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                os.close(fd)
                print(
                    f"error: could not acquire lock on {lock_path} (another instance running)",
                    file=sys.stderr,
                )
                sys.exit(1)
            raise
        return fd

    # Windows: 无 fcntl，仅依赖 PID 文件 + 进程检查（已在上方完成）
    return None


def _release_lock(lock_path: Path, fd: int | None) -> None:
    """释放锁并清理锁文件。"""
    if fd is not None and _HAS_FCNTL:
        try:
            _fcntl.flock(fd, _fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass
    try:
        lock_path.unlink()
    except FileNotFoundError:
        pass


def main() -> int:
    """加载配置，执行 dry-run 或应用已生成的重提取计划。"""
    load_project_env(PROJECT_ROOT / ".env")
    os.environ.setdefault("HL_MEM_EXTRACTOR", "real")
    args = parse_args()

    # 获取单实例锁，防止多个 reextract 进程并发写入数据库
    lock_path = PROJECT_ROOT / "var" / "reextract.lock"
    lock_fd = _acquire_lock(lock_path)
    try:
        return _run(args)
    finally:
        _release_lock(lock_path, lock_fd)


def _run(args: argparse.Namespace) -> int:
    """实际的 main 逻辑（在锁保护下执行）。"""
    settings = Settings.from_env()
    if args.database is not None:
        settings = Settings(**{**settings.__dict__, "database_path": str(args.database)})
        settings.validate()
    database_path = Path(settings.database_path)
    plan_path = args.plan or database_path.with_name(f"{database_path.name}.reextract-plan.jsonl")
    database = Database(database_path)
    connection = database.open_worker()
    try:
        if args.dry_run:
            counts = dry_run(connection, settings, plan_path, args.batch_size, args.progress_every, args.limit)
            return 1 if counts["errors"] else 0
        apply_plan(connection, settings, plan_path, args.progress_every, args.limit)
        return 0
    finally:
        database.close()


if __name__ == "__main__":
    raise SystemExit(main())
