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

TAG_INFO_WEIGHT = {
    tag: 1.0
    for tag in ALLOWED_TOPIC_TAGS
    if tag not in LOW_INFORMATION_TAGS
}


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
