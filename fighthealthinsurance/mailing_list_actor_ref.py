from fighthealthinsurance.base_actor_ref import BaseActorRef
from fighthealthinsurance.mailing_list_actor import MailingListActor


class MailingListActorRef(BaseActorRef):
    """A reference to the mailing list actor."""

    actor_class = MailingListActor
    actor_name = "mailing_list_actor"
    has_run_method = False


mailing_list_actor_ref = MailingListActorRef()
