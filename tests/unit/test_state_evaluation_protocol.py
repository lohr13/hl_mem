import json
import sqlite3

import pytest

from hl_mem.evaluation.state_protocol import (
    CountMetrics,
    coordinate_from_mapping,
    coordinate_key,
    coordinate_mapping,
)
from hl_mem.evaluation.state_sqlite_snapshot import readonly_snapshot, table_columns


def test_coordinate_protocol_round_trips_the_shared_value_object_with_stable_key() -> None:
    left = {
        "namespace": "default",
        "canonical_subject": "hl_mem",
        "canonical_slot": "config.version",
        "coordinate_qualifiers": {"platform": "windows", "component": "server"},
    }
    right = {
        **left,
        "coordinate_qualifiers": {"component": "server", "platform": "windows"},
    }

    coordinate = coordinate_from_mapping(left)

    assert coordinate_mapping(coordinate) == right
    assert coordinate_key(left) == coordinate_key(right)
    assert json.loads(coordinate_key(coordinate)) == right


@pytest.mark.parametrize(
    ("counts", "expected"),
    [
        ((1, 1, 1), (0.5, 0.5, 0.5)),
        ((0, 0, 0), (1.0, 1.0, 1.0)),
        ((0, 0, 2), (0.0, 0.0, 0.0)),
        ((0, 2, 0), (0.0, 0.0, 0.0)),
    ],
)
def test_count_metrics_preserves_the_frozen_zero_denominator_rules(
    counts: tuple[int, int, int],
    expected: tuple[float, float, float],
) -> None:
    metrics = CountMetrics(*counts)

    assert (metrics.precision, metrics.recall, metrics.f1) == expected
    assert metrics.as_dict() == {
        "true_positive": counts[0],
        "false_positive": counts[1],
        "false_negative": counts[2],
        "precision": expected[0],
        "recall": expected[1],
        "f1": expected[2],
    }


def test_count_metrics_rejects_boolean_and_negative_counts() -> None:
    with pytest.raises(ValueError, match="non-negative integers"):
        CountMetrics(True, 0, 0)
    with pytest.raises(ValueError, match="non-negative integers"):
        CountMetrics(0, -1, 0)


def test_readonly_snapshot_starts_one_transaction_and_closes_it(tmp_path) -> None:
    path = tmp_path / "snapshot.sqlite"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE claims(id TEXT PRIMARY KEY)")
    connection.commit()
    connection.close()

    with readonly_snapshot(path) as snapshot:
        statements: list[str] = []
        snapshot.set_trace_callback(statements.append)
        assert table_columns(snapshot, "claims") == {"id"}
        assert snapshot.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            snapshot.execute("INSERT INTO claims VALUES('write')")

    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        snapshot.execute("SELECT 1")
