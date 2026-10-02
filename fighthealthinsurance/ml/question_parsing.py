"""Turning a model's reply into the intake questions.

The questions prompt asks for one question per line, each with an optional
answer after its question mark. Models do not always comply. Given a denial
of "Test", one replied with a sentence explaining it could not write
questions, then a fill-in template, "[Question]? [Answer if available]". The
old parser kept every line and added a "?" to any line without one, so the
person was shown the explanation and the template as their questions, and
the shared generic-question cache kept them for everyone with the same
procedure and diagnosis.

So a line is a question only when the model wrote a question mark, and a
question is dropped when it is a placeholder, an explanation or a refusal.
The prompt also gives the model a way to say there is nothing to ask:
NO_QUESTIONS, which parses to an empty list. A reply with nothing usable in
it parses to None, so "the model said none" and "the model said nothing we
could use" stay apart.

Pure functions with no Django imports: the parser tests call
RemoteFullOpenLike.get_appeal_questions on a MagicMock, so a method on the
class would be mocked out.
"""

import re
from typing import List, Optional, Tuple

NO_QUESTIONS = "NO_QUESTIONS"

# Real questions are short. The refusal that started this was about 280
# characters; the good examples in the system prompt are all under 100.
MAX_QUESTION_CHARS = 250

# A fill-in placeholder such as "[Question]" or "[Answer if available]": a
# bracket with letters in it. A citation marker such as "[1]" or "[2, 3]",
# which a search-backed model puts after a sentence, is not one; it is
# removed instead.
_PLACEHOLDER = re.compile(r"\[[^\]]*[A-Za-z][^\]]*\]")
_CITATION_MARK = re.compile(r"\s*\[\d+(?:[,\s\u2013-]*\d+)*\]")

# Phrases that mark a question as the model talking about the task rather
# than asking about the patient. Kept narrow: a phrase here drops a question
# outright, and real questions do start "Could you share whether..." or
# "Could you provide the date...", so only phrasings tied to a refusal.
_META_PHRASES = (
    "i cannot",
    "i can't",
    "i can not",
    "i'm sorry",
    "i am sorry",
    "i'm unable",
    "i am unable",
    "as an ai",
    "the input provided",
    "here is the format",
    "please provide more",
    "could you provide more",
    "share the denial letter",
    "provide the denial",
    "share the denial",
    "the actual denial",
    "once you share",
)

# A reply that refuses anywhere is not used at all: a question left after
# the refusal is the model asking for input ("Can you provide the denial
# letter?"), not asking about the patient. First person, so a question or
# answer about the patient does not trip it.
_REFUSAL_PHRASES = (
    "i cannot",
    "i can't",
    "i can not",
    "i'm sorry",
    "i am sorry",
    "i'm unable",
    "i am unable",
    "as an ai",
)

# An answer is only shown as a hint, so blanking a doubtful one costs
# little: the broader list also catches the model asking for more.
_META_ANSWER_PHRASES = _META_PHRASES + (
    "please provide",
    "could you provide",
    "could you share",
    "more details",
    "more information",
    "i can help",
)


def _phrase_pattern(phrases: Tuple[str, ...]) -> "re.Pattern[str]":
    """Match a phrase only as whole words: "as an ai" must not match inside
    "Has an airway obstruction been documented?"."""
    return re.compile(
        r"\b(?:" + "|".join(re.escape(p) for p in phrases) + r")\b", re.IGNORECASE
    )


_META = _phrase_pattern(_META_PHRASES)
_META_ANSWER = _phrase_pattern(_META_ANSWER_PHRASES)
_REFUSAL = _phrase_pattern(_REFUSAL_PHRASES)

_NUMBERING = re.compile(r"^\s*(?:\d+[.)\-]|\*|•|-)\s+")
_BOLD_LEAD = re.compile(r"\*\*([^*]+?)\*\*\s*(.*)")
_ANSWER_PREFIX = re.compile(r"^(?:A\s*:|:)[\s:]*")
# A label in front of the question, such as "Question:" or "Q2:".
_QUESTION_LABEL = re.compile(r"^(?:question|q)\s*\d*\s*:\s*", re.IGNORECASE)
_LETTER = re.compile(r"[A-Za-z]")
# A numbered item inside a one-line reply: "... 45 2. Has the patient".
_ITEM_NUMBER = re.compile(r"(?<=\s)(\d{1,2})[.)]\s+(?=[A-Z*])")


def _numbered_items(text: str) -> List[str]:
    """A one-line reply cut at its numbering, but only numbering that counts
    up from 1 and starts a sentence: "(at least 30) despite" is a number in
    a question, not item 30."""
    if not re.match(r"\s*1[.)]\s", text):
        return [text]
    items, start, expected = [], 0, 2
    for match in _ITEM_NUMBER.finditer(text):
        if int(match.group(1)) != expected:
            continue
        items.append(text[start : match.start()])
        start, expected = match.start(), expected + 1
    items.append(text[start:])
    return items


# Lines that introduce a list rather than belong to it.
_HEADER_STARTS = ("here are", "questions", "additional")


def is_junk_question(question: str) -> bool:
    """A question that is a placeholder, an explanation or a refusal, or too
    short or too long to be a real question. Does not require a "?": stored
    questions from before this check, and the payer-rule questions, are
    judged by it too."""
    q = question.strip()
    if len(_LETTER.findall(q)) < 3:
        return True
    # An introduction the old parser turned into a question by adding "?"
    # ("Here are some questions to help with the appeal:?"). Cached rows
    # from before this check can still hold one.
    if q.rstrip("?").rstrip().endswith(":") or q.lower().startswith("here are"):
        return True
    if len(q) > MAX_QUESTION_CHARS:
        return True
    if _PLACEHOLDER.search(q):
        return True
    return bool(_META.search(q))


def clean_suggested_answer(answer: str) -> str:
    """The model's suggested answer, or "" when it is a placeholder, a
    request for more information, or the UNKNOWN the prompt allows for
    patient details it does not have. Any of those would otherwise be shown
    beside the box as "One way to answer: ..."."""
    a = _CITATION_MARK.sub("", answer.replace("**", "")).strip()
    a = _ANSWER_PREFIX.sub("", a).strip()
    if not a or _PLACEHOLDER.search(a) or a.upper().strip(".") == "UNKNOWN":
        return ""
    # What is left of a placeholder or a word cut short: "[" or a lone "U".
    if a.startswith("[") or (len(a) == 1 and a.isalpha()):
        return ""
    if _META_ANSWER.search(a):
        return ""
    # A hint holding another question is the rest of a line with several
    # questions on it, not an answer; nor is one that starts with UNKNOWN.
    if "?" in a or a.split()[0].upper().strip(".,;:") == "UNKNOWN":
        return ""
    return a


def _said_no_questions(text: str) -> bool:
    """NO_QUESTIONS on a line of its own, allowing for markdown around it:
    a model may put a sentence or a stray closing </think> beside it."""
    return any(
        line.strip().strip("*`. ").upper() == NO_QUESTIONS for line in text.split("\n")
    )


def _refuses(text: str) -> bool:
    """Whether the model refused anywhere outside an answer: in a line with
    no question mark, or before a line's first one. A suggested answer may
    quote the patient ("I can't walk more than a block") and is not read."""
    for line in text.split("\n"):
        if _REFUSAL.search(line.split("?", 1)[0]):
            return True
    return False


def _split_line(line: str) -> Optional[Tuple[str, str]]:
    """The (question, answer) on one line, or None when the line holds no
    question. Never adds a question mark the model did not write."""
    line = _NUMBERING.sub("", line.strip())
    if not line or line.lower().startswith(_HEADER_STARTS):
        return None
    bold = _BOLD_LEAD.match(line)
    if bold:
        question, rest = bold.group(1).strip(), bold.group(2).strip()
        # "**What is the patient's age?** 45" or "**What is the patient's
        # age**? 45". A bold span that is not a question, such as
        # "**Question:** Has the patient...? Yes", is markup: the line goes
        # on to the split below without it.
        if question.endswith("?"):
            return question, rest
        if rest.startswith("?"):
            return question + "?", rest[1:]
    line = line.replace("**", "")
    if "?" not in line:
        return None
    question, answer = line.split("?", 1)
    return _QUESTION_LABEL.sub("", question.strip()) + "?", answer


def parse_appeal_questions(text: Optional[str]) -> Optional[List[Tuple[str, str]]]:
    """The (question, suggested answer) pairs in a model's reply.

    [] when the model said NO_QUESTIONS; None when the reply was missing,
    in the wrong shape, or had no usable question in it."""
    if text is None:
        return None
    if "Rationale for questions" in text:
        return None
    if _refuses(text):
        return None
    pairs: List[Tuple[str, str]] = []
    lines = text.split("\n")
    # A reply on one line can still number its questions; each numbered item
    # is read as a line of its own. Without numbering a line is one question,
    # however many "?" it holds: cutting it anywhere else guesses where one
    # question's answer ends and the next question starts.
    if len(lines) == 1:
        lines = _numbered_items(text)
    for line in lines:
        split = _split_line(line)
        if split is None:
            continue
        question, answer = split
        question = _CITATION_MARK.sub("", question[:-1]).strip() + "?"
        if is_junk_question(question):
            continue
        pairs.append((question, clean_suggested_answer(answer)))
    if not pairs and _said_no_questions(text):
        return []
    return pairs or None
