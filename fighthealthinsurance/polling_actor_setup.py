import time

import ray
from loguru import logger

from fighthealthinsurance.chooser_refill_actor_ref import chooser_refill_actor_ref
from fighthealthinsurance.email_polling_actor_ref import email_polling_actor_ref
from fighthealthinsurance.fax_polling_actor_ref import fax_polling_actor_ref
from fighthealthinsurance.imr_refresh_actor_ref import imr_refresh_actor_ref
from fighthealthinsurance.pa_requirement_refresh_actor_ref import (
    pa_requirement_refresh_actor_ref,
)
from fighthealthinsurance.payer_policy_refresh_actor_ref import (
    payer_policy_refresh_actor_ref,
)
from fighthealthinsurance.ucr_refresh_actor_ref import ucr_refresh_actor_ref

logger.info("Waiting for ray to (probably) launch.")
time.sleep(60)

success = False
attempt = 0

epar = None
fpar = None
cpar = None
ipar = None
upar = None
ppar = None
pprar = None
while not success and attempt < 10:
    logger.info("Attempting to launch actors.")
    attempt = attempt + 1
    try:
        epar, etask = email_polling_actor_ref.get
        fpar, ftask = fax_polling_actor_ref.get
        cpar, ctask = chooser_refill_actor_ref.get
        ipar, itask = imr_refresh_actor_ref.get
        upar, utask = ucr_refresh_actor_ref.get
        ppar, ptask = pa_requirement_refresh_actor_ref.get
        pprar, pptask = payer_policy_refresh_actor_ref.get
        logger.info(f"Launched email polling actor {epar}")
        logger.info(f"Launched fax polling actor {fpar}")
        logger.info(f"Launched chooser refill actor {cpar}")
        logger.info(f"Launched IMR refresh actor {ipar}")
        logger.info(f"Launched UCR refresh actor {upar}")
        logger.info(f"Launched PA requirement refresh actor {ppar}")
        logger.info(f"Launched payer policy refresh actor {pprar}")
        logger.info("Checking that polling tasks are still running")
        time.sleep(10)
        ready, wait = ray.wait(
            [etask, ftask, ctask, itask, utask, ptask, pptask], timeout=10
        )
        logger.info(f"Finished {ready}")
        result = ray.get(ready)
        logger.info(f"Results: {result}")
        if len(result) > 0:
            raise Exception("We should not have any polling actors finished!")
        for actor in [epar, fpar, cpar, ipar, upar, ppar, pprar]:
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

__all__ = ["epar", "fpar", "cpar", "ipar", "upar", "ppar", "pprar"]
