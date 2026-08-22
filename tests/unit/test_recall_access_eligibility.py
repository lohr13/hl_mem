import pytest

from hl_mem.application.recall import is_access_recording_eligible
from hl_mem.domain.recall import RecallIntent


@pytest.mark.parametrize(
    ("intent", "as_of", "known_as_of"),
    [
        (RecallIntent.CURRENT_STATE, None, None),
        (RecallIntent.HISTORICAL, None, None),
        (RecallIntent.CURRENT_STATE, "2026-08-01T00:00:00+00:00", None),
        (RecallIntent.CURRENT_STATE, None, "2026-08-01T00:00:00+00:00"),
    ],
)
def test_access_recording_eligibility_preserves_v0293_time_travel_behavior(
    intent: RecallIntent,
    as_of: str | None,
    known_as_of: str | None,
) -> None:
    assert is_access_recording_eligible(
        intent=intent,
        as_of=as_of,
        known_as_of=known_as_of,
    )
