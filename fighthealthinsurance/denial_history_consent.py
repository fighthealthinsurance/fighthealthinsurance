"""Whether a case's health history may be used in writing its appeal.

One question, one column, one function. It is deliberately separate from
``include_provided_health_history_in_appeal``, which decides whether the raw
history is attached to the fax as its own document: that is a wider
disclosure, it is off by default, and a caller can set it through the API, so
its value cannot be read as an answer to this question.

``health_history_consent`` is NULL until somebody is asked. A row from before
the question existed keeps the behaviour it was created under, which is that
the history is used, because that is what the site has always done with a
history typed into a box labelled for it and silently dropping it would make
those letters worse without telling anyone. Once asked, the answer is the
answer.
"""

from loguru import logger


def history_may_be_used(denial) -> bool:
    """The person's answer, or the status quo for a row nobody asked."""
    answer = getattr(denial, "health_history_consent", None)
    if answer is None:
        return True
    return bool(answer)


def has_been_asked(denial) -> bool:
    """Whether this case has an answer on record at all."""
    return getattr(denial, "health_history_consent", None) is not None


def history_may_be_used_now(denial) -> bool:
    """The answer as the database holds it, for a synchronous caller.

    The drafting path builds its prompt inside a worker thread, off the
    event loop, so it cannot await. It is also the reader that matters most,
    being the one that writes the letter, so it asks again rather than
    trusting the row its generation started with.
    """
    from fighthealthinsurance.models import Denial

    denial_id = getattr(denial, "denial_id", None)
    if denial_id is None:
        return history_may_be_used(denial)
    try:
        # values() rather than values_list(): a missing row and a row
        # whose answer is NULL both come back as None from a flat
        # list, and they mean opposite things. No row is not an
        # unanswered question, it is a case that is gone, most likely
        # deleted on request, and its history goes nowhere.
        row = (
            Denial.objects.filter(denial_id=denial_id)
            .values("health_history_consent")
            .first()
        )
    except Exception as e:
        # Fail closed, for the reason given in the async twin below.
        logger.opt(exception=True).warning(
            f"Could not re-read health history consent for denial "
            f"{denial_id}, so treating it as refused: {e}"
        )
        return False
    if row is None:
        logger.warning(
            f"No denial {denial_id} to read health history consent from; "
            "treating it as refused"
        )
        return False
    answer = row["health_history_consent"]
    if answer is None:
        return True
    return bool(answer)


async def ahistory_may_be_used(denial) -> bool:
    """The answer as the database holds it right now.

    A generation runs for tens of seconds and carries the row it started
    with. Somebody can untick the box in the middle of that, and the
    in-memory copy would still say yes, so the history would reach a model
    after they asked us not to use it. This re-reads the one column at the
    point of use, which is as close to the handover as it is worth getting.

    A refusal that lands after this read still races the call it is racing;
    the point is that the window is one query wide rather than the length of
    a generation.
    """
    from fighthealthinsurance.models import Denial

    denial_id = getattr(denial, "denial_id", None)
    if denial_id is None:
        return history_may_be_used(denial)
    try:
        # values() rather than values_list(): a missing row and a row
        # whose answer is NULL both come back as None from a flat
        # list, and they mean opposite things. No row is not an
        # unanswered question, it is a case that is gone, most likely
        # deleted on request, and its history goes nowhere.
        row = (
            await Denial.objects.filter(denial_id=denial_id)
            .values("health_history_consent")
            .afirst()
        )
    except Exception as e:
        # Fail closed. The snapshot is exactly what cannot be trusted here:
        # a refusal is written to the database, so a read that fails may be
        # failing to see one, and the in-memory copy would still say yes.
        # The cost of being wrong this way is a letter without a history the
        # person would have allowed; the cost of the other way is using one
        # they refused.
        logger.opt(exception=True).warning(
            f"Could not re-read health history consent for denial "
            f"{denial_id}, so treating it as refused: {e}"
        )
        return False
    if row is None:
        logger.warning(
            f"No denial {denial_id} to read health history consent from; "
            "treating it as refused"
        )
        return False
    answer = row["health_history_consent"]
    if answer is None:
        return True
    return bool(answer)
