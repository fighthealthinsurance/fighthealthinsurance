"""Fax precheck status and failure-reason constants.

Kept in their own dependency-free module so they can be imported from both
``fax_send_core`` (which touches Django/PHI) and the Temporal workflow
definition (which runs in a sandbox and must avoid Django imports). These
strings are the only fax-related values that cross the workflow boundary.
"""

STATUS_OK = "ok"
STATUS_NOT_FOUND = "not_found"
STATUS_ALREADY_SENT = "already_sent"
STATUS_MISSING_DENIAL = "missing_denial"
STATUS_MISSING_DESTINATION = "missing_destination"

# Why a fax failed, as best the fax backends can tell (fax_utils.classify_fax_failure).
# Stored on FaxesToSend.failure_reason and read by finalize_fax; a None reason
# means the backend gave us nothing we recognise.
FAILURE_VOICE = "voice"
FAILURE_DISCONNECTED = "disconnected"
FAILURE_INVALID_NUMBER = "invalid_number"
FAILURE_BUSY = "busy"
FAILURE_NO_ANSWER = "no_answer"
# Our end broke: a backend we could not log in to, a modem with no dial
# tone, a document we could not build. Not the person's number at all.
FAILURE_OUR_SIDE = "our_side"
# The reasons that mean the number itself is wrong, not the line being busy
# or the fax machine at the other end having a bad day.
WRONG_NUMBER_FAILURES = (FAILURE_VOICE, FAILURE_DISCONNECTED, FAILURE_INVALID_NUMBER)
# The far end answered (or didn't) but the number may be fine.
REMOTE_LINE_FAILURES = (FAILURE_BUSY, FAILURE_NO_ANSWER)
