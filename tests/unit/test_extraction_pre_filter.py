"""Extraction pre-filter 的行为回归测试。"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import patch

from hl_mem.ingest.pre_filter import ExtractionPreFilter
from hl_mem.observability.audit import AuditLogger
from hl_mem.settings import Settings
from hl_mem.storage.events import EventRepository
from hl_mem.workers.worker import Worker


class ExtractionPreFilterTests(unittest.TestCase):
    """验证预筛只拒绝明确的控制流噪声。"""

    def setUp(self) -> None:
        self.pre_filter = ExtractionPreFilter()

    def test_explicit_memory_is_always_allowed(self) -> None:
        decision = self.pre_filter.evaluate(
            {"event_type": "explicit_memory", "actor_type": "user"},
            {"text": "[process] wait session=abc"},
        )
        self.assertTrue(decision.should_extract)
        self.assertEqual(decision.reason, "explicit_memory")

    def test_durable_fact_overrides_short_action_narration(self) -> None:
        decision = self.pre_filter.evaluate(
            {"event_type": "message", "actor_type": "assistant"},
            {"text": "Remember: the API port is 8200."},
        )
        self.assertTrue(decision.should_extract)
        self.assertEqual(decision.reason, "durable_signal")

    def test_tool_control_frame_is_skipped(self) -> None:
        decision = self.pre_filter.evaluate(
            {"event_type": "message", "actor_type": "tool"},
            {"text": "[process] wait session=proc_123"},
        )
        self.assertFalse(decision.should_extract)
        self.assertEqual(decision.reason, "tool_control_frame")

    def test_terminal_wrapper_without_fact_signal_is_skipped(self) -> None:
        decision = self.pre_filter.evaluate(
            {"event_type": "message", "actor_type": "tool"},
            {"text": "[terminal] ran `git status --short` -> exit 0, 1 lines output"},
        )
        self.assertFalse(decision.should_extract)
        self.assertEqual(decision.reason, "tool_control_frame")

    def test_transient_tool_failure_is_skipped(self) -> None:
        decision = self.pre_filter.evaluate(
            {"event_type": "message", "actor_type": "tool"},
            {"text": '{"output":"[Command timed out after 10s]","exit_code":124,"error":null}'},
        )
        self.assertFalse(decision.should_extract)
        self.assertEqual(decision.reason, "transient_tool_result")

    def test_tool_output_with_timeout_and_version_fact_is_allowed(self) -> None:
        decision = self.pre_filter.evaluate(
            {"event_type": "message", "actor_type": "tool"},
            {"text": "Codex CLI 0.41.0\n[Command timed out after 10s]"},
        )
        self.assertTrue(decision.should_extract)
        self.assertEqual(decision.reason, "eligible")

    def test_cancelled_tool_result_with_version_output_is_allowed(self) -> None:
        decision = self.pre_filter.evaluate(
            {"event_type": "message", "actor_type": "tool"},
            {"text": '{"status":"cancelled","output":"Codex CLI 0.41.0"}'},
        )
        self.assertTrue(decision.should_extract)
        self.assertEqual(decision.reason, "eligible")

    def test_short_tool_error_envelope_is_skipped(self) -> None:
        decision = self.pre_filter.evaluate(
            {"event_type": "message", "actor_type": "tool"},
            {"text": '{"success":false,"error":"content is required for replace action"}'},
        )
        self.assertFalse(decision.should_extract)
        self.assertEqual(decision.reason, "transient_tool_error")

    def test_short_assistant_action_narration_is_skipped(self) -> None:
        decision = self.pre_filter.evaluate(
            {"event_type": "message", "actor_type": "assistant"},
            {"text": "Let me check the worker status:"},
        )
        self.assertFalse(decision.should_extract)
        self.assertEqual(decision.reason, "assistant_action_narration")

    def test_assistant_explanation_with_restart_fact_is_allowed(self) -> None:
        decision = self.pre_filter.evaluate(
            {"event_type": "message", "actor_type": "assistant"},
            {"text": "服务进程需要重启才能加载新代码，但会话在代码变更后不需要重启。"},
        )
        self.assertTrue(decision.should_extract)
        self.assertEqual(decision.reason, "eligible")

    def test_short_assistant_runtime_fact_is_allowed(self) -> None:
        decision = self.pre_filter.evaluate(
            {"event_type": "message", "actor_type": "assistant"},
            {"text": "Codex 在跑版本号升级任务。"},
        )
        self.assertTrue(decision.should_extract)
        self.assertEqual(decision.reason, "eligible")

    def test_assistant_action_followed_by_path_fact_is_allowed(self) -> None:
        decision = self.pre_filter.evaluate(
            {"event_type": "message", "actor_type": "assistant"},
            {"text": "Let me check. The database path is var/hl_mem.db."},
        )
        self.assertTrue(decision.should_extract)
        self.assertEqual(decision.reason, "eligible")

    def test_short_operational_status_query_is_skipped(self) -> None:
        decision = self.pre_filter.evaluate(
            {"event_type": "message", "actor_type": "user"},
            {"text": "Is the deployment done now?"},
        )
        self.assertFalse(decision.should_extract)
        self.assertEqual(decision.reason, "operational_status_query")

    def test_short_action_request_is_allowed_as_possible_preference(self) -> None:
        decision = self.pre_filter.evaluate(
            {"event_type": "message", "actor_type": "user"},
            {"text": "请让 Codex 审查架构。"},
        )
        self.assertTrue(decision.should_extract)
        self.assertEqual(decision.reason, "eligible")

    def test_tool_output_with_durable_fact_is_allowed(self) -> None:
        decision = self.pre_filter.evaluate(
            {"event_type": "message", "actor_type": "tool"},
            {"text": 'version = "0.12.3"\ndatabase_path = "var/hl_mem.db"'},
        )
        self.assertTrue(decision.should_extract)
        self.assertEqual(decision.reason, "eligible")

    def test_terminal_wrapper_with_output_summary_is_allowed(self) -> None:
        decision = self.pre_filter.evaluate(
            {"event_type": "message", "actor_type": "tool"},
            {"text": "[terminal] ran `pwd && codex --version` -> REDACTED_PATH; codex-cli 0.41.0"},
        )
        self.assertTrue(decision.should_extract)
        self.assertEqual(decision.reason, "eligible")


class ExtractionPreFilterSettingsTests(unittest.TestCase):
    """验证环境变量开关的向后兼容契约。"""

    def test_default_is_off(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            self.assertFalse(Settings.from_env().extract_pre_filter)

    def test_on_enables_pre_filter(self) -> None:
        with patch.dict("os.environ", {"HL_MEM_EXTRACT_PRE_FILTER": "on"}, clear=True):
            self.assertTrue(Settings.from_env().extract_pre_filter)

    def test_invalid_value_is_rejected(self) -> None:
        with patch.dict("os.environ", {"HL_MEM_EXTRACT_PRE_FILTER": "sometimes"}, clear=True):
            with self.assertRaisesRegex(Exception, "HL_MEM_EXTRACT_PRE_FILTER"):
                Settings.from_env()


class _CountingExtractor:
    def __init__(self) -> None:
        self.calls = 0

    def extract(self, _content: dict[str, Any]) -> list[Any]:
        self.calls += 1
        return []


class _BrokenPreFilter:
    rule_version = "broken-test"

    def evaluate(self, _event: dict[str, Any], _content: dict[str, Any]) -> None:
        raise RuntimeError("classifier unavailable")


class _RecordingAudit:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str, dict[str, Any]]] = []

    def emit(self, phase: str, action: str, outcome: str, **kwargs: Any) -> bool:
        self.rows.append((phase, action, outcome, kwargs))
        return True

    def close(self) -> bool:
        return True


class _Budget:
    def can_spend(self, _tokens: int) -> bool:
        return True

    def get_stats(self) -> dict[str, int]:
        return {}


class WorkerPreFilterIntegrationTests(unittest.TestCase):
    """验证 Worker 的跳过审计和错误降级。"""

    def _worker(
        self,
        root: Path,
        content: str,
        pre_filter: Any,
    ) -> tuple[Worker, _CountingExtractor, _RecordingAudit]:
        path = root / "pre-filter.db"
        extractor = _CountingExtractor()
        audit = _RecordingAudit()
        worker = Worker(
            Settings(database_path=str(path), extract_pre_filter=True),
            {
                "extractor": extractor,
                "embedder": object(),
                "image_describer": None,
                "pre_filter": pre_filter,
                "audit": audit,
                "budget": _Budget(),
            },
        )
        now = datetime.now(timezone.utc).isoformat()
        EventRepository(worker.connection).insert_event(
            {
                "id": "event",
                "event_type": "message",
                "actor_type": "tool",
                "content": {"text": content},
                "occurred_at": now,
                "recorded_at": now,
            }
        )
        return worker, extractor, audit

    def test_skip_is_audited_without_calling_extractor(self) -> None:
        with TemporaryDirectory() as directory:
            worker, extractor, audit = self._worker(
                Path(directory),
                "[process] wait session=proc_123",
                ExtractionPreFilter(),
            )
            result = worker._extract({"event_id": "event"}, "job")
            worker.database.close()
        self.assertEqual(result, {"claims": 0, "pre_filter": "tool_control_frame"})
        self.assertEqual(extractor.calls, 0)
        self.assertTrue(
            any(row[:3] == ("extraction_pre_filter", "evaluated", "skip") for row in audit.rows)
        )

    def test_pre_filter_error_falls_back_to_extraction(self) -> None:
        with TemporaryDirectory() as directory:
            worker, extractor, audit = self._worker(
                Path(directory),
                "Durable project fact",
                _BrokenPreFilter(),
            )
            result = worker._extract({"event_id": "event"}, "job")
            worker.database.close()
        self.assertEqual(result["claims"], 0)
        self.assertEqual(extractor.calls, 1)
        self.assertTrue(
            any(row[:3] == ("extraction_pre_filter", "evaluated", "error_fallback") for row in audit.rows)
        )

    def test_enabled_worker_uses_persistent_audit_by_default(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "audit-default.db"
            worker = Worker(
                Settings(database_path=str(path), extract_pre_filter=True),
                {
                    "extractor": _CountingExtractor(),
                    "embedder": object(),
                    "image_describer": None,
                    "budget": _Budget(),
                },
            )
            self.assertIsInstance(worker.audit, AuditLogger)
            worker.audit.close()
            worker.database.close()


if __name__ == "__main__":
    unittest.main()
