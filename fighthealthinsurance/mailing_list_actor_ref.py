from fighthealthinsurance.actor_ref_config import MAILING_LIST_ACTOR_CONFIG
from fighthealthinsurance.base_actor_ref import BaseActorRef
from fighthealthinsurance.mailing_list_actor import MailingListActor


class MailingListActorRef(BaseActorRef):
    """A reference to the mailing list actor."""

    actor_class = MailingListActor
    config = MAILING_LIST_ACTOR_CONFIG


mailing_list_actor_ref = MailingListActorRef()
