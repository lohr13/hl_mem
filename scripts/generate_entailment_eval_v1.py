"""Generate the manually reasoned extraction and entailment evaluation datasets."""

from __future__ import annotations

import hashlib
import itertools
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
TESTSET_PATH = ROOT / "scripts" / "extraction_testset.jsonl"
PREDICTIONS_PATH = ROOT / "scripts" / "after_qwen_v0211.jsonl"
GOLD_PATH = ROOT / "evaluation" / "datasets" / "extraction_gold_v1.jsonl"
ENTAILMENT_PATH = ROOT / "evaluation" / "datasets" / "entailment_eval_v1.jsonl"
NESTED_PREFIX = '{"text": "'


def c(subject: str, predicate: str, value: str, scope: str) -> dict[str, str]:
    return {"subject": subject, "predicate": predicate, "value": value, "scope": scope}


# Empty events are intentional negatives. These annotations do not inherit the older
# scripts/gold_dataset.jsonl because that file admits transient health/test/tool snapshots.
GOLD_CLAIMS: dict[str, list[dict[str, str]]] = {
    "cbbc932288a64ff297b333898432f679": [
        c("hl_mem", "状态", "hl_mem 当前记忆数据库存在严重语义重复", "temporal"),
        c("hl_mem", "状态", "hl_mem 中大量 temporal 内容被错误标为 permanent", "temporal"),
        c("hl_mem", "状态", "hl_mem 的 Experience 通道尚未产出 policy", "temporal"),
        c("用户", "身份", "用户名称为本地小马", "permanent"),
        c("用户", "配置", "用户使用 REDACTED_GPU GPU", "permanent"),
        c("用户", "偏好", "用户偏好竖屏显示", "permanent"),
    ],
    "b551e4a6e68b4bbc858357d28b284085": [
        c("Hermes", "配置", "Hermes 的 memory provider 配置为 hl_mem", "permanent"),
        c("Hermes", "配置", "Hermes 通过 http://127.0.0.1:8200 实时调用 hl_mem", "permanent"),
        c("Hermes", "事实", "Hermes 加载的 hl_mem adapter 代码变更需要重启 Hermes 才能生效", "permanent"),
        c("用户", "计划", "用户计划清理 Hindsight 残留进程和相关启动逻辑", "temporal"),
    ],
    "f0886e4d4a894cd685788b62f368b78e": [
        c("Hermes", "事实", "Hermes 以 python.exe 进程运行", "permanent"),
    ],
    "cdf0ed0f321744c98cbe1e4f39372787": [
        c(
            "hl_mem",
            "事实",
            "fact.other、plan.other、state.other 等通用兜底 canonical attribute 共享 conflict_key 时不一定构成真实冲突",
            "permanent",
        ),
    ],
    "85949cd50aa84acabb901463d7b5f904": [
        c(
            "hl_mem",
            "事实",
            "未安装 socksio 时，ALL_PROXY 的 SOCKS5 配置会导致 httpx 无法连接百炼 API",
            "permanent",
        ),
        c("hl_mem", "配置", "hl_mem 运行环境已清空 ALL_PROXY", "permanent"),
        c(
            "hl_mem",
            "配置",
            "hl_mem 的 NO_PROXY 包含 aliyuncs.com 和 bigmodel.cn，以便国内 API 直连",
            "permanent",
        ),
    ],
    "55feebc14c6c42f5859c6c1581d85955": [
        c("hl_mem", "事实", "hl_mem 是单机单 Agent 的记忆系统", "permanent"),
        c("hl_mem", "使用", "hl_mem 使用 SQLite 存储", "permanent"),
        c("hl_mem", "配置", "hl_mem 定位为 localhost 服务", "permanent"),
        c("hl_mem", "配置", "hl_mem 的单机部署跳过 API 鉴权和租户隔离", "permanent"),
    ],
    "c881d477be3e4bab9183b9a53dade7c0": [
        c(
            "hl_mem",
            "计划",
            "hl_mem 重构将新增 src/hl_mem/workers/lifecycle.py，并在状态变更路径接入转换守卫",
            "temporal",
        ),
        c(
            "hl_mem",
            "计划",
            "hl_mem 重构将把 reclassify 和 retention 接入 worker 调度循环",
            "temporal",
        ),
        c(
            "hl_mem",
            "计划",
            "hl_mem 重构将把 decay.py 的 POLICY 和 ACCESS_BONUS 改为环境变量可配置",
            "temporal",
        ),
        c("hl_mem 重构任务", "配置", "hl_mem 重构任务禁止运行 pytest，测试由外部执行", "temporal"),
        c("hl_mem 重构任务", "配置", "hl_mem 重构任务禁止修改 tests 目录", "temporal"),
        c("hl_mem 重构任务", "配置", "hl_mem 重构任务禁止新增依赖", "temporal"),
    ],
    "520d7c99f4494b9fbf059ea6eee8aead": [
        c(
            "Hermes hl_mem provider",
            "计划",
            "Hermes hl_mem provider 将实现 MemoryProvider 接口并采用不暴露工具的 context-only 模式",
            "temporal",
        ),
        c(
            "Hermes hl_mem provider",
            "配置",
            "Hermes hl_mem provider 的默认服务地址为 http://localhost:8200",
            "permanent",
        ),
        c(
            "Hermes hl_mem provider",
            "使用",
            "Hermes hl_mem provider 使用 urllib.request 且不引入外部依赖",
            "permanent",
        ),
        c(
            "Hermes hl_mem provider",
            "配置",
            "Hermes hl_mem provider 通过 HL_MEM_URL、HL_MEM_ENABLED 和 HL_MEM_TIMEOUT 配置",
            "permanent",
        ),
        c(
            "Hermes hl_mem provider",
            "计划",
            "Hermes hl_mem provider 的 sync_turn 将非阻塞地提交用户和助手两条事件",
            "temporal",
        ),
        c(
            "Hermes hl_mem provider",
            "计划",
            "Hermes hl_mem provider 的 queue_prefetch 将在后台线程执行 recall 并缓存结果",
            "temporal",
        ),
    ],
    "62430c21dedd4ece812cbbdf253ff157": [
        c("hl_mem", "事实", "hl_mem 是本地优先的记忆系统服务", "permanent"),
        c("hl_mem", "使用", "hl_mem 使用 FastAPI", "permanent"),
        c("hl_mem", "使用", "hl_mem 使用 SQLite WAL", "permanent"),
        c("hl_mem", "事实", "hl_mem 已完成包含 Episode、Trace 和 Policy 的 Experience 通道", "permanent"),
        c("hl_mem", "事实", "hl_mem 通过 HTTP 向 Hermes Agent 提供记忆服务", "permanent"),
    ],
    "b045157e30d444be84f8b7e97048be38": [
        c("hl_mem", "计划", "hl_mem 将以 adapters/hermes/provider.py 作为唯一 Hermes provider 实现", "temporal"),
        c("hl_mem", "计划", "hl_mem 的 Hermes plugin/__init__.py 将改为薄委托层", "temporal"),
        c("hl_mem", "配置", "hl_mem 的 Hermes provider 默认地址将统一为 127.0.0.1:8200", "permanent"),
        c("hl_mem", "计划", "hl_mem 将新增 components.py 以集中组件工厂", "temporal"),
        c("hl_mem", "计划", "hl_mem 的 server.py 和 worker.py 将委托 components.py 创建组件", "temporal"),
        c("hl_mem", "计划", "hl_mem 将删除未被导入的 extended_pipeline.py", "temporal"),
        c("hl_mem", "计划", "hl_mem 将在 observation.py 标记遗留实现", "temporal"),
        c("hl_mem 重构任务", "配置", "hl_mem 重构任务禁止运行 pytest，测试由外部执行", "temporal"),
        c("hl_mem 重构任务", "配置", "hl_mem 重构任务禁止修改 tests 目录", "temporal"),
        c("hl_mem 重构任务", "配置", "hl_mem 重构任务要求保持现有 180 个测试通过", "temporal"),
        c("hl_mem 重构任务", "配置", "hl_mem 重构任务禁止新增依赖", "temporal"),
    ],
    "c69bd2fab42d44ae8e8b465bf22b978a": [
        c("hl_mem", "使用", "hl_mem 使用 SQLite WAL 作为存储后端", "permanent"),
        c("hl_mem", "事实", "hl_mem 将 decay、TTL 和 reclassify 生命周期任务内置于 Worker", "permanent"),
        c("hl_mem", "事实", "hl_mem 要求每条 claim 链接原始 event 形成证据链", "permanent"),
        c("hl_mem", "事实", "hl_mem 召回使用多因子排序和 LLM reranker", "permanent"),
        c("hl_mem", "状态", "hl_mem 尚未实现 entity graph", "temporal"),
        c("hl_mem", "状态", "hl_mem 尚不支持程序性记忆，Policy/Procedure 设计处于冻结状态", "temporal"),
        c("hl_mem", "状态", "hl_mem 的 retrieval_feedback 表尚未投入使用", "temporal"),
    ],
    "14f97bc20ae549babf13c9314848b69d": [
        c("hl_mem", "事实", "hl_mem 是本地优先的记忆系统服务", "permanent"),
        c("hl_mem", "使用", "hl_mem 使用 FastAPI", "permanent"),
        c("hl_mem", "使用", "hl_mem 使用 SQLite WAL", "permanent"),
        c("hl_mem", "事实", "hl_mem 已完成包含 Episode、Trace 和 Policy 的 Experience 通道", "permanent"),
        c("hl_mem", "事实", "hl_mem 通过 HTTP 向 Hermes Agent 提供记忆服务", "permanent"),
    ],
    "099828184fc64161967d09757ea8544e": [
        c("hl_mem", "配置", "hl_mem 仅用于 localhost 上的单机单 Agent 场景，Hermes 是唯一调用方", "permanent"),
        c("hl_mem", "配置", "hl_mem 的单机部署不要求 API 鉴权", "permanent"),
        c("hl_mem", "配置", "hl_mem 的单机部署不要求租户隔离", "permanent"),
        c("hl_mem", "计划", "hl_mem 将把 SQLite 存储改为每请求独立连接的连接池模式", "temporal"),
        c(
            "hl_mem",
            "配置",
            "hl_mem 将在 production 模式缺少 embedding API key 时启动失败，并在 dev 或 test 模式允许 FakeEmbedder",
            "permanent",
        ),
        c("hl_mem", "配置", "hl_mem 将在 production 模式默认强制启用 reranker，缺少 API key 时启动失败", "permanent"),
        c("hl_mem", "计划", "hl_mem 将把 event 插入和 extract job 入队合并为单一事务", "temporal"),
        c("hl_mem", "计划", "hl_mem 将把 Episode 终结和 reward backprop 合并为单一事务", "temporal"),
        c(
            "hl_mem",
            "计划",
            "hl_mem 将为 Episode 增加 running 到 success、failed 或 cancelled 的单向状态机，并禁止终态新增 Trace",
            "temporal",
        ),
        c("hl_mem", "计划", "hl_mem 将在 BEGIN IMMEDIATE 事务内计算 Trace 序号", "temporal"),
        c("hl_mem", "计划", "hl_mem 的 healthz 将返回 embedder 和 reranker 状态", "temporal"),
    ],
    "3aaef3c68ec14a56bc2f9350301ec504": [
        c("hl_mem", "状态", "hl_mem 版本号已从 0.1.0 更新为 0.2.0", "temporal"),
        c("hl_mem", "计划", "hl_mem 的 healthz 将返回版本号", "temporal"),
        c("hl_mem", "计划", "hl_mem CLI 将支持 --version 参数", "temporal"),
    ],
}


# Each tuple is (support_label, memory_worthy, rationale), in claims_data order.
QWEN_ANNOTATIONS: dict[str, list[tuple[str, bool, str]]] = {
    "cbbc932288a64ff297b333898432f679": [
        (
            "partially_entailed",
            False,
            "报告只说明该路径作为重复 claim 多次出现，未直接确认它仍是有效工作目录；路径实现细节也不是高价值记忆。",
        ),
        ("entailed", True, "报告明确把竖屏列为正确的用户核心偏好。"),
        ("entailed", True, "报告明确陈述正确的核心身份记忆为“本地小马”。"),
        ("entailed", True, "报告明确陈述用户核心硬件事实包含 REDACTED_GPU。"),
    ],
    "b551e4a6e68b4bbc858357d28b284085": [
        ("entailed", True, "原文明示 Hermes config 的 provider 已设为 hl_mem。"),
        ("entailed", True, "原文明示 adapter 由 Hermes 加载，因此 provider.py 修复需要重启 Hermes。"),
        ("entailed", False, "原文支持 Hindsight 残留进程正在运行，但这是短暂进程快照，不宜长期记忆。"),
    ],
    "481f1ff93ec442aa8e29bf9e307d06e9": [
        ("partially_entailed", False, "命题仅出现在明确标为 REFERENCE ONLY 的历史摘要中，不能安全提升为当前有效决策。"),
        ("partially_entailed", False, "SQLite WAL 约束只来自明确标为历史参考的摘要，当前有效性未由本事件重新确认。"),
        ("partially_entailed", False, "覆盖式安装偏好只存在于要求丢弃的历史摘要，不能作为当前新记忆。"),
        ("partially_entailed", False, "非交互 CLI 约束只存在于历史摘要，且摘要明确禁止恢复为当前指令。"),
    ],
    "f0886e4d4a894cd685788b62f368b78e": [
        ("entailed", False, "原文确认 298 行完整版 adapter 正在运行，但行数和一次运行状态属于低价值实现快照。"),
    ],
    "cdf0ed0f321744c98cbe1e4f39372787": [
        ("entailed", True, "代码 diff 的注释直接说明通用兜底 attribute 共享 conflict_key 不代表真实冲突。"),
    ],
    "85949cd50aa84acabb901463d7b5f904": [
        ("entailed", True, "原文明示 NO_PROXY 新增 aliyuncs.com 和 bigmodel.cn 以直连国内 API。"),
    ],
    "55feebc14c6c42f5859c6c1581d85955": [
        ("entailed", False, "命令确实为本次 Codex 进程设置了这些代理变量，但它只是任务局部执行环境。"),
        ("entailed", True, "审查提示词直接给出单机单 Agent、SQLite、localhost 且跳过鉴权与租户隔离的项目边界。"),
    ],
    "c881d477be3e4bab9183b9a53dade7c0": [
        ("partially_entailed", False, "原文是待执行重构指令，并未证明 POLICY 和 ACCESS_BONUS 已完成改造。"),
        ("partially_entailed", False, "原文要求新建并集成 lifecycle.py，但不能据此断言改动已经落地。"),
        ("partially_entailed", False, "原文要求接入 worker 调度，输出片段不足以确认全部调度改动已经完成。"),
        ("entailed", True, "原文明确规定本次重构不得运行 pytest，测试由外部执行。"),
        ("entailed", True, "原文明确规定本次重构不得修改 tests 目录。"),
    ],
    "b045157e30d444be84f8b7e97048be38": [
        ("partially_entailed", False, "原文明确要求合并 provider，但可见输出不足以证明整项改造已经完成。"),
        ("partially_entailed", False, "原文要求集中工厂，且片段显示部分委托 diff；仍不足以确认所有调用点和新模块均已完成。"),
        ("partially_entailed", False, "原文要求清理两个文件，但没有可见的完整完成证据。"),
        ("entailed", True, "原文逐项明确了不跑 pytest、不改 tests、保持 180 tests 和不新增依赖的任务约束。"),
    ],
    "c69bd2fab42d44ae8e8b465bf22b978a": [
        ("entailed", True, "对比分析直接陈述 hl_mem 使用 SQLite WAL。"),
        ("entailed", True, "原文直接陈述 decay、TTL 和 reclassify 内置于 Worker。"),
        ("entailed", True, "原文直接陈述所有 claim 必须链接原始 event。"),
        ("entailed", True, "原文直接陈述三层矛盾检测为 hash、conflict_key 和 cosine。"),
        ("entailed", True, "原文直接陈述 hl_mem 尚无 entity graph。"),
        ("entailed", True, "原文直接陈述 Policy/Procedure 设计冻结且不支持程序性记忆。"),
        ("entailed", True, "原文直接陈述 retrieval_feedback 表已设计但尚未使用。"),
    ],
    "099828184fc64161967d09757ea8544e": [
        ("entailed", True, "原文把单机单 Agent、localhost、Hermes 唯一调用方及跳过隔离列为明确前提。"),
        ("entailed", True, "命题使用“将改为”，准确表达了原文要求的连接池改造计划，而非既成事实。"),
        ("entailed", True, "原文明确规定 HL_MEM_ENV 下 production 与 dev/test 的 embedding 降级策略。"),
        ("entailed", True, "原文明确要求 healthz 返回 embedder 和 reranker 状态。"),
        ("entailed", True, "原文明确定义 Episode 的合法转换和终态禁止新增 Trace。"),
    ],
}


MUTATIONS: list[dict[str, Any]] = [
    {
        "event_id": "cbbc932288a64ff297b333898432f679",
        "claim": {"subject": "用户", "predicate": "身份", "value": "用户名称为云端小马"},
        "support_label": "contradicted",
        "rationale": "只改了身份值；原文明确给出的名称是“本地小马”。",
    },
    {
        "event_id": "cbbc932288a64ff297b333898432f679",
        "claim": {"subject": "用户", "predicate": "配置", "value": "用户使用 RTX 5090 GPU"},
        "support_label": "contradicted",
        "rationale": "只改了 GPU 型号；原文明确支持 REDACTED_GPU。",
    },
    {
        "event_id": "b551e4a6e68b4bbc858357d28b284085",
        "claim": {"subject": "Hermes", "predicate": "配置", "value": "Hermes 的 memory provider 配置为 Hindsight"},
        "support_label": "contradicted",
        "rationale": "只改了 provider 值；原文明示 provider 为 hl_mem 而不是 Hindsight。",
    },
    {
        "event_id": "b551e4a6e68b4bbc858357d28b284085",
        "claim": {"subject": "Hermes", "predicate": "配置", "value": "Hermes 通过 http://127.0.0.1:8000 实时调用 hl_mem"},
        "support_label": "contradicted",
        "rationale": "只改了端口；原文给出的地址端口为 8200。",
    },
    {
        "event_id": "f0886e4d4a894cd685788b62f368b78e",
        "claim": {"subject": "Hindsight", "predicate": "事实", "value": "Hindsight 以 python.exe 进程运行"},
        "support_label": "unsupported",
        "rationale": "只改了主体；本事件没有提供 Hindsight 进程类型的信息。",
    },
    {
        "event_id": "cdf0ed0f321744c98cbe1e4f39372787",
        "claim": {
            "subject": "hl_mem",
            "predicate": "事实",
            "value": "fact.other、plan.other、state.other 等通用兜底 canonical attribute 共享 conflict_key 时一定构成真实冲突",
        },
        "support_label": "contradicted",
        "rationale": "只改了极性；代码注释明确说明共享 conflict_key 不代表真实冲突。",
    },
    {
        "event_id": "85949cd50aa84acabb901463d7b5f904",
        "claim": {
            "subject": "hl_mem",
            "predicate": "配置",
            "value": "hl_mem 的 NO_PROXY 不包含 aliyuncs.com 和 bigmodel.cn",
        },
        "support_label": "contradicted",
        "rationale": "只改了否定极性；原文明确说已加入这两个域名。",
    },
    {
        "event_id": "55feebc14c6c42f5859c6c1581d85955",
        "claim": {"subject": "hl_mem", "predicate": "事实", "value": "hl_mem 是多机多 Agent 的记忆系统"},
        "support_label": "contradicted",
        "rationale": "只改了部署规模；原文明确限定单机单 Agent。",
    },
    {
        "event_id": "c881d477be3e4bab9183b9a53dade7c0",
        "claim": {
            "subject": "hl_mem",
            "predicate": "计划",
            "value": "hl_mem 重构将新增 src/hl_mem/workers/status_machine.py，并在状态变更路径接入转换守卫",
        },
        "support_label": "contradicted",
        "rationale": "只改了目标文件路径；原文指定的是 workers/lifecycle.py。",
    },
    {
        "event_id": "520d7c99f4494b9fbf059ea6eee8aead",
        "claim": {
            "subject": "Zep provider",
            "predicate": "配置",
            "value": "Zep provider 通过 HL_MEM_URL、HL_MEM_ENABLED 和 HL_MEM_TIMEOUT 配置",
        },
        "support_label": "unsupported",
        "rationale": "只改了主体；原文没有涉及 Zep provider 的配置。",
    },
    {
        "event_id": "62430c21dedd4ece812cbbdf253ff157",
        "claim": {"subject": "hl_mem", "predicate": "使用", "value": "hl_mem 使用 PostgreSQL"},
        "support_label": "contradicted",
        "rationale": "只改了数据库；原文明确说明 hl_mem 使用 SQLite WAL。",
    },
    {
        "event_id": "c69bd2fab42d44ae8e8b465bf22b978a",
        "claim": {"subject": "hl_mem", "predicate": "状态", "value": "hl_mem 已实现 entity graph"},
        "support_label": "contradicted",
        "rationale": "只改了实现极性；原文明确陈述尚未实现 entity graph。",
    },
    {
        "event_id": "099828184fc64161967d09757ea8544e",
        "claim": {
            "subject": "hl_mem",
            "predicate": "配置",
            "value": "hl_mem 将在 production 模式缺少 embedding API key 时静默使用 FakeEmbedder",
        },
        "support_label": "contradicted",
        "rationale": "只改了缺 key 时的行为；原文要求 production 模式直接启动失败。",
    },
]


TEST_COUNTS = {
    "user_pref": 4,
    "project_config": 4,
    "tool_workflow": 4,
    "status_report": 2,
    "chat_confirm": 2,
    "long_content": 4,
}


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def extract_text(content: str) -> str:
    try:
        value = json.loads(content)
        return value["text"]
    except json.JSONDecodeError:
        encoded = content[len(NESTED_PREFIX) :]
        while encoded.endswith("\\"):
            encoded = encoded[:-1]
        return json.loads(f'"{encoded}"')


def make_splits(
    events: list[dict[str, Any]],
    pair_weights: dict[str, int],
) -> dict[str, str]:
    """Choose grouped, category-stratified events while also balancing pair counts 60/40."""
    by_category: dict[str, list[str]] = {}
    for event in events:
        by_category.setdefault(event["category"], []).append(event["id"])

    def rank(event_id: str) -> str:
        return hashlib.sha256(f"extraction-entailment-v1:{event_id}".encode("ascii")).hexdigest()

    # Dynamic programming keeps one deterministic selection per attainable pair count.
    selections: dict[int, tuple[str, ...]] = {0: ()}
    for category in sorted(by_category):
        options: list[tuple[int, tuple[str, ...]]] = []
        for combo in itertools.combinations(by_category[category], TEST_COUNTS[category]):
            ordered = tuple(sorted(combo, key=rank))
            options.append((sum(pair_weights[event_id] for event_id in combo), ordered))
        next_selections: dict[int, tuple[str, ...]] = {}
        for current_weight, current_selection in selections.items():
            for option_weight, option_selection in options:
                total_weight = current_weight + option_weight
                candidate = current_selection + option_selection
                if total_weight not in next_selections or candidate < next_selections[total_weight]:
                    next_selections[total_weight] = candidate
        selections = next_selections

    target_test_pairs = round(sum(pair_weights.values()) * 0.40)
    best_weight = min(
        selections,
        key=lambda weight: (abs(weight - target_test_pairs), weight, selections[weight]),
    )
    test_ids = set(selections[best_weight])
    return {event["id"]: "test" if event["id"] in test_ids else "dev" for event in events}


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = "\n".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) for row in rows) + "\n"
    path.write_text(payload, encoding="utf-8", newline="\n")


def main() -> None:
    events = read_jsonl(TESTSET_PATH)
    predictions = read_jsonl(PREDICTIONS_PATH)
    prediction_by_event = {row["event_id"]: row.get("claims_data", []) for row in predictions}
    mutations_by_event: dict[str, list[dict[str, Any]]] = {}
    for mutation in MUTATIONS:
        mutations_by_event.setdefault(mutation["event_id"], []).append(mutation)
    pair_weights = {
        event["id"]: (
            len(GOLD_CLAIMS.get(event["id"], []))
            + len(prediction_by_event.get(event["id"], []))
            + len(mutations_by_event.get(event["id"], []))
        )
        for event in events
    }
    splits = make_splits(events, pair_weights)

    event_rows: list[dict[str, Any]] = []
    gold_by_event: dict[str, list[dict[str, str]]] = {}
    for event in events:
        event_id = event["id"]
        text = extract_text(event["content"])
        claims: list[dict[str, str]] = []
        for index, claim in enumerate(GOLD_CLAIMS.get(event_id, []), 1):
            claims.append({"gold_claim_id": f"{event_id}:g{index:02d}", **claim})
        gold_by_event[event_id] = claims
        event_rows.append(
            {
                "event_id": event_id,
                "category": event["category"],
                "text": text,
                "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "should_memorize": bool(claims),
                "gold_claims": claims,
                "split": splits[event_id],
            }
        )

    pair_rows: list[dict[str, Any]] = []

    def append_pair(
        event_id: str,
        source: str,
        claim: dict[str, str],
        support_label: str,
        memory_worthy: bool,
        rationale: str,
    ) -> None:
        pair_rows.append(
            {
                "pair_id": f"ent-{len(pair_rows) + 1:03d}",
                "event_id": event_id,
                "candidate_source": source,
                "claim": {key: claim[key] for key in ("subject", "predicate", "value")},
                "support_label": support_label,
                "memory_worthy": memory_worthy,
                "rationale": rationale,
                "split": splits[event_id],
            }
        )

    for event in events:
        event_id = event["id"]
        for claim in gold_by_event[event_id]:
            append_pair(
                event_id,
                "gold",
                claim,
                "entailed",
                True,
                "该原子命题由当前事件直接支持，并通过未来效用与持续性准入门。",
            )

        predicted = prediction_by_event.get(event_id, [])
        annotations = QWEN_ANNOTATIONS.get(event_id, [])
        if len(predicted) != len(annotations):
            raise ValueError(
                f"qwen annotation count mismatch for {event_id}: {len(predicted)} != {len(annotations)}"
            )
        for claim, (label, memory_worthy, rationale) in zip(predicted, annotations, strict=True):
            append_pair(event_id, "qwen_after_v0211", claim, label, memory_worthy, rationale)

        for mutation in mutations_by_event.get(event_id, []):
            append_pair(
                event_id,
                "mutation",
                mutation["claim"],
                mutation["support_label"],
                False,
                mutation["rationale"],
            )

    write_jsonl(GOLD_PATH, event_rows)
    write_jsonl(ENTAILMENT_PATH, pair_rows)
    print(f"Wrote {len(event_rows)} events to {GOLD_PATH.relative_to(ROOT)}")
    print(f"Wrote {len(pair_rows)} pairs to {ENTAILMENT_PATH.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
