#!/usr/bin/env python
"""Build Gold v2 directly from the 50 source events.

This builder intentionally reads only ``evaluation/datasets/extraction_testset.jsonl``.
All annotations below are independent manual adjudications of the event text;
no previous Gold dataset is loaded or compared.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
TESTSET_PATH = ROOT / "evaluation" / "datasets" / "extraction_testset.jsonl"
OUTPUT_PATH = ROOT / "evaluation" / "datasets" / "extraction_gold_v2.jsonl"


def claim(subject: str, predicate: str, value: str, scope: str, rationale: str) -> dict[str, str]:
    return {
        "subject": subject,
        "predicate": predicate,
        "value": value,
        "scope": scope,
        "label": "gold_positive",
        "rationale": rationale,
    }


def negative(value: str, kind: str, rationale: str) -> dict[str, str]:
    return {
        "value": value,
        "label": f"gold_negative_{kind}",
        "rationale": rationale,
    }


def event(claims: list[dict[str, str]], negatives: list[dict[str, str]]) -> dict[str, Any]:
    return {"claims": claims, "negatives": negatives}


ANNOTATIONS: dict[str, dict[str, Any]] = {}


ANNOTATIONS.update(
    {
        "cbbc932288a64ff297b333898432f679": event(
            [
                claim(
                    "用户",
                    "身份",
                    "用户名称为本地小马",
                    "permanent",
                    "审计报告明确确认这是正确的用户核心身份，未来个性化和身份查询会复用。",
                ),
                claim(
                    "用户",
                    "配置",
                    "用户使用 REDACTED_GPU GPU",
                    "permanent",
                    "硬件型号是稳定环境信息，会影响本地模型、性能和部署建议。",
                ),
                claim(
                    "用户",
                    "偏好",
                    "用户偏好竖屏显示",
                    "permanent",
                    "显示方向是稳定用户偏好，会影响界面和内容布局建议。",
                ),
                claim(
                    "hl_mem",
                    "配置",
                    "hl_mem 项目工作目录为 REDACTED_PATH",
                    "permanent",
                    "报告把该路径列为反复出现的既有项目路径，后续命令和文件定位会使用。",
                ),
                claim(
                    "hl_mem",
                    "状态",
                    "hl_mem 的记忆数据库存在严重语义重复",
                    "temporal",
                    "这是影响检索质量的明确审计结论，虽可修复但在当时具有持续调试价值。",
                ),
                claim(
                    "hl_mem",
                    "状态",
                    "hl_mem 尚未有效完成 subject 实体归一化，导致同一实体名称碎片化",
                    "temporal",
                    "这是明确的能力边界和召回缺陷，不是瞬时服务状态。",
                ),
                claim(
                    "hl_mem",
                    "状态",
                    "hl_mem 中大量 temporal 内容被错误标为 permanent",
                    "temporal",
                    "scope 误分类是明确的数据质量问题，会影响生命周期治理和召回。",
                ),
                claim(
                    "hl_mem",
                    "状态",
                    "hl_mem 的 Experience 通道尚未产出 policy",
                    "temporal",
                    "报告明确指出 policy 归纳链路当时没有产物，属于可检索的能力状态。",
                ),
                claim(
                    "hl_mem",
                    "事实",
                    "hl_mem 的每条 claim 都通过 evidence_links 指向原始 event",
                    "permanent",
                    "证据链完整是稳定的数据模型特征，后续解释和审计会引用。",
                ),
                claim(
                    "hl_mem",
                    "事实",
                    "hl_mem 的冲突检测和 supersede 替代链已投入运行",
                    "permanent",
                    "报告确认替代链已经工作，属于稳定系统能力。",
                ),
            ],
            [
                negative(
                    "Events 4078、Claims 740、Episodes 34、Traces 3297",
                    "metric_snapshot",
                    "数据库数量会随每次写入和清理变化。",
                ),
                negative("90% 的 claim confidence 位于 0.9 到 1.0", "metric_snapshot", "这是单次审计的分布统计。"),
                negative(
                    "建议立即跑一轮去重清洗", "unadopted_suggestion", "截断文本只显示建议开头，没有确认执行结果。"
                ),
            ],
        ),
        "8c0fe35de56d4bd5aad9463d41a4d237": event(
            [
                claim(
                    "Hermes 插件安装器",
                    "事实",
                    "install_to_hermes.py 在安装开始时打印目标路径",
                    "permanent",
                    "完成报告和代码 diff 都确认该交互行为已经落地。",
                ),
                claim(
                    "Hermes 插件安装器",
                    "事实",
                    "install_to_hermes.py 在安装和校验成功后打印成功提示",
                    "permanent",
                    "完成报告和代码 diff 都确认该稳定 CLI 行为。",
                ),
            ],
            [
                negative("定向测试 1 passed", "test_snapshot", "单次测试结果和数量会随版本变化。"),
                negative("工作区干净并已提交 d6863ea", "git_snapshot", "这是一次任务完成时的仓库状态。"),
            ],
        ),
        "b551e4a6e68b4bbc858357d28b284085": event(
            [
                claim(
                    "Hermes",
                    "配置",
                    "Hermes 的 memory provider 配置为 hl_mem",
                    "permanent",
                    "这是决定 Hermes 实际使用哪个记忆系统的生效配置。",
                ),
                claim(
                    "Hermes",
                    "配置",
                    "Hermes 通过 http://127.0.0.1:8200 调用 hl_mem",
                    "permanent",
                    "固定本地 API 地址是未来联调和故障排查会引用的环境配置。",
                ),
                claim(
                    "Hermes",
                    "事实",
                    "Hermes 的 recall、prefetch 和 sync_turn 会实时请求 hl_mem",
                    "permanent",
                    "原文明示三类调用的集成方式，属于稳定适配器契约。",
                ),
                claim(
                    "Hermes",
                    "事实",
                    "hl_mem adapter 代码变更需要重启 Hermes 才能重新加载",
                    "permanent",
                    "这是稳定的插件加载和生效机制。",
                ),
                claim(
                    "Hindsight",
                    "状态",
                    "Hindsight 已退役，残留进程和启动逻辑应被清理",
                    "permanent",
                    "用户明确下达清理指令，后续事件也确认已完成，属于既定迁移决策。",
                ),
                claim(
                    "Hermes",
                    "使用",
                    "在微信或 QQ 中发送 /restart 可以重启 Hermes gateway",
                    "permanent",
                    "这是可复用的运维操作方法。",
                ),
            ],
            [
                negative(
                    "hl_mem 服务已运行最新代码且所有新端点返回 200",
                    "operational_snapshot",
                    "服务运行状态会随进程和部署刷新。",
                ),
                negative("Hindsight 残留进程当前仍在运行", "operational_snapshot", "进程存活是短时运行快照。"),
            ],
        ),
        "7362fa45fa9244a1b3b9f8b0fd63afe5": event(
            [],
            [negative("Codex 正在审查并将整理、修复 P0/P1", "process_progress", "这是上下文压缩保存的临时任务进度。")],
        ),
        "83986d56dd2a4a71a44e88bce960d8c5": event(
            [], [negative("Codex 正在审查并将整理、修复 P0/P1", "process_progress", "这是重复的临时任务进度。")]
        ),
        "4ffbc3accc1342709c73af49d479dc84": event(
            [],
            [
                negative(
                    "搜索如何更好使用 Codex CLI 以及是否安装 Superpowers",
                    "question",
                    "这是一次调研请求，没有表达稳定偏好或已采纳结论。",
                )
            ],
        ),
        "600db63481484541b1fade5face6894f": event(
            [], [negative("是否使用更新脚本更新了内置插件", "question", "疑问句没有提供可确认事实。")]
        ),
        "151b6c9864474b56a7695ea1472025aa": event(
            [], [negative("Codex 正在审查并将整理、修复 P0/P1", "process_progress", "这是重复的临时任务进度。")]
        ),
        "73fdf9a5a2604c44a8eab90f795e5493": event(
            [], [negative("Codex 正在审查并将整理、修复 P0/P1", "process_progress", "这是重复的临时任务进度。")]
        ),
        "481f1ff93ec442aa8e29bf9e307d06e9": event(
            [],
            [
                negative(
                    "压缩摘要中的历史目标、约束和待办",
                    "stale_context",
                    "原文明示该摘要仅供参考且不得恢复为当前任务或新记忆。",
                )
            ],
        ),
        "a4df36be1f314b3baaf984304fcebe76": event(
            [
                claim(
                    "Hermes 插件安装器",
                    "配置",
                    "install_to_hermes.py 优先使用 HERMES_HOME 指定的 Hermes 根目录",
                    "permanent",
                    "代码 diff 显示显式环境路径优先，属于稳定安装配置。",
                ),
                claim(
                    "Hermes 插件安装器",
                    "事实",
                    "install_to_hermes.py 能从 Hermes 根目录解析 hermes-agent/plugins/memory",
                    "permanent",
                    "已落地的目录探测逻辑会用于后续安装和升级。",
                ),
            ],
            [negative("本次 diff 修改成功且 lint 为 ok", "tool_snapshot", "这是单次编辑工具结果，不是产品事实。")],
        ),
        "cffd9434c6324969993db7a446c1c1b4": event(
            [
                claim(
                    "hl_mem",
                    "事实",
                    "hl_mem 已实现 SQLite 连接池",
                    "permanent",
                    "git 历史明确记录该修复，且它是稳定架构能力。",
                ),
                claim(
                    "hl_mem",
                    "事实",
                    "hl_mem 的真实组件配置缺失时采用 fail-fast",
                    "permanent",
                    "提交标题明确记录 fail-fast 修复，未来部署会依赖。",
                ),
                claim(
                    "hl_mem",
                    "事实",
                    "hl_mem 已修复关键写入事务边界",
                    "permanent",
                    "transaction safety 是提交中明确落地的架构改动。",
                ),
                claim(
                    "hl_mem",
                    "事实",
                    "hl_mem 已引入生命周期状态机",
                    "permanent",
                    "state machine 是提交中明确落地的稳定能力。",
                ),
                claim(
                    "hl_mem",
                    "事实",
                    "hl_mem 提供数据清理脚本处理误 disputed 和 stale/expired 数据",
                    "permanent",
                    "git 历史确认脚本被新增并执行，脚本能力可持续复用。",
                ),
                claim(
                    "hl_mem",
                    "事实",
                    "hl_mem 的 healthz 返回应用版本",
                    "permanent",
                    "提交标题确认这一管理面能力已经实现。",
                ),
                claim(
                    "hl_mem",
                    "事实",
                    "hl_mem CLI 支持 --version",
                    "permanent",
                    "提交标题确认这一稳定 CLI 契约已经实现。",
                ),
            ],
            [
                negative(
                    "308 条 false disputes 被恢复，34 条 stale/expired 被处理",
                    "metric_snapshot",
                    "清理数量只描述一次执行。",
                ),
                negative("10 个文件新增 758 行删除 181 行", "git_snapshot", "变更统计不具有长期语义价值。"),
            ],
        ),
        "ab462b869abf488d9c026378eb91c00f": event(
            [],
            [
                negative(
                    "uv run hl-mem --version 在 10 秒后超时",
                    "operational_snapshot",
                    "这是一次命令执行故障，不能代表稳定 CLI 行为。",
                )
            ],
        ),
        "386a72a005ae438a919fbab1fe350770": event(
            [
                claim(
                    "hl_mem",
                    "状态",
                    "hl_mem 的冲突写入当时不是原子事务",
                    "temporal",
                    "审查明确指出可导致孤立 disputed 的数据一致性缺陷。",
                ),
                claim(
                    "hl_mem",
                    "状态",
                    "hl_mem 的冲突候选查询当时没有确定排序",
                    "temporal",
                    "这是会导致错误 supersede 链的明确实现缺陷。",
                ),
                claim(
                    "hl_mem",
                    "状态",
                    "hl_mem 的旧 fact_hash 拼接没有字段边界",
                    "temporal",
                    "审查给出可复现碰撞示例，属于明确历史缺陷。",
                ),
                claim(
                    "hl_mem MCP",
                    "状态",
                    "hl_mem 的 MCP save 到 recall 链路当时断裂",
                    "temporal",
                    "审查明确说明 save 只写 event 而不创建提取任务。",
                ),
                claim(
                    "hl_mem MCP",
                    "状态",
                    "hl_mem 的 MCP 调用当时存在数据库连接泄漏",
                    "temporal",
                    "审查指出连接未关闭或归还，属于明确能力风险。",
                ),
                claim(
                    "hl_mem",
                    "状态",
                    "hl_mem 的 recall 包当时承载了 dedup 和 conflict 等写入逻辑",
                    "temporal",
                    "这是明确的包职责和依赖方向问题。",
                ),
                claim(
                    "hl_mem",
                    "状态",
                    "hl_mem 的 storage 层当时反向依赖 ingest 和 recall 高层模块",
                    "temporal",
                    "审查明确指出底层到高层的反向依赖。",
                ),
                claim(
                    "hl_mem",
                    "状态",
                    "hl_mem 的 Worker 当时依赖 api.pipeline",
                    "temporal",
                    "这是明确的后台层到 API 层耦合问题。",
                ),
                claim(
                    "hl_mem",
                    "状态",
                    "hl_mem 的 Claim 状态机当时不完整且部分路径绕过守卫",
                    "temporal",
                    "审查列出 candidate/retracted 和 update_status 的具体缺口。",
                ),
                claim(
                    "hl_mem",
                    "状态",
                    "hl_mem 当时存在两套 Hermes provider 且默认 URL 不一致",
                    "temporal",
                    "重复实现和 8000/8200 差异是明确审查发现。",
                ),
                claim(
                    "hl_mem",
                    "状态",
                    "hl_mem 的 server.py 当时混合 DTO、工厂、生命周期、路由和事务逻辑",
                    "temporal",
                    "这是具体且可检索的职责过载事实。",
                ),
                claim(
                    "hl_mem",
                    "状态",
                    "hl_mem 的 embedder、reranker 和 extractor 工厂逻辑当时分散在多个模块",
                    "temporal",
                    "审查明确指出组件创建行为不一致。",
                ),
                claim(
                    "hl_mem",
                    "状态",
                    "hl_mem 的 repository.py 当时同时承担 CRUD、FTS、向量、可见性、supersede 和 job lease",
                    "temporal",
                    "这是明确的存储层职责过载。",
                ),
                claim(
                    "hl_mem",
                    "状态",
                    "hl_mem 当时存在三套不一致的召回和删除语义",
                    "temporal",
                    "REST、MCP 和 forget 的行为差异会影响接口一致性。",
                ),
                claim(
                    "hl_mem",
                    "状态",
                    "hl_mem 当时存在两套 Observation 构建逻辑且 REST recall 固定返回空 observations",
                    "temporal",
                    "这是明确的重复实现和能力断点。",
                ),
                claim(
                    "hl_mem",
                    "状态",
                    "hl_mem 的 migration 当时只有文件名记录而没有 SQL 内容校验",
                    "temporal",
                    "这是明确的迁移不可变性风险。",
                ),
            ],
            [
                negative("整体架构评分 6.2/10", "review_opinion", "评分是主观审查结论，不是原子技术事实。"),
                negative("Codex 阅读了 50 个文件共 5137 行", "process_snapshot", "文件和行数是一次审查的过程统计。"),
            ],
        ),
        "3f772a65f9f14ae882635ed5ef30c22d": event(
            [
                claim(
                    "Hermes hl_mem 插件",
                    "配置",
                    "Hermes hl_mem 插件安装在 C:/Users/Administrator/AppData/Local/hermes/hermes-agent/plugins/memory/hl_mem",
                    "permanent",
                    "安装目标路径是后续升级、诊断和卸载会引用的环境配置。",
                ),
                claim(
                    "Hermes hl_mem 插件",
                    "事实",
                    "Hermes hl_mem 插件包含 __init__.py 和 plugin.yaml",
                    "permanent",
                    "安装结果明确给出稳定插件文件结构。",
                ),
            ],
            [
                negative(
                    "备份目录 backup_20260722_101059", "operational_snapshot", "带时间戳的备份路径只属于一次安装。"
                ),
                negative("source and installed files match", "verification_snapshot", "这是一次安装后的校验结果。"),
            ],
        ),
        "e3bad10de97b4de6a65012868147dd8c": event(
            [], [negative("正在杀掉旧 hl_mem 服务", "process_progress", "这是截断的执行步骤，没有稳定结果。")]
        ),
        "5547f50ab656459a94e5279d005906f3": event(
            [
                claim(
                    "hl_mem",
                    "事实",
                    "hl_mem 的版本号同时维护在 pyproject.toml 和 src/hl_mem/__init__.py",
                    "permanent",
                    "这是稳定的版本管理位置，未来发版会反复使用。",
                ),
                claim(
                    "hl_mem",
                    "事实",
                    "hl_mem 的 /healthz 返回应用版本号",
                    "permanent",
                    "报告明确确认该管理接口能力已完成。",
                ),
                claim("hl_mem", "事实", "hl_mem CLI 支持 --version", "permanent", "报告明确确认 CLI 能力已加入。"),
                claim("hl_mem", "配置", "hl_mem 服务使用 127.0.0.1:8200", "permanent", "本地服务地址是稳定联调配置。"),
                claim(
                    "Hindsight",
                    "状态",
                    "Hindsight 已从当前记忆系统部署中彻底清理",
                    "permanent",
                    "这是完成后的系统迁移状态，未来判断当前 provider 时会引用。",
                ),
            ],
            [
                negative("hl_mem 当前运行版本为 0.2.0", "version_snapshot", "具体版本号会随发版变化。"),
                negative(
                    "Hermes 插件 v2.0.0 已加载且 Git 工作区干净",
                    "operational_snapshot",
                    "加载版本和仓库状态都是当时快照。",
                ),
            ],
        ),
        "f0886e4d4a894cd685788b62f368b78e": event(
            [
                claim(
                    "Hermes",
                    "事实",
                    "Hermes gateway 以 python.exe 进程运行",
                    "temporal",
                    "这是解释进程检索方式的环境事实，可能随启动器变化故标 temporal。",
                ),
                claim(
                    "Hermes hl_mem provider",
                    "事实",
                    "Hermes 将 hl_mem 注册为不暴露工具的 context-only memory provider",
                    "permanent",
                    "日志中的 0 tools 和 activated 明确确认稳定集成模式。",
                ),
            ],
            [
                negative(
                    "gateway 在 10:12:40 启动并一直处理消息", "operational_snapshot", "启动时间和存活状态会刷新。"
                ),
                negative(
                    "298 行完整版 adapter 正在运行", "operational_snapshot", "代码行数和当前运行状态都不是稳定契约。"
                ),
            ],
        ),
        "cdf0ed0f321744c98cbe1e4f39372787": event(
            [
                claim(
                    "hl_mem",
                    "事实",
                    "generic catch-all canonical attributes 共享 conflict_key 时不一定构成真实冲突",
                    "permanent",
                    "代码注释明确记录冲突语义的根因规则。",
                ),
                claim(
                    "hl_mem 数据清理",
                    "事实",
                    "cleanup_data.py 会把旧 predicate-only 冲突检测造成的 false disputed 恢复为 active",
                    "permanent",
                    "diff 明确展示清理脚本的稳定修复行为。",
                ),
                claim(
                    "hl_mem",
                    "状态",
                    "旧 predicate-only 冲突检测会把不同事实错误标为 disputed",
                    "temporal",
                    "这是解释历史脏数据来源的明确技术事实。",
                ),
            ],
            [negative("共有 308 条 disputed claim 受影响", "metric_snapshot", "受影响数量只对应当时数据库。")],
        ),
        "0078820bb2c54883829981827b0cedb3": event(
            [], [negative("读取 src/hl_mem/__init__.py 命令超时", "operational_snapshot", "这是单次终端调用故障。")]
        ),
    }
)


ANNOTATIONS.update(
    {
        "f86275d47bdf449eb27e201273032430": event(
            [],
            [
                negative(
                    "进程退出、8200 未监听且直接启动超时",
                    "operational_snapshot",
                    "进程、端口和超时都是一次运行诊断快照。",
                )
            ],
        ),
        "520d7c99f4494b9fbf059ea6eee8aead": event(
            [
                claim(
                    "Hermes hl_mem 插件",
                    "配置",
                    "Hermes hl_mem 插件位于 C:/Users/Administrator/AppData/Local/hermes/hermes-agent/plugins/memory/hl_mem",
                    "permanent",
                    "插件目录是后续安装和排障会引用的固定环境路径。",
                ),
                claim(
                    "Hermes hl_mem 插件",
                    "事实",
                    "Hermes hl_mem 插件实现 MemoryProvider 接口并采用不暴露工具的 context-only 模式",
                    "permanent",
                    "后续实现确认该接口和模式已经落地并持续使用。",
                ),
                claim(
                    "Hermes hl_mem 插件",
                    "配置",
                    "Hermes hl_mem 插件默认连接 http://localhost:8200",
                    "permanent",
                    "任务指定并在早期实现中落地了固定默认服务地址。",
                ),
                claim(
                    "Hermes hl_mem 插件",
                    "配置",
                    "早期 Hermes hl_mem 插件通过 HL_MEM_URL、HL_MEM_ENABLED 和 HL_MEM_TIMEOUT 配置",
                    "temporal",
                    "该环境变量配置实际落地过，后来迁移到集中 Settings，因此保留为历史配置。",
                ),
                claim(
                    "hl_mem API",
                    "事实",
                    "POST /v1/events 用于保存对话事件",
                    "permanent",
                    "这是插件依赖的稳定写入 API 契约。",
                ),
                claim(
                    "hl_mem API",
                    "事实",
                    "POST /v1/recall 用于语义检索 claims 和 observations",
                    "permanent",
                    "这是插件依赖的稳定召回 API 契约。",
                ),
                claim("hl_mem API", "事实", "GET /healthz 用于服务健康检查", "permanent", "这是稳定管理接口契约。"),
                claim(
                    "Hermes hl_mem 插件",
                    "使用",
                    "早期 Hermes hl_mem 插件使用 urllib.request 且不引入外部依赖",
                    "temporal",
                    "后续提交确认该实现曾落地，后来被 httpx 取代。",
                ),
                claim(
                    "Hermes hl_mem 插件",
                    "事实",
                    "queue_prefetch 在后台执行 recall 并缓存结果，prefetch 返回缓存",
                    "permanent",
                    "后台预取和缓存语义已经落地，未来性能和一致性讨论会引用。",
                ),
                claim(
                    "Hermes hl_mem 插件",
                    "事实",
                    "插件网络调用失败时会捕获异常并降级而不向 Hermes 抛出",
                    "permanent",
                    "失败隔离是明确且已实现的适配器可靠性约束。",
                ),
                claim(
                    "Hermes hl_mem 插件",
                    "事实",
                    "register(ctx) 通过 ctx.register_memory_provider 注册 hl_mem provider",
                    "permanent",
                    "这是稳定的 Hermes 插件注册契约。",
                ),
                claim(
                    "Hermes hl_mem 插件",
                    "事实",
                    "sync_turn 会向 hl_mem 写入用户和助手两条事件",
                    "permanent",
                    "后续实现确认双事件写入已落地。",
                ),
            ],
            [
                negative(
                    "创建插件的后台进程被 process.kill 终止", "process_snapshot", "进程结局不代表设计是否后来落地。"
                ),
                negative(
                    "sync_turn 以非阻塞方式提交两条事件",
                    "unimplemented_spec",
                    "后续实现会写入两条事件，但 HTTP 调用本身是同步的。",
                ),
                negative(
                    "is_available 只检查 HL_MEM_URL 或 config 是否设置",
                    "unimplemented_spec",
                    "早期实现实际主要依据 HL_MEM_ENABLED，原规格没有准确落地。",
                ),
            ],
        ),
        "62430c21dedd4ece812cbbdf253ff157": event(
            [
                claim(
                    "hl_mem", "事实", "hl_mem 是本地优先的记忆系统服务", "permanent", "这是用户直接提供的稳定产品定位。"
                ),
                claim("hl_mem", "使用", "hl_mem 使用 FastAPI", "permanent", "框架选型是稳定技术栈事实。"),
                claim("hl_mem", "使用", "hl_mem 使用 SQLite WAL", "permanent", "存储选型是稳定架构事实。"),
                claim(
                    "hl_mem",
                    "事实",
                    "hl_mem 已实现 Episode、Trace 和 Policy 的 Experience 通道",
                    "permanent",
                    "项目背景明确说明该能力已完成。",
                ),
                claim(
                    "hl_mem",
                    "事实",
                    "hl_mem 通过 HTTP 向 Hermes Agent 提供记忆服务",
                    "permanent",
                    "这是稳定的系统集成方式。",
                ),
                claim(
                    "hl_mem",
                    "事实",
                    "hl_mem 的召回包含 FTS、Dense 混合搜索和 reranker",
                    "permanent",
                    "审查范围直接描述现有召回技术链。",
                ),
                claim(
                    "hl_mem",
                    "事实",
                    "hl_mem 的中文 FTS 后来采用 trigram tokenizer",
                    "permanent",
                    "审查指出中文分词缺陷，后续 migration 和提交确认已落地 trigram。",
                ),
                claim(
                    "hl_mem",
                    "事实",
                    "hl_mem 的 FastAPI lifespan 清理使用 try/finally",
                    "permanent",
                    "审查提出生命周期清理缺口，当前 server.py 已确认修复。",
                ),
                claim(
                    "hl_mem",
                    "事实",
                    "hl_mem 的 token budget 使用 SQLite 事务保证多进程安全",
                    "permanent",
                    "审查指出进程内锁不足，当前 budget.py 已采用 SQLite BEGIN IMMEDIATE。",
                ),
            ],
            [
                negative("版本 0.2.0 且 188 个测试通过", "version_snapshot", "版本号和测试数量都会变化。"),
                negative(
                    "reranker 应复用持久化 httpx.Client 并增加日志",
                    "unimplemented_suggestion",
                    "当前工厂仍未默认注入持久化 client，建议没有完整落地。",
                ),
                negative("代码审查任务要求不要修改代码", "task_instruction", "这是一次只读审查约束。"),
            ],
        ),
        "b045157e30d444be84f8b7e97048be38": event(
            [
                claim(
                    "hl_mem",
                    "事实",
                    "adapters/hermes/provider.py 是唯一 Hermes provider 实现",
                    "permanent",
                    "任务正常完成且当前代码保持单一 provider 实现。",
                ),
                claim(
                    "hl_mem",
                    "事实",
                    "Hermes plugin/__init__.py 是委托 provider.py 的薄入口层",
                    "permanent",
                    "完成提交和当前代码均确认该结构。",
                ),
                claim(
                    "hl_mem",
                    "配置",
                    "Hermes provider 默认地址统一为 127.0.0.1:8200",
                    "permanent",
                    "统一地址是已落地的稳定集成配置。",
                ),
                claim(
                    "hl_mem",
                    "事实",
                    "components.py 集中管理 extractor、embedder 和 reranker 等组件工厂",
                    "permanent",
                    "集中工厂已经落地并持续使用。",
                ),
                claim(
                    "hl_mem",
                    "事实",
                    "server.py 和 worker.py 委托 components.py 创建组件",
                    "permanent",
                    "可见 diff 和当前代码均确认委托关系。",
                ),
                claim(
                    "hl_mem",
                    "状态",
                    "重构阶段的 observation.py 曾被标记为未接入正式管线的遗留实现",
                    "temporal",
                    "该标记确实落地过，但模块后来进入生产链路。",
                ),
            ],
            [
                negative(
                    "删除未被导入的 extended_pipeline.py",
                    "unimplemented_spec",
                    "文件没有被删除，后来缩减为兼容导出层。",
                ),
                negative("本次任务禁止 pytest、修改 tests 和新增依赖", "task_instruction", "这些只约束一次重构任务。"),
                negative("保持现有 180 个测试通过", "test_snapshot", "测试数量会变化。"),
            ],
        ),
        "4418c2aeadbe4cc6b9089e93337ca1e3": event(
            [
                claim(
                    "hl_mem",
                    "配置",
                    "hl_mem 数据库路径为 REDACTED_PATH/var/hl_mem.db",
                    "permanent",
                    "工具输出明确给出稳定本地数据库位置。",
                ),
                claim(
                    "hl_mem 数据清理",
                    "事实",
                    "cleanup_data.py 支持 dry-run 预览而不直接修改数据",
                    "permanent",
                    "输出明确显示 DRY RUN 和拟议变更分类，是稳定工具能力。",
                ),
                claim(
                    "hl_mem 数据清理",
                    "事实",
                    "cleanup_data.py 会恢复 generic attribute 造成的 false disputed",
                    "permanent",
                    "输出展示该清理类别和原因，未来维护会复用。",
                ),
                claim(
                    "hl_mem 数据清理",
                    "事实",
                    "cleanup_data.py 会过期测试结果、迁移数量等 stale 状态快照",
                    "permanent",
                    "输出示例明确说明 stale snapshot 的清理策略。",
                ),
            ],
            [
                negative(
                    "dry-run 提议 334 项变更，其中 300 项 restore_disputed", "metric_snapshot", "数量只对应当时数据库。"
                )
            ],
        ),
        "741f7d75c2004db9967271d15c3465e3": event(
            [],
            [
                negative(
                    "healthz 正常、recall 返回 3 条且 GET events 报错",
                    "operational_snapshot",
                    "这是一次 API 探测结果，错误详情还被截断。",
                )
            ],
        ),
        "c69bd2fab42d44ae8e8b465bf22b978a": event(
            [
                claim(
                    "hl_mem",
                    "使用",
                    "hl_mem 使用 SQLite WAL 单文件存储",
                    "permanent",
                    "对比分析明确陈述现有存储后端和零运维特征。",
                ),
                claim(
                    "hl_mem Worker",
                    "事实",
                    "hl_mem 将 decay、TTL 和 reclassify 生命周期任务内置于 Worker",
                    "permanent",
                    "这是稳定生命周期架构。",
                ),
                claim(
                    "hl_mem",
                    "事实",
                    "hl_mem 的冲突检测依次使用 hash、conflict_key 和 cosine",
                    "permanent",
                    "分析明确列出三层冲突检测链。",
                ),
                claim(
                    "hl_mem",
                    "事实",
                    "hl_mem 要求每条 claim 链接原始 event 形成证据链",
                    "permanent",
                    "强制证据链是稳定数据模型约束。",
                ),
                claim(
                    "hl_mem",
                    "事实",
                    "hl_mem 的召回使用 FTS、Dense、RRF、多因子排序和 LLM reranker",
                    "permanent",
                    "分析综合列出了现有召回技术链。",
                ),
                claim("hl_mem", "事实", "hl_mem 内建全链路审计 trace", "permanent", "审计能力是明确系统特征。"),
                claim(
                    "hl_mem",
                    "状态",
                    "当时 /v1/memories/{id}/explain 尚未实现",
                    "temporal",
                    "分析明确区分设计存在和能力未落地。",
                ),
                claim(
                    "hl_mem",
                    "事实",
                    "SQLite WAL 对 hl_mem 的单用户场景足够",
                    "permanent",
                    "这是分析给出的稳定基础设施取舍。",
                ),
                claim(
                    "hl_mem",
                    "事实",
                    "hl_mem 使用 valid 和 recorded 双时间模型",
                    "permanent",
                    "双时间是明确的时序数据模型能力。",
                ),
                claim(
                    "hl_mem",
                    "状态",
                    "该分析发生时 hl_mem 尚未实现 entity graph",
                    "temporal",
                    "这是当时明确的能力边界，后来已被关系能力部分取代。",
                ),
                claim("hl_mem", "事实", "hl_mem 面向单用户万级记忆规模", "permanent", "分析明确给出适用规模边界。"),
                claim(
                    "hl_mem",
                    "事实",
                    "hl_mem 的事实通道采用 event→claim→observation 流程",
                    "permanent",
                    "这是稳定事实记忆结构。",
                ),
                claim(
                    "hl_mem",
                    "状态",
                    "该分析发生时 hl_mem 尚未实现 Episode 记忆",
                    "temporal",
                    "这是当时的能力边界，后来 Experience 通道已落地。",
                ),
                claim(
                    "hl_mem",
                    "状态",
                    "该分析发生时 hl_mem 尚不支持程序性记忆且 Policy/Procedure 设计冻结",
                    "temporal",
                    "这是当时明确的程序性记忆状态。",
                ),
                claim(
                    "hl_mem",
                    "状态",
                    "该分析发生时 retrieval_feedback 表尚未投入使用",
                    "temporal",
                    "这是当时明确的反馈能力边界，后来已接入。",
                ),
                claim(
                    "hl_mem",
                    "事实",
                    "hl_mem 的轻量关系图使用 SQLite memory_relations 而不依赖 Neo4j",
                    "permanent",
                    "原文提出 SQLite 轻量图方向，当前代码和 migration 确认已落地。",
                ),
            ],
            [
                negative(
                    "建议新增 entity_relations 表",
                    "superseded_proposal",
                    "方向后来落地为 memory_relations，原表名方案不应记成当前事实。",
                ),
                negative(
                    "Hindsight 一次启动即稳定",
                    "comparative_opinion",
                    "这是竞品对比中的概括性评价，不是 hl_mem 用户事实。",
                ),
            ],
        ),
        "14f97bc20ae549babf13c9314848b69d": event(
            [
                claim(
                    "hl_mem", "事实", "hl_mem 是本地优先的记忆系统服务", "permanent", "这是用户直接提供的稳定产品定位。"
                ),
                claim("hl_mem", "使用", "hl_mem 使用 FastAPI", "permanent", "框架选型是稳定技术栈事实。"),
                claim("hl_mem", "使用", "hl_mem 使用 SQLite WAL", "permanent", "存储选型是稳定架构事实。"),
                claim(
                    "hl_mem",
                    "事实",
                    "hl_mem 已实现 Episode、Trace 和 Policy 的 Experience 通道",
                    "permanent",
                    "项目背景明确说明该能力已完成。",
                ),
                claim(
                    "hl_mem",
                    "事实",
                    "hl_mem 通过 HTTP 向 Hermes Agent 提供记忆服务",
                    "permanent",
                    "这是稳定的系统集成方式。",
                ),
                claim(
                    "hl_mem",
                    "事实",
                    "hl_mem 的召回包含 FTS、Dense 混合搜索和 reranker",
                    "permanent",
                    "审查范围直接描述现有召回技术链。",
                ),
                claim(
                    "hl_mem",
                    "事实",
                    "hl_mem 的中文 FTS 后来采用 trigram tokenizer",
                    "permanent",
                    "审查指出中文分词缺陷，后续 migration 和提交确认已落地 trigram。",
                ),
                claim(
                    "hl_mem",
                    "事实",
                    "hl_mem 的 FastAPI lifespan 清理使用 try/finally",
                    "permanent",
                    "审查提出生命周期清理缺口，当前 server.py 已确认修复。",
                ),
                claim(
                    "hl_mem",
                    "事实",
                    "hl_mem 的 token budget 使用 SQLite 事务保证多进程安全",
                    "permanent",
                    "审查指出进程内锁不足，当前 budget.py 已采用 SQLite BEGIN IMMEDIATE。",
                ),
            ],
            [
                negative("版本 0.2.0 且 188 个测试通过", "version_snapshot", "版本号和测试数量都会变化。"),
                negative(
                    "reranker 应复用持久化 httpx.Client 并增加日志",
                    "unimplemented_suggestion",
                    "当前工厂仍未默认注入持久化 client，建议没有完整落地。",
                ),
            ],
        ),
        "099828184fc64161967d09757ea8544e": event(
            [
                claim(
                    "hl_mem",
                    "配置",
                    "hl_mem 仅用于 localhost 上的单机单 Agent 场景，Hermes 是唯一调用方",
                    "permanent",
                    "这是用户明确规定的系统边界和调用方约束。",
                ),
                claim(
                    "hl_mem",
                    "配置",
                    "hl_mem 的单机部署不要求 API 鉴权",
                    "permanent",
                    "用户明确说明该问题在当前场景可以跳过。",
                ),
                claim(
                    "hl_mem", "配置", "hl_mem 的单租户部署不要求租户隔离", "permanent", "用户明确说明只有一个 tenant。"
                ),
                claim(
                    "hl_mem",
                    "事实",
                    "hl_mem 的 SQLite 存储采用每请求独立连接的连接池模式",
                    "permanent",
                    "任务正常完成且当前 Database/FastAPI 依赖注入确认已落地。",
                ),
                claim(
                    "hl_mem",
                    "配置",
                    "hl_mem 的 real embedding 模式缺少 API key 时启动失败，测试配置允许 FakeEmbedder",
                    "permanent",
                    "fail-fast 和显式测试替身均已落地。",
                ),
                claim(
                    "hl_mem",
                    "配置",
                    "hl_mem 的 real reranker 模式缺少 API key 时启动失败",
                    "permanent",
                    "组件配置校验确认该 fail-fast 规则仍有效。",
                ),
                claim(
                    "hl_mem",
                    "事实",
                    "hl_mem 的 healthz 返回 embedder 和 reranker 状态",
                    "permanent",
                    "该管理面能力在任务中实施并保留至当前。",
                ),
                claim(
                    "hl_mem",
                    "事实",
                    "hl_mem 将 event 插入和 extract job 入队放在单一事务中",
                    "permanent",
                    "当前 IngestService 以 BEGIN IMMEDIATE 和 commit=False 保证原子性。",
                ),
                claim(
                    "hl_mem",
                    "事实",
                    "hl_mem 将 Episode 终结和 reward backprop 放在单一事务中",
                    "permanent",
                    "当前 API 路径确认两个操作共用事务。",
                ),
                claim(
                    "hl_mem",
                    "事实",
                    "hl_mem 的 Episode 采用 running→success|failed|cancelled 单向状态机且终态禁止新增 Trace",
                    "permanent",
                    "状态机和终态守卫已经落地。",
                ),
                claim(
                    "hl_mem",
                    "事实",
                    "hl_mem 在 BEGIN IMMEDIATE 事务内计算 Trace sequence_no",
                    "permanent",
                    "当前 ExperienceRepository 确认序号计算和写入在即时事务内。",
                ),
            ],
            [
                negative("本次任务要一次性修复所有硬伤", "task_instruction", "这是一次执行要求，不是项目事实。"),
                negative(
                    "当时全局共享单个 Connection", "superseded_state", "这是修复前状态，事件同时确认任务已正常完成。"
                ),
            ],
        ),
        "3aaef3c68ec14a56bc2f9350301ec504": event(
            [
                claim(
                    "hl_mem",
                    "事实",
                    "hl_mem 的版本号同时维护在 pyproject.toml 和 src/hl_mem/__init__.py",
                    "permanent",
                    "任务 diff 和当前代码确认稳定版本源位置。",
                ),
                claim(
                    "hl_mem",
                    "事实",
                    "hl_mem 的 /healthz 返回应用版本号",
                    "permanent",
                    "后续提交和当前 server.py 确认该能力已落地。",
                ),
                claim(
                    "hl_mem",
                    "事实",
                    "hl_mem CLI 支持 --version 参数",
                    "permanent",
                    "后续提交和当前 cli.py 确认该稳定 CLI 契约。",
                ),
            ],
            [
                negative("版本号从 0.1.0 升级到 0.2.0", "version_snapshot", "具体版本迁移是历史发版快照。"),
                negative("后台进程被 process.kill 终止", "process_snapshot", "进程状态不影响后来是否落地。"),
                negative("运行 pytest 并提交 a058866", "task_instruction", "这是一次任务的验收和提交要求。"),
            ],
        ),
    }
)


def load_testset() -> list[dict[str, Any]]:
    return [json.loads(line) for line in TESTSET_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]


def main() -> None:
    rows = load_testset()
    event_ids = [str(row["id"]) for row in rows]
    if len(rows) != 50 or len(set(event_ids)) != 50:
        raise ValueError("Gold v2 requires exactly 50 unique source events")
    if set(event_ids) != set(ANNOTATIONS):
        missing = sorted(set(event_ids) - set(ANNOTATIONS))
        extra = sorted(set(ANNOTATIONS) - set(event_ids))
        raise ValueError(f"annotation coverage mismatch: missing={missing}, extra={extra}")

    output: list[dict[str, Any]] = []
    all_claim_ids: list[str] = []
    for row in rows:
        event_id = str(row["id"])
        annotation = ANNOTATIONS[event_id]
        claims: list[dict[str, Any]] = []
        for index, source_claim in enumerate(annotation["claims"], 1):
            annotated = {"gold_claim_id": f"{event_id}:g{index:02d}", **source_claim}
            claims.append(annotated)
            all_claim_ids.append(annotated["gold_claim_id"])
        negatives = list(annotation["negatives"])
        output.append(
            {
                "event_id": event_id,
                "category": row["category"],
                "actor_type": row["actor_type"],
                "should_memorize": bool(claims),
                "gold_claims": claims,
                "negative_examples": negatives,
            }
        )

    if len(all_claim_ids) != len(set(all_claim_ids)):
        raise ValueError("duplicate gold_claim_id")
    for row in output:
        if row["should_memorize"] != bool(row["gold_claims"]):
            raise ValueError(f"should_memorize mismatch for {row['event_id']}")
        if not row["negative_examples"]:
            raise ValueError(f"event lacks a representative negative: {row['event_id']}")
        for item in row["gold_claims"]:
            if item["scope"] not in {"permanent", "temporal"}:
                raise ValueError(f"bad scope in {item['gold_claim_id']}")
            if item["label"] != "gold_positive" or not item["rationale"].strip():
                raise ValueError(f"bad positive annotation in {item['gold_claim_id']}")
        for item in row["negative_examples"]:
            if not item["label"].startswith("gold_negative_") or not item["rationale"].strip():
                raise ValueError(f"bad negative annotation in {row['event_id']}")

    payload = "\n".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) for row in output) + "\n"
    OUTPUT_PATH.write_text(payload, encoding="utf-8", newline="\n")
    stats = {
        "events": len(output),
        "positive": len(all_claim_ids),
        "negative": sum(len(row["negative_examples"]) for row in output),
        "should_memorize": sum(row["should_memorize"] for row in output),
        "permanent": sum(item["scope"] == "permanent" for row in output for item in row["gold_claims"]),
        "temporal": sum(item["scope"] == "temporal" for row in output for item in row["gold_claims"]),
        "output": str(OUTPUT_PATH),
    }
    print(json.dumps(stats, ensure_ascii=False))


ANNOTATIONS.update(
    {
        "85949cd50aa84acabb901463d7b5f904": event(
            [
                claim(
                    "hl_mem 网络环境",
                    "事实",
                    "缺少 socksio 时，socks5 ALL_PROXY 会导致 httpx 连接百炼 API 失败",
                    "permanent",
                    "原文给出已定位的根因，未来代理故障排查会直接复用。",
                ),
                claim(
                    "hl_mem 网络环境",
                    "配置",
                    "hl_mem 调用国内 API 时清空 ALL_PROXY 并保留 HTTP_PROXY 供 Codex 使用",
                    "permanent",
                    "这是明确执行并验证有效的代理分流配置。",
                ),
                claim(
                    "hl_mem 网络环境",
                    "配置",
                    "NO_PROXY 包含 aliyuncs.com 和 bigmodel.cn 以便国内 API 直连",
                    "permanent",
                    "固定域名绕过代理是稳定环境配置。",
                ),
            ],
            [
                negative(
                    "healthz、stats 和 recall 当前全部正常",
                    "operational_snapshot",
                    "接口健康和一次 recall 结果会刷新。",
                ),
                negative("当前有 78 events 和 108 claims", "metric_snapshot", "数据库计数是瞬时统计。"),
                negative("Codex 现在是否正常", "question", "问题本身不提供事实答案。"),
            ],
        ),
        "535f101c1e6d4e0796adcbc7406b8eda": event(
            [], [negative("字符串替换因找到两个匹配而失败", "tool_error", "这是一次编辑工具错误，没有稳定项目语义。")]
        ),
        "e0e569339d574c19bedfe27e3521b270": event(
            [], [negative("正在使用 uv run 启动进程", "process_progress", "截断内容只表示执行中的步骤。")]
        ),
        "ae98cca4c33340dc858a3e9130d7a31f": event(
            [], [negative("main 已 push 到 GitHub", "git_snapshot", "这是一次推送结果，不是长期技术事实。")]
        ),
        "6198823152654d9ca90b623c12694142": event(
            [], [negative("Codex 只完成打印优化，准备再次发指令", "process_progress", "这是任务编排过程状态。")]
        ),
        "58c6fbaeaa4e42d7857f52f92ffd96b7": event(
            [], [negative("正在测试 CLI --version", "process_progress", "没有给出可持久化的完成结果。")]
        ),
        "c34ac56955954a5486daf90a6336f004": event(
            [], [negative("正在杀掉旧 hl_mem 服务", "process_progress", "这是执行中的临时步骤。")]
        ),
        "0f6d5da1676645e1a122fa46a98dd711": event(
            [], [negative("查询当前版本号", "version_snapshot", "命令没有在事件中给出稳定版本管理结论。")]
        ),
        "0ce3852ca5d74afdb5992a7bbe6df98c": event(
            [
                claim(
                    "hl_mem API",
                    "事实",
                    "v1/events 是 POST 端点，对它发 GET 请求会返回 405",
                    "permanent",
                    "这是可复用的稳定 HTTP 方法契约。",
                )
            ],
            [
                negative(
                    "healthz 正常且 recall 返回 3 条", "operational_snapshot", "服务健康和返回数量是一次探测结果。"
                ),
                negative("正在启动 Codex 审查", "process_progress", "这是后续动作。"),
            ],
        ),
        "55feebc14c6c42f5859c6c1581d85955": event(
            [
                claim(
                    "hl_mem",
                    "事实",
                    "hl_mem 面向单机单 Agent 场景",
                    "permanent",
                    "这是用户明确给出的产品边界，会影响所有架构取舍。",
                ),
                claim("hl_mem", "使用", "hl_mem 使用 SQLite 存储", "permanent", "数据库选型是稳定技术栈事实。"),
                claim("hl_mem", "事实", "hl_mem 面向本地零运维部署", "permanent", "零运维是明确的产品定位和设计约束。"),
                claim("hl_mem", "配置", "hl_mem 作为 localhost 服务运行", "permanent", "本地服务边界是稳定部署配置。"),
                claim(
                    "hl_mem",
                    "配置",
                    "hl_mem 的单机部署不要求 API 鉴权",
                    "permanent",
                    "用户明确说明这是当前场景下的既定取舍。",
                ),
                claim("hl_mem", "配置", "hl_mem 的单机部署不要求租户隔离", "permanent", "单租户边界是明确架构约束。"),
                claim(
                    "hl_mem",
                    "配置",
                    "hl_mem 源代码位于 src/hl_mem",
                    "permanent",
                    "固定源码根目录会被后续审查和开发任务复用。",
                ),
                claim(
                    "hl_mem",
                    "事实",
                    "hl_mem 按 api、ingest、recall、storage、workers、experience、security、observability、adapters 和 mcp 分包",
                    "permanent",
                    "原文明确列出当前模块结构，未来架构讨论会引用。",
                ),
                claim(
                    "用户",
                    "偏好",
                    "评审 hl_mem 时应按单机单 Agent 实际场景判断，不套用企业级标准",
                    "permanent",
                    "用户明确规定评审尺度，属于稳定工作方式偏好。",
                ),
            ],
            [
                negative("当前 180 个测试通过", "test_snapshot", "测试数量会随版本变化。"),
                negative("本次审查不要修改代码或运行 pytest", "task_instruction", "只约束这一次只读审查。"),
            ],
        ),
        "c881d477be3e4bab9183b9a53dade7c0": event(
            [
                claim(
                    "hl_mem",
                    "事实",
                    "hl_mem 使用统一 lifecycle 状态转换守卫并接入 claim 状态变更路径",
                    "permanent",
                    "任务正常完成，后续代码确认统一状态机已经落地。",
                ),
                claim(
                    "hl_mem Worker",
                    "事实",
                    "hl_mem Worker 调度 reclassify 和 retention 生命周期任务",
                    "permanent",
                    "任务完成输出和当前 worker 都确认该调度能力。",
                ),
                claim(
                    "hl_mem",
                    "配置",
                    "hl_mem 的 decay policy 和 access bonus 参数可通过配置调整",
                    "permanent",
                    "配置外置任务已经落地，未来生命周期调参会使用。",
                ),
                claim(
                    "hl_mem Worker",
                    "配置",
                    "hl_mem 的 daily reclassify 默认调度时间曾为 04:30",
                    "temporal",
                    "可见 diff 明确给出当时默认 cron，属于低频但可能变化的运行配置。",
                ),
                claim(
                    "hl_mem",
                    "配置",
                    "hl_mem Python 代码遵循类型标注和 from __future__ import annotations 风格",
                    "permanent",
                    "用户把它列为项目现有代码风格，后续开发任务会复用。",
                ),
            ],
            [
                negative(
                    "本次重构禁止运行 pytest、修改 tests 或新增依赖",
                    "task_instruction",
                    "这些是单次任务约束，不能推断为永久项目政策。",
                ),
                negative("现有 205 个测试必须通过", "test_snapshot", "测试数量会随开发变化。"),
            ],
        ),
        "5dbcb695295e468b8230be94405acd22": event(
            [], [negative("kill Codex 和读取 init.py 均超时", "tool_error", "这是一次工具执行错误。")]
        ),
        "e67cf826cab941c0882f6c0ec228466f": event(
            [
                claim(
                    "Hermes 插件安装器",
                    "事实",
                    "install_to_hermes.py 在安装开始时打印目标路径",
                    "permanent",
                    "事件中的完成报告和 diff 确认该行为已经落地。",
                ),
                claim(
                    "Hermes 插件安装器",
                    "事实",
                    "install_to_hermes.py 在安装和校验成功后打印成功提示",
                    "permanent",
                    "事件中的完成报告和 diff 确认该稳定 CLI 行为。",
                ),
            ],
            [
                negative("定向测试 1 passed", "test_snapshot", "单次测试结果不应形成长期记忆。"),
                negative("工作区干净并已提交 d6863ea", "git_snapshot", "这是一次完成时的仓库快照。"),
            ],
        ),
        "e40c54fbbb814380927e42c68590d51f": event(
            [
                claim(
                    "hl_mem reranker",
                    "配置",
                    "当时 production 模式要求启用 reranker，开发环境默认关闭",
                    "temporal",
                    "工具输出直接展示当时的环境分支逻辑；配置后来可能演进。",
                ),
                claim(
                    "hl_mem reranker",
                    "配置",
                    "当时 HL_MEM_RERANKER 支持 off、fake、on 和 real 四种模式",
                    "temporal",
                    "工具输出明确列出合法模式。",
                ),
                claim(
                    "hl_mem reranker",
                    "配置",
                    "当时 reranker API key 可由 RERANKER_API_KEY 或 EMBEDDING_API_KEY 提供",
                    "temporal",
                    "这是代码中明确的历史密钥回退规则，后来可能变更。",
                ),
                claim(
                    "hl_mem reranker",
                    "配置",
                    "当时 reranker 默认使用 https://dashscope.aliyuncs.com 和 gte-rerank-v2",
                    "temporal",
                    "工具输出给出可复用但可变的 provider/model 默认值。",
                ),
            ],
            [
                negative(
                    "当前 shell 未设置 HL_MEM 和 RERANKER 环境变量",
                    "operational_snapshot",
                    "当前进程环境会随会话变化。",
                )
            ],
        ),
        "f6b3d998204e44d3b98eae0579239408": event(
            [], [negative("8200 端口存在大量 TIME_WAIT 连接", "operational_snapshot", "TCP 连接状态会即时变化。")]
        ),
        "00a1906076b74b51b08f62ee04b9a492": event([], [negative("空消息", "empty_content", "没有可提取内容。")]),
        "a1570b78e8784185883e81b376d24834": event(
            [],
            [
                negative(
                    "服务运行最新代码、embedder real、reranker off",
                    "operational_snapshot",
                    "这是一次 healthz 运行配置快照，后续文本还表示正在排查。",
                )
            ],
        ),
        "f3f4d2a51be04c468c8763b19ef0dfb0": event(
            [], [negative("代码已改好，正在检查 CLI 和 git", "process_progress", "这是 assistant 的过程汇报。")]
        ),
        "7b13bcb42084448cace7c0c129358956": event([], [negative("空消息", "empty_content", "没有可提取内容。")]),
        "ff99d2263b4c435cb1a122c833f73b9c": event(
            [], [negative("import 正常，准备检查启动方式", "process_progress", "这是临时诊断进度。")]
        ),
    }
)


if __name__ == "__main__":
    main()
