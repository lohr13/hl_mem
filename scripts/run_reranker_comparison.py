"""受控 reranker/HyDE/channel A/B 配置比较入口。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from evaluation.ab_tests.framework import Variant, run_ab


def main() -> None:
    """加载预计算单案例指标并按指定单变量比较。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument(
        "--variable",
        choices=(
            "reranker",
            "hyde",
            "tag_channel",
            "graph_channel",
            "temporal_channel",
        ),
        required=True,
    )
    parser.add_argument("--variants", nargs="+", required=True)
    args = parser.parse_args()
    cases = json.loads(args.input.read_text(encoding="utf-8"))
    variants = [Variant(name, args.variable, name) for name in args.variants]
    result = run_ab(
        cases,
        variants,
        lambda case, variant: case.get("variants", {}).get(variant.name, {}),
    )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
