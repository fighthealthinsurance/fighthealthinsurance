"""
Actor health check module for Ray polling actors.

This module provides health checks for the three polling actors:
- EmailPollingActor
- FaxPollingActor
- ChooserRefillActor

It checks if actors are alive and returns their status.
"""

import ray
from typing import Dict, Any, List, Optional
from loguru import logger
from dataclasses import dataclass


@dataclass
class ActorHealthDetail:
    name: str
    alive: bool
    error: Optional[str] = None


def check_actor_health() -> Dict[str, Any]:
    """
    Check health of all polling actors.

    Returns:
        Dict with keys:
        - alive_actors: int count of alive actors
        - total_actors: int total expected actors
        - details: List of actor health details
    """
    actors_to_check = [
        ("email_polling_actor", "fhi"),
        ("fax_polling_actor", "fhi"),
        ("chooser_refill_actor", "fhi"),
    ]

    details: List[ActorHealthDetail] = []
    alive_count = 0

    for actor_name, namespace in actors_to_check:
        try:
            # Try to get the actor reference
            actor = ray.get_actor(actor_name, namespace=namespace)

            # Try to call health_check method with a timeout
            try:
                result = ray.get(actor.health_check.remote(), timeout=5)
                if result:
                    alive_count += 1
                    details.append(ActorHealthDetail(name=actor_name, alive=True))
                else:
                    details.append(
                        ActorHealthDetail(
                            name=actor_name,
                            alive=False,
                            error="health_check returned False",
                        )
                    )
            except ray.exceptions.GetTimeoutError:
                details.append(
                    ActorHealthDetail(
                        name=actor_name,
                        alive=False,
                        error="health_check timeout",
                    )
                )
            except Exception as e:
                details.append(
                    ActorHealthDetail(
                        name=actor_name,
                        alive=False,
                        error=f"health_check error: {str(e)}",
                    )
                )
        except ValueError:
            # Actor doesn't exist
            details.append(
                ActorHealthDetail(
                    name=actor_name,
                    alive=False,
                    error="actor not found",
                )
            )
        except Exception as e:
            logger.warning(f"Error checking actor {actor_name}: {e}")
            details.append(
                ActorHealthDetail(
                    name=actor_name,
                    alive=False,
                    error=str(e),
                )
            )

    return {
        "alive_actors": alive_count,
        "total_actors": len(actors_to_check),
        "details": [
            {"name": d.name, "alive": d.alive, "error": d.error} for d in details
        ],
    }


def relaunch_actors(force: bool = False) -> Dict[str, Any]:
    """
    Relaunch polling actors.

    Args:
        force: If True, kill existing actors before relaunching.
               If False, only launch if actors don't exist.

    Returns:
        Dict with status of relaunch operation.
    """
    from fighthealthinsurance.email_polling_actor_ref import email_polling_actor_ref
    from fighthealthinsurance.fax_polling_actor_ref import fax_polling_actor_ref
    from fighthealthinsurance.chooser_refill_actor_ref import chooser_refill_actor_ref

    results = {
        "email_polling_actor": {"status": "pending"},
        "fax_polling_actor": {"status": "pending"},
        "chooser_refill_actor": {"status": "pending"},
    }

    actors = [
        ("email_polling_actor", email_polling_actor_ref),
        ("fax_polling_actor", fax_polling_actor_ref),
        ("chooser_refill_actor", chooser_refill_actor_ref),
    ]

    for actor_name, actor_ref in actors:
        try:
            if force:
                # Try to kill the existing actor
                try:
                    actor = ray.get_actor(actor_name, namespace="fhi")
                    ray.kill(actor)
                    logger.info(f"Killed existing actor: {actor_name}")
                    results[actor_name]["killed"] = True
                except ValueError:
                    # Actor doesn't exist, that's fine
                    results[actor_name]["killed"] = False
                except Exception as e:
                    logger.warning(f"Error killing actor {actor_name}: {e}")
                    results[actor_name]["kill_error"] = str(e)

                # Reset the actor reference to force recreation
                if actor_name == "email_polling_actor":
                    actor_ref.email_polling_actor = None
                elif actor_name == "fax_polling_actor":
                    actor_ref.fax_polling_actor = None
                elif actor_name == "chooser_refill_actor":
                    actor_ref.chooser_refill_actor = None
                
                # Clear the cached property by deleting it from the instance
                try:
                    if hasattr(actor_ref, 'get'):
                        delattr(actor_ref, 'get')
                except AttributeError:
                    # Already cleared or never cached
                    pass

            # Launch the actor
            actor, task = actor_ref.get
            results[actor_name]["status"] = "launched"
            results[actor_name]["actor"] = str(actor)
            logger.info(f"Launched actor: {actor_name}")

        except Exception as e:
            logger.error(f"Error relaunching actor {actor_name}: {e}")
            results[actor_name]["status"] = "error"
            results[actor_name]["error"] = str(e)

    return results
