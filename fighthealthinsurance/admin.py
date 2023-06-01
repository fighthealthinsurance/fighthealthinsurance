# type: ignore
from django.apps import apps
from django.contrib import admin

from fighthealthinsurance.models import *

# Auto magic
models = apps.get_models()

for model in models:
    print(f"Registering {model} in {model.__module__}")
    # A bit ugly but auto register everything which has not exploded when auto registering cauze I'm lazy
    if ("django.contrib" not in model.__module__ and
        "newsletter" not in model.__module__ and
        "cookie_consent" not in model.__module__):

        print(f"Registering {model}")
        try:
            admin.site.register(model)
        except Exception:
            pass
