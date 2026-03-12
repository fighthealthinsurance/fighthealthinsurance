from typing import Callable, Optional


# Canonical guardrail phrases that indicate unusable/refusal-style LLM output.
BAD_OUTPUT_PHRASES: tuple[str, ...] = (
    "Therefore, the Health Plans denial should be overturned.",
    "llama llama virus",
    "The independent medical review found that",
    "The independent review findings for",
    "the physician reviewer overturned",
    "91111111111111111111111",
    "I need the text to be able to help you with your appeal",
    "I cannot directly create",
    "As an AI, I do not have the capability",
    "Unfortunately, I cannot directly",
    "I am an AI assistant and do not have the authority to create medical documents",
)


def is_bad_output(
    result: Optional[str],
    *,
    min_length: int = 3,
    check_guardrail_phrases: bool = False,
    check_severe_repetition: bool = False,
    repetition_checker: Optional[Callable[[str], bool]] = None,
) -> bool:
    """Return True when LLM output should be treated as unusable.

    This centralizes bad-output heuristics so all callers share one canonical
    phrase set and evaluator logic.
    """
    if result is None:
        return True

    if len(result.strip(" ")) < min_length:
        return True

    result_lower = result.lower()
    if check_guardrail_phrases:
        for phrase in BAD_OUTPUT_PHRASES:
            if phrase.lower() in result_lower:
                return True

    if (
        check_severe_repetition
        and repetition_checker is not None
        and repetition_checker(result)
    ):
        return True

    return False
