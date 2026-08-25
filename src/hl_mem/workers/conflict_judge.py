"""本地 Qwen 冲突裁决与 job 边界。"""

from __future__ import annotations

from typing import Any, Mapping, Protocol

from hl_mem.domain.governance import CONFLICT_AUTO_POLICY_VERSION
from hl_mem.evaluation.local_qwen_runner import (
    LocalQwenRunner,
    OversizedDocket,
    QwenRunConfig,
)
from hl_mem.settings import Settings
from hl_mem.workers.auto_resolve_conflicts import (
    AutoDecision,
    StaleConflictDecision,
    apply_auto_conflict_decision,
    conflict_docket_fingerprint,
    load_conflict_docket,
    validate_l2_result,
)


class ConflictJudgeProtocol(Protocol):
    def judge(self, docket: Mapping[str, Any]) -> AutoDecision: ...


def _conservative_token_count(text: str) -> int:
    """UTF-8 byte count is a safe upper bound when the local tokenizer is unavailable."""

    return len(text.encode("utf-8"))


class LocalConflictJudge:
    """使用批次 0 安全 runner 完成候选顺序互换双遍裁决。"""

    def __init__(self, runner: LocalQwenRunner, *, rule_enabled: bool = True) -> None:
        self.runner = runner
        self.rule_enabled = rule_enabled

    @classmethod
    def from_settings(cls, settings: Settings) -> "LocalConflictJudge":
        settings.validate()
        config = QwenRunConfig(
            base_url=settings.maintenance_judge_base_url,
            model=settings.maintenance_judge_model,
            prompt_version=settings.maintenance_judge_prompt_version,
            tokenizer_identity=settings.maintenance_judge_tokenizer_identity,
            enable_thinking=False,
            timeout_seconds=settings.maintenance_judge_timeout_seconds,
        )
        return cls(LocalQwenRunner(token_counter=_conservative_token_count, config=config))

    def judge(self, docket: Mapping[str, Any]) -> AutoDecision:
        case = docket.get("case") or {}
        payload = {
            "case_id": str(case.get("id") or ""),
            "case": dict(case),
            "claims": list(docket.get("claims") or []),
            "candidates": list(docket.get("candidates") or []),
            "evidence": list(docket.get("evidence") or []),
            "policy": {
                "allowed_decisions": ["keep_left", "keep_right", "coexist", "reject", "select_candidate"],
                "required_fields": [
                    "decision",
                    "winner_candidate_key",
                    "confidence",
                    "rationale_code",
                    "decisive_evidence_ids",
                    "ambiguity_flags",
                ],
                "confidence_floor": 0.90,
                "exclusive_groups_require_winner": True,
            },
        }
        try:
            result = self.runner.run_case(payload)
        except OversizedDocket:
            return AutoDecision(
                "manual_required",
                None,
                0.0,
                "L3",
                "oversized_docket",
                resolver_model=self.runner.config.model,
            )
        return validate_l2_result(
            docket,
            result,
            confidence_floor=0.90,
            rule_enabled=self.rule_enabled,
            resolver_model=self.runner.config.model,
        )


def run_conflict_llm_job(
    connection: Any,
    payload: Mapping[str, Any],
    judge: ConflictJudgeProtocol,
    *,
    mode: str,
    now: str,
) -> dict[str, Any]:
    """模型调用在事务外；应用前由 application service 重读并执行 CAS。"""

    case_id = str(payload["case_id"])
    docket = load_conflict_docket(connection, case_id)
    expected_revision = int(payload["revision"])
    expected_fingerprint = str(payload["input_fingerprint"])
    if int(docket["case"].get("revision") or 0) != expected_revision:
        raise StaleConflictDecision(f"stale conflict revision before L2: {case_id}")
    if conflict_docket_fingerprint(docket) != expected_fingerprint:
        raise StaleConflictDecision(f"stale conflict fingerprint before L2: {case_id}")
    decision = judge.judge(docket)
    return apply_auto_conflict_decision(
        connection,
        case_id,
        decision,
        expected_revision=expected_revision,
        expected_fingerprint=expected_fingerprint,
        policy_version=str(payload.get("policy_version") or CONFLICT_AUTO_POLICY_VERSION),
        mode=mode,
        now=now,
    )
