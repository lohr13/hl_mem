"""跨领域治理动作的窄合同与安全快照。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping

_FORBIDDEN_REASONING_KEYS = frozenset(
    {
        "chain_of_thought",
        "chain-of-thought",
        "cot",
        "hidden_reasoning",
        "reasoning_content",
        "thinking",
    }
)
_MAX_SNAPSHOT_BYTES = 65_536
_MAX_EVIDENCE_IDS = 128


class UnsafeGovernanceSnapshot(ValueError):
    """治理快照包含隐藏推理或超过有界大小。"""


def _reject_hidden_reasoning(value: Any, path: str = "$") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = str(key).strip().casefold()
            if normalized in _FORBIDDEN_REASONING_KEYS:
                raise UnsafeGovernanceSnapshot(f"forbidden governance snapshot field {path}.{key}")
            _reject_hidden_reasoning(item, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_hidden_reasoning(item, f"{path}[{index}]")


def canonical_snapshot(value: Mapping[str, Any]) -> str:
    """返回可复算的有界 JSON；禁止保存隐藏推理。"""

    _reject_hidden_reasoning(value)
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    if len(serialized.encode("utf-8")) > _MAX_SNAPSHOT_BYTES:
        raise UnsafeGovernanceSnapshot(f"governance snapshot exceeds {_MAX_SNAPSHOT_BYTES} bytes")
    return serialized


def snapshot_fingerprint(value: Mapping[str, Any] | str) -> str:
    """计算治理快照的 SHA-256 指纹。"""

    serialized = value if isinstance(value, str) else canonical_snapshot(value)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class DecisionEnvelope:
    """所有治理领域共享的决策外壳，不共享领域 decision 枚举。"""

    domain: str
    subject_ref: str
    input_fingerprint: str
    policy_version: str
    tier: str
    decision: str
    confidence: float | None
    resolution_rule: str
    resolver_model: str | None
    evidence_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        required = {
            "domain": self.domain,
            "subject_ref": self.subject_ref,
            "input_fingerprint": self.input_fingerprint,
            "policy_version": self.policy_version,
            "tier": self.tier,
            "decision": self.decision,
            "resolution_rule": self.resolution_rule,
        }
        empty = next((name for name, value in required.items() if not value.strip()), None)
        if empty is not None:
            raise ValueError(f"DecisionEnvelope.{empty} must not be empty")
        if self.confidence is not None and not 0 <= self.confidence <= 1:
            raise ValueError("DecisionEnvelope.confidence must be between 0 and 1")
        if len(self.evidence_ids) > _MAX_EVIDENCE_IDS:
            raise ValueError(f"DecisionEnvelope.evidence_ids exceeds {_MAX_EVIDENCE_IDS}")
        if any(not evidence_id.strip() for evidence_id in self.evidence_ids):
            raise ValueError("DecisionEnvelope.evidence_ids must not contain empty IDs")

    @property
    def decision_hash(self) -> str:
        """散列应用语义，供相同输入的幂等一致性校验。"""

        payload = {
            "confidence": self.confidence,
            "decision": self.decision,
            "evidence_ids": sorted(set(self.evidence_ids)),
            "resolution_rule": self.resolution_rule,
            "resolver_model": self.resolver_model,
            "tier": self.tier,
        }
        return snapshot_fingerprint(payload)
