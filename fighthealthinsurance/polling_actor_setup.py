from fighthealthinsurance.email_polling_actor_ref import email_polling_actor_ref
from fighthealthinsurance.fax_polling_actor_ref import fax_polling_actor_ref
import time
import ray

epar, etask = email_polling_actor_ref.get
fpar, ftask = fax_polling_actor_ref.get
print(f"Launched email polling actor {epar}")
print(f"Launched fax polling actor {fpar}")

print(f"Double check that we're not finishing the tasks")
time.sleep(10)
ready, wait = ray.wait([etask, ftask], timeout=1)
print(f"Finished {ready}")
result = ray.get(ready)
print(f"Which resulted in {result}")

__all__ = ["epar", "fpar"]
