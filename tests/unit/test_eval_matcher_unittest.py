"""Gold matcher 的不依赖 pytest 的定向回归测试。"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


def _load_evaluator():
    script = Path(__file__).resolve().parents[2] / "scripts" / "eval_against_gold.py"
    spec = importlib.util.spec_from_file_location("eval_against_gold_unittest", script)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载 {script}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class GoldMatcherTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.evaluator = _load_evaluator()

    def test_normalizes_hl_mem_component_subjects_without_collapsing_user(self) -> None:
        self.assertEqual(
            self.evaluator.normalize_subject("Hermes hl_mem provider"),
            self.evaluator.normalize_subject("hl_mem adapter"),
        )
        self.assertNotEqual(
            self.evaluator.normalize_subject("用户"),
            self.evaluator.normalize_subject("hl_mem"),
        )

    def test_matches_proxy_endpoint_by_shared_port_and_semantic_keywords(self) -> None:
        score = self.evaluator.value_similarity(
            "HTTP 代理地址配置为 http://127.0.0.1:10808",
            "V2Ray 代理端口为 10808",
        )

        self.assertGreaterEqual(score, 0.62)

    def test_matches_shared_path_anchor(self) -> None:
        score = self.evaluator.value_similarity(
            "Hermes 插件目录为 D:/workspace/hermes/plugins/hl_mem",
            "hl_mem 安装目标路径是 D:\\workspace\\hermes\\plugins\\hl_mem",
        )

        self.assertGreaterEqual(score, 0.62)

    def test_does_not_match_unrelated_claims_that_only_share_a_number(self) -> None:
        score = self.evaluator.value_similarity(
            "hl_mem 服务端口为 8200",
            "请求超时为 8200 秒",
        )

        self.assertLess(score, 0.62)

    def test_rejects_same_ip_with_conflicting_ports(self) -> None:
        score = self.evaluator.value_similarity(
            "HTTP 代理地址为 http://127.0.0.1:10808",
            "HTTP 代理地址为 http://127.0.0.1:8200",
        )

        self.assertLess(score, 0.62)

    def test_rejects_same_port_with_conflicting_ips(self) -> None:
        score = self.evaluator.value_similarity(
            "HTTP 代理地址为 http://127.0.0.1:8200",
            "HTTP 代理地址为 http://10.0.0.2:8200",
        )

        self.assertLess(score, 0.62)

    def test_matches_architecture_fact_across_use_and_fact_predicates(self) -> None:
        gold = [{"subject": "hl_mem", "predicate": "使用", "value": "hl_mem 使用 SQLite WAL 模式"}]
        predicted = [
            {
                "subject": "Hermes hl_mem provider",
                "predicate": "事实",
                "canonical_attribute": "fact.architecture",
                "value": "SQLite 存储启用了 WAL 模式",
            }
        ]

        matches = self.evaluator.match_claims(gold, predicted, value_threshold=0.62)

        self.assertEqual(len(matches), 1)

    def test_does_not_bridge_plan_and_fact_predicates(self) -> None:
        gold = [{"subject": "hl_mem", "predicate": "事实", "value": "hl_mem 使用 SQLite WAL 模式"}]
        predicted = [
            {
                "subject": "hl_mem",
                "predicate": "计划",
                "value": "hl_mem 计划使用 SQLite WAL 模式",
            }
        ]

        matches = self.evaluator.match_claims(gold, predicted, value_threshold=0.62)

        self.assertEqual(matches, [])

    def test_rejects_opposite_negation_even_when_the_rest_matches(self) -> None:
        score = self.evaluator.value_similarity(
            "FastAPI lifespan 清理使用 try/finally",
            "FastAPI lifespan 清理未放在 try/finally 中",
        )

        self.assertLess(score, 0.62)

    def test_does_not_treat_a_generic_provider_as_a_specific_context_only_provider(self) -> None:
        score = self.evaluator.value_similarity(
            "Hermes 将 hl_mem 注册为不暴露工具的 context-only memory provider",
            "hl_mem 是一个 Memory provider",
        )

        self.assertLess(score, 0.62)


if __name__ == "__main__":
    unittest.main()
