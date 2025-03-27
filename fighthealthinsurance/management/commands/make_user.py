from typing import Any
from django.core.management.base import BaseCommand, CommandParser, CommandError
from django.core.validators import validate_email
from django.core.exceptions import ValidationError
from django.contrib.auth import get_user_model
from fhi_users.auth.auth_utils import combine_domain_and_username
from fhi_users.models import (
    UserDomain,
    PatientUser,
    PatientDomainRelation,
    ProfessionalUser,
    ProfessionalDomainRelation,
)


class Command(BaseCommand):
    """Make a new user (for local dev work) if user already exists just move on."""

    help = "Securely create a new user with proper input validation and error handling."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--username",
            required=True,
            help="User's username (generally same as e-mail).",
        )
        parser.add_argument(
            "--email", required=True, help="User's valid email address."
        )
        parser.add_argument(
            "--first-name", required=False, help="User's first name", default="unknown"
        )
        parser.add_argument(
            "--password", required=True, help="User's password (minimum 8 characters)."
        )
        parser.add_argument(
            "--domain",
            required=True,
            help="Domain associated with the user (e.g., company or organization name).",
        )
        parser.add_argument(
            "--visible-phone-number",
            required=True,
            help="Visible phone number for the domain.",
        )
        parser.add_argument(
            "--is-provider",
            type=lambda x: x.lower() in ["true", "1", "yes"],
            default=False,
            help="Set to 'true' if the user is a provider; otherwise 'false'.",
        )

    def handle(self, *args: str, **options: Any) -> None:
        User = get_user_model()

        # Directly index into options since these are required
        username_raw = options["username"].strip()
        email = options["email"].strip()
        password = options["password"]
        domain_input = options["domain"]
        first_name = options.get("first_name", "test_first_name")
        visible_phone_number = options.get("visible_phone_number", "0")
        is_provider = options.get("is_provider", True)

        try:
            validate_email(email)
        except ValidationError:
            raise CommandError("Invalid email address provided.")

        if len(password) < 8:
            raise CommandError("Password must be at least 8 characters long.")

        domain_clean = domain_input.strip()

        try:
            user_domain, created = UserDomain.objects.get_or_create(
                name=domain_clean,
                defaults={
                    "active": True,
                    "pending": False,
                    "visible_phone_number": visible_phone_number,
                },
            )
            if created:
                self.stdout.write(
                    self.style.SUCCESS(f"Domain '{domain_clean}' created successfully.")
                )
            else:
                self.stdout.write(f"Domain '{domain_clean}' already exists.")
        except Exception as e:
            raise CommandError(f"Error handling domain creation: {str(e)}")

        user_domain.beta = True
        user_domain.save()

        try:
            combined_username = combine_domain_and_username(
                username_raw, domain_name=user_domain.name
            )
        except Exception as e:
            raise CommandError(f"Error combining username and domain: {str(e)}")

        if User.objects.filter(username=combined_username).exists():
            self.stdout.write(
                f"User with username '{combined_username}' already exists."
            )
        else:
            try:
                user = User.objects.create_user(
                    username=combined_username,
                    email=email,
                    password=password,
                    first_name=first_name,
                )
                if hasattr(user, "is_provider"):
                    user.is_provider = is_provider
                    user.save()
                self.stdout.write(
                    self.style.SUCCESS(
                        f"User '{combined_username}' created successfully."
                    )
                )
            except Exception as e:
                raise CommandError(f"Failed to create user: {str(e)}")
        user = User.objects.get(username=combined_username)
        if ProfessionalUser.objects.filter(user=user).exists():
            self.stdout.write(
                f"ProfessionalUser with user '{user.username}' already exists."
            )
            pro_user = ProfessionalUser.objects.filter(user=user).get()
            if not ProfessionalDomainRelation.objects.filter(
                professional=pro_user,
                domain=user_domain,
            ).exists():
                ProfessionalDomainRelation.objects.create(
                    professional=pro_user,
                    domain=user_domain,
                    active=True,
                    pending=False,
                    admin=True,
                )
        elif is_provider:
            pro_user = ProfessionalUser.objects.create(user=user, active=True)
            ProfessionalDomainRelation.objects.create(
                professional=pro_user,
                domain=user_domain,
                active=True,
                pending=False,
                admin=True,
            )
        elif PatientUser.objects.filter(user=user).exists():
            self.stdout.write(
                f"PatientUser with user '{user.username}' already exists."
            )
        else:
            patient_user = PatientUser.objects.create(user=user, active=True)
            PatientDomainRelation.objects.create(
                patient=patient_user,
                domain=user_domain,
            )
