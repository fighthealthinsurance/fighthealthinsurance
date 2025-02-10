# Different clinics will have different "domains" and the django username will be the user visible username + a clinic name seperated by a panda unicode
# This may seem kind of silly -- but given the requirement that username be unique
# _even_ if you define a custom user model this feels like the most reasonable workaround.

from fhi_users.models import UserDomain
from django.contrib.auth.models import AbstractUser  # Add this import
from django.contrib.auth import get_user_model

# See https://github.com/typeddjango/django-stubs/issues/599
from typing import TYPE_CHECKING, Optional

from fhi_users.models import ProfessionalDomainRelation, UserDomain

if TYPE_CHECKING:
    from django.contrib.auth.models import User
else:
    User = get_user_model()


def validate_username(username: str) -> bool:
    return "🐼" not in username


def is_valid_domain(domain_name: str) -> bool:
    return UserDomain.objects.filter(name=domain_name).exists()


def user_is_admin_in_domain(
    user: User,
    domain_id: Optional[str] = None,
    domain_name: Optional[str] = None,
    phone_number: Optional[str] = None,
) -> bool:
    try:
        domain_id = resolve_domain_id(domain_id, domain_name, phone_number)
    except Exception as e:
        return False
    return (
        ProfessionalDomainRelation.objects.filter(
            professional__user=user,
            domain_id=domain_id,
            admin=True,
            pending=False,
            active=True,
        ).count()
        > 0
    )


def resolve_domain_id(
    domain_id: Optional[str] = None,
    domain_name: Optional[str] = None,
    phone_number: Optional[str] = None,
) -> str:
    if domain_id:
        return domain_id
    elif domain_name and len(domain_name) > 0:
        # Try and resolve with domain name then fall back to phone number if it fails
        try:
            return UserDomain.objects.get(name=domain_name).id
        except UserDomain.DoesNotExist as e:
            if phone_number:
                return UserDomain.objects.get(visible_phone_number=phone_number).id
            else:
                raise e
    elif phone_number and len(phone_number) > 0:
        return UserDomain.objects.get(visible_phone_number=phone_number).id
    else:
        raise Exception("No domain id, name or phone number provided.")


def combine_domain_and_username(
    username: str,
    domain_id: Optional[str] = None,
    domain_name: Optional[str] = None,
    phone_number: Optional[str] = None,
) -> str:
    domain_id = resolve_domain_id(domain_id, domain_name, phone_number)
    return f"{username}🐼{domain_id}"


def create_user(
    email: str,
    raw_username: str,
    domain_name: Optional[str],
    phone_number: Optional[str],
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

    username = combine_domain_and_username(
        raw_username, domain_name=domain_name, phone_number=phone_number
    )
    user = User.objects.create_user(
        username=username,
        email=email,
        password=password,
        first_name=first_name,
        last_name=last_name,
    )
    return user
