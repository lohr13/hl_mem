"""LLM 调用 span 持久化测试。"""

from __future__ import annotations

from hl_mem.observability.llm_spans import LLMSpanRecorder, llm_span_stats
from hl_mem.storage.database import Database


def test_span_recorder_records_success(tmp_path) -> None:
    """成功调用应持久化 token、延迟和追踪字段。"""
    connection = Database(tmp_path / "spans.db").open()
    recorder = LLMSpanRecorder(connection)

    span_id = recorder.record(
        operation="extract",
        provider="zhipu",
        model="glm",
        status="success",
        latency_ms=12.5,
        started_at="2026-07-24T00:00:00+00:00",
        structured_mode="json_object",
        input_tokens=10,
        output_tokens=4,
        cached_tokens=2,
        total_tokens=14,
        trace_id="trace-1",
    )

    row = connection.execute("SELECT * FROM llm_call_spans WHERE span_id=?", (span_id,)).fetchone()
    assert row["trace_id"] == "trace-1"
    assert row["status"] == "success"
    assert (row["input_tokens"], row["output_tokens"], row["cached_tokens"], row["total_tokens"]) == (10, 4, 2, 14)


def test_span_recorder_records_error(tmp_path) -> None:
    """失败调用应记录错误类型。"""
    connection = Database(tmp_path / "spans-error.db").open()

    span_id = LLMSpanRecorder(connection).record(
        operation="conflict",
        provider="dashscope",
        model="qwen",
        status="error",
        latency_ms=3.0,
        started_at="2026-07-24T00:00:00+00:00",
        error_class="RuntimeError",
    )

    row = connection.execute("SELECT status,error_class FROM llm_call_spans WHERE span_id=?", (span_id,)).fetchone()
    assert tuple(row) == ("error", "RuntimeError")


def test_span_recorder_disabled_when_no_connection() -> None:
    """未注入连接时记录器应保持无操作。"""
    assert (
        LLMSpanRecorder().record(
            operation="other",
            provider="zhipu",
            model="glm",
            status="success",
            latency_ms=1.0,
            started_at="2026-07-24T00:00:00+00:00",
        )
        is None
    )


def test_llm_span_stats_aggregation(tmp_path) -> None:
    """统计应按 operation/provider/model/status 聚合。"""
    connection = Database(tmp_path / "spans-stats.db").open()
    recorder = LLMSpanRecorder(connection)
    for total_tokens, latency_ms in ((10, 10.0), (20, 30.0)):
        recorder.record(
            operation="extract",
            provider="zhipu",
            model="glm",
            status="success",
            latency_ms=latency_ms,
            started_at="2026-07-24T00:00:00+00:00",
            total_tokens=total_tokens,
        )

    assert llm_span_stats(connection)["operations"] == [
        {
            "operation": "extract",
            "provider": "zhipu",
            "model": "glm",
            "status": "success",
            "count": 2,
            "total_tokens": 30,
            "avg_latency_ms": 20.0,
        }
    ]


def test_llm_span_migration_is_registered(tmp_path) -> None:
    """打开数据库应应用并注册 019 migration。"""
    connection = Database(tmp_path / "migration.db").open()
    version = connection.execute(
        "SELECT version FROM schema_migrations WHERE version='019_llm_call_spans'"
    ).fetchone()
    assert version is not None
