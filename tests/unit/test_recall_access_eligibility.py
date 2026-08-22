import pytest

from hl_mem.application.recall import is_access_recording_eligible
from hl_mem.domain.recall import RecallIntent


@pytest.mark.parametrize(
    ("intent", "as_of", "known_as_of", "expected"),
    [
        (RecallIntent.CURRENT_STATE, None, None, True),
        (RecallIntent.HISTORICAL, None, None, False),
        (RecallIntent.CURRENT_STATE, "2026-08-01T00:00:00+00:00", None, False),
        (RecallIntent.CURRENT_STATE, None, "2026-08-01T00:00:00+00:00", False),
    ],
)
def test_access_recording_eligibility_preserves_v0293_time_travel_behavior(
    intent: RecallIntent,
    as_of: str | None,
    known_as_of: str | None,
    expected: bool,
) -> None:
    assert (
        is_access_recording_eligible(
            intent=intent,
            as_of=as_of,
            known_as_of=known_as_of,
        )
        is expected
    )
