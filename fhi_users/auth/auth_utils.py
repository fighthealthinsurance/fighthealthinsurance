# Different clinics will have different "domains" and the django username will be the user visible username + a clinic name seperated by a panda unicode
# This may seem kind of silly -- but given the requirement that username be unique
# _even_ if you define a custom user model this feels like the most reasonable workaround.

from fhi_users.models import UserDomain
from django.contrib.auth.models import AbstractUser  # Add this import
from django.contrib.auth import get_user_model

# See https://github.com/typeddjango/django-stubs/issues/599
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from django.contrib.auth.models import User
else:
    User = get_user_model()


def validate_username(username: str) -> bool:
    return "🐼" not in username


def is_valid_domain(domain_name: str) -> bool:
    return UserDomain.objects.filter(name=domain_name).exists()


def combine_domain_and_username(username: str, domain_name: str) -> str:
    domain_id = UserDomain.objects.get(name=domain_name).id
    return f"{username}🐼{domain_id}"


def create_user(
    email: str,
    raw_username: str,
    domain_name: str,
    password: str,
    first_name: str,
    last_name: str,
) -> User:
    """Create a new user with the given email and password.

    Args:
        email: The user's email address
        password: The user's password
        first_name: The user's first name
        last_name: The user's last name

    Returns:
        The newly created User object
    """

    username = combine_domain_and_username(raw_username, domain_name)
    user = User.objects.create_user(
        username=username,
        email=email,
        password=password,
        first_name=first_name,
        last_name=last_name,
    )
    return user
