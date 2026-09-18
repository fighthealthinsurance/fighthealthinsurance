"""Whether a case's health history may be used in the letter.

The site has always put a history somebody typed into the appeal, and until
now the column that was supposed to govern that was never asked about and
never read on the drafting path. One function, so the page that asks, the
prompt that uses it and the scan that reads it cannot drift apart again.
"""


def history_may_be_used(denial) -> bool:
    """The person's answer, defaulting to yes for a row nobody ever asked.

    A row predating the question carries ``False`` only because that was the
    old column default, never because anybody chose it; migration 0211 turns
    those into the answer they were actually given, which is the history they
    typed on purpose. After that, ``False`` here means somebody unticked the
    box.
    """
    return bool(getattr(denial, "include_provided_health_history_in_appeal", True))
