from typing import Optional
from decouple import config, UndefinedValueError
import os


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
