from __future__ import annotations

import unittest

from hl_mem.domain.recall import RecallIntent
from hl_mem.recall.staged_pipeline import _preference_first


class PreferenceFinalizationTest(unittest.TestCase):
    @staticmethod
    def _claim(claim_id: str, predicate: str) -> dict[str, str]:
        return {"id": claim_id, "predicate": predicate}

    def test_preference_intent_reserves_only_three_slots_then_uses_global_order(self) -> None:
        fact = self._claim("relevant-fact", "事实")
        plan = self._claim("relevant-plan", "计划")
        preferences = [self._claim(f"preference-{index}", "偏好") for index in range(1, 11)]
        globally_ranked = [fact, preferences[0], plan, *preferences[1:]]

        final = _preference_first(globally_ranked, 10, RecallIntent.PREFERENCE)

        self.assertEqual(
            [claim["id"] for claim in final],
            [
                "preference-1",
                "preference-2",
                "preference-3",
                "relevant-fact",
                "relevant-plan",
                "preference-4",
                "preference-5",
                "preference-6",
                "preference-7",
                "preference-8",
            ],
        )

    def test_non_preference_intent_preserves_existing_ranking(self) -> None:
        claims = [
            self._claim("fact", "事实"),
            self._claim("preference", "偏好"),
            self._claim("plan", "计划"),
        ]

        final = _preference_first(claims, 2, RecallIntent.CURRENT_STATE)

        self.assertEqual([claim["id"] for claim in final], ["fact", "preference"])


if __name__ == "__main__":
    unittest.main()
