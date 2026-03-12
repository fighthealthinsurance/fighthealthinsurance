from fighthealthinsurance.actor_ref_config import EMAIL_POLLING_ACTOR_CONFIG
from fighthealthinsurance.base_actor_ref import BaseActorRef
from fighthealthinsurance.email_polling_actor import EmailPollingActor


class EmailPollingActorRef(BaseActorRef):
    """A reference to the email polling actor."""

    actor_class = EmailPollingActor
    config = EMAIL_POLLING_ACTOR_CONFIG


email_polling_actor_ref = EmailPollingActorRef()
