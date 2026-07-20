from hl_mem.ingest.event_filter import EventFilter


def test_explicit_memory_always_passes() -> None:
    assert EventFilter().should_extract(
        {"event_type": "explicit_memory", "actor_type": "user", "content": {"text": "好"}}
    ) == (True, "explicit_memory")


def test_short_acknowledgement_and_raw_tool_output_are_filtered() -> None:
    filter_ = EventFilter()
    assert filter_.should_extract({"content": {"text": "嗯"}}) == (False, "too_short")
    assert filter_.should_extract(
        {"event_type": "message", "actor_type": "assistant", "content": {"text": "好的。"}}
    ) == (False, "acknowledgement")
    assert filter_.should_extract(
        {"event_type": "tool_result", "actor_type": "tool", "content": {"stdout": "build succeeded"}}
    ) == (False, "raw_tool_output")


def test_regular_and_structured_tool_events_pass() -> None:
    filter_ = EventFilter()
    assert filter_.should_extract({"content": {"text": "用户偏好简短回答"}})[0]
    assert filter_.should_extract(
        {"event_type": "tool_result", "content": {"text": "服务配置详情", "service": "api"}}
    )[0]
