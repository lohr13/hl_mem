"""提取侧语言路由、episodic 分层与相对时间回归测试。"""

from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import hl_mem.cli as cli_module
from hl_mem.application.ingest import IngestService
from hl_mem.domain.claims.retention import TTLPolicy, compute_expiration
from hl_mem.ingest.chunking import ChunkingPolicy
from hl_mem.ingest.embedder import FakeEmbedder
from hl_mem.ingest.llm_extractor import (
    ENGLISH_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
    LLMExtractor,
    detect_extraction_language,
)
from hl_mem.ingest.relative_time import infer_occurrence
from hl_mem.llm.types import LLMRequest, LLMResponse
from hl_mem.settings import Settings
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.database import Database
from hl_mem.storage.events import EventRepository


class _RecordingClient:
    class _Provider:
        name = "fake"

    provider = _Provider()
    model = "test-model"

    def __init__(self, response: dict[str, object]) -> None:
        self.response = response
        self.requests: list[LLMRequest] = []

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        return LLMResponse(json.dumps(self.response, ensure_ascii=False), "stop", 1)


def _compact_claim(**overrides: object) -> dict[str, object]:
    claim: dict[str, object] = {
        "subject": "user",
        "value": "The user spent 4 hours assembling an IKEA bookcase",
        "kind": "fact",
        "confidence": 1.0,
        "notability": "medium",
        "evidence_quote": "I spent 4 hours assembling an IKEA bookcase",
    }
    claim.update(overrides)
    return claim


class ExtractionLanguageRoutingTest(unittest.TestCase):
    def test_detects_primary_language_without_treating_product_names_as_english_dialogue(self) -> None:
        self.assertEqual(detect_extraction_language("我昨天组装 IKEA 书架用了四小时"), "zh")
        self.assertEqual(detect_extraction_language("我用 OpenAI GPT API"), "zh")
        self.assertEqual(detect_extraction_language("I spent four hours assembling an IKEA bookcase"), "en")
        self.assertEqual(detect_extraction_language("I went to 北京 yesterday"), "en")

    def test_routes_english_chunk_to_native_english_prompt_and_preserves_english_claim(self) -> None:
        source = "I spent 4 hours assembling an IKEA bookcase"
        client = _RecordingClient({"claims": [_compact_claim(subject="I")], "should_memorize": True})

        claims = LLMExtractor(client, ChunkingPolicy(10_000, 0, 2)).extract(
            source,
            {"occurred_at": "2026-08-08T12:00:00+08:00"},
        )

        self.assertEqual(client.requests[0].messages[0].content, ENGLISH_SYSTEM_PROMPT)
        self.assertIn("Event occurred at", client.requests[0].messages[1].content)
        self.assertEqual(claims[0].subject, "user")
        self.assertEqual(claims[0].value, "The user spent 4 hours assembling an IKEA bookcase")

    def test_routes_chinese_chunk_to_chinese_prompt(self) -> None:
        source = "我昨天组装 IKEA 书架用了四小时"
        client = _RecordingClient({"claims": [], "should_memorize": False})

        LLMExtractor(client, ChunkingPolicy(10_000, 0, 2)).extract(source)

        self.assertEqual(client.requests[0].messages[0].content, SYSTEM_PROMPT)
        self.assertIn("事件发生时间", client.requests[0].messages[1].content)

    def test_chinese_persona_subject_is_canonicalized_immediately(self) -> None:
        source = "我昨天组装了 IKEA 书架"
        client = _RecordingClient(
            {
                "claims": [
                    _compact_claim(
                        subject="用户",
                        value="用户组装了 IKEA 书架",
                        evidence_quote=source,
                    )
                ],
                "should_memorize": True,
            }
        )

        claim = LLMExtractor(client, ChunkingPolicy(10_000, 0, 2)).extract(
            source,
            {"occurred_at": "2026-08-08T12:00:00+08:00"},
        )[0]

        self.assertEqual(claim.subject, "user")

    def test_named_subject_is_not_replaced_with_user(self) -> None:
        source = "The IKEA bookcase took 4 hours to assemble"
        client = _RecordingClient(
            {
                "claims": [
                    _compact_claim(
                        subject="IKEA bookcase",
                        value="The IKEA bookcase took 4 hours to assemble",
                        evidence_quote=source,
                    )
                ],
                "should_memorize": True,
            }
        )

        claim = LLMExtractor(client, ChunkingPolicy(10_000, 0, 2)).extract(source)[0]

        self.assertEqual(claim.subject, "IKEA bookcase")


class EpisodicExtractionTest(unittest.TestCase):
    def test_low_notability_incidental_detail_becomes_bounded_episodic_claim(self) -> None:
        source = "I spent 4 hours assembling an IKEA bookcase"
        client = _RecordingClient(
            {
                "claims": [_compact_claim(notability="low")],
                "should_memorize": True,
            }
        )

        claim = LLMExtractor(client, ChunkingPolicy(10_000, 0, 2)).extract(source)[0]

        self.assertEqual(claim.reason, "accepted_episodic")
        self.assertEqual(claim.scope, "temporal")
        self.assertEqual(claim.volatility, "ephemeral")
        self.assertEqual(claim.importance, 0.3)

    def test_low_notability_operational_snapshot_is_still_rejected(self) -> None:
        source = "935 tests passed"
        client = _RecordingClient(
            {
                "claims": [
                    _compact_claim(
                        subject="hl_mem",
                        value=source,
                        evidence_quote=source,
                        notability="low",
                    )
                ],
                "should_memorize": True,
            }
        )

        claims = LLMExtractor(client, ChunkingPolicy(10_000, 0, 2)).extract(source)

        self.assertEqual(claims, [])

    def test_non_episodic_ephemeral_ttl_keeps_event_time_anchor(self) -> None:
        expires_at, reason = compute_expiration(
            scope="temporal",
            importance=0.3,
            volatility="ephemeral",
            canonical_slot=None,
            valid_to=None,
            observed_at="2026-05-01T09:00:00+00:00",
            recorded_from="2026-08-08T09:00:00+00:00",
            policy=TTLPolicy(temporal_ttl_days_low=3),
        )

        self.assertEqual(expires_at, "2026-05-04T09:00:00+00:00")
        self.assertEqual(reason, "temporal_low")

    def test_episodic_claim_ttl_starts_when_historical_evidence_is_recorded(self) -> None:
        source = "I spent 4 hours assembling an IKEA bookcase"
        client = _RecordingClient({"claims": [_compact_claim(notability="low")], "should_memorize": True})
        extracted = LLMExtractor(client, ChunkingPolicy(10_000, 0, 2)).extract(source)[0]

        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "episodic.db")
            connection = database.open()
            event = {
                "id": "event-1",
                "tenant_id": "default",
                "actor_type": "user",
                "event_type": "message",
                "content": {"text": source},
                "occurred_at": "2026-05-01T09:00:00+00:00",
                "recorded_at": "2026-08-08T09:00:00+00:00",
            }
            EventRepository(connection).insert_event(event)

            result = IngestService.store_extracted(
                connection,
                extracted,
                event,
                "2026-08-08T09:00:00+00:00",
                FakeEmbedder(8),
                policy=TTLPolicy(temporal_ttl_days_low=3),
            )
            expires_at = connection.execute("SELECT expires_at FROM claims WHERE id=?", (result.claim_id,)).fetchone()[
                0
            ]
            database.close()

        self.assertEqual(expires_at, "2026-08-11T09:00:00+00:00")

    def test_stable_temporal_ttl_keeps_event_time_anchor(self) -> None:
        expires_at, _reason = compute_expiration(
            scope="temporal",
            importance=0.3,
            volatility="stable",
            canonical_slot=None,
            valid_to=None,
            observed_at="2026-05-01T09:00:00+00:00",
            recorded_from="2026-08-08T09:00:00+00:00",
            policy=TTLPolicy(temporal_ttl_days_low=3),
        )

        self.assertEqual(expires_at, "2026-05-04T09:00:00+00:00")


class RelativeOccurrenceTest(unittest.TestCase):
    def test_compact_extractor_uses_event_time_for_english_relative_date(self) -> None:
        source = "Yesterday I spent 4 hours assembling an IKEA bookcase"
        client = _RecordingClient(
            {
                "claims": [
                    _compact_claim(
                        value="The user spent 4 hours assembling an IKEA bookcase yesterday",
                        evidence_quote=source,
                    )
                ],
                "should_memorize": True,
            }
        )

        claim = LLMExtractor(client, ChunkingPolicy(10_000, 0, 2)).extract(
            source,
            {"occurred_at": "2026-08-08T18:30:00+08:00"},
        )[0]

        self.assertEqual(claim.occurred_start, "2026-08-07T00:00:00+08:00")
        self.assertEqual(claim.occurred_end, "2026-08-08T00:00:00+08:00")

    def test_parses_english_chinese_and_mixed_relative_dates_from_event_time(self) -> None:
        base = "2026-08-08T18:30:00+08:00"
        cases = (
            (
                "I finished it yesterday",
                ("2026-08-07T00:00:00+08:00", "2026-08-08T00:00:00+08:00"),
            ),
            (
                "I moved three months ago",
                ("2026-05-08T00:00:00+08:00", "2026-05-09T00:00:00+08:00"),
            ),
            (
                "Let's meet next Friday",
                ("2026-08-14T00:00:00+08:00", "2026-08-15T00:00:00+08:00"),
            ),
            (
                "I called them last Friday",
                ("2026-08-07T00:00:00+08:00", "2026-08-08T00:00:00+08:00"),
            ),
            (
                "这是我三个月前买的",
                ("2026-05-08T00:00:00+08:00", "2026-05-09T00:00:00+08:00"),
            ),
            (
                "昨天 I spent four hours on it",
                ("2026-08-07T00:00:00+08:00", "2026-08-08T00:00:00+08:00"),
            ),
        )

        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(infer_occurrence(text, base), expected)

    def test_week_phrases_are_monday_bounded_intervals(self) -> None:
        base = "2026-08-08T18:30:00+08:00"
        cases = (
            ("last week", ("2026-07-27T00:00:00+08:00", "2026-08-03T00:00:00+08:00")),
            ("this week", ("2026-08-03T00:00:00+08:00", "2026-08-10T00:00:00+08:00")),
            ("next week", ("2026-08-10T00:00:00+08:00", "2026-08-17T00:00:00+08:00")),
            ("上周", ("2026-07-27T00:00:00+08:00", "2026-08-03T00:00:00+08:00")),
        )

        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(infer_occurrence(text, base), expected)

    def test_parses_english_absolute_dates_at_date_precision(self) -> None:
        base = "2026-08-08T18:30:00-05:00"
        cases = (
            ("May 20, 2023", ("2023-05-20T00:00:00-05:00", "2023-05-21T00:00:00-05:00")),
            ("February 15th", ("2026-02-15T00:00:00-05:00", "2026-02-16T00:00:00-05:00")),
            ("3/15/2023", ("2023-03-15T00:00:00-05:00", "2023-03-16T00:00:00-05:00")),
        )

        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(infer_occurrence(text, base), expected)

    def test_parses_explicit_relative_and_absolute_ranges(self) -> None:
        base = "2026-08-08T18:30:00+08:00"
        cases = (
            (
                "from last week to yesterday",
                ("2026-07-27T00:00:00+08:00", "2026-08-08T00:00:00+08:00"),
            ),
            (
                "between May 20, 2023 and May 22, 2023",
                ("2023-05-20T00:00:00+08:00", "2023-05-23T00:00:00+08:00"),
            ),
        )

        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(infer_occurrence(text, base), expected)

    def test_parses_absolute_date_range_without_using_relative_base(self) -> None:
        self.assertEqual(
            infer_occurrence(
                "The workshop runs from 2026-08-20 to 2026-08-21",
                "2026-01-01T00:00:00-05:00",
            ),
            ("2026-08-20T00:00:00-05:00", "2026-08-22T00:00:00-05:00"),
        )

    def test_month_end_leap_year_and_timezone_boundaries(self) -> None:
        cases = (
            (
                "three months ago",
                "2026-05-31T10:00:00+00:00",
                ("2026-02-28T00:00:00+00:00", "2026-03-01T00:00:00+00:00"),
            ),
            (
                "one month ago",
                "2024-03-31T10:00:00+00:00",
                ("2024-02-29T00:00:00+00:00", "2024-03-01T00:00:00+00:00"),
            ),
            (
                "February 29th",
                "2024-07-01T10:00:00+00:00",
                ("2024-02-29T00:00:00+00:00", "2024-03-01T00:00:00+00:00"),
            ),
            (
                "tomorrow",
                "2026-12-31T23:30:00+14:00",
                ("2027-01-01T00:00:00+14:00", "2027-01-02T00:00:00+14:00"),
            ),
        )

        for text, base, expected in cases:
            with self.subTest(text=text, base=base):
                self.assertEqual(infer_occurrence(text, base), expected)

    def test_multiple_unconnected_dates_do_not_create_a_false_range(self) -> None:
        self.assertEqual(
            infer_occurrence(
                "The deadline was May 20, 2023; the report was revised June 1, 2023.",
                "2026-08-08T18:30:00+08:00",
            ),
            ("2023-05-20T00:00:00+08:00", "2023-05-21T00:00:00+08:00"),
        )

    def test_explicit_datetime_keeps_point_precision(self) -> None:
        self.assertEqual(
            infer_occurrence("2026-08-20 14:30", "2026-08-08T18:30:00+08:00"),
            ("2026-08-20T14:30:00+08:00", None),
        )

    def test_relative_dates_require_valid_event_time(self) -> None:
        self.assertEqual(infer_occurrence("yesterday", None), (None, None))
        self.assertEqual(infer_occurrence("yesterday", "not-a-time"), (None, None))
        self.assertEqual(infer_occurrence("February 15th", None), (None, None))
        self.assertEqual(infer_occurrence("February 29th", "2023-01-01T00:00:00+00:00"), (None, None))
        self.assertEqual(
            infer_occurrence("May 20, 2023", None),
            ("2023-05-20T00:00:00+00:00", "2023-05-21T00:00:00+00:00"),
        )


class NaturalIndexDefaultTest(unittest.TestCase):
    @staticmethod
    def _insert_claim(repository: ClaimRepository) -> None:
        repository.insert_claim(
            {
                "id": "claim-1",
                "namespace_key": "default",
                "subject_entity_id": "hl_mem",
                "predicate": "事实",
                "value": "SQLite",
                "canonical_slot": None,
                "topic_tags_json": "[]",
                "status": "active",
                "confidence": 1.0,
                "importance": 0.5,
                "scope": "permanent",
                "valid_from": "2026-08-08T00:00:00+00:00",
                "recorded_from": "2026-08-08T00:00:00+00:00",
            }
        )

    def test_settings_default_to_natural_projection_v2(self) -> None:
        settings = Settings()
        self.assertEqual(settings.index_text_mode, "natural")
        self.assertEqual(settings.index_text_version, "v2")

    def test_repository_fallback_uses_resolved_index_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = replace(
                Settings.for_test(),
                database_path=str(Path(directory) / "repository.db"),
                index_text_mode="natural",
            )
            database = Database(settings=settings)
            connection = database.open()
            self._insert_claim(ClaimRepository(connection, settings=settings))

            stored = connection.execute("SELECT index_text FROM claims WHERE id='claim-1'").fetchone()[0]
            database.close()

            self.assertEqual(stored, "hl_mem：SQLite")

    def test_backfill_cli_accepts_natural_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "backfill.db"
            settings = replace(
                Settings.for_test(),
                database_path=str(database_path),
                embedding_dim=8,
                index_text_mode="natural",
            )
            database = Database(settings=settings)
            connection = database.open()
            self._insert_claim(ClaimRepository(connection, settings=settings))
            database.close()
            output = StringIO()

            with (
                patch.object(cli_module, "load_settings", return_value=settings),
                patch.object(cli_module, "make_embedder", return_value=FakeEmbedder(8)),
                redirect_stdout(output),
            ):
                cli_module.main(["backfill-index-text", "--mode", "natural", "--dry-run"])

            self.assertEqual(json.loads(output.getvalue())["mode"], "natural")


if __name__ == "__main__":
    unittest.main()
