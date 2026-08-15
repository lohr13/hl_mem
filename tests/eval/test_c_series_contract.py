"""C-series 可见 design/dev 契约与 runner 静态边界。"""

from __future__ import annotations

import ast
import importlib.util
import json
from collections import Counter
from pathlib import Path

import pytest

from hl_mem.evaluation.c_series import relation_multihop_intent_v1

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "eval" / "fixtures" / "c_series_relation_design_dev.json"
RUNNER = ROOT / "evaluation" / "tools" / "run_c_series_relation_experiment.py"
INTENT_FIXTURE = ROOT / "tests" / "eval" / "fixtures" / "c_series_intent_routing_dev.json"
ANNOTATION_C = ROOT / "tests" / "eval" / "fixtures" / "c_series_intent_annotation_c.json"
ANNOTATION_D = ROOT / "tests" / "eval" / "fixtures" / "c_series_intent_annotation_d.json"


def test_visible_relation_fixture_covers_frozen_six_categories() -> None:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 3
    assert payload["scorer_version"] == "answer-entity-packet-v1"
    cases = payload["cases"]
    assert len(cases) >= 12
    assert Counter(case["category"] for case in cases) == {
        "recommendation_execution": 2,
        "reporting_ownership": 2,
        "enumeration_completeness": 2,
        "cross_event_two_hop": 2,
        "conflict_current_value": 2,
        "no_answer_trap": 2,
    }
    for case in cases:
        gold = case["gold"]
        assert set(gold) == {
            "answerability",
            "answer_entities",
            "role_action_object",
            "forbidden_entities",
            "forbidden_assertions",
        }
        if gold["answerability"] == "no_answer":
            assert gold["answer_entities"] is None
            assert gold["forbidden_entities"] or gold["forbidden_assertions"]


def test_intent_dev_is_balanced_and_router_meets_frozen_gate() -> None:
    queries = json.loads(INTENT_FIXTURE.read_text(encoding="utf-8"))["cases"]
    labels = Counter(item["needs_relation_or_multihop"] for item in queries)
    assert len(queries) >= 200
    assert labels[True] >= 100
    assert labels[False] >= 100
    assert len({item["query"] for item in queries}) == len(queries)
    assert all(item["provenance"]["authoring"] == "deterministic_design_dev" for item in queries)
    predictions = [relation_multihop_intent_v1(item["query"]).eligible for item in queries]
    tp = sum(pred and item["needs_relation_or_multihop"] for pred, item in zip(predictions, queries, strict=True))
    fp = sum(pred and not item["needs_relation_or_multihop"] for pred, item in zip(predictions, queries, strict=True))
    fn = sum(not pred and item["needs_relation_or_multihop"] for pred, item in zip(predictions, queries, strict=True))
    tn = len(queries) - tp - fp - fn
    assert tp / (tp + fp) >= 0.90
    assert tp / (tp + fn) >= 0.90
    assert fp / (fp + tn) <= 0.05


def _expand_annotation(path: Path) -> tuple[dict, dict[str, bool]]:
    artifact = json.loads(path.read_text(encoding="utf-8"))
    labels: dict[str, bool] = {}
    for rule in artifact["adjudicated_original_id_label_runs"]:
        for index in range(rule["start"], rule["end_inclusive"] + 1):
            labels[f"{rule['id_prefix']}{index:03d}"] = rule["label"]
    return artifact, labels


def test_two_independent_agent_annotations_are_complete_and_adjudicated() -> None:
    cases = json.loads(INTENT_FIXTURE.read_text(encoding="utf-8"))["cases"]
    expected = {item["id"]: item["needs_relation_or_multihop"] for item in cases}
    artifact_c, labels_c = _expand_annotation(ANNOTATION_C)
    artifact_d, labels_d = _expand_annotation(ANNOTATION_D)
    assert artifact_c["annotator"] != artifact_d["annotator"]
    assert artifact_c["annotation_kind"] == artifact_d["annotation_kind"] == "independent-agent-blind"
    assert artifact_c["human_annotation"] is artifact_d["human_annotation"] is False
    assert artifact_c["annotator_visible_fields"] == artifact_d["annotator_visible_fields"] == ["blind_id", "query"]
    assert artifact_c["raw_source_sha256"] == "ae25a8f72f8142301024db04d383dac34c59ae2e91ea0b2fc0387a95e55f8a1d"
    assert artifact_d["raw_source_sha256"] == "61e81f156a00739e546de31502e3de2ffc2a2025c3391246a1852c0c2d28e36f"
    assert labels_c == labels_d == expected
    assert artifact_c["notes_count"] == artifact_d["notes_count"] == 0


def test_actual_frozen_40_cache_mapping_and_real_recall_smoke() -> None:
    runner_path = ROOT / "evaluation" / "tools" / "run_c_series_relation_experiment.py"
    spec = importlib.util.spec_from_file_location("c_series_runner_contract", runner_path)
    assert spec and spec.loader
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    cases = runner._e2e_inputs()
    assert len(cases) == 40
    assert all(Path(case["db_path"]).is_file() for case in cases)
    assert all(case["allowed_modalities"] == ["text"] for case in cases)
    assert all(case["source_cache_sha256"] and case["source_corpora"] for case in cases)
    settings = runner.load_settings(ROOT / "hl_mem.toml", ROOT / ".env")
    settings = runner.dataclasses.replace(settings, recall_dense_enabled=False, reranker_mode="off")
    result = runner.recall_visible_case(
        cases[0],
        settings,
        runner.FakeEmbedder(settings.embedding_dim),
        None,
        db_path=Path(cases[0]["db_path"]),
        arm_id="C0",
    )
    assert len(result.packet) <= 10
    assert len(result.seed_packet) <= 5
    runner_source = RUNNER.read_text(encoding="utf-8")
    assert 'planner_prompt(str(case["question"]), execution.seed_packet' in runner_source
    assert 'planner_prompt(str(case["question"]), packet[:5]' not in runner_source


def test_intent_gate_enforces_wilson_bounds(monkeypatch) -> None:
    runner_path = ROOT / "evaluation" / "tools" / "run_c_series_relation_experiment.py"
    spec = importlib.util.spec_from_file_location("c_series_runner_wilson", runner_path)
    assert spec and spec.loader
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    metrics = runner._intent_metrics()
    assert metrics["wilson_95"]["precision"]["low"] >= 0.82
    assert metrics["wilson_95"]["recall"]["low"] >= 0.82
    assert metrics["wilson_95"]["fpr"]["high"] <= 0.10
    monkeypatch.setattr(runner, "_wilson", lambda successes, total: {"low": 0.81, "high": 0.11})
    with pytest.raises(RuntimeError, match="intent router preregistration gate failed"):
        runner._intent_metrics()


def test_runner_dependency_closure_has_no_sealed_payload_access() -> None:
    pending = [
        RUNNER,
        ROOT / "src" / "hl_mem" / "evaluation" / "c_series_runtime.py",
        ROOT / "evaluation" / "tools" / "score_c_series_relation_experiment.py",
    ]
    visited: set[Path] = set()
    while pending:
        path = pending.pop()
        if path in visited:
            continue
        visited.add(path)
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                lowered = node.value.casefold()
                if ("sealed" in lowered or "holdout" in lowered) and node.value != "sealed_payload_sha256":
                    assert node.value == "c-series expected sealed hash only"
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("hl_mem"):
                module_names = [node.module]
            elif isinstance(node, ast.Import):
                module_names = [alias.name for alias in node.names]
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "import_module"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                module_names = [node.args[0].value]
            else:
                module_names = []
            for module_name in module_names:
                relative = Path(*module_name.split(".")).with_suffix(".py")
                for candidate in (ROOT / "src" / relative, ROOT / relative):
                    if candidate.is_file():
                        pending.append(candidate)
    runner_source = RUNNER.read_text(encoding="utf-8")
    assert runner_source.count("EXPECTED_SEALED_SHA256") == 2
