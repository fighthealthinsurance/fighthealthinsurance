"""Helpers that keep credentials out of log lines.

A Django session key is the value of the session cookie, so anyone who reads
one can act as that visitor. A chat session key identifies an anonymous chat
and works as its password. Log lines record at most the first
``SESSION_KEY_LOG_CHARS`` characters of either, through
:func:`session_key_prefix_for_log`.
"""

SESSION_KEY_LOG_CHARS = 8


def session_key_prefix_for_log(session_key: object) -> str:
    """The first 8 characters of a session key, repr'd, or "none" if absent.

    A chat session key comes from client JSON and can be any type, so it is
    converted with str() before slicing, and repr() escapes newlines and
    quotes.
    """
    if not session_key:
        return "none"
    return repr(str(session_key)[:SESSION_KEY_LOG_CHARS])
