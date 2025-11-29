from functools import cached_property
from fighthealthinsurance.chooser_refill_actor import ChooserRefillActor


class ChooserRefillActorRef:
    """A reference to the chooser refill actor."""

    chooser_refill_actor = None

    @cached_property
    def get(self):
        name = "chooser_refill_actor"
        if self.chooser_refill_actor is None:
            self.chooser_refill_actor = ChooserRefillActor.options(  # type: ignore
                name=name, lifetime="detached", namespace="fhi", get_if_exists=True
            ).remote()
        # Kick off the remote task
        rr = self.chooser_refill_actor.run.remote()
        print(f"Remote run of chooser refill actor {rr}")
        return (self.chooser_refill_actor, rr)


chooser_refill_actor_ref = ChooserRefillActorRef()
