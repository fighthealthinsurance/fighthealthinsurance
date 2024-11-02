from functools import cached_property

import time
import ray
from fighthealthinsurance.fax_utils import FaxActor


class FaxActorRef:
    @cached_property
    def get(self):

        # Shut down existing actor if needed.
        name = f"FaxActor"
        fax_actor = None
        try:
            fax_actor = ray.get_actor(name, namespace="fhi")
            if fax_actor is not None:
                fax_version = 1
                if ray.get(fax_actor.version.remote()) != fax_version:
                    ray.kill(fax_actor)
                    # This sleep is kind of a "code smell" but Ray's actor tracking has some
                    # race conditions inside it we are unlikely to be the people to fix.
                    time.sleep(10)
                    fax_actor = None
        except Exception as e:
            print(f"No exisitng fax actor to stop {e}")

        if fax_actor is None:
            fax_actor = FaxActor.options(  # type: ignore
                name=name, lifetime="detached", namespace="fhi"
            ).remote()
        return fax_actor


fax_actor_ref = FaxActorRef()
