"""Shared actor-ref constants and typed configuration objects."""

from dataclasses import dataclass
from enum import Enum


class ActorStartMode(str, Enum):
    """How an actor should be started when retrieved by an actor ref."""

    NONE = "none"
    RUN_ON_GET = "run_on_get"


@dataclass(frozen=True)
class ActorRefConfig:
    """Typed configuration for actor refs."""

    name: str
    start_mode: ActorStartMode = ActorStartMode.NONE
    lifetime: str = "detached"
    namespace: str = "fhi"
    get_if_exists: bool = True


FAX_POLLING_ACTOR_CONFIG = ActorRefConfig(
    name="fax_polling_actor", start_mode=ActorStartMode.RUN_ON_GET
)
CHOOSER_REFILL_ACTOR_CONFIG = ActorRefConfig(
    name="chooser_refill_actor", start_mode=ActorStartMode.RUN_ON_GET
)
EMAIL_POLLING_ACTOR_CONFIG = ActorRefConfig(
    name="email_polling_actor", start_mode=ActorStartMode.RUN_ON_GET
)
MAILING_LIST_ACTOR_CONFIG = ActorRefConfig(name="mailing_list_actor")
EXTRALINK_PREFETCH_ACTOR_CONFIG = ActorRefConfig(name="extralink_prefetch_actor")
FAX_ACTOR_CONFIG = ActorRefConfig(name="FaxActor")

