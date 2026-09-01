from dataclasses import FrozenInstanceError

import pytest

from hl_mem.adapters.hermes.renderer import RenderedContext, render_context


def test_renderer_preserves_item_order_and_keeps_feedback_ids_out_of_text() -> None:
    rendered = render_context(
        {
            "schema_major": 1,
            "schema_minor": 0,
            "query_id": "query-1",
            "items": [
                {
                    "text": "first memory",
                    "feedback_id": "feedback-secret-1",
                    "value_json": "raw-secret-1",
                    "evidence": [{"text": "evidence-secret-1"}],
                },
                {
                    "text": "second memory",
                    "feedback_id": "feedback-secret-2",
                    "search_trace": "trace-secret-2",
                },
            ],
            "search_trace": "top-level-trace-secret",
        }
    )

    assert rendered == RenderedContext(
        text="first memory\nsecond memory",
        included_feedback_ids=("feedback-secret-1", "feedback-secret-2"),
    )
    assert "feedback-secret" not in rendered.text
    assert "raw-secret" not in rendered.text
    assert "evidence-secret" not in rendered.text
    assert "trace-secret" not in rendered.text


def test_renderer_adds_one_bounded_external_source_caution() -> None:
    rendered = render_context(
        {
            "schema_major": 1,
            "items": [
                {
                    "type": "claim",
                    "text": "A product is free",
                    "feedback_id": "feedback-1",
                    "evidence": [
                        {
                            "type": "event",
                            "id": "event-1",
                            "provenance": {
                                "origin_class": "external_derived",
                                "session_kind": "interactive",
                                "observed_at": "2026-09-01T00:00:00+00:00",
                                "source_hint": "https://example.com",
                                "ignored": "secret-value",
                            },
                        },
                        {
                            "type": "event",
                            "id": "event-2",
                            "provenance": {
                                "origin_class": "external",
                                "session_kind": "interactive",
                                "observed_at": "x" * 500,
                                "source_hint": "https://user:password@bad.test/?token=secret",
                            },
                        },
                    ],
                }
            ],
        }
    )

    assert rendered.text == (
        "A product is free\n"
        "source note: external_derived, observed 2026-09-01T00:00:00+00:00, "
        "https://example.com; verify time-sensitive facts"
    )
    assert rendered.text.count("source note:") == 1
    for secret in ("secret-value", "password", "token="):
        assert secret not in rendered.text


def test_renderer_keeps_direct_and_legacy_items_byte_equivalent() -> None:
    payload = {
        "schema_major": 1,
        "items": [
            {
                "type": "claim",
                "text": "direct memory",
                "feedback_id": "feedback-1",
                "evidence": [
                    {
                        "type": "event",
                        "id": "event-1",
                        "provenance": {"origin_class": "direct_user", "session_kind": "interactive"},
                    }
                ],
            },
            {
                "type": "claim",
                "text": "legacy memory",
                "feedback_id": "feedback-2",
                "evidence": [{"type": "event", "id": "event-2"}],
            },
        ],
    }

    assert render_context(payload).text == "direct memory\nlegacy memory"


def test_renderer_adds_complete_claim_relation_after_text() -> None:
    rendered = render_context(
        {
            "schema_major": 1,
            "items": [
                {
                    "type": "claim",
                    "text": "团队后来采用海风看板",
                    "role": "团队",
                    "action": "采用",
                    "object": "海风看板",
                    "feedback_id": "feedback-1",
                }
            ],
        }
    )

    assert rendered.text == "团队后来采用海风看板\nrelation: 团队 → 采用 → 海风看板"
    assert rendered.included_feedback_ids == ("feedback-1",)


def test_renderer_omits_incomplete_relation_fields() -> None:
    rendered = render_context(
        {
            "schema_major": 1,
            "items": [
                {
                    "type": "claim",
                    "text": "团队后来采用海风看板",
                    "role": "团队",
                    "action": "采用",
                    "feedback_id": "feedback-1",
                }
            ],
        }
    )

    assert rendered.text == "团队后来采用海风看板"


def test_renderer_normalizes_relation_component_line_breaks() -> None:
    rendered = render_context(
        {
            "schema_major": 1,
            "items": [
                {
                    "type": "claim",
                    "text": "团队后来采用海风看板",
                    "role": " 团队\n一组 ",
                    "action": "采\r\n用",
                    "object": "海风\t看板",
                    "feedback_id": "feedback-1",
                }
            ],
        }
    )

    assert rendered.text == "团队后来采用海风看板\nrelation: 团队 一组 → 采 用 → 海风 看板"


def test_renderer_skips_blank_or_non_string_text_and_their_receipts() -> None:
    rendered = render_context(
        {
            "schema_major": 1,
            "items": [
                {"text": "", "feedback_id": "feedback-empty"},
                {"text": "   \t", "feedback_id": "feedback-blank"},
                {"text": {"raw": "not text"}, "feedback_id": "feedback-object"},
                {"text": "kept", "feedback_id": "feedback-kept"},
                {"text": "also kept", "feedback_id": None},
                "invalid item",
            ],
        }
    )

    assert rendered.text == "kept\nalso kept"
    assert rendered.included_feedback_ids == ("feedback-kept",)


@pytest.mark.parametrize("schema_major", [None, 0, 2, "1", True])
def test_renderer_rejects_unknown_or_invalid_schema_major(schema_major: object) -> None:
    with pytest.raises(ValueError, match="unsupported context packet schema major"):
        render_context({"schema_major": schema_major, "items": []})


def test_rendered_context_is_frozen_and_slotted() -> None:
    rendered = RenderedContext("memory", ("feedback-1",))

    with pytest.raises(FrozenInstanceError):
        rendered.text = "changed"  # type: ignore[misc]
    assert not hasattr(rendered, "__dict__")
