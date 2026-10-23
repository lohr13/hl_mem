from hl_mem.ingest.event_filter import EventFilter


def test_explicit_memory_always_passes() -> None:
    assert EventFilter().should_extract(
        {
            "event_type": "explicit_memory",
            "actor_type": "user",
            "content": {"text": "好"},
        }
    ) == (True, "explicit_memory")


def test_short_acknowledgement_and_raw_tool_output_are_filtered() -> None:
    filter_ = EventFilter()
    assert filter_.should_extract({"content": {"text": "嗯"}}) == (False, "too_short")
    assert filter_.should_extract(
        {
            "event_type": "message",
            "actor_type": "assistant",
            "content": {"text": "好的。"},
        }
    ) == (False, "acknowledgement")
    assert filter_.should_extract(
        {
            "event_type": "tool_result",
            "actor_type": "tool",
            "content": {"stdout": "build succeeded"},
        }
    ) == (False, "raw_tool_output")


def test_regular_and_structured_tool_events_pass() -> None:
    filter_ = EventFilter()
    assert filter_.should_extract({"content": {"text": "用户偏好简短回答"}})[0]
    assert filter_.should_extract(
        {
            "event_type": "tool_result",
            "content": {"text": "服务配置详情", "service": "api"},
        }
    )[0]


def test_producer_exclusion_is_enforced_before_extraction() -> None:
    assert EventFilter(memory_disposition_mode="enforce").should_extract(
        {
            "event_type": "message",
            "actor_type": "assistant",
            "content": {"text": "这是一份足够长的自动报告正文"},
            "metadata": {"memory_disposition": "exclude"},
        }
    ) == (False, "excluded_by_producer_disposition")


def test_producer_exclusion_observe_mode_keeps_normal_extraction() -> None:
    assert EventFilter(memory_disposition_mode="observe").should_extract(
        {
            "event_type": "message",
            "actor_type": "assistant",
            "content": {"text": "这是一份足够长的自动报告正文"},
            "metadata": {"memory_disposition": "exclude"},
        }
    ) == (True, "excluded_by_producer_disposition")


def test_producer_exclusion_off_mode_ignores_marker() -> None:
    assert EventFilter(memory_disposition_mode="off").should_extract(
        {
            "event_type": "message",
            "actor_type": "assistant",
            "content": {"text": "这是一份足够长的自动报告正文"},
            "metadata": {"memory_disposition": "exclude"},
        }
    ) == (True, "eligible")


def test_missing_or_malformed_producer_disposition_fails_open() -> None:
    filter_ = EventFilter(memory_disposition_mode="enforce")
    base = {
        "event_type": "message",
        "actor_type": "assistant",
        "content": {"text": "这是一份足够长的自动报告正文"},
    }

    assert filter_.should_extract(base) == (True, "eligible")
    assert filter_.should_extract({**base, "metadata": "exclude"}) == (True, "eligible")
    assert filter_.should_extract({**base, "metadata": {"memory_disposition": "future"}}) == (True, "eligible")
