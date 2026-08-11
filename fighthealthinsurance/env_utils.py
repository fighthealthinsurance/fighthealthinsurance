import os
from typing import Optional

from decouple import UndefinedValueError, config


def get_env_variable(var_name: str, default: Optional[str] = None) -> str:
    """
    Get env variable. First we look at .env file then shell envs.
    Note: since we control deployment with our k8s configs this is safe *but*
    if you're going to deploy this outside of such an enviornment you may need to revisit this.
    """
    try:
        return config(var_name, default=default)  # type: ignore
    except UndefinedValueError:
        try:
            r = os.getenv(var_name)
            if r:
                return r
            else:
                raise RuntimeError("Missing")
        except:
            if default:
                return default
            raise RuntimeError(
                f"Critical environment variable {var_name} is missing. Ensure it is set securely."
            )


def is_deployed_environment() -> bool:
    """True when this process runs in a real (non-local) deployment.

    Every real deployment of this app runs in Kubernetes, and the kubelet
    injects KUBERNETES_SERVICE_HOST into every container it starts, so its
    presence distinguishes cluster pods from laptops with zero configuration.
    FHI_DEPLOYED=1 opts a non-Kubernetes deployment in. Both are read from
    the process environment only (not .env): copied-around .env files are
    exactly how local machines end up looking like prod in the first place.
    """
    if os.getenv("KUBERNETES_SERVICE_HOST"):
        return True
    return (os.getenv("FHI_DEPLOYED") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def should_enable_sentry(sentry_endpoint: Optional[str], debug: bool) -> bool:
    """Gate for sentry_sdk.init (see asgi.py): report only from real deployments.

    DEBUG alone is not enough of a guard: a dev shell carrying the production
    SENTRY_ENDPOINT plus a Prod DJANGO_CONFIGURATION (common when poking at
    prod settings locally, and editors auto-export .env into the process env)
    passed the old ``endpoint and not DEBUG`` check and flooded Sentry with
    local-editing noise tagged as production.
    """
    return bool(sentry_endpoint) and not debug and is_deployed_environment()
