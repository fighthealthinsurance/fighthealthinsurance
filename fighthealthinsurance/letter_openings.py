"""How a letter opens: no assistant preamble, and a salutation to the plan.

Two things a model puts at the top of a draft that do not belong to the
letter.

**The preamble.** Asked for a letter, a chat-tuned model often answers like
a chat: "Sure, I can help you draft a template for an appeal letter. Here's
a simple one:" and then the letter. ``strip_letter_preamble`` removes that
opening (and a markdown fence around the letter, when there is one). It runs
on the appeal-generation path only (``RemoteOpenLike._checked_infer_outcome``
in ml_models, for ``infer_type == "full"``), never on the chat path, where
"Sure, I can help" is the assistant talking to the person and belongs there.

**The salutation.** Every appeal letter and prior authorization request this
app writes goes to the insurance company: a first-level (internal) appeal is
read by the plan's appeals department, and no review board sees it. Models
still like to open with "Dear Esteemed Members of the Review Board," -- the
fine-tuned models picked the habit up from training letters derived from
external-review (IMR) case files, and the hosted models produce it on their
own -- so such a letter addresses a body it will never reach, and reads as
if the writer does not know where they are in the process.
``readdress_review_board_salutation`` rewrites that salutation to one
addressed to the plan. ``substitute_appeal_fields`` (common_view_logic) runs
it on every appeal letter and ``PriorAuthTextSubstituter`` on every prior
authorization proposal. It is deliberately NOT run on letters that really
are addressed to a reviewing body: the regulator cover letters
(generate_regulator_letter) pass through neither substitution, and an
external review request, should the app ever write one, must skip it too.

Both are fixes, not rejections: a letter that opens badly is usually a good
letter otherwise. ``APPEAL_SALUTATION_INSTRUCTION`` is the matching prompt
line, so the models are told the recipient up front and the rewrite is a
backstop.

The training pipeline (healthinsurance-llm-internal: dataset_tools/
bad_strings.py and spark_utils.py) carries the same patterns to keep such
letters out of the training data; keep the two in step.
"""

import re

# The prompt sentence. The plan's name is substituted downstream, so the
# example salutation names the department rather than a placeholder a model
# might copy literally.
APPEAL_SALUTATION_INSTRUCTION = (
    "Address the letter to the insurance company's appeals department (for "
    'example "Dear Appeals Department,"). This is an internal appeal to the '
    "plan, not an external or independent review, so do not address it to a "
    '"Review Board", to "Esteemed Members" of anything, or to any panel of '
    "reviewers."
)

# What a salutation line may start with: indentation, and markdown emphasis
# or a heading marker. Not ">": a blockquoted line is a quotation, and its
# "Dear ..." is someone else's salutation.
_LINE_LEAD = r"[ \t*_#]*"

# The letter's own salutation, when it is to a review board, panel or
# committee. The letter's salutation is the first line that opens one ("Dear
# ...", "To Whom ..."), so a salutation quoted later in the body ("Dear Review
# Board," from the plan's earlier correspondence) is never matched; the lines
# before it (sender, date, address, a "Re:" line) are skipped. The match must
# open like a salutation -- "Dear ...", "To the ...", or "Esteemed Members of
# the ..." -- and end in a salutation's punctuation, so a body sentence that
# mentions a review board ("I will request an external review by an
# independent review board.") is left alone. The words between the opener and
# "Review Board" ("Health Plan", "Independent Medical", "Appeals") must be
# capitalised, as a body's name is, which keeps "To the extent the review
# board, ..." out. The name may sit in a template's placeholder brackets
# ("Dear [Esteemed Members of the Health Plan Review Board],").
#
# Group 1 is the salutation line's lead, group 2 its punctuation; the match
# starts at the top of the text. Written in the syntax Python's re and Java's
# regex (Spark's rlike, in the training pipeline) share: inline flags, \A,
# scoped (?-i:...), no named groups.
REVIEW_BOARD_SALUTATION_PATTERN = (
    r"(?i)\A"
    r"(?:(?!" + _LINE_LEAD + r"(?:Dear|To\s+Whom)\b)[^\n]*\n)*?"
    r"(" + _LINE_LEAD + r")"
    r"(?:"
    r"(?:Dear|To)\s+(?:the\s+)?(?:\[|\{\{)?"
    r"(?:(?:Esteemed|Honou?rable|Distinguished|Respected)\s+)?"
    r"(?:Members\s+of\s+the\s+)?"
    r"|"
    r"(?:\[|\{\{)?"
    r"(?:Esteemed|Honou?rable|Distinguished|Respected)\s+Members\s+of\s+the\s+"
    r")"
    r"(?-i:(?:[A-Z][A-Za-z'’/-]*\s+){0,4}?)"
    r"Review\s+(?:Board|Panel|Committee)"
    r"(?:\s+Members)?"
    r"(?:\]|\}\})?"
    r"\s*([,:;])"
)

REVIEW_BOARD_SALUTATION_RE = re.compile(REVIEW_BOARD_SALUTATION_PATTERN)


def addressed_to_review_board(text: str) -> bool:
    """Whether the salutation of the letter in ``text`` is to a review board,
    panel or committee (see ``REVIEW_BOARD_SALUTATION_PATTERN``)."""
    return bool(text) and REVIEW_BOARD_SALUTATION_RE.match(text) is not None


def readdress_review_board_salutation(text: str, recipient: str) -> str:
    """``text`` with the letter's salutation rewritten as "Dear <recipient>"
    when it is to a review board, keeping the line's own punctuation and any
    markdown around it. Only the letter's own salutation is rewritten: one
    quoted later in the body stays as written. ``recipient`` is the plan's
    name, or the caller's placeholder for it when the plan is not known; a
    blank one leaves the text as it is, since "Dear ," would be worse than
    the salutation it replaces."""
    if not text or not recipient or not recipient.strip():
        return text
    match = REVIEW_BOARD_SALUTATION_RE.match(text)
    if match is None:
        return text
    lead, punctuation = match.group(1), match.group(2)
    salutation = f"{lead}Dear {recipient.strip()}{punctuation}"
    return text[: match.start(1)] + salutation + text[match.end() :]


# ---------------------------------------------------------------------------
# The preamble
# ---------------------------------------------------------------------------

# A line is the assistant talking to the person about the letter when it opens
# the way such a reply does ...
_PREAMBLE_OPENER_RE = re.compile(
    r"^\W*(?:Sure|Certainly|Of course|Absolutely|Okay|OK|Here(?:'s| is| are)|"
    r"Below is|I(?:'d| would) be (?:happy|glad) to|I can (?:\w+ )?help|"
    r"Happy to help|Let me (?:help|draft|write|put together|prepare))\b",
    re.IGNORECASE,
)
# ... and is about the letter it presents (or ends in the colon that presents
# it), or is a bare "Certainly!" that the presenting line then follows.
_PREAMBLE_PRESENTS_RE = re.compile(
    r"\b(?:here(?:'s| is| are)|below is|below|help you|draft|template|sample|"
    r"example|letter|appeal)\b|:\W*$",
    re.IGNORECASE,
)
_PREAMBLE_INTERJECTION_RE = re.compile(
    r"^\W*(?:Sure|Certainly|Of course|Absolutely|Okay|OK)\W*$", re.IGNORECASE
)
# A letter's own first line speaks for the writer ("Here is my formal appeal
# of ..."); the assistant's preamble speaks of "your appeal" or "a draft".
_LETTER_VOICE_RE = re.compile(r"\b(?:my|our|I am|I'm|we are|we're)\b", re.IGNORECASE)
# ... except that an offer of help to the reader is the assistant talking,
# first person or not ("Certainly, I am happy to help you draft your appeal
# letter:"). A letter's line says "your denial", never "help you" or "for you".
_ASSISTANT_OFFER_RE = re.compile(
    r"\b(?:help(?:ing)?\s+you|for\s+you|(?:happy|glad|pleased)\s+to\s+"
    r"(?:help|assist)|you\s+can\s+(?:use|adapt|edit|customi[sz]e|send))\b",
    re.IGNORECASE,
)
# An opening fence, with an optional info string ("```", "```markdown",
# "``` markdown"), or the closing one.
_FENCE_RE = re.compile(r"^\s*```\s*[\w-]*\s*$")
# At most this many opening lines are a preamble ("Certainly!", then "Here is
# the letter:"); anything longer is the letter.
_MAX_PREAMBLE_LINES = 3


def is_letter_preamble(line: str) -> bool:
    """Whether ``line`` is the assistant introducing the letter rather than a
    line of the letter."""
    if _PREAMBLE_OPENER_RE.match(line) is None:
        return False
    if _PREAMBLE_INTERJECTION_RE.match(line):
        return True
    if _ASSISTANT_OFFER_RE.search(line):
        return True
    if _LETTER_VOICE_RE.search(line):
        return False
    return _PREAMBLE_PRESENTS_RE.search(line) is not None


def strip_letter_preamble(text: str) -> str:
    """``text`` with the assistant's introduction removed from its top, and
    the markdown fence that often wraps the letter after one. Everything else
    is kept as it is; text with no preamble, or nothing but one, comes back
    unchanged."""
    if not text:
        return text
    lines = text.split("\n")

    def next_content_line(index: int) -> int:
        while index < len(lines) and not lines[index].strip():
            index += 1
        return index

    start = next_content_line(0)
    index = start
    for _ in range(_MAX_PREAMBLE_LINES):
        if index < len(lines) and is_letter_preamble(lines[index]):
            index = next_content_line(index + 1)
        else:
            break
    if index == start or index >= len(lines):
        return text
    rest = lines[index:]
    if _FENCE_RE.match(rest[0]):
        rest = rest[1:]
        last = len(rest) - 1
        while last >= 0 and not rest[last].strip():
            last -= 1
        if last >= 0 and _FENCE_RE.match(rest[last]):
            rest = rest[:last]
        while rest and not rest[0].strip():
            rest = rest[1:]
    letter = "\n".join(rest)
    return letter if letter.strip() else text
