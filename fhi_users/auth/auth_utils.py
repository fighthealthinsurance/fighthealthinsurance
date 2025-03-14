# Different clinics will have different "domains" and the django username will be the user visible username + a clinic name seperated by a panda unicode
# This may seem kind of silly -- but given the requirement that username be unique
# _even_ if you define a custom user model this feels like the most reasonable workaround.

import uuid
from uuid import UUID
from typing import Optional, Union

from fhi_users.models import UserDomain, ProfessionalDomainRelation, PatientUser
from django.contrib.auth import get_user_model
from loguru import logger

# See https://github.com/typeddjango/django-stubs/issues/599
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from django.contrib.auth.models import User
else:
    User = get_user_model()


def get_next_fake_username() -> str:
    return f"{str(uuid.uuid4())}-fake@fighthealthinsurance.com"


def validate_username(username: str) -> bool:
    return "🐼" not in username


def is_valid_domain(domain_name: str) -> bool:
    return UserDomain.find_by_name(domain_name).exists()


def user_is_admin_in_domain(
    user: User,
    domain_id: Optional[str] = None,
    domain_name: Optional[str] = None,
    phone_number: Optional[str] = None,
) -> bool:
    try:
        # resolve_domain_id now returns a UUID
        domain_id_uuid = resolve_domain_id(
            domain_id=domain_id, domain_name=domain_name, phone_number=phone_number
        )
    except Exception:
        return False
    return (
        ProfessionalDomainRelation.objects.filter(
            professional__user=user,
            domain_id=domain_id_uuid,
            admin=True,
            pending=False,
            active=True,
        ).count()
        > 0
    )


def resolve_domain_id(
    domain: Optional[UserDomain] = None,
    domain_id: Optional[Union[str, UUID]] = None,
    domain_name: Optional[str] = None,
    phone_number: Optional[str] = None,
) -> UUID:
    if domain:
        return domain.id  # domain.id is already a UUID
    if domain_id:
        # Convert to UUID if provided as a string
        if isinstance(domain_id, str):
            return uuid.UUID(domain_id)
        return domain_id
    elif domain_name and len(domain_name) > 0:
        # Try and resolve with domain name then fall back to phone number if it fails.
        domains = UserDomain.find_by_name(domain_name)
        if domains.exists():
            return domains.first().id  # type: ignore
        if phone_number:
            return UserDomain.objects.get(visible_phone_number=phone_number).id
        raise UserDomain.DoesNotExist()
    elif phone_number and len(phone_number) > 0:
        return UserDomain.objects.get(visible_phone_number=phone_number).id
    else:
        raise Exception("No domain id, name or phone number provided.")


def combine_domain_and_username(
    raw_username: str,
    *ignore,
    domain: Optional[UserDomain] = None,
    domain_id: Optional[Union[str, UUID]] = None,
    domain_name: Optional[str] = None,
    phone_number: Optional[str] = None,
) -> str:
    # Now, this returns a UUID, which is converted to a string in the f-string.
    domain_id = resolve_domain_id(
        domain_id=domain_id,
        domain_name=domain_name,
        phone_number=phone_number,
        domain=domain,
    )
    username = f"{raw_username}🐼{domain_id}"
    logger.debug(f"Made user username: {username}")
    return username


def get_patient_or_create_pending_patient(
    email: str, raw_username: str, domain: UserDomain, fname: str, lname: str
) -> PatientUser:
    username = combine_domain_and_username(raw_username, domain=domain)
    try:
        user = User.objects.get(username=username)
    except User.DoesNotExist:
        user = User.objects.create_user(
            username=username,
            email=email,
            password=None,
            first_name=fname,
            last_name=lname,
            is_active=False,
        )
    try:
        patient_user = PatientUser.objects.get(user=user)
    except PatientUser.DoesNotExist:
        patient_user = PatientUser.objects.create(user=user, active=False)
    return patient_user


def create_user(
    email: str,
    raw_username: str,
    domain_name: Optional[str],
    phone_number: Optional[str],
    password: str,
    first_name: str,
    last_name: str,
) -> User:
    username = combine_domain_and_username(
        raw_username, domain_name=domain_name, phone_number=phone_number
    )
    try:
        user = User.objects.get(
            username=username,
            email=email,
            password=None,
            is_active=False,
        )
        user.password = password
        user.first_name = first_name
        user.last_name = last_name
        user.save()
        return user
    except User.DoesNotExist:
        user = User.objects.create_user(
            username=username,
            email=email,
            password=password,
            first_name=first_name,
            last_name=last_name,
            is_active=False,
        )
    return user
