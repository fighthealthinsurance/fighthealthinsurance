# configurations_mypy_django_plugin.py
import os

from configurations.importer import install
from mypy.version import __version__
from mypy_django_plugin import main
from fighthealthinsurance.utils import get_env_variable

def plugin(version):
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", get_env_variable("DJANGO_SETTINGS_MODULE", "fighthealthinsurance.settings"))
    os.environ.setdefault("DJANGO_CONFIGURATION", get_env_variable("DJANGO_CONFIGURATION", "Dev"))
    install()
    return main.plugin(version)
