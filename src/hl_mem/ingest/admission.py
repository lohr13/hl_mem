"""LLM 记忆候选的确定性准入策略。"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
from typing import Any

from hl_mem.domain.claims.attributes import MUTUALLY_EXCLUSIVE_SLOTS

VALID_KINDS = frozenset({"preference", "architecture", "identity", "config", "fact", "plan"})
VALID_NOTABILITY = frozenset({"high", "medium", "low"})
LOW_VALUE_HEALTH_STATES = frozenset({"ok", "running", "stopped", "健康", "正常"})
NUMERIC_OR_VERSION_RE = re.compile(r"[0-9.]+")
RECOVERY_CODE_RE = re.compile(r"(?i)(?<![a-z0-9])[a-z0-9]{5}-[a-z0-9]{5}(?![a-z0-9])")
SECRET_ASSIGNMENT_RE = re.compile(r"""(?i)["']?(?:password|passwd|api[\s_-]?key)["']?\s*[:=]""")
SECRET_FIELD_NAME_RE = re.compile(r"(?i)(?:password|passwd|api[\s_-]?key)")
SK_TOKEN_RE = re.compile(r"(?i)(?<![a-z0-9])sk-[a-z0-9_-]+")
ALNUM_SECRET_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9]{16,32}(?![A-Za-z0-9])")
OPERATIONAL_SNAPSHOT_RE = re.compile(
    r"(?ix)(?:"
    r"\bhealthz\b.{0,24}\b(?:ok|healthy|unhealthy)\b|"
    r"\b(?:ci|build)\b.{0,16}全绿|"
    r"全绿.{0,12}\b(?:ci|build)\b|"
    r"\b(?:ci|build)\b.{0,12}(?:已|当前|目前|状态).{0,8}"
    r"(?:通过|失败|passed|failed|success(?:ful)?)|"
    r"\b\d+\s+(?:tests?\s+)?(?:passed|failed|skipped)\b|"
    r"\b(?:passed|failed|skipped)\s*[:=]?\s*\d+\b|"
    r"(?<!要求)(?<!必须)测试(?:已经|已|全部)?(?:通过|失败|全绿)|"
    r"环境变量.{0,12}(?:已)?(?:清空|移除|删除|unset)|"
    r"(?:正在|刚刚|已经|已)(?:执行|处理|跑|运行).{0,16}(?:benchmark|基准|测试|任务|脚本)|"
    r"(?:正在|已经|已)(?:重启|启动|停止).{0,8}(?:服务|进程)|"
    r"(?:当前|目前|已经|已|正在).{0,8}(?:服务|进程).{0,8}(?:运行|启动|停止|重启)|"
    r"(?:服务|进程).{0,8}(?:已经|已|正在).{0,8}(?:运行|启动|停止|重启)|"
    r"(?:服务|进程)(?:状态)?.{0,8}\b(?:ok|running|stopped|healthy|unhealthy)\b|"
    r"(?:当前|目前).{0,12}版本(?:为|是|[:=])?\s*v?\d+(?:\.\d+){1,3}|"
    r"(?:安装|卸载)(?:脚本|程序).{0,20}(?:print|打印|输出|成功|完成|失败)|"
    r"(?:安装|卸载)(?:已经|已)?(?:成功|完成|失败)|"
    r"(?:print|打印|输出).{0,20}(?:安装|卸载)(?:成功|完成|失败)|"
    r"(?:已经|已|本次|此次)?(?:修复|fixed)(?:了)?.{0,24}(?:bug|缺陷|错误|不可达代码)|"
    r"(?:bug|缺陷|错误|不可达代码).{0,24}(?:已经|已)?(?:修复|去掉|删除|移除)|"
    r"(?:已经|已|本次|此次)?(?:去掉|删除|移除)(?:了)?.{0,24}(?:代码|分支|文件|逻辑|实现|兼容层|脚本)|"
    r"新增(?:了)?(?:单元|集成|回归)?测试|"
    r"(?:单元|集成|回归)测试(?:已经|已|全部)?(?:通过|失败|全绿)|"
    r"新增(?:了)?文件|"
    r"(?:本次|此次|当前)?.{0,8}代码行数|"
    r"\bcommit\s*(?:hash|id)\b|"
    r"\bcommit\s+[0-9a-f]{7,40}\b|"
    r"__version__.{0,16}(?:版本|version|更新|修改|改为|设置|[:=])|"
    r"pyproject(?:\.toml)?.{0,16}(?:版本|version).{0,12}(?:更新|修改|改为|设置|为|[:=])|"
    r"\b(?:fixed|removed|deleted|added)\b.{0,24}\b(?:bug|dead\s+code|test|file)\b"
    r")"
)

_EVIDENCE_FUZZY_THRESHOLD = 0.80


@dataclass(frozen=True, slots=True)
class MemoryCandidate:
    """LLM 输出的最小记忆候选。"""

    subject: str
    value: str
    kind: str
    confidence: float
    notability: str
    evidence_quote: str


@dataclass(frozen=True, slots=True)
class AdmissionDecision:
    """确定性准入结果及稳定原因码。"""

    accepted: bool
    reason: str


def _normalized_evidence(value: Any) -> str:
    """规范化证据文本，忽略空白和标点差异。"""
    normalized = unicodedata.normalize("NFKC", str(value)).casefold()
    return "".join(
        character
        for character in normalized
        if not character.isspace() and not unicodedata.category(character).startswith(("P", "Z"))
    )


def evidence_quote_matches(evidence_quote: str, source_text: str) -> bool:
    """判断引用能否在原文中精确或小范围模糊定位。"""
    quote = _normalized_evidence(evidence_quote)
    source = _normalized_evidence(source_text)
    if not quote or not source:
        return False
    if quote in source:
        return True
    if len(quote) < 6:
        return False
    if len(quote) >= len(source):
        return SequenceMatcher(None, quote, source, autojunk=False).ratio() >= _EVIDENCE_FUZZY_THRESHOLD

    window_length = len(quote)
    last_start = len(source) - window_length
    step = max(1, window_length // 8)
    starts = list(range(0, last_start + 1, step))
    if starts[-1] != last_start:
        starts.append(last_start)
    return any(
        SequenceMatcher(
            None,
            quote,
            source[start : start + window_length],
            autojunk=False,
        ).ratio()
        >= _EVIDENCE_FUZZY_THRESHOLD
        for start in starts
    )


def low_value_reason(value: Any, canonical_slot: str | None = None) -> str | None:
    """复用生产输出边界的低价值值判断。"""
    normalized = unicodedata.normalize("NFKC", str(value)).strip()
    if not normalized:
        return "low_value"
    if NUMERIC_OR_VERSION_RE.fullmatch(normalized) and canonical_slot not in MUTUALLY_EXCLUSIVE_SLOTS:
        return "low_value"
    if canonical_slot == "state.service_health" and normalized.casefold() in LOW_VALUE_HEALTH_STATES:
        return "low_value"
    return None


def secret_reason(value: Any) -> str | None:
    """识别禁止进入 Claim 存储的确定性凭据格式。"""
    if isinstance(value, dict):
        for key, nested_value in value.items():
            normalized_key = unicodedata.normalize("NFKC", str(key)).strip()
            key_reason = secret_reason(normalized_key)
            if key_reason is not None:
                return key_reason
            has_value = nested_value is not None and (
                not isinstance(nested_value, (str, dict, list, tuple, set, frozenset)) or bool(nested_value)
            )
            if SECRET_FIELD_NAME_RE.fullmatch(normalized_key) and has_value:
                return "secret_assignment"
            nested_reason = secret_reason(nested_value)
            if nested_reason is not None:
                return nested_reason
        return None
    if isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            nested_reason = secret_reason(item)
            if nested_reason is not None:
                return nested_reason
        return None
    if value is None:
        return None
    text = unicodedata.normalize("NFKC", str(value)).strip()
    if RECOVERY_CODE_RE.search(text):
        return "recovery_code"
    if SECRET_ASSIGNMENT_RE.search(text):
        return "secret_assignment"
    if SK_TOKEN_RE.search(text):
        return "sk_token"
    for match in ALNUM_SECRET_RE.finditer(text):
        token = match.group(0)
        if any(character.isalpha() for character in token) and any(character.isdigit() for character in token):
            return "mixed_alnum_token"
    return None


def admit_claim(candidate: MemoryCandidate, source_text: str) -> AdmissionDecision:
    """纯函数准入判断，同时供 dry-run benchmark 和生产写入使用。"""
    if candidate.notability == "low":
        return AdmissionDecision(False, "low_notability")
    if candidate.notability not in VALID_NOTABILITY or candidate.kind not in VALID_KINDS:
        return AdmissionDecision(False, "invalid_candidate")
    if not isinstance(candidate.confidence, (int, float)) or not math.isfinite(candidate.confidence):
        return AdmissionDecision(False, "invalid_candidate")
    if not 0.0 <= float(candidate.confidence) <= 1.0:
        return AdmissionDecision(False, "invalid_candidate")
    if not unicodedata.normalize("NFKC", str(candidate.subject)).strip():
        return AdmissionDecision(False, "empty_subject")
    if not unicodedata.normalize("NFKC", str(candidate.value)).strip():
        return AdmissionDecision(False, "empty_value")

    credential_reason = secret_reason(asdict(candidate))
    if credential_reason is not None:
        return AdmissionDecision(False, credential_reason)
    if not evidence_quote_matches(candidate.evidence_quote, source_text):
        return AdmissionDecision(False, "no_evidence")
    if OPERATIONAL_SNAPSHOT_RE.search(unicodedata.normalize("NFKC", str(candidate.value))):
        return AdmissionDecision(False, "operational_snapshot")
    if low_value_reason(candidate.value) is not None:
        return AdmissionDecision(False, "low_value")
    return AdmissionDecision(True, "accepted")


def admission_rules_fingerprint() -> dict[str, Any]:
    """返回影响准入结果的稳定规则指纹。"""
    return {
        "valid_kinds": sorted(VALID_KINDS),
        "valid_notability": sorted(VALID_NOTABILITY),
        "low_value_health_states": sorted(LOW_VALUE_HEALTH_STATES),
        "evidence_fuzzy_threshold": _EVIDENCE_FUZZY_THRESHOLD,
        "patterns": {
            "numeric_or_version": NUMERIC_OR_VERSION_RE.pattern,
            "recovery_code": RECOVERY_CODE_RE.pattern,
            "secret_assignment": SECRET_ASSIGNMENT_RE.pattern,
            "secret_field_name": SECRET_FIELD_NAME_RE.pattern,
            "sk_token": SK_TOKEN_RE.pattern,
            "mixed_alnum_secret": ALNUM_SECRET_RE.pattern,
            "operational_snapshot": OPERATIONAL_SNAPSHOT_RE.pattern,
        },
    }
