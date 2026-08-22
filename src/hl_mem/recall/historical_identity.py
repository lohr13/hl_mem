"""历史召回的稳定双时间文本投影。"""

from typing import Any

from hl_mem.domain.temporal import RecallIntent


def project_historical_identity(
    results: list[dict[str, Any]], request: Any, intent: RecallIntent
) -> list[dict[str, Any]]:
    if intent is not RecallIntent.HISTORICAL and request.as_of is None and request.known_as_of is None:
        return results
    projected: list[dict[str, Any]] = []
    for item in results:
        if item.get("type") != "claim":
            projected.append(item)
            continue
        valid = f"{item.get('valid_from') or 'unknown'}..{item.get('valid_to') or 'open'}"
        label = (
            f"【time: valid={valid}; recorded={item.get('recorded_from') or 'unknown'}; "
            f"status={item.get('status') or 'unknown'}】"
        )
        projected.append({**item, "text": f"{item['text']}\n{label}"})
    return projected
