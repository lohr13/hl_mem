"""v0.15.0 校准、索引文本与 provider 持久化测试。"""

from hl_mem.domain.claims.claim import build_index_text
from hl_mem.monitoring.metrics import PersistentProviderMetrics, ProviderCall
from hl_mem.recall.calibration import fit_logistic
from hl_mem.storage.database import Database


def test_calibration_probability_orders_relevant_above_irrelevant() -> None:
    model = fit_logistic([({"semantic": 0.9}, 1), ({"semantic": 0.1}, 0)], iterations=500)
    assert model.predict({"semantic": 0.9}) > model.predict({"semantic": 0.1})


def test_claim_index_text_contains_slot_and_tags() -> None:
    text = build_index_text(
        {
            "subject_entity_id": "hl_mem",
            "predicate": "embedding 模型",
            "value": "text-embedding-v4 2048维",
            "canonical_slot": "config.model",
            "topic_tags": ["model", "implementation"],
        }
    )
    assert text == "hl_mem embedding 模型 text-embedding-v4 2048维 config.model model implementation"


def test_provider_calls_persist_across_recorders(tmp_path) -> None:
    database = Database(tmp_path / "provider.db")
    connection = database.open()
    PersistentProviderMetrics(connection).record(ProviderCall("llm", "extract", "success", 12.0, query_id="q1"))
    row = connection.execute("SELECT provider_type,query_id FROM provider_calls").fetchone()
    assert tuple(row) == ("llm", "q1")
    database.close()
