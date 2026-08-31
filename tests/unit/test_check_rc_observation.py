from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from scripts.check_rc_observation import evaluate

NOW = datetime(2026, 9, 8, 1, 0, tzinfo=timezone.utc)
TAG = "v1.0.0rc1"
COMMIT = "a" * 40


def _release(*, age_hours: int = 169) -> dict[str, Any]:
    return {
        "tag": TAG,
        "commit": COMMIT,
        "published_at": (NOW - timedelta(hours=age_hours)).isoformat(),
        "draft": False,
        "prerelease": True,
    }


def _artifact(day: str, *, tag: str = TAG, commit: str = COMMIT) -> dict[str, Any]:
    return {
        "name": f"rc-observation-{tag}-{day}",
        "tag": tag,
        "commit": commit,
        "utc_date": day,
        "run_url": "https://github.com/lohr13/hl_mem/actions/runs/123",
        "workflow_conclusion": "success",
        "quality_smoke": "passed",
        "public_recall": "passed",
        "migration": "passed",
        "security": "passed",
    }


def _artifacts(offsets: list[int] | None = None) -> list[dict[str, Any]]:
    base = datetime(2026, 9, 1, tzinfo=timezone.utc)
    return [_artifact((base + timedelta(days=offset)).date().isoformat()) for offset in (offsets or list(range(7)))]


def test_seven_green_consecutive_days_pass() -> None:
    assert evaluate(_release(), _artifacts(), [], NOW) == []


def test_six_days_are_rejected() -> None:
    assert any("seven" in failure for failure in evaluate(_release(), _artifacts(list(range(6))), [], NOW))


def test_missing_date_is_not_replaced_by_an_extra_or_duplicate() -> None:
    artifacts = _artifacts([0, 1, 2, 4, 5, 6, 7])
    artifacts.append(dict(artifacts[0]))

    assert any("consecutive" in failure for failure in evaluate(_release(), artifacts, [], NOW))


def test_open_high_priority_issue_since_publication_is_rejected() -> None:
    issues = [
        {
            "number": 17,
            "state": "open",
            "created_at": (NOW - timedelta(days=2)).isoformat(),
            "labels": ["priority:P1"],
        }
    ]

    assert any("P0/P1" in failure for failure in evaluate(_release(), _artifacts(), issues, NOW))


def test_release_younger_than_168_hours_is_rejected() -> None:
    assert any("168" in failure for failure in evaluate(_release(age_hours=167), _artifacts(), [], NOW))


def test_artifact_tag_or_commit_mismatch_is_rejected() -> None:
    artifacts = _artifacts()
    artifacts[0] = _artifact("2026-09-01", tag="v1.0.0rc2", commit="b" * 40)

    failures = evaluate(_release(), artifacts, [], NOW)

    assert any("tag" in failure for failure in failures)
    assert any("commit" in failure for failure in failures)
