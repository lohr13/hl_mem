"""搜索召回链路的结构化追踪模型与记录器。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class CandidateTrace:
    """记录单个候选在搜索各阶段的排名、分数与排除原因。"""

    claim_id: str
    channels: dict[str, int] = field(default_factory=dict)
    channel_scores: dict[str, float] = field(default_factory=dict)
    pre_rank: int | None = None
    pre_score: float | None = None
    rerank_rank: int | None = None
    rerank_score: float | None = None
    final_rank: int | None = None
    included: bool = False
    filter_reasons: list[str] = field(default_factory=list)
    relation_paths: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class SearchPhaseMetrics:
    """记录搜索阶段耗时，单位为微秒。"""

    fts_us: int = 0
    dense_us: int = 0
    relation_us: int = 0
    fusion_us: int = 0
    reranker_us: int = 0
    assembly_us: int = 0
    total_us: int = 0


@dataclass
class SearchTrace:
    """描述一次搜索的安全、可序列化追踪信息。"""

    query_id: str
    query_hash: str
    intent: str
    limit: int
    candidate_limit: int
    candidates: dict[str, CandidateTrace]
    phases: SearchPhaseMetrics
    outcome: str = "success"
    truncated: bool = False


class SearchTracer:
    """以候选数量上限记录搜索过程，不保留查询或记忆正文。"""

    def __init__(self, trace: SearchTrace, max_candidates: int = 200) -> None:
        self.trace = trace
        self.max_candidates = max(1, max_candidates)

    def _candidate(self, claim_id: str, *, preserve: bool = False) -> CandidateTrace | None:
        candidate = self.trace.candidates.get(claim_id)
        if candidate is not None:
            return candidate
        if len(self.trace.candidates) >= self.max_candidates:
            self.trace.truncated = True
            if not preserve:
                return None
            removable = next(
                (
                    existing_id
                    for existing_id, existing in reversed(self.trace.candidates.items())
                    if not existing.included
                ),
                None,
            )
            if removable is None:
                return None
            del self.trace.candidates[removable]
        candidate = CandidateTrace(claim_id=claim_id)
        self.trace.candidates[claim_id] = candidate
        return candidate

    def record_channel(self, channel: str, claims: list[dict[str, Any]]) -> None:
        """记录通道返回的 1-based 排名及可用通道分数。"""
        for rank, claim in enumerate(claims, 1):
            candidate = self._candidate(str(claim["id"]))
            if candidate is None:
                continue
            candidate.channels[channel] = rank
            score = claim.get("_score")
            if isinstance(score, (int, float)):
                candidate.channel_scores[channel] = float(score)

    def record_filter(self, claim_id: str, reason: str) -> None:
        """为已进入候选集的 claim 追加受控排除原因。"""
        candidate = self._candidate(str(claim_id))
        if candidate is not None and reason not in candidate.filter_reasons:
            candidate.filter_reasons.append(reason)

    def record_pre_rank(self, claims: list[dict[str, Any]], scores: dict[str, float]) -> None:
        """记录融合及多因子先验排序。"""
        for rank, claim in enumerate(claims, 1):
            claim_id = str(claim["id"])
            candidate = self._candidate(claim_id)
            if candidate is not None:
                candidate.pre_rank = rank
                candidate.pre_score = float(scores[claim_id])

    def record_rerank(self, results: list[tuple[str, float]]) -> None:
        """记录 reranker 返回的候选顺序与分数。"""
        for rank, (claim_id, score) in enumerate(results, 1):
            candidate = self._candidate(str(claim_id))
            if candidate is not None:
                candidate.rerank_rank = rank
                candidate.rerank_score = float(score)

    def record_relation_path(self, claim_id: str, path: dict[str, Any]) -> None:
        """记录关系扩展候选的一跳来源，不包含 claim 正文。"""
        candidate = self._candidate(str(claim_id))
        if candidate is not None:
            candidate.relation_paths.append(path)

    def record_final(self, claims: list[dict[str, Any]]) -> None:
        """记录最终返回项，并优先保留这些候选。"""
        final_ids = {str(claim["id"]) for claim in claims}
        for candidate in self.trace.candidates.values():
            candidate.included = candidate.claim_id in final_ids
        for rank, claim in enumerate(claims, 1):
            candidate = self._candidate(str(claim["id"]), preserve=True)
            if candidate is not None:
                candidate.final_rank = rank
                candidate.included = True

    def to_dict(self) -> dict[str, Any]:
        """返回不含查询明文、claim value 或密钥的 JSON 兼容字典。"""
        return asdict(self.trace)
