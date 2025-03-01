"""
WSGI config for fighthealthinsurance project.

It exposes the WSGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/4.1/howto/deployment/wsgi/
"""

import os

from configurations.wsgi import get_wsgi_application
from fighthealthinsurance.utils import get_env_variable

os.environ.setdefault("DJANGO_SETTINGS_MODULE", get_env_variable("DJANGO_SETTINGS_MODULE", "fighthealthinsurance.settings"))
os.environ.setdefault("DJANGO_CONFIGURATION", get_env_variable("ENVIRONMENT", "Dev"))

application = get_wsgi_application()
