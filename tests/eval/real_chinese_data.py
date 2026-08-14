"""从 PerLTQA 与 MemDaily 生成小规模、可追溯的中文召回隔离评测集。"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from hl_mem.domain.recall import route_recall_intent

DEFAULT_PERLTQA_MEMORY_PATH = Path("D:/workspace/PerLTQA/Dataset/zh/perltmem.json")
DEFAULT_PERLTQA_QA_PATH = Path("D:/workspace/PerLTQA/Dataset/zh/perltqa.json")
DEFAULT_MEMDAILY_PATH = Path("D:/workspace/MemSim/data_generation/final_dataset/memdaily.json")
DEFAULT_OUTPUT_DIR = Path.home() / "hl_mem_eval_data" / "datasets"

PERLTQA_CORPUS_NAME = "perltqa_breadth_corpus.jsonl"
PERLTQA_CASES_NAME = "perltqa_breadth_eval.jsonl"
MEMDAILY_CORPUS_NAME = "memdaily_depth_corpus.jsonl"
MEMDAILY_CASES_NAME = "memdaily_depth_eval.jsonl"
MANIFEST_NAME = "chinese_real_eval_manifest.json"

PERLTQA_MEMORY_TYPES = ("profile", "social_relationship", "events", "dialogues")
MEMDAILY_QUESTION_TYPES = (
    "simple",
    "conditional",
    "comparative",
    "aggregative",
    "post_processing",
    "noisy",
)
PERLTQA_POSITIVE_PER_TYPE = 14
PERLTQA_NO_ANSWER_PER_TYPE = 2
MEMDAILY_POSITIVE_PER_TYPE = 7
MEMDAILY_NO_ANSWER_PER_TYPE = 1
PERLTQA_PREFERENCE_QUOTAS = {"social_relationship": 4, "events": 4, "dialogues": 4}
MEMDAILY_PREFERENCE_QUOTAS = {"simple": 3, "conditional": 3, "noisy": 2}

_PREFERENCE_MARKERS = ("偏好", "喜欢", "喜好", "爱好", "习惯", "最爱", "特别爱")
_PROFILE_LABELS = {
    "Protagonist": "姓名",
    "Gender": "性别",
    "Nickname": "昵称",
    "Title": "头衔",
    "Age": "年龄",
    "Occupation": "职业",
    "Nationality": "国籍",
    "Physical Characteristics": "外貌特征",
    "Hobbies": "爱好",
    "Achievements": "成就",
    "Ethnic Background": "族裔背景",
    "Education Background": "教育背景",
    "Employer": "雇主",
    "Awards and Role Models": "奖项与榜样",
    "Awards": "奖项",
    "Role Models": "榜样",
}


@dataclass(frozen=True)
class PerLTQASelection:
    role: str
    memory_type: str
    source_ref: str
    question_contains: str


@dataclass(frozen=True)
class MemDailySelection:
    question_type: str
    tid: int


@dataclass(frozen=True)
class DatasetBundle:
    corpus: tuple[dict[str, Any], ...]
    cases: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class _PerLTQACandidate:
    role: str
    memory_type: str
    source_ref: str
    question: str
    answer: Any
    intent: str
    ordinal: int


def _stable_rank(*parts: object) -> str:
    payload = "\x1f".join(str(part) for part in parts)
    return hashlib.sha256(f"hl-mem-real-zh-v2\x1f{payload}".encode()).hexdigest()


def _namespace(dataset: str, *parts: object) -> str:
    return ":".join((dataset, *(str(part) for part in parts)))


DEFAULT_PERLTQA_SELECTIONS = (
    PerLTQASelection("张小红", "profile", "Occupation", "职业"),
    PerLTQASelection("梁欣", "profile", "Hobbies", "爱好"),
    PerLTQASelection("张小红", "social_relationship", "4_0", "兄弟是谁"),
    PerLTQASelection("梁欣", "social_relationship", "17_4", "王伟喜欢做什么"),
    PerLTQASelection("张小红", "events", "4_6_1", "特别喜欢哪个国家"),
    PerLTQASelection("梁欣", "events", "17_5_2", "品尝了哪些川菜佳肴"),
    PerLTQASelection("张小红", "dialogues", "4_6_1#15", "去年张小红观看了什么演出"),
    PerLTQASelection("梁欣", "dialogues", "17_5_2#10", "特别喜欢哪道菜"),
)

DEFAULT_MEMDAILY_SELECTIONS = (
    MemDailySelection("simple", 10),
    MemDailySelection("conditional", 12),
    MemDailySelection("comparative", 0),
    MemDailySelection("aggregative", 0),
    MemDailySelection("post_processing", 0),
    MemDailySelection("noisy", 3),
)


def _load_json(path: Path) -> Any:
    if not path.is_file():
        raise FileNotFoundError(f"source dataset does not exist: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _require_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _require_list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a JSON array")
    return value


def _required_text(row: dict[str, Any], field: str, label: str) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label}.{field} must be a non-empty string")
    return value.strip()


def _reference_ids(raw: Any, memory_type: str) -> tuple[str, ...]:
    if memory_type == "profile":
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError("PerLTQA profile Reference Memory must be a non-empty string")
        return (raw.strip(),)
    if not isinstance(raw, str):
        raise ValueError("PerLTQA Reference Memory must be a stringified list")
    try:
        parsed = ast.literal_eval(raw)
    except (SyntaxError, ValueError) as error:
        raise ValueError(f"invalid PerLTQA Reference Memory: {raw!r}") from error
    if not isinstance(parsed, list) or not parsed or any(not isinstance(item, str) or not item for item in parsed):
        raise ValueError(f"PerLTQA Reference Memory must contain source IDs: {raw!r}")
    return tuple(parsed)


def _iter_qa_items(group: Any, label: str) -> Iterable[dict[str, Any]]:
    if isinstance(group, list):
        values = group
    elif isinstance(group, dict):
        values = [item for key, items in group.items() for item in _require_list(items, f"{label}.{key}")]
    else:
        raise ValueError(f"{label} must be a list or object of lists")
    for index, item in enumerate(values):
        yield _require_mapping(item, f"{label}[{index}]")


def _perltqa_candidates(qa_by_role: dict[str, dict[str, Any]]) -> list[_PerLTQACandidate]:
    candidates: list[_PerLTQACandidate] = []
    ordinal = 0
    for role, groups in qa_by_role.items():
        for memory_type in PERLTQA_MEMORY_TYPES:
            group = groups.get(memory_type)
            if group is None:
                continue
            for item in _iter_qa_items(group, f"PerLTQA QA {role}.{memory_type}"):
                refs = _reference_ids(item.get("Reference Memory"), memory_type)
                if len(refs) != 1:
                    continue
                question = _required_text(item, "Question", f"PerLTQA QA {role}.{memory_type}")
                intent = route_recall_intent(question, None).value
                if intent not in {"current_state", "preference"}:
                    continue
                candidates.append(
                    _PerLTQACandidate(
                        role=role,
                        memory_type=memory_type,
                        source_ref=refs[0],
                        question=question,
                        answer=item.get("Answer"),
                        intent=intent,
                        ordinal=ordinal,
                    )
                )
                ordinal += 1
    return candidates


def _pick_diverse_perltqa(
    candidates: Sequence[_PerLTQACandidate],
    count: int,
    *,
    selected: Sequence[_PerLTQACandidate] = (),
) -> list[_PerLTQACandidate]:
    chosen = list(selected)
    chosen_keys = {(item.role, item.memory_type, item.source_ref) for item in chosen}
    role_counts = Counter(item.role for item in chosen)
    ref_counts = Counter(item.source_ref for item in chosen)
    available = [item for item in candidates if (item.role, item.memory_type, item.source_ref) not in chosen_keys]
    while len(chosen) < count and available:
        item = min(
            available,
            key=lambda candidate: (
                role_counts[candidate.role] > 0,
                role_counts[candidate.role],
                ref_counts[candidate.source_ref] > 0,
                ref_counts[candidate.source_ref],
                _stable_rank(
                    candidate.role,
                    candidate.memory_type,
                    candidate.source_ref,
                    candidate.question,
                ),
            ),
        )
        chosen.append(item)
        chosen_keys.add((item.role, item.memory_type, item.source_ref))
        role_counts[item.role] += 1
        ref_counts[item.source_ref] += 1
        available = [
            candidate
            for candidate in available
            if (candidate.role, candidate.memory_type, candidate.source_ref) not in chosen_keys
        ]
    if len(chosen) != count:
        raise ValueError(f"cannot select {count} diverse PerLTQA cases; found {len(chosen)}")
    return chosen


def _default_perltqa_candidates(
    candidates: Sequence[_PerLTQACandidate],
) -> list[_PerLTQACandidate]:
    selected: list[_PerLTQACandidate] = []
    for memory_type in PERLTQA_MEMORY_TYPES:
        type_candidates = [item for item in candidates if item.memory_type == memory_type]
        preference_quota = PERLTQA_PREFERENCE_QUOTAS.get(memory_type, 0)
        preferred = _pick_diverse_perltqa(
            [item for item in type_candidates if item.intent == "preference"],
            preference_quota,
        )
        selected.extend(
            _pick_diverse_perltqa(
                type_candidates,
                PERLTQA_POSITIVE_PER_TYPE,
                selected=preferred,
            )
        )
    return selected


def _answer_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if value is None:
        return ""
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _answer_absent(answer: Any, corpus_text: str) -> bool:
    normalized = re.sub(r"[\s，。！？、；：,.!?;:]", "", _answer_text(answer).casefold())
    if len(normalized) < 2:
        return False
    normalized_corpus = re.sub(r"[\s，。！？、；：,.!?;:]", "", corpus_text.casefold())
    return normalized not in normalized_corpus


def _is_natural_memdaily_preference(query: str) -> bool:
    tail = query
    for marker in ("真正想问的是", "真正想要了解的是", "真正的问题是", "实际想问的问题是", "想弄清楚的是"):
        if marker in tail:
            tail = tail.rsplit(marker, 1)[1]
    return any(
        marker in tail
        for marker in ("兴趣爱好是什么", "爱好是什么", "平时都喜欢", "平时喜欢", "喜欢干点啥", "喜欢干些什么")
    )


def _memdaily_subject_hint(message: str) -> str:
    match = re.match(
        r"^((?:(?:我的(?:一个)?|我))?[\u4e00-\u9fff]{1,8}?)(?=的|是|在|他在|今年|身高|只有|就|过生日|，|,)",
        message,
    )
    return match.group(1).strip(" ，,。") if match else "这些记录中的目标人物"


def _memory_id(dataset: str, *parts: object) -> str:
    return ":".join((dataset, *(str(part) for part in parts)))


def _claim_row(
    *,
    memory_id: str,
    subject: str,
    value: str,
    source_dataset: str,
    source_memory_type: str,
    source_ref: str,
    qualifiers: dict[str, Any],
    preference_hint: bool = False,
) -> dict[str, Any]:
    is_preference = preference_hint or any(marker in value for marker in _PREFERENCE_MARKERS)
    return {
        "memory_id": memory_id,
        "subject": subject,
        "predicate": "偏好" if is_preference else "事实",
        "value": value,
        "canonical_attribute": "preference.other" if is_preference else "fact.other",
        "canonical_slot": None,
        "qualifiers": qualifiers,
        "topic_tags": ["preference"] if is_preference else ["fact"],
        "importance": 0.8,
        "source_dataset": source_dataset,
        "source_memory_type": source_memory_type,
        "source_ref": source_ref,
    }


def _render_perltqa_memory(role: str, memory_type: str, source_ref: str, payload: Any) -> str:
    if memory_type == "profile":
        label = _PROFILE_LABELS.get(source_ref, source_ref)
        rendered = json.dumps(payload, ensure_ascii=False) if isinstance(payload, (dict, list)) else str(payload)
        return f"{role}的{label}：{rendered}"
    source = _require_mapping(payload, f"PerLTQA {role}.{memory_type}.{source_ref}")
    if memory_type == "events":
        return _required_text(source, "content", f"PerLTQA {role}.events.{source_ref}")
    if memory_type == "dialogues":
        contents = _require_mapping(source.get("contents"), f"PerLTQA {role}.dialogues.{source_ref}.contents")
        lines: list[str] = []
        for timestamp, utterances in contents.items():
            for utterance in _require_list(utterances, f"PerLTQA dialogue {source_ref}.{timestamp}"):
                if not isinstance(utterance, str) or not utterance.strip():
                    raise ValueError(f"PerLTQA dialogue {source_ref} contains an empty utterance")
                lines.append(f"{timestamp} {utterance.strip()}")
        if not lines:
            raise ValueError(f"PerLTQA dialogue {source_ref} is empty")
        return "\n".join(lines)
    if memory_type == "social_relationship":
        return "\n".join(f"{key}：{value}" for key, value in source.items())
    raise ValueError(f"unsupported PerLTQA memory type: {memory_type}")


def _selected_source_refs(
    source: dict[str, Any],
    targets: set[str],
    distractors_per_type: int,
) -> set[str]:
    missing = targets - set(source)
    if missing:
        raise ValueError(f"PerLTQA source refs do not exist: {sorted(missing)}")
    selected = set(targets)
    distractor_count = 0
    for source_ref in source:
        if source_ref in selected:
            continue
        if distractor_count >= distractors_per_type:
            break
        selected.add(source_ref)
        distractor_count += 1
    return selected


def build_perltqa_breadth(
    memory_path: Path,
    qa_path: Path,
    *,
    selections: Sequence[PerLTQASelection] | None = None,
    distractors_per_type: int = 8,
) -> DatasetBundle:
    """将 PerLTQA 分层抽样为四类等额、persona 隔离的广度评测。"""
    if selections is not None and not selections:
        raise ValueError("PerLTQA selections cannot be empty")
    if distractors_per_type < 0:
        raise ValueError("distractors_per_type cannot be negative")
    memory_rows = _require_list(_load_json(memory_path), "PerLTQA memory root")
    qa_rows = _require_list(_load_json(qa_path), "PerLTQA QA root")

    memories_by_role: dict[str, dict[str, Any]] = {}
    for index, raw_memory in enumerate(memory_rows):
        memory = _require_mapping(raw_memory, f"PerLTQA memory[{index}]")
        profile = _require_mapping(memory.get("profile"), f"PerLTQA memory[{index}].profile")
        role = _required_text(profile, "Protagonist", f"PerLTQA memory[{index}].profile")
        if role in memories_by_role:
            raise ValueError(f"duplicate PerLTQA role: {role}")
        memories_by_role[role] = memory

    qa_by_role: dict[str, dict[str, Any]] = {}
    for index, raw_bundle in enumerate(qa_rows):
        bundle = _require_mapping(raw_bundle, f"PerLTQA QA[{index}]")
        if len(bundle) != 1:
            raise ValueError(f"PerLTQA QA[{index}] must contain exactly one role")
        role, groups = next(iter(bundle.items()))
        qa_by_role[str(role)] = _require_mapping(groups, f"PerLTQA QA[{index}].{role}")

    candidates = _perltqa_candidates(qa_by_role)
    auto_select = selections is None
    selected_candidates: list[_PerLTQACandidate] = []
    if auto_select:
        renderable_candidates: list[_PerLTQACandidate] = []
        for candidate in candidates:
            memory = memories_by_role.get(candidate.role)
            source = memory.get(candidate.memory_type) if isinstance(memory, dict) else None
            if not isinstance(source, dict) or candidate.source_ref not in source:
                continue
            try:
                _render_perltqa_memory(
                    candidate.role,
                    candidate.memory_type,
                    candidate.source_ref,
                    source[candidate.source_ref],
                )
            except ValueError:
                continue
            renderable_candidates.append(candidate)
        candidates = renderable_candidates
        selected_candidates = _default_perltqa_candidates(candidates)
    else:
        for selection in selections or ():
            groups = qa_by_role.get(selection.role)
            if groups is None:
                raise ValueError(f"PerLTQA QA role does not exist: {selection.role}")
            group = groups.get(selection.memory_type)
            matches: list[tuple[dict[str, Any], tuple[str, ...]]] = []
            for item in _iter_qa_items(group, f"PerLTQA QA {selection.role}.{selection.memory_type}"):
                refs = _reference_ids(item.get("Reference Memory"), selection.memory_type)
                question = _required_text(item, "Question", f"PerLTQA QA {selection.role}.{selection.memory_type}")
                if selection.source_ref in refs and selection.question_contains in question:
                    matches.append((item, refs))
            if len(matches) != 1:
                raise ValueError(
                    f"PerLTQA selection must match exactly one QA: {selection.role}/{selection.memory_type}/"
                    f"{selection.source_ref}/{selection.question_contains!r}; got {len(matches)}"
                )
            item, refs = matches[0]
            if len(refs) != 1:
                raise ValueError("PerLTQA evaluation requires exactly one reference memory per QA")
            question = _required_text(item, "Question", f"PerLTQA QA {selection.role}.{selection.memory_type}")
            selected_candidates.append(
                _PerLTQACandidate(
                    role=selection.role,
                    memory_type=selection.memory_type,
                    source_ref=refs[0],
                    question=question,
                    answer=item.get("Answer"),
                    intent=route_recall_intent(question, None).value,
                    ordinal=len(selected_candidates),
                )
            )

    cases: list[dict[str, Any]] = []
    target_refs: dict[tuple[str, str], set[str]] = {}
    for selected in selected_candidates:
        target_refs.setdefault((selected.role, selected.memory_type), set()).add(selected.source_ref)
        cases.append(
            {
                "case_id": _memory_id("perltqa", selected.role, selected.memory_type, selected.source_ref),
                "namespace": _namespace("perltqa", selected.role),
                "query": selected.question,
                "expected_type": "claim",
                "expected_memory_ids": [
                    _memory_id("perltqa", selected.role, selected.memory_type, selected.source_ref)
                ],
                "expected_intent": selected.intent,
                "expected_intent_source": "keyword",
                "slice": f"perltqa_{selected.memory_type}",
                "source_dataset": "PerLTQA",
                "source_role": selected.role,
                "source_memory_type": selected.memory_type,
                "source_ref": [selected.source_ref],
                "source_answer": selected.answer,
            }
        )

    corpus: list[dict[str, Any]] = []
    included_refs: dict[tuple[str, str], set[str]] = {}
    selected_roles = list(dict.fromkeys(item.role for item in selected_candidates))
    for role in selected_roles:
        memory = memories_by_role.get(role)
        if memory is None:
            raise ValueError(f"PerLTQA memory role does not exist: {role}")
        for memory_type in PERLTQA_MEMORY_TYPES:
            source = _require_mapping(memory.get(memory_type), f"PerLTQA memory {role}.{memory_type}")
            renderable_source: dict[str, Any] = {}
            for source_ref, payload in source.items():
                try:
                    _render_perltqa_memory(role, memory_type, source_ref, payload)
                except ValueError:
                    continue
                renderable_source[source_ref] = payload
            selected_refs = _selected_source_refs(
                renderable_source,
                target_refs.get((role, memory_type), set()),
                distractors_per_type,
            )
            included_refs[(role, memory_type)] = selected_refs
            for source_ref, payload in renderable_source.items():
                if source_ref not in selected_refs:
                    continue
                row = _claim_row(
                    memory_id=_memory_id("perltqa", role, memory_type, source_ref),
                    subject=role,
                    value=_render_perltqa_memory(role, memory_type, source_ref, payload),
                    source_dataset="PerLTQA",
                    source_memory_type=memory_type,
                    source_ref=source_ref,
                    qualifiers={
                        "dataset": "PerLTQA",
                        "role": role,
                        "memory_type": memory_type,
                        "source_ref": source_ref,
                    },
                    preference_hint=memory_type == "profile" and source_ref == "Hobbies",
                )
                row["namespace"] = _namespace("perltqa", role)
                corpus.append(row)

    if auto_select:
        corpus_by_namespace: dict[str, str] = defaultdict(str)
        for row in corpus:
            corpus_by_namespace[str(row["namespace"])] += "\n" + str(row["value"])
        positive_keys = {(item.role, item.memory_type, item.source_ref) for item in selected_candidates}
        for memory_type in PERLTQA_MEMORY_TYPES:
            eligible = [
                item
                for item in candidates
                if item.memory_type == memory_type
                and (item.role, item.memory_type) in target_refs
                and (item.role, item.memory_type, item.source_ref) not in positive_keys
                and item.source_ref not in included_refs[(item.role, item.memory_type)]
                and _answer_absent(item.answer, corpus_by_namespace[_namespace("perltqa", item.role)])
            ]
            preferred = _pick_diverse_perltqa(
                [item for item in eligible if item.intent == "preference"],
                min(1, sum(item.intent == "preference" for item in eligible)),
            )
            negatives = _pick_diverse_perltqa(
                eligible,
                PERLTQA_NO_ANSWER_PER_TYPE,
                selected=preferred,
            )
            for selected in negatives:
                cases.append(
                    {
                        "case_id": _memory_id(
                            "perltqa",
                            selected.role,
                            selected.memory_type,
                            "no-answer",
                            selected.source_ref,
                        ),
                        "namespace": _namespace("perltqa", selected.role),
                        "query": selected.question,
                        "expected_type": "empty",
                        "expected_memory_ids": [],
                        "expected_intent": selected.intent,
                        "expected_intent_source": "keyword",
                        "slice": f"perltqa_{selected.memory_type}",
                        "source_dataset": "PerLTQA",
                        "source_role": selected.role,
                        "source_memory_type": selected.memory_type,
                        "source_ref": [selected.source_ref],
                        "source_answer": selected.answer,
                        "negative_kind": "withheld_source_memory",
                        "withheld_memory_ids": [
                            _memory_id("perltqa", selected.role, selected.memory_type, selected.source_ref)
                        ],
                    }
                )
    return DatasetBundle(tuple(corpus), tuple(cases))


def build_memdaily_depth(
    dataset_path: Path,
    *,
    selections: Sequence[MemDailySelection] | None = None,
) -> DatasetBundle:
    """按六题型等额抽样，并在场景 namespace 内评测完整消息流。"""
    if selections is not None and not selections:
        raise ValueError("MemDaily selections cannot be empty")
    root = _require_mapping(_load_json(dataset_path), "MemDaily root")
    auto_select = selections is None
    selected_roles: list[tuple[str, dict[str, Any]]] = []
    if auto_select:
        for question_type in MEMDAILY_QUESTION_TYPES:
            category = _require_mapping(root.get(question_type), f"MemDaily.{question_type}")
            roles = [
                _require_mapping(role, f"MemDaily.{question_type}.role")
                for role in _require_list(category.get("roles"), f"MemDaily.{question_type}.roles")
            ]
            ranked: list[tuple[str, dict[str, Any], str, bool]] = []
            for role in roles:
                qa = _require_mapping(role.get("QA"), f"MemDaily.{question_type}.{role.get('tid')}.QA")
                query = _required_text(qa, "question", f"MemDaily.{question_type}.{role.get('tid')}.QA")
                intent = route_recall_intent(query, None).value
                ranked.append(
                    (
                        _stable_rank(question_type, role.get("tid"), query),
                        role,
                        intent,
                        _is_natural_memdaily_preference(query),
                    )
                )
            ranked.sort(key=lambda item: item[0])
            preference_quota = MEMDAILY_PREFERENCE_QUOTAS.get(question_type, 0)
            preferred = [role for _, role, intent, natural in ranked if intent == "preference" and natural][
                :preference_quota
            ]
            if len(preferred) != preference_quota:
                raise ValueError(
                    f"MemDaily {question_type} has only {len(preferred)} natural preference cases; "
                    f"requires {preference_quota}"
                )
            chosen_tids = {role.get("tid") for role in preferred}
            fillers = [
                role
                for _, role, intent, _ in ranked
                if intent == "current_state" and role.get("tid") not in chosen_tids
            ][: MEMDAILY_POSITIVE_PER_TYPE - len(preferred)]
            chosen = [*preferred, *fillers]
            if len(chosen) != MEMDAILY_POSITIVE_PER_TYPE:
                raise ValueError(
                    f"MemDaily {question_type} has only {len(chosen)} eligible cases; "
                    f"requires {MEMDAILY_POSITIVE_PER_TYPE}"
                )
            selected_roles.extend((question_type, role) for role in chosen)
    else:
        for selection in selections or ():
            category = _require_mapping(root.get(selection.question_type), f"MemDaily.{selection.question_type}")
            roles = _require_list(category.get("roles"), f"MemDaily.{selection.question_type}.roles")
            matches = [
                _require_mapping(role, f"MemDaily.{selection.question_type}.role")
                for role in roles
                if isinstance(role, dict) and role.get("tid") == selection.tid
            ]
            if len(matches) != 1:
                raise ValueError(
                    f"MemDaily selection must match exactly one role: {selection.question_type}/{selection.tid}; "
                    f"got {len(matches)}"
                )
            selected_roles.append((selection.question_type, matches[0]))

    corpus: list[dict[str, Any]] = []
    cases: list[dict[str, Any]] = []
    target_messages_by_case: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for question_type, role in selected_roles:
        tid = role.get("tid")
        if not isinstance(tid, int) or isinstance(tid, bool):
            raise ValueError(f"MemDaily {question_type} role has an invalid tid")
        namespace = _namespace("memdaily", question_type, tid)
        messages = _require_list(role.get("message_list"), f"MemDaily.{question_type}.{tid}.message_list")
        mids: set[int] = set()
        messages_by_mid: dict[int, dict[str, Any]] = {}
        for raw_message in messages:
            message = _require_mapping(raw_message, f"MemDaily.{question_type}.{tid}.message")
            mid = message.get("mid")
            if not isinstance(mid, int) or isinstance(mid, bool) or mid in mids:
                raise ValueError(f"MemDaily {question_type}/{tid} has an invalid or duplicate mid")
            mids.add(mid)
            messages_by_mid[mid] = message
            text = _required_text(message, "message", f"MemDaily {question_type}/{tid}/{mid}")
            timestamp = _required_text(message, "time", f"MemDaily {question_type}/{tid}/{mid}")
            place = _required_text(message, "place", f"MemDaily {question_type}/{tid}/{mid}")
            row = _claim_row(
                memory_id=_memory_id("memdaily", question_type, tid, "message", mid),
                subject=f"MemDaily {question_type} 场景 {tid}",
                value=f"时间：{timestamp}\n地点：{place}\n消息：{text}",
                source_dataset="MemDaily",
                source_memory_type="message_list",
                source_ref=str(mid),
                qualifiers={
                    "dataset": "MemDaily",
                    "question_type": question_type,
                    "tid": tid,
                    "mid": mid,
                    "time": timestamp,
                    "place": place,
                },
            )
            row["namespace"] = namespace
            corpus.append(row)
        qa = _require_mapping(role.get("QA"), f"MemDaily.{question_type}.{tid}.QA")
        target_steps = qa.get("target_step_id")
        if (
            not isinstance(target_steps, list)
            or not target_steps
            or any(not isinstance(mid, int) or isinstance(mid, bool) for mid in target_steps)
            or not set(target_steps).issubset(mids)
        ):
            raise ValueError(f"MemDaily {question_type}/{tid} has invalid target_step_id")
        target_messages_by_case[(question_type, tid)] = [messages_by_mid[mid] for mid in target_steps]
        query = _required_text(qa, "question", f"MemDaily.{question_type}.{tid}.QA")
        cases.append(
            {
                "case_id": _memory_id("memdaily", question_type, tid),
                "namespace": namespace,
                "query": query,
                "expected_type": "claim",
                "expected_memory_ids": [
                    _memory_id("memdaily", question_type, tid, "message", mid) for mid in target_steps
                ],
                "expected_intent": route_recall_intent(query, None).value,
                "expected_intent_source": "keyword",
                "slice": f"memdaily_{question_type}",
                "source_dataset": "MemDaily",
                "source_question_type": question_type,
                "source_tid": tid,
                "source_qid": qa.get("qid"),
                "source_target_step_id": list(target_steps),
                "source_answer": qa.get("answer"),
            }
        )

    if auto_select:
        for question_type in MEMDAILY_QUESTION_TYPES:
            source_case = next(
                case
                for case in cases
                if case["source_question_type"] == question_type and case["expected_intent"] == "current_state"
            )
            tid = int(source_case["source_tid"])
            target_message = target_messages_by_case[(question_type, tid)][0]
            message_text = _required_text(target_message, "message", f"MemDaily.{question_type}.{tid}.negative")
            subject_hint = _memdaily_subject_hint(message_text)
            query = f"{subject_hint}的护照号码是多少？"
            namespace = _namespace("memdaily", question_type, tid)
            if any("护照" in str(row["value"]) for row in corpus if row.get("namespace") == namespace):
                raise ValueError(f"MemDaily {question_type}/{tid} cannot support the passport no-answer case")
            cases.append(
                {
                    "case_id": _memory_id("memdaily", question_type, tid, "no-answer", "passport"),
                    "namespace": namespace,
                    "query": query,
                    "expected_type": "empty",
                    "expected_memory_ids": [],
                    "expected_intent": "current_state",
                    "expected_intent_source": "keyword",
                    "slice": f"memdaily_{question_type}",
                    "source_dataset": "MemDaily",
                    "source_question_type": question_type,
                    "source_tid": tid,
                    "negative_kind": "missing_sibling_attribute",
                    "missing_attribute": "passport_number",
                }
            )
    return DatasetBundle(tuple(corpus), tuple(cases))


def _write_jsonl(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    rendered = "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows)
    path.write_text(rendered, encoding="utf-8", newline="\n")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_real_chinese_datasets(
    *,
    perltqa_memory_path: Path = DEFAULT_PERLTQA_MEMORY_PATH,
    perltqa_qa_path: Path = DEFAULT_PERLTQA_QA_PATH,
    memdaily_path: Path = DEFAULT_MEMDAILY_PATH,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
) -> dict[str, Path]:
    """生成两个真实隔离 suite 及可审计 manifest。"""
    breadth = build_perltqa_breadth(perltqa_memory_path, perltqa_qa_path)
    depth = build_memdaily_depth(memdaily_path)
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "perltqa_corpus": output_dir / PERLTQA_CORPUS_NAME,
        "perltqa_cases": output_dir / PERLTQA_CASES_NAME,
        "memdaily_corpus": output_dir / MEMDAILY_CORPUS_NAME,
        "memdaily_cases": output_dir / MEMDAILY_CASES_NAME,
        "manifest": output_dir / MANIFEST_NAME,
    }
    _write_jsonl(paths["perltqa_corpus"], breadth.corpus)
    _write_jsonl(paths["perltqa_cases"], breadth.cases)
    _write_jsonl(paths["memdaily_corpus"], depth.corpus)
    _write_jsonl(paths["memdaily_cases"], depth.cases)
    manifest = {
        "schema_version": 2,
        "sources": {
            "perltqa_memory": {"path": perltqa_memory_path.as_posix(), "sha256": _sha256(perltqa_memory_path)},
            "perltqa_qa": {"path": perltqa_qa_path.as_posix(), "sha256": _sha256(perltqa_qa_path)},
            "memdaily": {"path": memdaily_path.as_posix(), "sha256": _sha256(memdaily_path)},
        },
        "suites": {
            "breadth": {
                "dataset": "PerLTQA",
                "corpus_file": PERLTQA_CORPUS_NAME,
                "cases_file": PERLTQA_CASES_NAME,
                "corpus_count": len(breadth.corpus),
                "case_count": len(breadth.cases),
                "positive_case_count": sum(case["expected_type"] == "claim" for case in breadth.cases),
                "no_answer_case_count": sum(case["expected_type"] == "empty" for case in breadth.cases),
                "preference_case_count": sum(case["expected_intent"] == "preference" for case in breadth.cases),
                "positive_preference_case_count": sum(
                    case["expected_type"] == "claim" and case["expected_intent"] == "preference"
                    for case in breadth.cases
                ),
                "slice_counts": dict(sorted(Counter(case["slice"] for case in breadth.cases).items())),
                "namespace_count": len({case["namespace"] for case in breadth.cases}),
            },
            "depth": {
                "dataset": "MemDaily",
                "corpus_file": MEMDAILY_CORPUS_NAME,
                "cases_file": MEMDAILY_CASES_NAME,
                "corpus_count": len(depth.corpus),
                "case_count": len(depth.cases),
                "positive_case_count": sum(case["expected_type"] == "claim" for case in depth.cases),
                "no_answer_case_count": sum(case["expected_type"] == "empty" for case in depth.cases),
                "preference_case_count": sum(case["expected_intent"] == "preference" for case in depth.cases),
                "positive_preference_case_count": sum(
                    case["expected_type"] == "claim" and case["expected_intent"] == "preference" for case in depth.cases
                ),
                "slice_counts": dict(sorted(Counter(case["slice"] for case in depth.cases).items())),
                "namespace_count": len({case["namespace"] for case in depth.cases}),
            },
        },
    }
    paths["manifest"].write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return paths


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--perltqa-memory", type=Path, default=DEFAULT_PERLTQA_MEMORY_PATH)
    parser.add_argument("--perltqa-qa", type=Path, default=DEFAULT_PERLTQA_QA_PATH)
    parser.add_argument("--memdaily", type=Path, default=DEFAULT_MEMDAILY_PATH)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args(argv)
    paths = write_real_chinese_datasets(
        perltqa_memory_path=args.perltqa_memory,
        perltqa_qa_path=args.perltqa_qa,
        memdaily_path=args.memdaily,
        output_dir=args.output_dir,
    )
    print(json.dumps({key: str(path) for key, path in paths.items()}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
