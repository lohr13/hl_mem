"""LLM 提取 JSON 的确定性 schema 兼容修复。"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from hl_mem.observability.audit import current_audit

TOPIC_TAG_ZH_TO_EN: dict[str, str] = {
    "账户": "account",
    "接口": "api",
    "架构": "architecture",
    "行为": "behavior",
    "缺陷修复": "bugfix",
    "能力": "capability",
    "原因": "cause",
    "选择": "choice",
    "配置": "config",
    "连接性": "connectivity",
    "约束": "constraint",
    "联系方式": "contact",
    "决策": "decision",
    "依赖": "dependency",
    "数据库": "dependency",
    "部署": "deployment",
    "评估": "evaluation",
    "事实": "fact",
    "框架": "framework",
    "目标": "goal",
    "硬件": "hardware",
    "身份": "identity",
    "实现": "implementation",
    "问题": "issue",
    "任务": "job",
    "成员关系": "membership",
    "记忆": "memory",
    "迁移": "migration",
    "操作系统": "os",
    "其他": "other",
    "计划": "plan",
    "偏好": "preference",
    "流程": "process",
    "协议": "protocol",
    "需求": "requirement",
    "解决方案": "resolution",
    "角色": "role",
    "路由": "routing",
    "日程": "schedule",
    "状态": "state",
    "测试": "test",
    "超时": "timeout",
    "工具选择": "tool_choice",
    "版本": "version",
    "工作流": "workflow",
}

SENSITIVITY_ZH_TO_EN: dict[str, str] = {
    "普通": "normal",
    "正常": "normal",
    "一般": "normal",
    "敏感": "sensitive",
    "受限": "restricted",
    "限制": "restricted",
    "严格限制": "restricted",
}


def _emit_repair(path: str, original: Any, repaired: Any, repair_type: str) -> None:
    """为每个实际发生的确定性修复写入审计事件。"""
    current_audit().emit(
        "extract",
        "schema_json_repair",
        "applied",
        detail={
            "path": path,
            "repair_type": repair_type,
            "original_value": original,
            "repaired_value": repaired,
        },
    )


def _repair_entities(container: dict[str, Any], path: str) -> None:
    """把单个实体字符串修复为 schema 要求的数组。"""
    original = container.get("entities")
    if not isinstance(original, str):
        return
    repaired = [original]
    container["entities"] = repaired
    _emit_repair(path, original, repaired, "string_to_array")


def _repair_topic_tags(claim: dict[str, Any], path: str) -> None:
    """把中文 topic tag 确定性映射为受控英文标签。"""
    original = claim.get("topic_tags")
    tags = [original] if isinstance(original, str) else original
    if not isinstance(tags, list):
        return
    repaired = [TOPIC_TAG_ZH_TO_EN.get(tag, tag) if isinstance(tag, str) else tag for tag in tags]
    if repaired == original:
        return
    claim["topic_tags"] = repaired
    _emit_repair(path, original, repaired, "topic_tag_mapping")


def repair_extraction_json(raw: dict[str, Any]) -> dict[str, Any]:
    """修复已知的 qwen3.7-plus JSON 形态偏差，并保留未知非法值供严格校验拒绝。"""
    repaired = deepcopy(raw)
    _repair_entities(repaired, "entities")

    sensitivity = repaired.get("sensitivity")
    normalized_sensitivity = SENSITIVITY_ZH_TO_EN.get(sensitivity) if isinstance(sensitivity, str) else None
    if normalized_sensitivity is not None:
        repaired["sensitivity"] = normalized_sensitivity
        _emit_repair("sensitivity", sensitivity, normalized_sensitivity, "sensitivity_mapping")

    claims = repaired.get("claims")
    if not isinstance(claims, list):
        return repaired
    for index, claim in enumerate(claims):
        if not isinstance(claim, dict):
            continue
        _repair_entities(claim, f"claims.{index}.entities")
        _repair_topic_tags(claim, f"claims.{index}.topic_tags")
    return repaired
