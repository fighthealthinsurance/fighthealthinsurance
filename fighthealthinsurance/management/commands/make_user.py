# Make a user
import os

from typing import Any

from fighthealthinsurance.models import PubMedArticleSummarized
from django.core.management.base import BaseCommand, CommandParser
from django.db import models
from fhi_users.auth.auth_utils import combine_domain_and_username
from django.contrib.auth import get_user_model
from fhi_users.models import UserDomain


class Command(BaseCommand):
    help = "Make a user"

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--username", help="User's username")
        parser.add_argument("--email", help="User's email")
        parser.add_argument("--password", help="User's password")
        parser.add_argument("--domain", help="Users associated domain")
        parser.add_argument("--is-provider", help="Is provider", type=bool)

    def handle(self, *args: str, **options: Any) -> None:
        User = get_user_model()
        raw_user = options["username"]
        email = options["email"]
        password = options["password"]
        domain = options["domain"]
        if not UserDomain.objects.filter(name=domain).exists():
            UserDomain.objects.create(name=domain, active=True)
        username = combine_domain_and_username(raw_user, domain)
        if not User.objects.filter(username=username).exists():
            User.objects.create_user(
                username=username,
                email=options["email"],
                password=options["password"],
            )
        return
