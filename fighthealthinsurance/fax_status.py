"""Fax precheck status constants.

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
