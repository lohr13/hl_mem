#!/usr/bin/env python
"""从提取测试集中生成固定的人工 gold 标注集。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent

SELECTED_EVENT_IDS = (
    "cbbc932288a64ff297b333898432f679",
    "b551e4a6e68b4bbc858357d28b284085",
    "4ffbc3accc1342709c73af49d479dc84",
    "386a72a005ae438a919fbab1fe350770",
    "5547f50ab656459a94e5279d005906f3",
    "f0886e4d4a894cd685788b62f368b78e",
    "85949cd50aa84acabb901463d7b5f904",
    "535f101c1e6d4e0796adcbc7406b8eda",
    "ae98cca4c33340dc858a3e9130d7a31f",
    "6198823152654d9ca90b623c12694142",
    "5dbcb695295e468b8230be94405acd22",
    "e40c54fbbb814380927e42c68590d51f",
    "f6b3d998204e44d3b98eae0579239408",
    "00a1906076b74b51b08f62ee04b9a492",
    "a1570b78e8784185883e81b376d24834",
    "f3f4d2a51be04c468c8763b19ef0dfb0",
    "7b13bcb42084448cace7c0c129358956",
    "f86275d47bdf449eb27e201273032430",
    "4418c2aeadbe4cc6b9089e93337ca1e3",
    "c69bd2fab42d44ae8e8b465bf22b978a",
)

GOLD_ANNOTATIONS: dict[str, dict[str, Any]] = {
    "cbbc932288a64ff297b333898432f679": {
        "should_memorize": True,
        "gold_claims": [
            {
                "subject": "hl_mem",
                "predicate": "状态",
                "value": "记忆数据库存在严重语义重复",
                "scope": "temporal",
            },
            {
                "subject": "hl_mem",
                "predicate": "状态",
                "value": "大量 temporal 内容被错误标为 permanent",
                "scope": "temporal",
            },
            {
                "subject": "hl_mem",
                "predicate": "状态",
                "value": "Experience 通道尚未产出 policy",
                "scope": "temporal",
            },
        ],
    },
    "b551e4a6e68b4bbc858357d28b284085": {
        "should_memorize": True,
        "gold_claims": [
            {
                "subject": "Hermes",
                "predicate": "配置",
                "value": "memory provider 配置为 hl_mem",
                "scope": "permanent",
            },
            {
                "subject": "Hermes",
                "predicate": "事实",
                "value": "加载修复后的 hl_mem adapter 代码需要重启 Hermes",
                "scope": "temporal",
            },
            {
                "subject": "用户",
                "predicate": "计划",
                "value": "清理 Hindsight 进程和相关启动逻辑",
                "scope": "temporal",
            },
        ],
    },
    "4ffbc3accc1342709c73af49d479dc84": {
        "should_memorize": True,
        "gold_claims": [
            {
                "subject": "用户",
                "predicate": "计划",
                "value": "调研 Codex CLI 是否需要新开会话以及是否需要安装扩展能力",
                "scope": "temporal",
            }
        ],
    },
    "386a72a005ae438a919fbab1fe350770": {
        "should_memorize": True,
        "gold_claims": [
            {
                "subject": "hl_mem",
                "predicate": "状态",
                "value": "冲突写入不是原子事务",
                "scope": "temporal",
            },
            {
                "subject": "hl_mem",
                "predicate": "状态",
                "value": "MCP memory_save 不会创建 extract job",
                "scope": "temporal",
            },
            {
                "subject": "hl_mem",
                "predicate": "状态",
                "value": "存在两套重复的 Hermes provider 实现",
                "scope": "temporal",
            },
        ],
    },
    "5547f50ab656459a94e5279d005906f3": {
        "should_memorize": True,
        "gold_claims": [
            {
                "subject": "hl_mem",
                "predicate": "状态",
                "value": "当前运行版本为 0.2.0",
                "scope": "temporal",
            },
            {
                "subject": "Hermes 插件",
                "predicate": "状态",
                "value": "v2.0.0 已加载",
                "scope": "temporal",
            },
            {
                "subject": "Hindsight",
                "predicate": "状态",
                "value": "已彻底清理",
                "scope": "temporal",
            },
        ],
    },
    "f0886e4d4a894cd685788b62f368b78e": {
        "should_memorize": True,
        "gold_claims": [
            {
                "subject": "Hermes",
                "predicate": "状态",
                "value": "以 python.exe 进程运行",
                "scope": "permanent",
            },
            {
                "subject": "Hermes",
                "predicate": "配置",
                "value": "hl_mem memory provider 已注册并激活",
                "scope": "temporal",
            },
            {
                "subject": "hl_mem",
                "predicate": "状态",
                "value": "版本管理方案尚待添加",
                "scope": "temporal",
            },
        ],
    },
    "85949cd50aa84acabb901463d7b5f904": {
        "should_memorize": True,
        "gold_claims": [
            {
                "subject": "hl_mem",
                "predicate": "事实",
                "value": "ALL_PROXY 的 SOCKS5 配置导致 httpx 因缺少 socksio 无法连接百炼 API",
                "scope": "temporal",
            },
            {
                "subject": "hl_mem",
                "predicate": "配置",
                "value": "NO_PROXY 包含 aliyuncs.com 和 bigmodel.cn",
                "scope": "permanent",
            },
        ],
    },
    "535f101c1e6d4e0796adcbc7406b8eda": {
        "should_memorize": False,
        "gold_claims": [],
    },
    "ae98cca4c33340dc858a3e9130d7a31f": {
        "should_memorize": True,
        "gold_claims": [
            {
                "subject": "hl_mem",
                "predicate": "状态",
                "value": "main 分支已推送提交范围 5799903..20fb725",
                "scope": "temporal",
            }
        ],
    },
    "6198823152654d9ca90b623c12694142": {
        "should_memorize": True,
        "gold_claims": [
            {
                "subject": "Codex",
                "predicate": "状态",
                "value": "只完成安装脚本打印优化，核心任务未完成",
                "scope": "temporal",
            }
        ],
    },
    "5dbcb695295e468b8230be94405acd22": {
        "should_memorize": True,
        "gold_claims": [
            {
                "subject": "Codex",
                "predicate": "状态",
                "value": "终止命令在 10 秒后超时",
                "scope": "temporal",
            },
            {
                "subject": "hl_mem",
                "predicate": "状态",
                "value": "读取 __init__.py 的命令路径错误并超时",
                "scope": "temporal",
            },
        ],
    },
    "e40c54fbbb814380927e42c68590d51f": {
        "should_memorize": True,
        "gold_claims": [
            {
                "subject": "hl_mem",
                "predicate": "配置",
                "value": "HL_MEM 环境变量未设置",
                "scope": "temporal",
            },
            {
                "subject": "hl_mem",
                "predicate": "配置",
                "value": "RERANKER 环境变量未设置",
                "scope": "temporal",
            },
        ],
    },
    "f6b3d998204e44d3b98eae0579239408": {
        "should_memorize": True,
        "gold_claims": [
            {
                "subject": "hl_mem",
                "predicate": "状态",
                "value": "端口 8200 存在大量 TIME_WAIT 连接",
                "scope": "temporal",
            }
        ],
    },
    "00a1906076b74b51b08f62ee04b9a492": {"should_memorize": False, "gold_claims": []},
    "a1570b78e8784185883e81b376d24834": {
        "should_memorize": True,
        "gold_claims": [
            {
                "subject": "hl_mem",
                "predicate": "状态",
                "value": "服务运行版本为 0.2.0",
                "scope": "temporal",
            },
            {
                "subject": "hl_mem",
                "predicate": "配置",
                "value": "embedder 使用 real 模式",
                "scope": "temporal",
            },
            {
                "subject": "hl_mem",
                "predicate": "配置",
                "value": "reranker 处于 off 模式",
                "scope": "temporal",
            },
        ],
    },
    "f3f4d2a51be04c468c8763b19ef0dfb0": {"should_memorize": False, "gold_claims": []},
    "7b13bcb42084448cace7c0c129358956": {"should_memorize": False, "gold_claims": []},
    "f86275d47bdf449eb27e201273032430": {
        "should_memorize": True,
        "gold_claims": [
            {
                "subject": "hl_mem",
                "predicate": "状态",
                "value": "进程 38136 已退出且端口 8200 未监听",
                "scope": "temporal",
            },
            {
                "subject": "hl_mem",
                "predicate": "状态",
                "value": "直接启动 uvicorn 的检查在 2 秒后超时",
                "scope": "temporal",
            },
        ],
    },
    "4418c2aeadbe4cc6b9089e93337ca1e3": {
        "should_memorize": True,
        "gold_claims": [
            {
                "subject": "hl_mem",
                "predicate": "状态",
                "value": "数据清理 dry run 提议 334 项变更",
                "scope": "temporal",
            },
            {
                "subject": "hl_mem",
                "predicate": "状态",
                "value": "其中 300 项为 restore_disputed",
                "scope": "temporal",
            },
            {
                "subject": "hl_mem",
                "predicate": "状态",
                "value": "其中 11 项为 expire_stale",
                "scope": "temporal",
            },
        ],
    },
    "c69bd2fab42d44ae8e8b465bf22b978a": {
        "should_memorize": True,
        "gold_claims": [
            {
                "subject": "hl_mem",
                "predicate": "事实",
                "value": "使用 SQLite WAL 存储",
                "scope": "permanent",
            },
            {
                "subject": "hl_mem",
                "predicate": "事实",
                "value": "使用 FTS、Dense、RRF 和 reranker 召回",
                "scope": "permanent",
            },
            {
                "subject": "hl_mem",
                "predicate": "状态",
                "value": "尚未实现 entity graph",
                "scope": "temporal",
            },
        ],
    },
}


def parse_args() -> argparse.Namespace:
    """解析输入、输出路径和模板模式。"""
    parser = argparse.ArgumentParser(description="生成 20 条 extraction gold 标注")
    parser.add_argument(
        "--input", type=Path, default=SCRIPT_DIR / "extraction_testset.jsonl"
    )
    parser.add_argument(
        "--output", type=Path, default=SCRIPT_DIR / "gold_dataset.jsonl"
    )
    parser.add_argument(
        "--template", action="store_true", help="只生成 gold_claims 为空的标注模板"
    )
    return parser.parse_args()


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    """读取 JSONL 文件并忽略空行。"""
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def build_gold_dataset(
    events: list[dict[str, Any]], *, template: bool
) -> list[dict[str, Any]]:
    """按固定 ID 顺序构造可复现的 gold 数据集。"""
    by_id = {event["id"]: event for event in events}
    missing = [event_id for event_id in SELECTED_EVENT_IDS if event_id not in by_id]
    if missing:
        raise ValueError(f"测试集缺少选定事件：{missing}")

    dataset: list[dict[str, Any]] = []
    for event_id in SELECTED_EVENT_IDS:
        event = by_id[event_id]
        annotation = (
            {"should_memorize": False, "gold_claims": []}
            if template
            else GOLD_ANNOTATIONS[event_id]
        )
        dataset.append(
            {
                "event_id": event_id,
                "category": event["category"],
                "actor_type": event["actor_type"],
                "content": event["content"],
                **annotation,
            }
        )
    return dataset


def main() -> None:
    """生成 gold 模板或已人工标注的数据集。"""
    args = parse_args()
    dataset = build_gold_dataset(load_jsonl(args.input), template=args.template)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        "\n".join(json.dumps(item, ensure_ascii=False) for item in dataset) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {len(dataset)} records to {args.output}")


if __name__ == "__main__":
    main()
