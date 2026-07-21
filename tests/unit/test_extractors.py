import hl_mem.ingest as ingest
from hl_mem.ingest.embeddings import FakeEmbedder, unpack_vector
from hl_mem.ingest.extractors import FakeExtractor
from tests.scenarios.chinese_test_cases import CHINESE_TEST_CASES


def test_fake_extractor_rules() -> None:
    claims = FakeExtractor().extract({"text": "记住发布前先跑回归测试"})
    assert claims[0].value == "发布前先跑回归测试"
    assert claims[0].predicate == "explicit_memory"


def test_fake_embedder_is_local_and_repeatable() -> None:
    embedder = FakeEmbedder(8)
    first = embedder.embed_one("中文")
    second = embedder.embed_one("中文")
    assert first == second
    assert len(unpack_vector(first)) == 8


def test_fake_embedder_is_not_exported_from_ingest_package() -> None:
    assert not hasattr(ingest, "FakeEmbedder")


def test_chinese_scenario_count_and_shape() -> None:
    assert len(CHINESE_TEST_CASES) == 30
    assert all(set(case) == {"input", "expected", "status"} for case in CHINESE_TEST_CASES)
    assert {case["status"] for case in CHINESE_TEST_CASES} == {
        "active", "superseded", "disputed", "expired"
    }
