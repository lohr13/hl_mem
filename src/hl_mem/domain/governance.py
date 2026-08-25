"""跨领域治理动作的窄合同与安全快照。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Sequence

from hl_mem.domain.claims.attributes import is_mutually_exclusive_attribute
from hl_mem.domain.claims.conflicts import coordinate_qualifier_key

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
CONFLICT_AUTO_POLICY_VERSION = "conflict-auto-v1"


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


_AUTHORITY = {"low": 1, "medium": 2, "high": 3}
_TERMINAL = frozenset({"superseded", "expired", "rejected", "rolled_back"})
_LIVING = frozenset({"active", "candidate", "disputed"})


def is_terminal_conflict_status(status: Any) -> bool:
    """Return whether a claim status is safe for deterministic lifecycle removal."""

    return str(status or "") in _TERMINAL


@dataclass(frozen=True)
class AutoDecision:
    """领域 decision 与共享治理外壳之间的冲突专用结果。"""

    decision: str
    winner_candidate_key: str | None
    confidence: float
    tier: str
    rule: str
    evidence_ids: tuple[str, ...] = ()
    resolver_model: str | None = None


@dataclass(frozen=True)
class L1Policy:
    """E1 预注册候选阈值；产品默认只在 observe 中使用。"""

    min_time_delta_seconds: int
    min_confidence_delta: float

    def __post_init__(self) -> None:
        if self.min_time_delta_seconds not in {0, 300, 3_600}:
            raise ValueError("min_time_delta_seconds must be an E1 preregistered value")
        if self.min_confidence_delta not in {0.10, 0.15, 0.20}:
            raise ValueError("min_confidence_delta must be an E1 preregistered value")


@dataclass(frozen=True)
class L2Admission:
    admitted: bool
    reason: str


def _claims(docket: Mapping[str, Any]) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    claims = docket.get("claims") or []
    if len(claims) < 2:
        return {}, {}
    return claims[0], claims[1]


def _context(docket: Mapping[str, Any]) -> Mapping[str, Any]:
    value = docket.get("context")
    return value if isinstance(value, Mapping) else {}


def _case(docket: Mapping[str, Any]) -> Mapping[str, Any]:
    value = docket.get("case")
    return value if isinstance(value, Mapping) else {}


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _confidence(claim: Mapping[str, Any]) -> float:
    value = claim.get("confidence")
    return float(value) if value is not None else 0.0


def _authority(claim: Mapping[str, Any]) -> int:
    return _AUTHORITY.get(str(claim.get("source_authority") or "medium"), 2)


def _is_plan(claim: Mapping[str, Any]) -> bool:
    kind = str(claim.get("assertion_kind") or "")
    slot = str(claim.get("canonical_slot") or "")
    return kind == "plan" or slot.startswith("plan.")


def _same_coordinates(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    slot = left.get("canonical_slot")
    if not isinstance(slot, str) or slot != right.get("canonical_slot"):
        return False
    if left.get("namespace_key") != right.get("namespace_key"):
        return False
    if left.get("subject_entity_id") != right.get("subject_entity_id"):
        return False
    return coordinate_qualifier_key(slot, left.get("qualifiers")) == coordinate_qualifier_key(
        slot, right.get("qualifiers")
    )


def _exclusive_pair(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    slot = left.get("canonical_slot")
    return bool(slot == right.get("canonical_slot") and is_mutually_exclusive_attribute(str(slot or "")))


def _winner_decision(docket: Mapping[str, Any], side: str) -> tuple[str, str]:
    claim = _claims(docket)[0 if side == "left" else 1]
    claim_id = str(claim.get("id") or side)
    if bool(_case(docket).get("group_native")):
        for candidate in docket.get("candidates") or []:
            if candidate.get("representative_claim_id") == claim_id:
                return "select_candidate", str(candidate.get("candidate_key"))
    return f"keep_{side}", claim_id


def _decision_for_side(
    docket: Mapping[str, Any],
    side: str,
    *,
    confidence: float,
    tier: str,
    rule: str,
) -> AutoDecision:
    decision, winner_key = _winner_decision(docket, side)
    return AutoDecision(decision, winner_key, confidence, tier, rule)


def _has_explicit_change(claim: Mapping[str, Any]) -> bool:
    qualifiers = claim.get("qualifiers") or {}
    return isinstance(qualifiers, Mapping) and any(qualifiers.get(key) for key in ("state_change", "change", "current"))


def decide_l0(docket: Mapping[str, Any]) -> AutoDecision | None:
    """按不可交换顺序执行六条确定性法条。"""

    left, right = _claims(docket)
    context = _context(docket)
    left_tip = context.get("left_tip_id")
    if left_tip and left_tip == context.get("right_tip_id"):
        return AutoDecision("obsolete", None, 1.0, "L0", "chain_endpoint_converged")

    if _case(docket).get("group_native"):
        living = [candidate for candidate in docket.get("candidates") or [] if not candidate.get("terminal")]
        if not living:
            return AutoDecision("obsolete", None, 1.0, "L0", "lifecycle_single_survivor")
        if len(living) == 1:
            return AutoDecision(
                "select_candidate",
                str(living[0].get("candidate_key")),
                1.0,
                "L0",
                "lifecycle_single_survivor",
            )
        if _case(docket).get("overflow"):
            return None
        return None

    left_terminal = is_terminal_conflict_status(left.get("status"))
    right_terminal = is_terminal_conflict_status(right.get("status"))
    if left_terminal and right_terminal:
        return AutoDecision("obsolete", None, 1.0, "L0", "lifecycle_single_survivor")
    if left_terminal != right_terminal and not context.get("survivor_contested"):
        side = "right" if left_terminal else "left"
        survivor = right if left_terminal else left
        if survivor.get("status") in _LIVING:
            return _decision_for_side(docket, side, confidence=1.0, tier="L0", rule="lifecycle_single_survivor")

    if left.get("value") == right.get("value") and left.get("value") is not None:
        side = min((left, "left"), (right, "right"), key=lambda item: str(item[0].get("id") or ""))[1]
        return _decision_for_side(docket, side, confidence=1.0, tier="L0", rule="exact_candidate")

    if _same_coordinates(left, right) and not (_is_plan(left) or _is_plan(right)):
        left_from = _parse_time(left.get("valid_from"))
        right_from = _parse_time(right.get("valid_from"))
        left_to = _parse_time(left.get("valid_to"))
        right_to = _parse_time(right.get("valid_to"))
        nonoverlap = bool(
            (left_to and right_from and left_to <= right_from) or (right_to and left_from and right_to <= left_from)
        )
        if nonoverlap or _has_explicit_change(left) or _has_explicit_change(right):
            if left_from and right_from and left_from != right_from:
                side = "left" if left_from > right_from else "right"
                return _decision_for_side(docket, side, confidence=1.0, tier="L0", rule="strict_temporal_state_change")

    if (
        _same_coordinates(left, right)
        and not (_is_plan(left) or _is_plan(right))
        and not context.get("later_counterevidence")
        and _authority(left) != _authority(right)
    ):
        side = "left" if _authority(left) > _authority(right) else "right"
        return _decision_for_side(docket, side, confidence=1.0, tier="L0", rule="strict_authority")

    if (
        context.get("nonexclusive_false_positive")
        and not _exclusive_pair(left, right)
        and not _same_coordinates(left, right)
    ):
        return AutoDecision("coexist", None, 1.0, "L0", "nonexclusive_false_positive")
    return None


def _candidate_for_claim(docket: Mapping[str, Any], claim_id: str) -> Mapping[str, Any]:
    return next(
        (
            candidate
            for candidate in docket.get("candidates") or []
            if candidate.get("representative_claim_id") == claim_id or candidate.get("candidate_key") == claim_id
        ),
        {},
    )


def _l1_safe(docket: Mapping[str, Any]) -> bool:
    left, right = _claims(docket)
    context = _context(docket)
    return bool(
        not _case(docket).get("overflow")
        and not context.get("entity_type_mismatch")
        and context.get("coordinates_complete", _same_coordinates(left, right))
        and not (_is_plan(left) or _is_plan(right))
    )


def decide_l1(docket: Mapping[str, Any], policy: L1Policy) -> AutoDecision | None:
    """只在硬门满足后运行三条合取启发式。"""

    if not _l1_safe(docket):
        return None
    left, right = _claims(docket)
    context = _context(docket)
    if context.get("reverse_evidence") or context.get("later_counterevidence"):
        return None

    left_time = _parse_time(left.get("valid_from") or left.get("observed_at"))
    right_time = _parse_time(right.get("valid_from") or right.get("observed_at"))
    if left_time and right_time and left_time != right_time:
        later_side = "left" if left_time > right_time else "right"
        later, earlier = (left, right) if later_side == "left" else (right, left)
        delta = abs((left_time - right_time).total_seconds())
        if (
            delta >= policy.min_time_delta_seconds
            and _authority(later) >= _authority(earlier)
            and _confidence(later) >= _confidence(earlier)
        ):
            return _decision_for_side(
                docket,
                later_side,
                confidence=_confidence(later),
                tier="L1",
                rule="temporal_confidence_dominance",
            )

    authority_delta = _authority(left) - _authority(right)
    if authority_delta:
        side = "left" if authority_delta > 0 else "right"
        winner, loser = (left, right) if side == "left" else (right, left)
        winner_candidate = _candidate_for_claim(docket, str(winner.get("id") or ""))
        loser_candidate = _candidate_for_claim(docket, str(loser.get("id") or ""))
        if (
            abs(authority_delta) >= 1
            and _confidence(winner) - _confidence(loser) + 1e-12 >= policy.min_confidence_delta
            and int(winner_candidate.get("evidence_count") or 0) >= int(loser_candidate.get("evidence_count") or 0)
        ):
            return _decision_for_side(
                docket,
                side,
                confidence=_confidence(winner),
                tier="L1",
                rule="authority_evidence_dominance",
            )

    candidates = list(docket.get("candidates") or [])
    if len(candidates) >= 2:
        ordered = sorted(candidates, key=lambda item: int(item.get("support_count") or 0), reverse=True)
        if int(ordered[0].get("support_count") or 0) > int(ordered[1].get("support_count") or 0):
            winner_id = str(ordered[0].get("representative_claim_id") or "")
            winner_claim = next((claim for claim in (left, right) if claim.get("id") == winner_id), {})
            other_claims = [claim for claim in (left, right) if claim.get("id") != winner_id]
            if winner_claim and all(_authority(winner_claim) >= _authority(claim) for claim in other_claims):
                side = "left" if left.get("id") == winner_id else "right"
                return _decision_for_side(
                    docket,
                    side,
                    confidence=_confidence(winner_claim),
                    tier="L1",
                    rule="group_support_dominance",
                )
    return None


def assess_l2_admission(
    docket: Mapping[str, Any],
    now: str,
    *,
    max_candidates: int,
    policy_version: str,
) -> L2Admission:
    """以稳定 reason 表达七组 L2 准入条件。"""

    context = _context(docket)
    case = _case(docket)
    claims = list(docket.get("claims") or [])
    candidates = list(docket.get("candidates") or [])
    if not context.get("previous_reason"):
        return L2Admission(False, "missing_prior_insufficiency")
    living_candidates = [candidate for candidate in candidates if not candidate.get("terminal")]
    if len(living_candidates) < 2:
        return L2Admission(False, "fewer_than_two_living_candidates")
    if case.get("overflow") or len(candidates) > max_candidates:
        return L2Admission(False, "candidate_overflow")
    if context.get("entity_type_mismatch"):
        return L2Admission(False, "entity_type_mismatch")
    if any(_is_plan(claim) for claim in claims):
        return L2Admission(False, "plan_not_allowed")
    if not context.get("evidence_readable"):
        return L2Admission(False, "evidence_damaged")
    if context.get("docket_oversized"):
        return L2Admission(False, "oversized_docket")
    if context.get("last_l2_policy_version") == policy_version:
        return L2Admission(False, "already_judged_current_input")
    not_before = _parse_time(context.get("not_before"))
    current = _parse_time(now)
    if not_before and current and not_before > current:
        return L2Admission(False, "not_before_pending")
    if not context.get("schema_valid"):
        return L2Admission(False, "schema_invalid")
    return L2Admission(True, "admitted")


def _decisive_evidence(result: Mapping[str, Any]) -> tuple[str, ...]:
    decisions = result.get("decisions") or []
    if not isinstance(decisions, Sequence) or not decisions:
        return ()
    first = decisions[0]
    if not isinstance(first, Mapping):
        return ()
    values = first.get("decisive_evidence_ids") or first.get("evidence_ids") or []
    return tuple(str(value) for value in values if str(value).strip())


def validate_l2_result(
    docket: Mapping[str, Any],
    result: Mapping[str, Any],
    *,
    confidence_floor: float,
    rule_enabled: bool,
    resolver_model: str | None = None,
) -> AutoDecision:
    """验证双遍输出；语义不足进入 L3，传输/JSON 异常由调用方退避。"""

    context = _context(docket)
    if context.get("docket_oversized"):
        return AutoDecision("manual_required", None, 0.0, "L3", "oversized_docket")
    if not result.get("consistent"):
        return AutoDecision("manual_required", None, 0.0, "L3", "candidate_order_disagreement")
    confidence = float(result.get("confidence") or 0.0)
    if confidence < confidence_floor:
        return AutoDecision("manual_required", None, confidence, "L3", "low_confidence")
    if context.get("equal_authority_first_hand_conflict"):
        return AutoDecision("manual_required", None, confidence, "L3", "equal_authority_counterevidence")
    decision = str(result.get("decision") or "")
    winner = result.get("winner_candidate_key")
    candidate_keys = {str(candidate.get("candidate_key")) for candidate in docket.get("candidates") or []}
    if decision in {"keep_left", "keep_right", "select_candidate"} and str(winner) not in candidate_keys:
        return AutoDecision("manual_required", None, confidence, "L3", "winner_membership_violation")
    left, right = _claims(docket)
    if _exclusive_pair(left, right) and decision in {"coexist", "reject"}:
        return AutoDecision("manual_required", None, confidence, "L3", "exclusive_group_violation")
    if decision not in {"keep_left", "keep_right", "coexist", "reject", "select_candidate"}:
        return AutoDecision("manual_required", None, confidence, "L3", "lifecycle_invariant_violation")
    if not rule_enabled:
        return AutoDecision("manual_required", None, confidence, "L3", "rule_not_enabled")
    rationale = "qwen_consistent"
    decisions = result.get("decisions") or []
    if decisions and isinstance(decisions[0], Mapping):
        rationale = str(decisions[0].get("rationale_code") or rationale)[:128]
    return AutoDecision(
        decision,
        str(winner) if winner is not None else None,
        confidence,
        "L2",
        rationale,
        _decisive_evidence(result),
        resolver_model,
    )
