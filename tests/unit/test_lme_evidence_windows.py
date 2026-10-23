"""Assert on final serialized evidence, including sentence tails and relations."""

import json
import sqlite3
from dataclasses import replace

from evaluation.tools import run_longmemeval_benchmark as runner
from evaluation.tools.longmemeval import reader_context as context
from tests.unit.test_longmemeval_batching import _record


def _events(prompt):
    return json.loads(prompt.split("Original Evidence Events:\n", 1)[1].split("\n\nQuestion:", 1)[0])


def test_final_prompt_preserves_completed_event_relation_among_topic_distractors():
    case = replace(runner.normalize_case(_record("generic")), question="How many conferences did I attend this year?")
    messages = [
        {"role": "user", "content": "I just returned from Morgan's robotics conference in April."},
        {"role": "assistant", "content": "Welcome back."},
        {"role": "user", "content": "I planned to attend conferences this year."},
        {"role": "assistant", "content": "Here is a conference planning guide."},
        {"role": "user", "content": "I wanted to attend conferences this year."},
        {"role": "assistant", "content": "Morgan wrote a poem about robotics."},
        {"role": "user", "content": "I intended to attend conferences this year."},
    ]
    with sqlite3.connect(":memory:") as connection:
        connection.row_factory = sqlite3.Row
        connection.execute(
            "CREATE TABLE events (id, content_json, occurred_at, recorded_at, event_type, actor_type, source_uri)"
        )
        connection.execute(
            "INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "source",
                json.dumps({"messages": messages}),
                "2023-05-20T00:00:00Z",
                None,
                "message",
                "user",
                None,
            ),
        )
        retrieved = [{"text": messages[i]["content"], "evidence_event_ids": ["source"]} for i in (2, 4, 6)]
        prompt = context.build_reader_user_prompt(connection, case, retrieved)
    evidence = _events(prompt)[0]
    assert "I just returned from Morgan's robotics conference in April." in evidence["content"]
    assert 0 in evidence["window"]["included_turns"]
    assert context.estimate_tokens(prompt) <= 6000


def test_final_fitting_keeps_whole_money_sentence_instead_of_prefix():
    case = replace(runner.normalize_case(_record("generic")), question="How much did I earn selling plants?")
    sentence = "On June 2 I sold twelve plants at the market, earning a total of $135.50."
    event = {
        "event_id": "source",
        "actor_type": "user",
        "content": "Some unrelated opening. " * 130 + sentence + " More unrelated context." * 60,
    }
    fitted = context.fit_reader_event(case, [], [], event)
    assert fitted is not None
    prompt = context.render_reader_user_prompt(case, [], [fitted])
    assert sentence in _events(prompt)[0]["content"]
    assert context.estimate_tokens(json.dumps(fitted, ensure_ascii=False, separators=(",", ":"))) <= 1200


def test_sentence_with_decimal_amount_is_not_cut_at_decimal_point():
    sentence = "I sold the remaining plants for $135.50 on June 2."
    excerpt = context.reader_turn_excerpt(
        "Earlier unrelated sentence. " * 50 + sentence + " Later unrelated sentence." * 50,
        80,
        [("sold the remaining plants", 3.0)],
    )
    assert sentence in excerpt


def test_second_fit_keeps_money_fact_before_speculation_matching_question():
    case = replace(runner.normalize_case(_record("generic")), question="How much did I earn from sales?")
    fact = "On June 2 I sold twelve plants at the market, earning a total of $135.50."
    event = {
        "event_id": "source",
        "actor_type": "user",
        "content": "[matched turn 0 user]\n"
        + fact
        + " I am still wondering how much I could earn from sales at the market this year because the market is unpredictable.",
        "window": {"mode": "windowed", "matched_turn": 0, "matched_turns": [0], "included_turns": [0]},
    }
    fitted = context.fit_reader_event(case, [], [], event, token_limit=190)
    assert fact in fitted["content"]


def test_final_fitting_retains_claim_focus_without_serializing_internal_needles():
    case = replace(runner.normalize_case(_record("generic")), question="What was the outcome?")
    fact = "The market sale brought in $135.50 on June 2."
    event = {
        "event_id": "source",
        "content": "[matched turn 0 user]\n" + fact + " What was the outcome? " * 30,
        "_reader_needles": [(fact, 3.0)],
    }
    fitted = context.fit_reader_event(case, [], [], event, token_limit=130)
    assert fact in fitted["content"]
    assert "_reader_needles" not in fitted


def test_fact_sentence_gets_space_before_unrelated_matched_windows():
    case = replace(
        runner.normalize_case(_record("generic")),
        question="What is the total money I earned selling products at markets?",
    )
    fact = "By the way, I even sold twelve bunches of fresh organic herbs from my backyard garden at the farmers' market on June 2, earning a total of $135.50."
    event = {
        "event_id": "source",
        "actor_type": "user",
        "content": "[matched turn 0 user]\nI am considering new products for an upcoming market. "
        + fact
        + "\n\n[matched turn 8 user]\nI am considering skincare products for the next market."
        + "\n\n[matched turn 11 assistant]\nHere are courses about product development.",
        "_reader_needles": [("considering new products for an upcoming market", 3.0)],
    }
    fitted = context.fit_reader_event(case, [], [], event, token_limit=150)
    assert fact in fitted["content"]


def test_cropped_window_metadata_describes_only_rendered_sources():
    case = runner.normalize_case(_record("generic"))
    event = {
        "event_id": "source",
        "content": "[matched turn 0 user]\n"
        + "Long context " * 200
        + "\n[matched turn 2 user]\nA short fact.\n[matched turn 4 user]\nAnother short fact.",
        "window": {
            "mode": "windowed",
            "matched_turn": 0,
            "matched_turns": [0, 2, 4],
            "included_turns": [0, 2, 4],
            "included_event_ids": ["a", "b", "c"],
            "match_score": 99,
            "match_scores": [{"turn": 0, "score": 99}, {"turn": 2, "score": 7}, {"turn": 4, "score": 3}],
        },
    }
    for budget in range(130, 301, 10):
        fitted = context.fit_reader_event(case, [], [], event, token_limit=budget)
        if fitted is None:
            continue
        window = fitted["window"]
        for turn in window["included_turns"]:
            assert f"turn {turn} user]" in fitted["content"]
        score = next((item["score"] for item in window["match_scores"] if item["turn"] == window["matched_turn"]), None)
        assert window["match_score"] == score
        assert window["included_event_ids"] == [
            dict(zip([0, 2, 4], ["a", "b", "c"]))[turn] for turn in window["included_turns"]
        ]


def test_matched_fact_sentences_share_budget_before_adjacent_chatter():
    messages = [
        {"role": "user", "content": "Unrelated preface. " * 55 + "I sold plants for $135.50 on June 2."},
        {"role": "assistant", "content": "Here is some general advice. " * 80},
        {"role": "user", "content": "I sold seeds for $48 on June 3."},
    ]
    content, window = context.reader_turn_window(
        messages,
        "How much did I earn from sales?",
        [
            ("sold plants", 2.0),
            ("sold seeds", 2.0),
        ],
    )
    assert "I sold plants for $135.50 on June 2." in content
    assert "I sold seeds for $48 on June 3." in content
    assert set(window["included_turns"]) >= {0, 2}


def test_final_prompt_reserves_evidence_for_multiple_sales_sessions():
    case = replace(runner.normalize_case(_record("generic")), question="How much did I earn from sales?")
    with sqlite3.connect(":memory:") as connection:
        connection.row_factory = sqlite3.Row
        connection.execute(
            "CREATE TABLE events (id, content_json, occurred_at, recorded_at, event_type, actor_type, source_uri)"
        )
        retrieved = []
        sentences = []
        for index in range(6):
            sentence = f"I sold batch {index} for ${110 + index}.50 on June {index + 1}."
            sentences.append(sentence)
            content = "An unrelated opening. " * 80 + sentence + " Unrelated ending." * 80
            connection.execute(
                "INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    str(index),
                    json.dumps({"text": content}),
                    "2023-05-20T00:00:00Z",
                    None,
                    "message",
                    "user",
                    None,
                ),
            )
            retrieved.append({"text": f"Batch {index} was sold.", "evidence_event_ids": [str(index)]})
        prompt = context.build_reader_user_prompt(connection, case, retrieved)
    evidence = _events(prompt)
    assert len(evidence) == 6
    assert all(sentence in evidence[index]["content"] for index, sentence in enumerate(sentences))
    assert context.estimate_tokens(prompt) <= 6000
