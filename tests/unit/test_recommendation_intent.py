"""High-precision recommendation and preference intent routing tests."""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from hl_mem.application.recall import RecallService
from hl_mem.domain.recall import route_recall_intent
from hl_mem.domain.temporal import RecallIntent
from hl_mem.ingest.embedder import FakeEmbedder
from hl_mem.settings import Settings
from hl_mem.storage.database import Database


class RecommendationIntentTests(unittest.TestCase):
    def test_personalized_recommendation_queries_route_to_preference(self) -> None:
        queries = (
            "Can you recommend a hotel for my trip to Miami?",
            "Recommend a restaurant for me",
            "Could you suggest a movie for me?",
            "Suggest a hotel for my trip",
            "Which movie should I watch?",
            "What should I eat tonight?",
            "What do I like for breakfast?",
            "你能给我推荐一家 Miami 酒店吗？",
            "给我推荐一家餐厅",
            "建议我选哪个编辑器",
            "哪家酒店适合我",
            "我该买哪款相机",
            "我想去哪度假",
        )

        for query in queries:
            with self.subTest(query=query):
                self.assertIs(route_recall_intent(query, None), RecallIntent.PREFERENCE)

    def test_ambiguous_terms_do_not_trigger_preference_without_personalized_choice(self) -> None:
        expected = {
            "Can you suggest why the build fails?": RecallIntent.CURRENT_STATE,
            "Suggest that we update the docs": RecallIntent.CURRENT_STATE,
            "How to deploy the recommended settings?": RecallIntent.PROCEDURE,
            "What is it like in Miami?": RecallIntent.CURRENT_STATE,
            "What is likely to fail?": RecallIntent.CURRENT_STATE,
            "建议如何部署服务": RecallIntent.PROCEDURE,
            "我想去迈阿密": RecallIntent.CURRENT_STATE,
            "这个方案适合批处理吗": RecallIntent.CURRENT_STATE,
        }

        for query, intent in expected.items():
            with self.subTest(query=query):
                self.assertIs(route_recall_intent(query, None), intent)

    def test_historical_markers_take_priority_over_preference_terms(self) -> None:
        self.assertIs(route_recall_intent("Which hotel did I prefer before?", None), RecallIntent.HISTORICAL)
        self.assertIs(route_recall_intent("我以前喜欢哪家酒店？", None), RecallIntent.HISTORICAL)

    def test_explicit_intent_overrides_automatic_recommendation_routing(self) -> None:
        with TemporaryDirectory() as root:
            database = Database(Path(root) / "explicit-intent.db")
            connection = database.open()
            try:
                response = RecallService(
                    connection,
                    FakeEmbedder(4),
                    settings=Settings.for_test(),
                ).recall(
                    "Recommend a hotel for me",
                    intent=RecallIntent.CURRENT_STATE,
                    debug=True,
                )
            finally:
                database.close()

        self.assertEqual(response["search_trace"]["intent"], "current_state")
        self.assertEqual(response["search_trace"]["intent_source"], "explicit")


if __name__ == "__main__":
    unittest.main()
