from fighthealthinsurance.base_actor_ref import BaseActorRef
from fighthealthinsurance.email_polling_actor import EmailPollingActor


class EmailPollingActorRef(BaseActorRef):
    """A reference to the email polling actor."""

    actor_class = EmailPollingActor
    actor_name = "email_polling_actor"
    has_run_method = True


email_polling_actor_ref = EmailPollingActorRef()
