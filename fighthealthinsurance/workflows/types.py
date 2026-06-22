"""Dataclasses passed to/from Temporal workflows.

These must stay free of Django imports so they are safe to import inside the
workflow sandbox. They carry only opaque identifiers (never PHI) so nothing
sensitive is written into Temporal workflow history.
"""

from dataclasses import dataclass


@dataclass
class SendFaxInput:
    """Input for ``SendFaxWorkflow``.

    Attributes:
        hashed_email: Hashed email used (with ``fax_uuid``) to look the fax up.
        fax_uuid: The ``FaxesToSend`` uuid, as a string.
        delay_send: When True, wait one hour before sending (durable timer that
            replaces the Ray fax-polling actor's "send faxes older than 1h"
            sweep).
    """

    hashed_email: str
    fax_uuid: str
    delay_send: bool = False
