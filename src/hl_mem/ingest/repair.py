"""LLM 提取 JSON 的确定性 schema 兼容修复。"""

from __future__ import annotations

import math
from copy import deepcopy
from typing import Any

from hl_mem.domain.claims.attributes import ALLOWED_TOPIC_TAGS
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

ENUM_MAPPINGS: dict[str, dict[str, str]] = {
    "sensitivity": {
        **SENSITIVITY_ZH_TO_EN,
        "Normal": "normal",
        "Sensitive": "sensitive",
        "Restricted": "restricted",
    },
    "scope": {
        "Permanent": "permanent",
        "Temporal": "temporal",
        "永久": "permanent",
        "临时": "temporal",
    },
    "volatility": {
        "Stable": "stable",
        "Ephemeral": "ephemeral",
        "稳定": "stable",
        "短暂": "ephemeral",
    },
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
    repaired = [] if not original.strip() else [original]
    container["entities"] = repaired
    _emit_repair(path, original, repaired, "string_to_array")


def _repair_topic_tags(claim: dict[str, Any], path: str) -> None:
    """把中文 topic tag 确定性映射为受控英文标签。"""
    original = claim.get("topic_tags")
    tags = ([] if not original.strip() else [original]) if isinstance(original, str) else original
    if not isinstance(tags, list):
        return
    repaired = [
        (
            TOPIC_TAG_ZH_TO_EN.get(tag, tag.lower() if tag.lower() in ALLOWED_TOPIC_TAGS else tag)
            if isinstance(tag, str)
            else tag
        )
        for tag in tags
    ]
    if repaired == original:
        return
    claim["topic_tags"] = repaired
    _emit_repair(path, original, repaired, "topic_tag_mapping")


def _repair_enum(container: dict[str, Any], field: str, path: str) -> None:
    """按白名单修复已知枚举的大小写或中文形式。"""
    original = container.get(field)
    if not isinstance(original, str):
        return
    repaired = ENUM_MAPPINGS[field].get(original)
    if repaired is None or repaired == original:
        return
    container[field] = repaired
    _emit_repair(path, original, repaired, f"{field}_mapping")


def _repair_number(container: dict[str, Any], field: str, path: str) -> None:
    """把有限的合法数字字符串转换为浮点数，不修复越界值。"""
    original = container.get(field)
    if not isinstance(original, str):
        return
    try:
        repaired = float(original)
    except ValueError:
        return
    if not math.isfinite(repaired) or not 0.0 <= repaired <= 1.0:
        return
    container[field] = repaired
    _emit_repair(path, original, repaired, "numeric_string_to_float")


def repair_extraction_json(raw: dict[str, Any]) -> dict[str, Any]:
    """修复已知的 qwen3.7-plus JSON 形态偏差，并保留未知非法值供严格校验拒绝。"""
    repaired = deepcopy(raw)
    _repair_entities(repaired, "entities")

    _repair_enum(repaired, "sensitivity", "sensitivity")

    claims = repaired.get("claims")
    if claims is None:
        repaired["claims"] = []
        _emit_repair("claims", None, [], "null_to_array")
        return repaired
    if not isinstance(claims, list):
        return repaired
    for index, claim in enumerate(claims):
        if not isinstance(claim, dict):
            continue
        _repair_entities(claim, f"claims.{index}.entities")
        _repair_topic_tags(claim, f"claims.{index}.topic_tags")
        _repair_enum(claim, "scope", f"claims.{index}.scope")
        _repair_enum(claim, "volatility", f"claims.{index}.volatility")
        _repair_number(claim, "importance", f"claims.{index}.importance")
        _repair_number(claim, "confidence", f"claims.{index}.confidence")
    return repaired
