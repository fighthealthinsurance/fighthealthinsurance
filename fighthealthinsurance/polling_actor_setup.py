from fighthealthinsurance.email_polling_actor_ref import email_polling_actor_ref
from fighthealthinsurance.fax_polling_actor_ref import fax_polling_actor_ref
from fighthealthinsurance.chooser_refill_actor_ref import chooser_refill_actor_ref
import time
import ray

print("Waiting for ray to (probably) launch.")
time.sleep(60)

success = False
attempt = 0

while not success and attempt < 5:
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
        ready, wait = ray.wait([etask, ftask, ctask], timeout=1)
        print(f"Finished {ready}")
        result = ray.get(ready)
        print(f"Which resulted in {result}")
        success = True
    except Exception as e:
        print(f"Error {e} trying to launch")


__all__ = ["epar", "fpar", "cpar"]
