import time

import ray
from django.conf import settings
from loguru import logger

from fighthealthinsurance.chooser_refill_actor_ref import chooser_refill_actor_ref
from fighthealthinsurance.email_polling_actor_ref import email_polling_actor_ref
from fighthealthinsurance.fax_polling_actor_ref import fax_polling_actor_ref
from fighthealthinsurance.imr_refresh_actor_ref import imr_refresh_actor_ref
from fighthealthinsurance.pa_refresh_actor_ref import pa_refresh_actor_ref
from fighthealthinsurance.ucr_refresh_actor_ref import ucr_refresh_actor_ref
from fighthealthinsurance.worst_insurance_refresh_actor_ref import (
    worst_insurance_refresh_actor_ref,
)

logger.info("Waiting for ray to (probably) launch.")
time.sleep(60)

# When Temporal owns fax sending, the SendFaxWorkflow durable timer replaces the
# FaxPollingActor's 60s "send faxes older than 1h" sweep, so we don't launch it.
_temporal_enabled = getattr(settings, "TEMPORAL_ENABLED", False)

success = False
attempt = 0

epar = None
fpar = None
cpar = None
ipar = None
upar = None
ppar = None
wpar = None
while not success and attempt < 10:
    logger.info("Attempting to launch actors.")
    attempt = attempt + 1
    try:
        epar, etask = email_polling_actor_ref.get
        cpar, ctask = chooser_refill_actor_ref.get
        ipar, itask = imr_refresh_actor_ref.get
        upar, utask = ucr_refresh_actor_ref.get
        ppar, ptask = pa_refresh_actor_ref.get
        wpar, wtask = worst_insurance_refresh_actor_ref.get
        logger.info(f"Launched email polling actor {epar}")
        logger.info(f"Launched chooser refill actor {cpar}")
        logger.info(f"Launched IMR refresh actor {ipar}")
        logger.info(f"Launched UCR refresh actor {upar}")
        logger.info(f"Launched PA refresh actor {ppar}")
        logger.info(f"Launched worst-insurance refresh actor {wpar}")

        # Collect the running-task and actor handles so the fax polling actor
        # can be conditionally excluded when Temporal owns fax sending.
        tasks = [etask, ctask, itask, utask, ptask, wtask]
        actors = [epar, cpar, ipar, upar, ppar, wpar]
        if not _temporal_enabled:
            fpar, ftask = fax_polling_actor_ref.get
            logger.info(f"Launched fax polling actor {fpar}")
            tasks.append(ftask)
            actors.append(fpar)
        else:
            logger.info("TEMPORAL_ENABLED: skipping fax polling actor launch")

        logger.info("Checking that polling tasks are still running")
        time.sleep(10)
        ready, wait = ray.wait(tasks, timeout=10)
        logger.info(f"Finished {ready}")
        result = ray.get(ready)
        logger.info(f"Results: {result}")
        if len(result) > 0:
            raise Exception("We should not have any polling actors finished!")
        for actor in actors:
            logger.info(f"Checking health of {actor}")
            result = ray.get(actor.health_check.remote())
            logger.info(f"Health check result: {result}")
        success = True
        logger.info("All polling actors launched successfully")

    except Exception:
        logger.opt(exception=True).error("Error trying to launch actors")
        time.sleep(60)

if not success:
    logger.error("Failed to launch polling actors after all attempts")

__all__ = ["epar", "fpar", "cpar", "ipar", "upar", "ppar", "wpar"]
