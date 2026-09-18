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


def history_may_be_used(denial) -> bool:
    """The person's answer, or the status quo for a row nobody asked."""
    answer = getattr(denial, "health_history_consent", None)
    if answer is None:
        return True
    return bool(answer)


def has_been_asked(denial) -> bool:
    """Whether this case has an answer on record at all."""
    return getattr(denial, "health_history_consent", None) is not None
