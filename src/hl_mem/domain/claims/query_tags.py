"""将自然语言查询确定性映射为 topic_tags。"""

from __future__ import annotations

import re

from hl_mem.domain.claims.attributes import ALLOWED_TOPIC_TAGS

LOW_INFORMATION_TAGS = frozenset({"other", "fact", "state", "choice", "config", "plan", "preference"})

CHINESE_TAG_MAP: tuple[tuple[str, str], ...] = (
    ("架构", "architecture"),
    ("设计", "architecture"),
    ("决策", "decision"),
    ("决定", "decision"),
    ("需求", "requirement"),
    ("实现", "implementation"),
    ("修复", "bugfix"),
    ("行为", "behavior"),
    ("依赖", "dependency"),
    ("版本", "version"),
    ("迁移", "migration"),
    ("评估", "evaluation"),
    ("工作流", "workflow"),
    ("测试", "test"),
    ("部署", "deployment"),
    ("进程", "process"),
    ("任务", "job"),
    ("连接", "connectivity"),
    ("硬件", "hardware"),
    ("超时", "timeout"),
    ("调度", "schedule"),
    ("路由", "routing"),
    ("协议", "protocol"),
    ("框架", "framework"),
    ("接口", "api"),
    ("角色", "role"),
    ("目标", "goal"),
    ("能力", "capability"),
    ("约束", "constraint"),
    ("问题", "issue"),
    ("原因", "cause"),
    ("解决", "resolution"),
)

TAG_INFO_WEIGHT = {tag: 1.0 for tag in ALLOWED_TOPIC_TAGS if tag not in LOW_INFORMATION_TAGS}

_SLOT_HINT_RULES: tuple[tuple[re.Pattern[str], str, tuple[str, ...]], ...] = (
    (re.compile(r"叫什么|姓名|名字|name", re.I), "identity.name", ("identity",)),
    (
        re.compile(r"GPU|显卡|显存|VRAM|graphics\s+card|内存|CPU|处理器|processor|硬件", re.I),
        "config.hardware",
        ("hardware",),
    ),
    (
        re.compile(r"提取模型|embedding\s*模型|reranker\s*模型|模型", re.I),
        "config.model",
        ("model", "implementation"),
    ),
    (re.compile(r"代理|REDACTED_PROXY|网络", re.I), "config.network", ("connectivity",)),
    (re.compile(r"喜欢|偏好|习惯|趁手|顺手", re.I), "preference.*", ()),
)


def extract_query_slot_hints(query: str) -> tuple[list[str], list[str]]:
    """从查询中提取高精度 slot hint 及受控 topic tag。"""
    slots: list[str] = []
    tags: list[str] = []
    for pattern, slot, related_tags in _SLOT_HINT_RULES:
        if pattern.search(query):
            slots.append(slot)
            tags.extend(tag for tag in related_tags if tag in ALLOWED_TOPIC_TAGS)
    return list(dict.fromkeys(slots)), list(dict.fromkeys(tags))


def extract_query_tags(query: str) -> list[str]:
    """使用中英文高置信规则从 query 提取去重后的有效标签。"""
    matches: list[tuple[int, str]] = []
    lowered = query.lower()
    for tag in ALLOWED_TOPIC_TAGS - LOW_INFORMATION_TAGS:
        match = re.search(rf"(?<![a-z0-9_]){re.escape(tag)}(?![a-z0-9_])", lowered)
        if match is not None:
            matches.append((match.start(), tag))
    for keyword, tag in CHINESE_TAG_MAP:
        start = query.find(keyword)
        if start >= 0:
            matches.append((start, tag))
    matches.sort(key=lambda item: item[0])
    return list(dict.fromkeys(tag for _, tag in matches))
