import time

import ray

from fighthealthinsurance.chooser_refill_actor_ref import chooser_refill_actor_ref
from fighthealthinsurance.email_polling_actor_ref import email_polling_actor_ref
from fighthealthinsurance.fax_polling_actor_ref import fax_polling_actor_ref

print("Waiting for ray to (probably) launch.")
time.sleep(60)

success = False
attempt = 0

epar = None
fpar = None
cpar = None
while not success and attempt < 10:
    print("attempting to launch actors.")
    attempt = attempt + 1
    try:
        epar, etask = email_polling_actor_ref.get
        fpar, ftask = fax_polling_actor_ref.get
        cpar, ctask = chooser_refill_actor_ref.get
        print(f"Launched email polling actor {epar}")
        print(f"Launched fax polling actor {fpar}")
        print(f"Launched chooser refill actor {cpar}")
        print(f"Double check that we're not finishing the tasks")
        time.sleep(10)
        ready, wait = ray.wait([etask, ftask, ctask], timeout=10)
        print(f"Finished {ready}")
        result = ray.get(ready)
        print(f"Which resulted in {result}")
        if len(result) > 0:
            raise Exception("We should not have any polling actors finished!")
        for actor in [epar, fpar, cpar]:
            print(f"Checking health of {actor}")
            result = ray.get(actor.health_check.remote())
            print(f"Got {result}")
        success = True
        print(f"Requesting polling runs")

    except Exception as e:
        print(f"Error {e} trying to launch")
        time.sleep(60)

if not success:
    print(f"No successes?!?")

__all__ = ["epar", "fpar", "cpar"]
