from __future__ import annotations

import re
from typing import Any, Literal

from hl_mem.domain.claims.state_projection import STATE_TRANSITION_SLOTS
from hl_mem.domain.entity import normalize_entity_id

STATE_CONTRACT_VERSION = "state-contract-v1"

_ZH_RULES = "状态提取覆盖规则（优先于后文 notability 与跳过规则）：\n- 每个顶层 claim 本身就是原子事实；一个 event 有多个可独立判断的事实时直接输出多个 claim，不建复合父 claim。\n- 明确且有证据的当前 version/release、health、deployment、instance、process、connectivity、job/task 快照必须提取，即使短期或 low。\n- 坐标自包含：每条 claim 的 subject 重复同 event 明示的稳定 owner；value 保留 owner、部署、实例、进程、任务、连接和状态值，不依赖 sibling。不得跨 event、引用或主体传播 owner。\n- 当前观察按当前事实提取；历史仍提取但保留“历史/曾经/当时/补录”和时间；计划或要求保留“计划/将/要求/必须/不得”，kind=plan；引用保留来源归属；否定保留否定词，均不得改写成当前肯定事实。\n- 例：“gateway-1 healthy，API 连接 reachable”。错：subject=API；对：两个 claim 都以 gateway-1 为 subject，value 各自包含 gateway-1，evidence_quote 引用完整 owner 片段。"
_EN_RULES = "State-extraction overrides (higher priority than later notability and skip rules):\n- Every top-level claim is atomic. Emit separate claims for independently truth-evaluable facts; never create a compound parent.\n- Extract every evidence-grounded current version/release, health, deployment, instance, process, connectivity, or job/task snapshot, even when short-lived or low-notability.\n- Self-contained coordinates: every claim repeats the stable owner stated in the same event as subject; value keeps the owner, deployment, instance, process, job, connectivity identifier, and state. Never propagate owners across events, quotations, or subjects.\n- Extract current observations as current facts. Historical claims retain the past cue and time; plans/requirements retain will, plan, require, must or must not with kind=plan; quotations retain attribution; negations retain negative words. Never rewrite these contexts as current positive facts.\n- Example: “gateway-1 is healthy; its API connection is reachable.” Both claims use gateway-1 as subject and carry it in value and the owner-bearing evidence quote; subject=API is wrong."
_ZH_OLD_SKIP = "跳过：\n- 服务健康快照、CI 测试数量、版本号查询结果、过程进度、纯问候、未确认建议。\n- running/stopped/ok、测试通过数、环境变量已清空、正在重启等操作快照。"
_EN_OLD_SKIP = "Skip:\n- Service-health snapshots, CI test counts, version-query results, work in progress, greetings, and unconfirmed advice.\n- Operational snapshots such as running/stopped/ok, tests passed, an environment variable being cleared, or a restart."
_ZH_NEW_SKIP = "跳过：\n- 纯问候、没有结果的纯查询、未确认建议。\n- 不要跳过有明确证据的 version/health/process/deployment/connectivity/job 快照；只跳过无结果提问、无证据猜测和无事实过程填充。"
_EN_NEW_SKIP = "Skip:\n- Greetings, bare queries with no result, and unconfirmed advice.\n- Do not skip an evidence-grounded version, health, process, deployment, connectivity, or job snapshot; skip only result-free questions, guesses, and process filler."


def with_state_snapshot_rules(prompt: str, *, language: Literal["zh", "en"]) -> str:
    """在八字段产品 prompt 上叠加状态规则，不替换其 schema。"""
    if language == "zh":
        boundary, rules, old_skip, new_skip = "输入边界：", _ZH_RULES, _ZH_OLD_SKIP, _ZH_NEW_SKIP
    else:
        boundary, rules, old_skip, new_skip = "Source boundaries:", _EN_RULES, _EN_OLD_SKIP, _EN_NEW_SKIP
    if prompt.count(boundary) != 1 or prompt.count(old_skip) != 1:
        raise RuntimeError("state prompt anchors must occur exactly once")
    return prompt.replace(boundary, f"{rules}\n\n{boundary}", 1).replace(old_skip, new_skip, 1)


_SLOT_PATTERNS = (
    ("config.version", re.compile(r"(?i)(?:版本|\bversion\b|\brelease\b|\bv\d+(?:\.\d+)*\b)")),
    ("state.process", re.compile(r"(?i)(?:进程|\bprocess\b|\brunning\b|\bstopped\b)")),
    ("state.deployment", re.compile(r"(?i)(?:部署|上线|\bdeploy(?:ed|ment)?\b)")),
    ("state.connectivity", re.compile(r"(?i)(?:连接|可达|不可达|\bconnectivity\b|\breachable\b)")),
    ("state.job", re.compile(r"(?i)(?:任务|\bjob\b|\btask\b|已完成|completed)")),
    ("state.service_health", re.compile(r"(?i)(?:健康|正常|挂了|\bhealthy\b|\bunhealthy\b|\bok\b)")),
)
_OWNER_SUFFIX = re.compile(
    r"(?i)^(.+?)(?:\s*的\s*|\s+)(?:[a-z0-9_.-]+\s*)?(?:服务|service|实例|instance|节点|node|进程|process|任务|job|部署|deployment)$"
)
_VALUE_OWNER_ALIAS = re.compile(
    r"(?i)^(.+?)(?:\s*的\s*|\s+)(?:[a-z0-9_.-]+\s*)?(?:服务|service)(?=版本|\s+(?:version|release)|$)"
)
_HISTORICAL = re.compile(r"(?i)(?:历史|回顾|曾经|当时|补录|historical|formerly|at that time|backfill)")
_NON_ASSERTED = re.compile(
    r"(?i)(?:计划|将要|要求|必须|不得|文档写道|引用|据报告|称|不是|并不是|未曾|\bplan\b|\bwill\b|\brequire\b|\bmust\b|according to|\bsaid\b|\bnot\b|never)"
)
_AXES = {
    "environment": re.compile(
        r"(?i)\b(production|prod|staging|stage|development|dev|test)\b|生产环境|预发环境|开发环境|测试环境"
    ),
    "instance": re.compile(r"(?i)(?:实例|instance|节点|node)\s*[:#=-]?\s*([a-z0-9_.-]+)"),
    "deployment": re.compile(r"(?i)(?:部署|deployment)\s*[:#=-]?\s*([a-z0-9_.-]+)"),
    "process": re.compile(r"(?i)([a-z0-9_.-]+)\s*(?:进程|process)"),
    "job": re.compile(r"(?i)([a-z0-9_.-]+)\s*(?:任务|job|task)"),
    "service": re.compile(r"(?i)([a-z0-9_.-]+)\s*(?:服务|service)"),
    "platform": re.compile(r"(?i)\b(windows|linux|macos|darwin)\b"),
    "component": re.compile(r"(?i)\b(server|cli|api|worker|plugin|sdk)\b"),
}
_GENERIC_OWNERS = {"api", "web", "worker", "sync", "service", "process", "job", "task", "unknown"}


def _state_slot(attribute: str, slot: str | None, text: str) -> str | None:
    if slot in STATE_TRANSITION_SLOTS or attribute in STATE_TRANSITION_SLOTS:
        return slot or attribute
    return next((name for name, pattern in _SLOT_PATTERNS if pattern.search(text)), None)


def _source_axes(text: str, evidence: str, slot: str, owner: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, pattern in _AXES.items():
        match = pattern.search(text)
        candidate = next((group for group in match.groups() if group), match.group(0)) if match else None
        if candidate and (not evidence or candidate.casefold() in evidence.casefold()):
            result[key] = candidate.strip().casefold()
    if slot == "config.version":
        result.pop("service", None)
    if slot == "state.service_health" and "service" not in result:
        result["service"] = owner
    return result


def canonicalize_state_fields(
    source: Any,
    subject: str,
    attribute: str,
    canonical_slot: str | None,
    qualifiers: dict[str, Any],
    assertion_kind: str | None = None,
) -> tuple[str, str, str | None, dict[str, Any]]:
    value = str(source.value)
    evidence_quote = str(getattr(source, "evidence_quote", ""))
    text = f"{subject} {value}"
    slot = _state_slot(attribute, canonical_slot, value)
    if slot is None:
        return subject, attribute, canonical_slot, dict(qualifiers)
    normalized_owner = normalize_entity_id(subject)
    owner_match = _OWNER_SUFFIX.fullmatch(normalized_owner)
    owner = normalize_entity_id(owner_match.group(1)) if owner_match else normalized_owner
    context = "historical" if _HISTORICAL.search(value) else "current"
    assertion = assertion_kind or getattr(source, "assertion_kind", "unknown")
    if getattr(source, "kind", None) == "plan" or assertion != "observation" or _NON_ASSERTED.search(value):
        context = "non_asserted"
    alias = _VALUE_OWNER_ALIAS.search(value) if slot == "config.version" else None
    ambiguous = owner in _GENERIC_OWNERS or (alias is not None and normalize_entity_id(alias.group(1)) != owner)
    result = {**qualifiers, "_state_context": "non_asserted" if ambiguous else context}
    if ambiguous or context == "non_asserted":
        return owner, slot, None, result
    result.update(_source_axes(text, evidence_quote, slot, owner))
    return owner, slot, slot, result
