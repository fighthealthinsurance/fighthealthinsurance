"""django-mfa2's app config, with the primary key type its migrations create.

Settings set ``DEFAULT_AUTO_FIELD`` to ``BigAutoField``, and django-mfa2's own
AppConfig names no ``default_auto_field``, so Django reads mfa's models with
big ids while mfa's shipped migrations create regular ones. Every ``migrate``
then reports model changes in mfa that no migration covers, and
``makemigrations --check`` fails. build.sh (via setup_templates.sh) and CI
answer that by generating an mfa migration inside the installed package,
which leaves their databases unlike prod's.

Pinning the field the migrations already use makes the models match them,
with no schema change. The class sits in its own module because Django stops
choosing an app's config automatically when its apps.py holds two.
"""

from mfa.apps import myAppNameConfig


class MfaConfig(myAppNameConfig):
    default_auto_field = "django.db.models.AutoField"
