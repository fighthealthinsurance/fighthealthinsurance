from functools import cached_property
from typing import Any, Optional

from fighthealthinsurance.mailing_list_actor import MailingListActor


class MailingListActorRef:
    """A reference to the mailing list actor."""

    mailing_list_actor: Optional[Any] = None

    @cached_property
    def get(self) -> Any:
        name = "mailing_list_actor"
        if self.mailing_list_actor is None:
            self.mailing_list_actor = MailingListActor.options(  # type: ignore
                name=name, lifetime="detached", namespace="fhi", get_if_exists=True
            ).remote()
        return self.mailing_list_actor


mailing_list_actor_ref = MailingListActorRef()
