"""从 query-candidate 标注集拟合 P(relevant) 校准模型。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from hl_mem.recall.calibration import fit_logistic

EVALUATION_ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    """读取 JSONL 特征标注并保存逻辑回归模型。"""
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--labels",
        type=Path,
        default=EVALUATION_ROOT / "datasets" / "recall_labels_v1.jsonl",
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    rows = []
    for line in args.labels.read_text(encoding="utf-8").splitlines():
        item = json.loads(line)
        rows.append((item["features"], int(item["label"] in {"relevant", "partially_relevant"})))
    fit_logistic(rows).save(args.output)


if __name__ == "__main__":
    main()
