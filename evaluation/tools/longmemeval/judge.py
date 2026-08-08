"""Official-compatible LongMemEval answer judging rules."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

LONGMEMEVAL_JUDGE_PROMPT_VERSION = "official-compatible-v1"


def longmemeval_judge_prompts(
    *,
    case_id: str,
    question_type: str,
    question: str,
    answer: str,
    predicted_answer: str,
) -> tuple[str, str]:
    """Build an official-compatible LongMemEval judge prompt for one case."""
    normalized_type = question_type.casefold()
    abstention = "_abs" in case_id.casefold() or normalized_type == "abstention"
    system_prompt = (
        "You are an official-style LongMemEval answer judge. Decide whether the model response contains the "
        "substantive correct answer under the supplied question-type rule. Allow paraphrases and extra information. "
        "Return only a JSON object in the form "
        '{"correct": true, "reason": "brief explanation"}. '
        "The correct field must be the JSON boolean true or false. Do not use Markdown."
    )
    if abstention:
        rule = (
            "This is an unanswerable question. Mark correct when the model correctly identifies it as unanswerable, "
            "including responses that say the available information is incomplete or does not contain the requested "
            "fact. The reference answer is an explanation of why the question cannot be answered, not a fact the model "
            "must repeat verbatim."
        )
        answer_label = "Unanswerability explanation"
    elif normalized_type in {"single-session-user", "single-session-assistant", "multi-session"}:
        rule = (
            "Mark correct if the response contains the substantive correct answer, is equivalent to it, or contains all "
            "the intermediate steps needed to obtain it. Extra information is allowed unless it clearly contradicts the "
            "correct answer. A concise response that states the core requested fact remains correct when it omits a "
            "nonessential qualifier; for example, '45 minutes' contains the core answer in '45 minutes each way'. "
            "Only mark incorrect when the response clearly contradicts the answer or contains merely a subset of multiple "
            "facts that the question necessarily requires."
        )
        answer_label = "Correct answer"
    elif normalized_type == "temporal-reasoning":
        rule = (
            "Apply the general contains-or-equivalent rule. Extra information is allowed unless contradictory. For a "
            "requested number of days, weeks, months, or another elapsed-time unit, do not penalize an off-by-one error. "
            "Only mark incorrect for a clear contradiction or omission of essential required information."
        )
        answer_label = "Correct answer"
    elif normalized_type == "knowledge-update":
        rule = (
            "Mark correct if the response contains the updated answer. The response may also mention previous information "
            "or the old value and remains correct as long as the required updated answer is present. Extra information is "
            "allowed unless it clearly contradicts which value is current."
        )
        answer_label = "Updated correct answer"
    elif normalized_type == "single-session-preference":
        rule = (
            "The reference is a rubric for a desired personalized response. The model does not need to cover every rubric "
            "point. Mark correct when it correctly recalls and uses the user's personal information in a response that "
            "satisfies the request; mark incorrect for misuse, contradiction, or no meaningful personalization."
        )
        answer_label = "Personalization rubric"
    else:
        raise ValueError(f"unsupported LongMemEval question_type: {question_type!r}")
    user_prompt = (
        f"Question type: {question_type}\n"
        f"Evaluation rule: {rule}\n\n"
        f"Question: {question}\n\n"
        f"{answer_label}: {answer}\n\n"
        f"Model response: {predicted_answer}\n\n"
        "Is the model response correct under the evaluation rule?"
    )
    return system_prompt, user_prompt


def judge_longmemeval_answer(
    *,
    api_key: str,
    base_url: str,
    model: str,
    case_id: str,
    question_type: str,
    question: str,
    answer: str,
    predicted_answer: str,
    call_with_retry: Callable[[Callable[[], tuple[str, int]]], tuple[str, int]],
    chat: Callable[..., tuple[str, int]],
    decode_response: Callable[[str | dict[str, Any]], dict[str, Any]],
) -> tuple[dict[str, Any], int]:
    """Judge one existing answer with injected transport and response validation."""
    system_prompt, user_prompt = longmemeval_judge_prompts(
        case_id=case_id,
        question_type=question_type,
        question=question,
        answer=answer,
        predicted_answer=predicted_answer,
    )
    judge_text, judge_tokens = call_with_retry(
        lambda: chat(
            api_key,
            base_url,
            model,
            system_prompt,
            user_prompt,
            temperature=0.0,
        )
    )
    judgment = decode_response(judge_text)
    if not isinstance(judgment.get("correct"), bool):
        raise ValueError("judge response is missing boolean 'correct'")
    return judgment, judge_tokens
