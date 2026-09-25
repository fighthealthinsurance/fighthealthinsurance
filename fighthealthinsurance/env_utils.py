import os
import sys
from pathlib import Path
from typing import Dict, Optional, overload

from decouple import RepositoryEnv

# The repository's .env, beside manage.py. get_env_variable falls back to it
# for a variable the process environment lacks, on a local run only (see
# dotenv_allowed). Tests point this at a file of their own, or at nothing.
LOCAL_DOTENV_PATH: Optional[Path] = Path(__file__).resolve().parent.parent / ".env"

# The settings classes the test suites run under (tox sets one per env).
_TEST_CONFIGURATIONS = frozenset({"Test", "TestSync", "TestActor"})


def _running_under_pytest() -> bool:
    """True once pytest is imported, which covers collection as well as tests."""
    return "pytest" in sys.modules


def dotenv_allowed() -> bool:
    """Whether this process may read the repo's .env: a local run, not a test.

    Never under the Test, TestSync or TestActor configurations or under pytest,
    so a developer's real keys in .env can't reach the test suite. Never in a
    deployment (is_deployed_environment), which takes its secrets from
    Kubernetes only; .dockerignore keeps .env out of the image as well. Every
    input here comes from the process environment, never from .env itself.
    """
    if os.getenv("DJANGO_CONFIGURATION") in _TEST_CONFIGURATIONS:
        return False
    if _running_under_pytest():
        return False
    if is_deployed_environment():
        return False
    return True


def local_dotenv_values() -> Dict[str, str]:
    """The settings in the repo's .env, or none where .env must not be read.

    Parsed by python-decouple: KEY=VALUE lines, # comments, and one pair of
    matching quotes stripped from a value.
    """
    if LOCAL_DOTENV_PATH is None or not dotenv_allowed():
        return {}
    try:
        return dict(RepositoryEnv(str(LOCAL_DOTENV_PATH)).data)
    except FileNotFoundError:
        return {}


# A str default always gets a str back. Spelled out rather than generic so
# that mypy types ``x or get_env_variable(NAME, "default")`` as str too.
@overload
def get_env_variable(var_name: str) -> Optional[str]: ...


@overload
def get_env_variable(var_name: str, default: str) -> str: ...


@overload
def get_env_variable(var_name: str, default: None) -> Optional[str]: ...


def get_env_variable(var_name: str, default: Optional[str] = None) -> Optional[str]:
    """Read a setting from the process environment, then from the repo's .env.

    Works like os.getenv, with one fallback. A variable in the process
    environment is returned exactly as it is there, even when empty, so the
    environment always wins. Only a variable missing from the environment is
    looked up in .env, and only when dotenv_allowed() says this is a local run
    that is neither a test nor a deployment. When neither has it, returns
    ``default``.
    """
    if var_name in os.environ:
        return os.environ[var_name]
    return local_dotenv_values().get(var_name, default)


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
