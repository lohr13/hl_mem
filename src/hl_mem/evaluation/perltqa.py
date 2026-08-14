"""PerLTQA 数据集 adapter。

PerLTQA 是中文长期记忆 QA benchmark，用法特殊：
- **不走 extraction**：把 perltmem.json 的标注记忆直接灌入 DB 测 recall
- 每个 character 的记忆按 4 类（profile/social/events/dialogues）拆成 claim
- QA question → recall query，Reference Memory → gold claim 的 source key
- 不需要 LLM answer，只测 recall@K 和 MRR

数据结构：
  perltmem.json: List[141] of {profile, profile_description, social_relationship,
                               events, dialogues}
  perltqa.json:  List[32]  of {character_name: {profile: [...],
                               social_relationship: {key: [...]},
                               events: {key: [...]},
                               dialogues: {key: [...]}}}
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# ─── Data structures ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PerLTQAClaimSpec:
    """One memory item to be written to the claims table.

    Attributes:
        source_key: the key in perltmem.json that this claim comes from
                    (e.g. "Gender", "1_0_0", "1_0", "1_0_0#0")
        category: one of "profile", "social_relationship", "events", "dialogues"
        text: the memory text to use as claim index_text
    """

    source_key: str
    category: str
    text: str


@dataclass(frozen=True)
class PerLTQAQuestion:
    """One QA question for recall evaluation.

    Attributes:
        question: the QA question text
        reference_keys: the source keys of the gold claims
                        (e.g. ["Gender"] or ["4_0_0"])
        category: one of "profile", "social_relationship", "events", "dialogues"
        answer: the gold answer (for reference, not used in recall scoring)
    """

    question: str
    reference_keys: tuple[str, ...]
    category: str
    answer: str


@dataclass(frozen=True)
class PerLTQACharacter:
    """All data for one character: memory claims + QA questions.

    Attributes:
        name: character name (protagonist)
        claims: memory items to write to DB
        questions: QA questions to test recall
    """

    name: str
    claims: tuple[PerLTQAClaimSpec, ...]
    questions: tuple[PerLTQAQuestion, ...]


# ─── Adapter ─────────────────────────────────────────────────────────────────


CATEGORIES = ("profile", "social_relationship", "events", "dialogues")


class PerLTQAAdapter:
    """Load PerLTQA memory + QA data and produce PerLTQACharacter objects."""

    VERSION = "2"

    def load(
        self,
        mem_source: Path,
        qa_source: Path,
        *,
        per_character: int | None = None,
        qa_per_category: int | None = None,
    ) -> list[PerLTQACharacter]:
        """Load and convert PerLTQA data.

        Args:
            mem_source: path to perltmem.json
            qa_source: path to perltqa.json
            per_character: max number of characters (None = all 32)
            qa_per_category: max QA items per category per character (None = all)
        """
        mem_list: list[dict[str, Any]] = json.loads(mem_source.read_text(encoding="utf-8"))
        qa_list: list[dict[str, Any]] = json.loads(qa_source.read_text(encoding="utf-8"))

        # Build lookup: character_name -> memory dict
        mem_by_name: dict[str, dict[str, Any]] = {}
        for char_mem in mem_list:
            if not isinstance(char_mem, Mapping):
                continue
            profile = char_mem.get("profile") or {}
            if isinstance(profile, Mapping):
                name = str(profile.get("Protagonist") or "").strip()
                if name:
                    mem_by_name[name] = dict(char_mem)

        # Build lookup: character_name -> QA dict
        qa_by_name: dict[str, dict[str, Any]] = {}
        for item in qa_list:
            if not isinstance(item, Mapping):
                continue
            for name, categories in item.items():
                if isinstance(categories, Mapping):
                    qa_by_name[str(name)] = dict(categories)

        characters: list[PerLTQACharacter] = []
        count = 0
        for name, qa_categories in qa_by_name.items():
            if per_character is not None and count >= per_character:
                break
            character_memory = mem_by_name.get(name)
            if character_memory is None:
                # QA character not found in memory — skip
                continue
            claims = self._extract_claims(name, character_memory)
            questions = self._extract_questions(name, qa_categories, limit_per_category=qa_per_category)
            if not questions:
                continue
            characters.append(
                PerLTQACharacter(
                    name=name,
                    claims=tuple(claims),
                    questions=tuple(questions),
                )
            )
            count += 1

        return characters

    # ─── Claim extraction ──────────────────────────────────────────────────

    def _extract_claims(self, char_name: str, char_mem: Mapping[str, Any]) -> list[PerLTQAClaimSpec]:
        """Extract memory claims from all 4 categories for a character."""
        claims: list[PerLTQAClaimSpec] = []

        # 1. Profile: each field value → claim
        profile = char_mem.get("profile") or {}
        if isinstance(profile, Mapping):
            for key, value in profile.items():
                text = str(value).strip()
                if text:
                    claims.append(
                        PerLTQAClaimSpec(
                            source_key=str(key),
                            category="profile",
                            text=text,
                        )
                    )

        # 2. Social relationship: keep the named character and relation with the description
        social = char_mem.get("social_relationship") or {}
        if isinstance(social, Mapping):
            for sr_key, sr_data in social.items():
                if not isinstance(sr_data, Mapping):
                    continue
                supporting_character = str(sr_data.get("Supporting Characters") or "").strip()
                desc = str(sr_data.get("Description") or "").strip()
                relationship = str(sr_data.get("Relationship") or "").strip()
                lines: list[str] = []
                if desc:
                    if supporting_character and supporting_character not in desc:
                        description = f"{supporting_character}是{desc}"
                    else:
                        description = desc
                    lines.append(description if description.endswith(("。", "！", "？")) else f"{description}。")
                elif supporting_character:
                    lines.append(f"具体人物：{supporting_character}")
                if supporting_character and relationship:
                    lines.append(f"{supporting_character}与{char_name}的关系是{relationship}。")
                text = "\n".join(lines)
                if text:
                    claims.append(
                        PerLTQAClaimSpec(
                            source_key=str(sr_key),
                            category="social_relationship",
                            text=text,
                        )
                    )

        # 3. Events: content → claim
        events = char_mem.get("events") or {}
        if isinstance(events, Mapping):
            for ev_key, ev_data in events.items():
                if not isinstance(ev_data, Mapping):
                    continue
                content = str(ev_data.get("content") or "").strip()
                if content:
                    claims.append(
                        PerLTQAClaimSpec(
                            source_key=str(ev_key),
                            category="events",
                            text=content,
                        )
                    )

        # 4. Dialogues: contents → claim (join all turns)
        dialogues = char_mem.get("dialogues") or {}
        if isinstance(dialogues, Mapping):
            for dlg_key, dlg_data in dialogues.items():
                if not isinstance(dlg_data, Mapping):
                    continue
                contents = dlg_data.get("contents") or {}
                if not isinstance(contents, Mapping):
                    continue
                # Join all timestamp → turn lines into one text
                dialogue_lines: list[str] = []
                for ts in sorted(contents.keys()):
                    turns = contents[ts]
                    if isinstance(turns, Sequence) and not isinstance(turns, (str, bytes)):
                        dialogue_lines.extend(str(t) for t in turns)
                    elif isinstance(turns, str):
                        dialogue_lines.append(turns)
                text = "\n".join(dialogue_lines).strip()
                if text:
                    claims.append(
                        PerLTQAClaimSpec(
                            source_key=str(dlg_key),
                            category="dialogues",
                            text=text,
                        )
                    )

        return claims

    # ─── Question extraction ───────────────────────────────────────────────

    def _extract_questions(
        self,
        char_name: str,
        qa_categories: Mapping[str, Any],
        *,
        limit_per_category: int | None = None,
    ) -> list[PerLTQAQuestion]:
        """Extract QA questions from all 4 categories."""
        questions: list[PerLTQAQuestion] = []

        for category in CATEGORIES:
            cat_data = qa_categories.get(category)
            if cat_data is None:
                continue
            count = 0

            if category == "profile":
                # profile is a flat list of QA dicts
                if not isinstance(cat_data, Sequence) or isinstance(cat_data, (str, bytes)):
                    continue
                for qa_item in cat_data:
                    if not isinstance(qa_item, Mapping):
                        continue
                    if limit_per_category is not None and count >= limit_per_category:
                        break
                    q = str(qa_item.get("Question") or "").strip()
                    if not q:
                        continue
                    ref_key = str(qa_item.get("Reference Memory") or "").strip()
                    answer = str(qa_item.get("Answer") or "").strip()
                    questions.append(
                        PerLTQAQuestion(
                            question=q,
                            reference_keys=(ref_key,) if ref_key else (),
                            category=category,
                            answer=answer,
                        )
                    )
                    count += 1
            else:
                # social_relationship/events/dialogues are dicts: {key: [qa_items]}
                if not isinstance(cat_data, Mapping):
                    continue
                for mem_key, qa_list_raw in cat_data.items():
                    if not isinstance(qa_list_raw, Sequence) or isinstance(qa_list_raw, (str, bytes)):
                        continue
                    for qa_item in qa_list_raw:
                        if not isinstance(qa_item, Mapping):
                            continue
                        if limit_per_category is not None and count >= limit_per_category:
                            break
                        q = str(qa_item.get("Question") or "").strip()
                        if not q:
                            continue
                        ref_raw = qa_item.get("Reference Memory") or ""
                        ref_keys = self._parse_reference_keys(ref_raw)
                        answer = str(qa_item.get("Answer") or "").strip()
                        questions.append(
                            PerLTQAQuestion(
                                question=q,
                                reference_keys=tuple(ref_keys),
                                category=category,
                                answer=answer,
                            )
                        )
                        count += 1
                    if limit_per_category is not None and count >= limit_per_category:
                        break

        return questions

    @staticmethod
    def _parse_reference_keys(ref_raw: Any) -> list[str]:
        """Parse Reference Memory field into a list of source keys.

        Profile: "Gender" → ["Gender"]
        Others: "['4_0_0']" → ["4_0_0"]
        """
        if isinstance(ref_raw, (list, tuple)):
            return [str(k).strip() for k in ref_raw if str(k).strip()]
        text = str(ref_raw).strip()
        if not text:
            return []
        # Try parsing as Python list literal: "['4_0_0']"
        if text.startswith("[") and text.endswith("]"):
            try:
                import ast

                parsed = ast.literal_eval(text)
                if isinstance(parsed, (list, tuple)):
                    return [str(k).strip() for k in parsed if str(k).strip()]
            except (ValueError, SyntaxError):
                pass
        return [text]
