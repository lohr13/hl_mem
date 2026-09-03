"""Paid Chinese end-to-end quality gate: extraction -> recall -> QA."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.eval.chinese_e2e import load_sample_manifest, run_chinese_e2e

pytestmark = [pytest.mark.eval, pytest.mark.real_api]

ROOT = Path(__file__).resolve().parents[2]
SAMPLE_MANIFEST_PATH = Path(__file__).parent / "fixtures" / "chinese_e2e_sample.json"
DEFAULT_CACHE_ROOT = ROOT / "var" / "eval" / "chinese_e2e_cache"
DEFAULT_REPORT_PATH = ROOT / "var" / "eval" / "chinese_e2e_report.json"


def _configured_cache_root() -> Path:
    return Path(os.getenv("HL_MEM_CHINESE_E2E_CACHE_ROOT", str(DEFAULT_CACHE_ROOT)))


def _metric(value: object) -> str:
    return f"{float(value):.3f}" if isinstance(value, (int, float)) else "n/a"


@pytest.mark.timeout(7200)
def test_chinese_extraction_recall_qa_e2e() -> None:
    manifest = load_sample_manifest(SAMPLE_MANIFEST_PATH)
    missing_sources = [item["path"] for item in manifest.sources.values() if not Path(item["path"]).is_file()]
    if missing_sources:
        pytest.skip(f"private Chinese E2E sources are not installed: {missing_sources}")

    refresh = os.getenv("HL_MEM_CHINESE_E2E_REFRESH") == "1"
    config_path = os.getenv("HL_MEM_CHINESE_E2E_CONFIG")
    env_path = os.getenv("HL_MEM_CHINESE_E2E_ENV")
    report_path = Path(os.getenv("HL_MEM_CHINESE_E2E_REPORT", str(DEFAULT_REPORT_PATH)))
    report = run_chinese_e2e(
        manifest_path=SAMPLE_MANIFEST_PATH,
        cache_root=_configured_cache_root(),
        report_path=report_path,
        refresh=refresh,
        config_path=Path(config_path) if config_path else None,
        env_path=Path(env_path) if env_path else None,
    )

    print("\nChinese extraction -> recall -> QA summary:")
    for dataset, metrics in report["metrics"]["by_dataset"].items():
        print(
            f"  {dataset}: cases={metrics['cases']} errors={metrics['failed_cases']} "
            f"QA-accuracy={_metric(metrics['qa_accuracy'])} QA-F1={_metric(metrics['qa_f1'])} "
            f"R@5={_metric(metrics['recall_at_5'])} MRR={_metric(metrics['mrr'])} "
            f"extraction={_metric(metrics['extraction_coverage'])}"
        )
    print(f"  cache={report['run']['cache_status_counts']} usage={report['run']['usage']}")
    print(f"  report={report_path}")

    failures = report["gate"]["failures"]
    assert report["gate"]["passed"], "Chinese E2E quality gate failed: " + "; ".join(
        f"{item['dataset']}.{item['metric']}={item['actual']} minimum={item['minimum']}" for item in failures
    )
