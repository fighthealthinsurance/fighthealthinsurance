#!/usr/bin/env python
"""Django's command-line utility for administrative tasks."""
import os
import sys
from decouple import config, UndefinedValueError
from fighthealthinsurance.utils import get_env_variable


def main():
    """Run administrative tasks."""

    os.environ.setdefault(
        "DJANGO_SETTINGS_MODULE",
        get_env_variable("DJANGO_SETTINGS_MODULE", "fighthealthinsurance.settings"),
    )
    os.environ.setdefault(
        "DJANGO_CONFIGURATION", get_env_variable("ENVIRONMENT", "Dev")
    )

    try:
        from configurations.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Couldn't import Django. Are you sure it's installed and "
            "available on your PYTHONPATH environment variable? Did you "
            "forget to activate a virtual environment?"
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
