# See https://stackoverflow.com/questions/39744593/how-to-create-a-django-superuser-if-it-doesnt-exist-non-interactively
# Covered by https://stackoverflow.com/help/licensing
import os

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Creates an admin user non-interactively if it doesn't exist"

    def add_arguments(self, parser):
        parser.add_argument("--username", help="Admin's username")
        parser.add_argument("--email", help="Admin's email")
        parser.add_argument("--password", help="Admin's password")
        parser.add_argument(
            "--no-input", help="Read options from the environment", action="store_true"
        )

    def handle(self, *args, **options):
        User = get_user_model()

        if options["no_input"]:
            options["username"] = os.environ["FIGHT_HEALTH_ADMIN_USER"]
            options["email"] = os.environ["FIGHT_HEALTH_ADMIN_USER"]
            options["password"] = os.environ["FIGHT_HEALTH_ADMIN_PASSWORD"]

        if not User.objects.filter(username=options["username"]).exists():
            User.objects.create_superuser(
                username=options["username"],
                email=options["email"],
                password=options["password"],
            )
