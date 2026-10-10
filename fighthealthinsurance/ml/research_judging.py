"""Judge PubMed articles against a denial's treatment with TypeSafe's System
One (Jev): is the article about this treatment for this condition, does it
support the treatment, does it argue against it.

Why: the PubMed search takes the first two hits per query in PubMed's own
order, and every one of them goes into the appeal's research context and
onto the fax page, ticked. Nothing asks whether an article is about the
treatment at all, or whether it says the treatment does not work, which is
the last thing an appeal should quote. Jev answers typed questions about a
text for a fraction of a cent, which is exactly this shape of problem.

What it does with the answers (pubmed_tools and the fax page): an article
confidently off topic, or confidently against the treatment without also
supporting it, is left out of the appeal context and offered unticked on
the fax page; the rest are ordered by how strongly they support it. An
article that was never judged keeps its place, ticked, as before. These are
filters on what we quote, never a claim about the evidence made to anyone.

Data protection: the request carries the article's public title and
abstract, and the denial's treatment and condition (the procedure and
diagnosis), never the letter or anything about the person. The treatment
and condition are health data about the case, so a request is only sent
with the person's external-model consent (``denial.use_external``). A stored
judgment names no person and no case (PubMedArticleJudgment), so it is read
for any case about the same treatment and condition. Inert until both
``TYPESAFE_API_KEY`` and ``TYPESAFE_RESEARCH_JUDGING_ENABLED`` are set;
fails closed; never logs the text.
"""

import asyncio
import dataclasses
import hashlib
import re
import typing

from django.conf import settings

from fighthealthinsurance.ml import letter_quality, spend, typesafe

# Bump when the questions change: a stored judgment from another rubric is
# not reused. The model half is the versioned model TypeSafe reports.
RUBRIC_VERSION = 1
_RUBRIC_SUFFIX = f"/research-rubric-{RUBRIC_VERSION}"
SCORER = f"typesafe/{typesafe.DEFAULT_MODEL}{_RUBRIC_SUFFIX}"

# Key of the cross-pod health record (models.ExternalServiceHealth).
SERVICE = "typesafe-research"

# An article is dropped from what we quote when it is confidently off topic,
# or confidently against the treatment while not also supporting it (a
# mixed result can still help an appeal).
OFF_TOPIC_BELOW = 0.3
UNDERMINES_AT = 0.6
SUPPORTS_BELOW = 0.4
# Where an article nobody judged sorts among judged ones: no evidence
# either way.
UNJUDGED_SUPPORT = 0.5

# Bounds on one context build: how many articles are judged, how many
# requests run at once, and how much of an abstract is sent.
MAX_ARTICLES = 12
CONCURRENCY = 4
ABSTRACT_CHAR_CAP = 6_000
# The treatment and condition as asked about; intake keeps both under 200.
TERM_CHAR_CAP = 200


def scorer_for(payload: typing.Any) -> str:
    return f"typesafe/{typesafe.reported_model(payload)}{_RUBRIC_SUFFIX}"


_SCORER_RE = re.compile(r"^typesafe/[A-Za-z0-9._-]{1,48}/research-rubric-(\d{1,4})$")


def same_rubric(scorer: typing.Optional[str]) -> bool:
    match = _SCORER_RE.match(str(scorer or ""))
    return match is not None and int(match.group(1)) == RUBRIC_VERSION


def enabled() -> bool:
    return typesafe.configured() and bool(
        getattr(settings, "TYPESAFE_RESEARCH_JUDGING_ENABLED", False)
    )


def _term(value: typing.Optional[str]) -> str:
    return " ".join(str(value or "").split())[:TERM_CHAR_CAP]


def treatment_key(
    procedure: typing.Optional[str], diagnosis: typing.Optional[str]
) -> str:
    """Identifies the treatment and condition a judgment was made for,
    case- and whitespace-insensitively."""
    normalized = f"{_term(procedure).lower()}|{_term(diagnosis).lower()}"
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def build_state(title: typing.Optional[str], abstract: typing.Optional[str]) -> str:
    """The article as Jev reads it. Named in capitals, as the questions name
    it: jev-1.13 reads literally (chat_gate.py)."""
    return (
        "THE ARTICLE:\n"
        f"Title: {_term(title) or '(none)'}\n\n"
        f"Abstract: {(abstract or '').strip()[:ABSTRACT_CHAR_CAP]}"
    )


def build_questions(
    treatment: str, condition: typing.Optional[str]
) -> dict[str, dict[str, typing.Any]]:
    treatment = _term(treatment)
    condition = _term(condition)
    for_condition = f' for "{condition}"' if condition else ""
    return {
        "on_topic": {
            "type": "noul",
            "instructions": (
                f'Does THE ARTICLE study "{treatment}"{for_condition}, or for a '
                "closely related condition or population?"
            ),
            "criteria": {
                "true": "The treatment (or a close equivalent) is what the article studies",
                "false": "The article is about something else, or only mentions it in passing",
            },
        },
        "supports": {
            "type": "noul",
            "instructions": (
                f'Does THE ARTICLE report "{treatment}" as effective, beneficial '
                f"or recommended{for_condition}?"
            ),
        },
        "undermines": {
            "type": "noul",
            "instructions": (
                f'Does THE ARTICLE report "{treatment}" as ineffective, harmful or '
                f"not recommended{for_condition}, or find the evidence for it "
                "insufficient?"
            ),
        },
    }


@dataclasses.dataclass(frozen=True)
class Judgment:
    on_topic: float
    supports: float
    undermines: float
    scorer: str = SCORER

    @property
    def drop(self) -> bool:
        """Leave it out of what we quote (and untick it on the fax page)."""
        return self.on_topic < OFF_TOPIC_BELOW or (
            self.undermines >= UNDERMINES_AT and self.supports < SUPPORTS_BELOW
        )


class JudgingError(Exception):
    """A response we could not turn into a Judgment."""


def _unit(value: typing.Any, what: str) -> float:
    if isinstance(value, bool):
        raise JudgingError(f"{what} is not a probability")
    number = float(value)
    if not 0.0 <= number <= 1.0:
        raise JudgingError(f"{what} out of range")
    return number


def parse(payload: typing.Any) -> Judgment:
    """Strict: a missing answer or an odd value means the API changed under
    us, and a silently wrong judgment would hide an article."""
    try:
        answers = payload["answers"]
        return Judgment(
            on_topic=_unit(answers["on_topic"]["noul"], "on_topic"),
            supports=_unit(answers["supports"]["noul"], "supports"),
            undermines=_unit(answers["undermines"]["noul"], "undermines"),
            scorer=scorer_for(payload),
        )
    except JudgingError:
        raise
    except (KeyError, TypeError, ValueError) as e:
        raise JudgingError(f"unexpected response shape: {type(e).__name__}") from e


outcomes: dict[str, int] = {"judged": 0, "failed": 0, "skipped": 0}


def _count(outcome: str) -> None:
    outcomes[outcome] = outcomes.get(outcome, 0) + 1


async def _post(
    state: str, questions: dict[str, typing.Any], timeout_seconds: float
) -> typing.Any:
    # Kept as a seam: tests stub this one function to stay off the network.
    return await typesafe.ask(
        state,
        questions,
        timeout_seconds=timeout_seconds,
        use=spend.typesafe_use(spend.RESEARCH),
    )


async def judge_article(
    title: typing.Optional[str],
    abstract: typing.Optional[str],
    treatment: typing.Optional[str],
    condition: typing.Optional[str],
    *,
    timeout_seconds: typing.Optional[float] = None,
    on_failure: typing.Optional[typing.Callable[[str], typing.Awaitable[None]]] = None,
) -> typing.Optional[Judgment]:
    """Judge one article, or return None. Never raises, never logs the text.

    Consent is the caller's to check. A failure is reported as every Jev
    feature's is (letter_quality.report_failure, which awaits ``on_failure``).
    """
    if not enabled():
        _count("skipped")
        return None
    if not _term(treatment) or not (abstract or "").strip():
        # Nothing to ask about, or nothing to read: the article keeps its
        # place unjudged.
        _count("skipped")
        return None
    timeout = timeout_seconds or float(
        getattr(settings, "TYPESAFE_TIMEOUT_SECONDS", 20)
    )
    try:
        payload = await _post(
            build_state(title, abstract),
            build_questions(treatment or "", condition),
            timeout,
        )
        judgment = parse(payload)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        _count(
            await letter_quality.report_failure(
                e, what="research judging", on_failure=on_failure
            )
        )
        return None
    _count("judged")
    return judgment


_T = typing.TypeVar("_T")


def order(
    articles: typing.Sequence[_T],
    judgments: typing.Mapping[str, Judgment],
    *,
    pmid_of: typing.Callable[[_T], str],
) -> tuple[list[_T], list[_T]]:
    """Split ``articles`` into (kept, dropped), kept ordered by how strongly
    each supports the treatment; an unjudged article counts as
    UNJUDGED_SUPPORT and ties keep their original order. A PMID seen twice
    (the article tables hold duplicates) is kept once."""
    seen: set[str] = set()
    unique: list[_T] = []
    for article in articles:
        pmid = pmid_of(article)
        if pmid in seen:
            continue
        seen.add(pmid)
        unique.append(article)
    kept = [a for a in unique if not _dropped(judgments.get(pmid_of(a)))]
    dropped = [a for a in unique if _dropped(judgments.get(pmid_of(a)))]
    kept.sort(key=lambda a: -_support(judgments.get(pmid_of(a))))
    return kept, dropped


def _dropped(judgment: typing.Optional[Judgment]) -> bool:
    return judgment is not None and judgment.drop


def _support(judgment: typing.Optional[Judgment]) -> float:
    return judgment.supports if judgment is not None else UNJUDGED_SUPPORT
