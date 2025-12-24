from functools import cached_property
import ray
from fighthealthinsurance.fax_actor import FaxActor


class FaxActorRef:
    fax_actor = None

    @cached_property
    def get(self):
        name = "FaxActor"
        namespace = "fhi"
        if self.fax_actor is None:
            # First try to get existing actor to avoid race conditions
            try:
                self.fax_actor = ray.get_actor(name, namespace=namespace)
            except ValueError:
                # Actor doesn't exist, create it
                self.fax_actor = FaxActor.options(  # type: ignore
                    name=name,
                    lifetime="detached",
                    namespace=namespace,
                    get_if_exists=True,
                ).remote()
        return self.fax_actor


fax_actor_ref = FaxActorRef()
