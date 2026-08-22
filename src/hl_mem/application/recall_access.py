"""召回访问记录资格策略。"""

from hl_mem.domain.recall import RecallIntent


def is_access_recording_eligible(
    *,
    intent: RecallIntent,
    as_of: str | None,
    known_as_of: str | None,
) -> bool:
    """判断本次召回是否具备记录访问副作用的资格。"""

    return intent is not RecallIntent.HISTORICAL and as_of is None and known_as_of is None
