from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.eval.chinese_recall import evaluate_cases, load_cases
from tests.eval.real_chinese_data import (
    MemDailySelection,
    PerLTQASelection,
    _memdaily_subject_hint,
    build_memdaily_depth,
    build_perltqa_breadth,
)


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("我的同事在江苏南京上班呢。", "我的同事"),
        ("我的一个下属过生日是11月13号。", "我的一个下属"),
        ("周立业是我的表哥，其年龄是38岁。", "周立业"),
    ],
)
def test_memdaily_no_answer_keeps_the_target_person_in_the_query(message: str, expected: str) -> None:
    assert _memdaily_subject_hint(message) == expected


def test_perltqa_adapter_preserves_four_memory_types_and_original_questions(tmp_path: Path) -> None:
    memory_path = tmp_path / "perltmem.json"
    qa_path = tmp_path / "perltqa.json"
    _write_json(
        memory_path,
        [
            {
                "profile": {"Protagonist": "林青", "Occupation": "建筑师"},
                "social_relationship": {
                    "7_0": {
                        "Supporting Characters": "周宁",
                        "Description": "周宁是林青的朋友，喜欢徒步。",
                        "Relationship": "朋友",
                    }
                },
                "events": {"7_0_0": {"content": "林青和周宁在黄山看到了云海。"}},
                "dialogues": {
                    "7_0_0#0": {
                        "events": "7_0_0",
                        "contents": {"2024-01-02 08:00": ["林青: 我最喜欢黄山的云海。"]},
                    }
                },
            }
        ],
    )
    _write_json(
        qa_path,
        [
            {
                "林青": {
                    "profile": [
                        {
                            "Question": "林青的职业是什么？",
                            "Answer": "建筑师",
                            "Reference Memory": "Occupation",
                            "Memory Anchors": [{"建筑师": [-1, -1]}],
                        }
                    ],
                    "social_relationship": {
                        "7_0": [
                            {
                                "Question": "周宁喜欢做什么？",
                                "Answer": "徒步",
                                "Reference Memory": "['7_0']",
                                "Memory Anchors": [{"徒步": [-1, -1]}],
                            }
                        ]
                    },
                    "events": {
                        "7_0_0": [
                            {
                                "Question": "林青和周宁看到了什么？",
                                "Answer": "云海",
                                "Reference Memory": "['7_0_0']",
                                "Memory Anchors": [{"云海": [-1, -1]}],
                            }
                        ]
                    },
                    "dialogues": {
                        "7_0_0#0": [
                            {
                                "Question": "林青最喜欢黄山的什么？",
                                "Answer": "云海",
                                "Reference Memory": "['7_0_0#0']",
                                "Memory Anchors": [{"云海": [-1, -1]}],
                            }
                        ]
                    },
                }
            }
        ],
    )
    selections = (
        PerLTQASelection("林青", "profile", "Occupation", "职业"),
        PerLTQASelection("林青", "social_relationship", "7_0", "周宁喜欢"),
        PerLTQASelection("林青", "events", "7_0_0", "看到了什么"),
        PerLTQASelection("林青", "dialogues", "7_0_0#0", "最喜欢"),
    )

    bundle = build_perltqa_breadth(memory_path, qa_path, selections=selections, distractors_per_type=1)

    assert {row["source_memory_type"] for row in bundle.corpus} == {
        "profile",
        "social_relationship",
        "events",
        "dialogues",
    }
    assert [case["query"] for case in bundle.cases] == [
        "林青的职业是什么？",
        "周宁喜欢做什么？",
        "林青和周宁看到了什么？",
        "林青最喜欢黄山的什么？",
    ]
    assert "2024-01-02 08:00 林青: 我最喜欢黄山的云海。" in next(
        row["value"] for row in bundle.corpus if row["source_memory_type"] == "dialogues"
    )
    assert sum(case["expected_intent"] == "preference" for case in bundle.cases) == 2


def test_memdaily_adapter_ingests_complete_stream_and_binds_all_target_steps(tmp_path: Path) -> None:
    source_path = tmp_path / "memdaily.json"
    _write_json(
        source_path,
        {
            "conditional": {
                "roles": [
                    {
                        "tid": 12,
                        "message_list": [
                            {"mid": 0, "message": "我妈妈喜欢登山。", "time": "2024年04月01日", "place": "青岛"},
                            {
                                "mid": 4,
                                "message": "我妈妈在深圳天使护理院工作。",
                                "time": "2024年04月03日",
                                "place": "青岛",
                            },
                            {"mid": 7, "message": "我同事喜欢跑步。", "time": "2024年04月04日", "place": "青岛"},
                        ],
                        "QA": {
                            "qid": 0,
                            "question": "深圳天使护理院的那个人平时喜欢干什么？",
                            "answer": "登山",
                            "target_step_id": [4, 0],
                        },
                    }
                ]
            }
        },
    )

    bundle = build_memdaily_depth(source_path, selections=(MemDailySelection("conditional", 12),))

    assert len(bundle.corpus) == 3
    assert bundle.cases[0]["expected_memory_ids"] == [
        "memdaily:conditional:12:message:4",
        "memdaily:conditional:12:message:0",
    ]
    assert bundle.cases[0]["expected_intent"] == "preference"
    assert bundle.cases[0]["slice"] == "memdaily_conditional"
    assert bundle.corpus[0]["value"] == "时间：2024年04月01日\n地点：青岛\n消息：我妈妈喜欢登山。"


def test_memdaily_adapter_rejects_missing_gold_message(tmp_path: Path) -> None:
    source_path = tmp_path / "memdaily.json"
    _write_json(
        source_path,
        {
            "simple": {
                "roles": [
                    {
                        "tid": 3,
                        "message_list": [{"mid": 0, "message": "唯一消息", "time": "现在", "place": "上海"}],
                        "QA": {"qid": 0, "question": "问题是什么？", "answer": "答案", "target_step_id": [9]},
                    }
                ]
            }
        },
    )

    with pytest.raises(ValueError, match="target_step_id"):
        build_memdaily_depth(source_path, selections=(MemDailySelection("simple", 3),))


def test_evaluation_report_measures_partial_multi_evidence_recall(tmp_path: Path) -> None:
    cases_path = tmp_path / "cases.jsonl"
    cases_path.write_text(
        json.dumps(
            {
                "case_id": "multi-evidence",
                "query": "两步证据是什么？",
                "expected_type": "claim",
                "expected_memory_ids": ["first", "second"],
                "expected_intent": "current_state",
                "expected_intent_source": "keyword",
                "slice": "multi_evidence",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    cases = load_cases(cases_path, {"first", "second"})

    class PartialEvidenceService:
        def recall(self, *_args: object, **_kwargs: object) -> dict[str, object]:
            return {
                "results": [{"id": "first", "text": "只返回了第一步证据", "score": 0.81}],
                "answerability": "supported",
                "search_trace": {
                    "intent": "current_state",
                    "intent_source": "keyword",
                    "candidates": {
                        "first": {
                            "channels": {"fts": 1, "dense": 1},
                            "channel_scores": {"dense": 0.72},
                            "rerank_score": 0.91,
                            "relevance_decision": "relevant",
                            "relevance_reason": "reranker_floor_met",
                        }
                    },
                },
            }

    report = evaluate_cases(PartialEvidenceService(), cases, limit=5)

    assert report.items[0].matched_expected_ids == ("first",)
    assert report.items[0].expected_count == 2
    assert report.mean_gold_recall == 0.5
    assert report.complete_evidence_accuracy == 0.0
    assert report.items[0].top_score == 0.81
    assert report.items[0].runner_up_score is None
    assert report.items[0].top_reranker_score == 0.91
    assert report.items[0].top_dense_score == 0.72
    assert report.items[0].top_relevance_decision == "relevant"
    assert report.items[0].top_relevance_reason == "reranker_floor_met"
