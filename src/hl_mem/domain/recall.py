"""召回意图路由及双时间可见性领域策略。"""

from __future__ import annotations

import re
from dataclasses import dataclass

from hl_mem.domain.constants import (
    INTENT_KEYWORDS_ANALOGICAL,
    INTENT_KEYWORDS_AS_OF,
    INTENT_KEYWORDS_HISTORICAL,
    INTENT_KEYWORDS_PREFERENCE,
    INTENT_KEYWORDS_PROCEDURAL,
    INTENT_KEYWORDS_RELATIONAL,
    INTENT_WORDS_PREFERENCE_EN,
    PREFERENCE_LIKE_WORDS_EN,
    PROCEDURE_ACTION_KEYWORDS,
    PROCEDURE_KEYWORDS,
    RECOMMENDATION_CHOICE_ACTIONS_EN,
    RECOMMENDATION_CHOICE_ACTIONS_ZH,
    RECOMMENDATION_VERBS_EN,
    RECOMMENDATION_VERBS_ZH,
    TOOL_KEYWORDS,
)
from hl_mem.domain.temporal import RecallIntent

__all__ = ["QueryRoute", "RecallIntent", "route_query", "route_recall_intent"]


def _words_pattern(words: tuple[str, ...]) -> str:
    return "(?:" + "|".join(re.escape(word) for word in words) + ")"


_EN_PREFERENCE_WORD_RE = re.compile(rf"\b{_words_pattern(INTENT_WORDS_PREFERENCE_EN)}\b")
_EN_FIRST_PERSON_LIKE_RE = re.compile(
    rf"(?:\b(?:i|we)\s+(?:really\s+)?{_words_pattern(PREFERENCE_LIKE_WORDS_EN)}\b|"
    rf"\b(?:what|which)\b[^?!.]{{0,80}}\bdo\s+(?:i|we)\s+{_words_pattern(PREFERENCE_LIKE_WORDS_EN)}\b|"
    rf"\b(?:do|did|will|would)\s+(?:i|we)\s+{_words_pattern(PREFERENCE_LIKE_WORDS_EN)}\b)"
)
_EN_RECOMMENDATION_RE = re.compile(rf"\b{_words_pattern(RECOMMENDATION_VERBS_EN)}\b(?!\s+(?:how|why|that|whether)\b)")
_EN_PERSONAL_CHOICE_RE = re.compile(
    rf"\b(?:what|which)\b[^?!.]{{0,80}}\bshould\s+(?:i|we)\s+" rf"{_words_pattern(RECOMMENDATION_CHOICE_ACTIONS_EN)}\b"
)
_EN_HISTORICAL_BEFORE_RE = re.compile(r"\b(?:did|had|used|was|were)\b[^?!.]{0,80}\bbefore\b")
_ZH_RECOMMEND_RE = re.compile(
    rf"(?:^|[，。！？?\s])(?:请)?(?:你)?(?:能|能否|能不能|可以|可否)?(?:给我)?"
    rf"{_words_pattern(RECOMMENDATION_VERBS_ZH)}"
    r"(?!系统|算法|功能|模块|机制|如何|怎么|为什么|为何|是否)"
)
_ZH_SUGGEST_CHOICE_RE = re.compile(rf"建议(?:给)?我(?:应该|应|该)?{_words_pattern(RECOMMENDATION_CHOICE_ACTIONS_ZH)}")
_ZH_SUITABLE_FOR_ME_RE = re.compile(r"适合我(?:的)?")
_ZH_SHOULD_CHOOSE_RE = re.compile(rf"我(?:应该|应|该){_words_pattern(RECOMMENDATION_CHOICE_ACTIONS_ZH)}")
_ZH_WANT_WHERE_RE = re.compile(r"(?:我)?想去(?:哪里|哪儿|哪(?:个|家|种|款)?)")


@dataclass(frozen=True)
class QueryRoute:
    """召回意图、候选通道和可选参考时间。"""

    intent: str
    channels: tuple[str, ...]
    reference_time: str | None


def route_query(query: str, reference_time: str | None = None) -> QueryRoute:
    """根据中文查询线索选择召回通道。"""
    lowered = query.lower()
    if any(word in lowered for word in INTENT_KEYWORDS_PROCEDURAL):
        return QueryRoute("procedure", ("procedure", "fts", "dense"), reference_time)
    if any(word in lowered for word in INTENT_KEYWORDS_HISTORICAL):
        return QueryRoute("historical", ("temporal", "fact", "fts", "dense"), reference_time)
    if any(word in lowered for word in INTENT_KEYWORDS_RELATIONAL):
        return QueryRoute("relation", ("relation", "fact", "fts", "dense"), reference_time)
    if any(word in lowered for word in INTENT_KEYWORDS_ANALOGICAL):
        return QueryRoute("similar_experience", ("episode", "fts", "dense"), reference_time)
    return QueryRoute("current_state", ("fact", "fts", "dense"), reference_time)


def route_recall_intent(query: str, as_of: str | None, now: str | None = None) -> RecallIntent:
    """根据查询语义推断召回意图；时间快照不改变用户意图。"""
    # 保留 as_of/now 参数以兼容既有调用；可见性与排序时钟由各自管线消费。
    lowered = query.casefold()
    if any(marker in lowered for marker in (*INTENT_KEYWORDS_AS_OF, "as_of")) or _EN_HISTORICAL_BEFORE_RE.search(
        lowered
    ):
        return RecallIntent.HISTORICAL
    if (
        any(marker in lowered for marker in INTENT_KEYWORDS_PREFERENCE)
        or _EN_PREFERENCE_WORD_RE.search(lowered)
        or _EN_FIRST_PERSON_LIKE_RE.search(lowered)
        or _EN_RECOMMENDATION_RE.search(lowered)
        or _EN_PERSONAL_CHOICE_RE.search(lowered)
        or _ZH_RECOMMEND_RE.search(query)
        or _ZH_SUGGEST_CHOICE_RE.search(query)
        or _ZH_SUITABLE_FOR_ME_RE.search(query)
        or _ZH_SHOULD_CHOOSE_RE.search(query)
        or _ZH_WANT_WHERE_RE.search(query)
    ):
        return RecallIntent.PREFERENCE
    if any(marker in lowered for marker in TOOL_KEYWORDS):
        return RecallIntent.TOOL
    if "上次" in lowered and any(marker in lowered for marker in PROCEDURE_ACTION_KEYWORDS):
        return RecallIntent.PROCEDURE
    if any(marker in lowered for marker in PROCEDURE_KEYWORDS):
        return RecallIntent.PROCEDURE
    return RecallIntent.CURRENT_STATE
