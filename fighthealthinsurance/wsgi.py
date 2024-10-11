"""
WSGI config for fighthealthinsurance project.

It exposes the WSGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/4.1/howto/deployment/wsgi/
"""

import os

from configurations.wsgi import get_wsgi_application
import djcelery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "fighthealthinsurance.settings")

application = get_wsgi_application()
djcelery.setup_loader()
