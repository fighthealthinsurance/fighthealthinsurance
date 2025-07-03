# Different clinics will have different "domains" and the django username will be the user visible username + a clinic name seperated by a panda unicode
# This may seem kind of silly -- but given the requirement that username be unique
# _even_ if you define a custom user model this feels like the most reasonable workaround.

import uuid

import re

from fhi_users.models import UserDomain
from django.contrib.auth import get_user_model

# See https://github.com/typeddjango/django-stubs/issues/599
from typing import TYPE_CHECKING, Optional

from fhi_users.models import (
    ProfessionalDomainRelation,
    UserDomain,
    PatientUser,
    ProfessionalUser,
)

from rest_framework.serializers import ValidationError


from loguru import logger

if TYPE_CHECKING:
    from django.contrib.auth.models import User
else:
    User = get_user_model()


def get_domain_id_from_request(request):
    """
    Helper function to get the domain id from the session.
    """
    session = request.session
    if hasattr(session, "get"):
        try:
            return session.get("domain_id")
        except Exception:
            pass
    if hasattr(session, "user"):
        current_user: User = request.user  # type: ignore
        domain_id = current_user.username.split("🐼")[-1]
        request.session["domain_id"] = domain_id
        return domain_id
    raise Exception("Could not find domain id in request session or user")


def normalize_phone_number(phone_number: Optional[str]) -> Optional[str]:
    """Normalize a phone number to a standard format."""
    # Remove all non-digit characters
    if phone_number is None:
        return None
    try:
        return generic_validate_phone_number(phone_number)
    except ValidationError:
        # In testing we have some short numbers like 42.
        lowered = phone_number.lower()
        return re.sub(r"[^0-9x]", "", lowered)


def get_next_fake_username() -> str:
    return f"{str(uuid.uuid4())}-fake@fighthealthinsurance.com"


def validate_username(username: str) -> bool:
    return "🐼" not in username


def validate_password(password: str) -> bool:
    # Check if password is at least 8 characters long
    if len(password) < 8:
        return False
    # Check if password contains at least one digit
    if not any(char.isdigit() for char in password):
        return False
    # Check if password is not entirely composed of digits
    if password.isdigit():
        return False
    return True


def is_valid_domain(domain_name: str) -> bool:
    return UserDomain.find_by_name(domain_name).exists()


def user_is_admin_in_domain(
    user: User,
    domain_id: Optional[str] = None,
    domain_name: Optional[str] = None,
    phone_number: Optional[str] = None,
) -> bool:
    try:
        domain_id = resolve_domain_id(
            domain_id=domain_id, domain_name=domain_name, phone_number=phone_number
        )
    except Exception as e:
        return False
    return (
        ProfessionalDomainRelation.objects.filter(
            professional__user=user,
            domain_id=domain_id,
            admin=True,
            pending_domain_relation=False,
            active_domain_relation=True,
        ).count()
        > 0
    )


def resolve_domain_id(
    domain: Optional[UserDomain] = None,
    domain_id: Optional[str] = None,
    domain_name: Optional[str] = None,
    phone_number: Optional[str] = None,
) -> str:
    phone_number = normalize_phone_number(phone_number)
    if domain:
        return domain.id
    if domain_id:
        return domain_id
    elif domain_name and len(domain_name) > 0:
        # Try and resolve with domain name then fall back to phone number if it fails
        # Use the new find_by_name method that strips URLs
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
    domain_id: Optional[str] = None,
    domain_name: Optional[str] = None,
    phone_number: Optional[str] = None,
) -> str:
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
    """Create a new user with the given email and password.

    Args:
        email: The user's email address
        password: The user's password
        first_name: The user's first name
        last_name: The user's last name

    Returns:
        The newly created User object
    """
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
    domain_id: Optional[str] = None,
) -> User:
    """Create a new user with the given email and password.

    Args:
        email: The user's email address
        password: The user's password
        first_name: The user's first name
        last_name: The user's last name

    Returns:
        The newly created User object -- the user is set to active false until they verify their email.
    """

    try:
        username = combine_domain_and_username(
            raw_username,
            domain_name=domain_name,
            phone_number=phone_number,
            domain_id=domain_id,
        )

        if not validate_password(password):
            logger.opt(exception=True).error(
                f"Invalid password format during user creation for email: {email}"
            )
            raise Exception(
                "Password is not valid: must be at least 8 characters and cannot be entirely numeric"
            )

        try:
            # Was the user created by a domain admin but without a password?
            user = User.objects.get(
                username=username,
                email=email,
                password=None,
            )
            user.password = password
            user.first_name = first_name
            user.last_name = last_name
            user.save()
            logger.info(
                f"Updated existing pending user with username: {username}, email: {email}"
            )
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
            logger.info(f"Created new user with username: {username}, email: {email}")
        return user
    except Exception as e:
        logger.opt(exception=True).error(
            f"Failed to create user {email} in domain {domain_name}: {str(e)}"
        )
        raise


def generic_validate_phone_number(value: str) -> str:
    """Validate and clean phone number to ensure it contains only digits, 'X', and hyphens."""
    # Remove all hyphens and spaces from the phone number
    cleaned_number = (
        value.replace("-", "")
        .replace(" ", "")
        .replace("+", "")
        .replace("(", "")
        .replace(")", "")
        .replace(",", "x")  # Some folks use comma for extensions
    )

    # Check that the remaining string only contains digits and 'X' or 'x'
    if cleaned_number != "42" and (
        not all(char.isdigit() or char == "X" or char == "x" for char in cleaned_number)
        or len(cleaned_number) < 10
    ):
        raise ValidationError(
            f"Phone number can only contain digits, 'X', and hyphens, and must be at least 10 digits long got {value}"
        )
    # Drop the leading '1' if present and the number is 11 digits long
    if cleaned_number.startswith("1") and len(cleaned_number) > 11:
        cleaned_number = cleaned_number[1:]
    return cleaned_number

def get_user_by_email(email: str) -> Optional[User]:
    """
    Get user by email address only (new email-only authentication).
    Returns the active user with this email, or None if not found/ambiguous.
    """
    if not email:
        return None

    try:
        # Try to get a single active user with this email
        return User.objects.get(email=email, is_active=True)
    except User.DoesNotExist:
        return None
    except User.MultipleObjectsReturned:
        # Multiple users with same email - this is a conflict that needs resolution
        logger.warning(f"Multiple active users found with email {email}")
        return None


def get_user_by_email_or_legacy_username(
    email: str, domain: Optional[str] = None, phone: Optional[str] = None
) -> Optional[User]:
    """
    Get user by email (preferred) or fall back to legacy domain-scoped username.
    This function supports both new email-only auth and legacy domain+email auth during migration.
    """
    # First try email-only lookup
    user = get_user_by_email(email)
    if user:
        return user

    # Fall back to legacy domain-scoped lookup if domain/phone provided
    if domain or phone:
        try:
            domain_id = resolve_domain_id(domain_name=domain, phone_number=phone)
            legacy_username = combine_domain_and_username(email, domain_id=domain_id)
            return User.objects.get(username=legacy_username, is_active=True)
        except (User.DoesNotExist, UserDomain.DoesNotExist, Exception):
            pass

    return None


def authenticate_email_only(email: str, password: str) -> Optional[User]:
    """
    Authenticate user using email and password only (no domain required).
    Returns authenticated user or None.
    """
    user = get_user_by_email(email)
    if user and user.check_password(password):
        return user
    return None


def get_primary_domain_for_user(user: User) -> Optional[UserDomain]:
    """
    Get the primary domain for a user.
    For professionals, returns their first active domain.
    For patients, returns their associated domain.
    """
    try:
        # Try professional first
        professional = ProfessionalUser.objects.get(user=user)
        active_relations = ProfessionalDomainRelation.objects.filter(
            professional=professional, active_domain_relation=True
        ).order_by(
            "-admin"
        )  # Admins first

        if active_relations.exists():
            return active_relations.first().domain

    except ProfessionalUser.DoesNotExist:
        pass

    try:
        # Try patient
        patient = PatientUser.objects.get(user=user)
        from fhi_users.models import PatientDomainRelation

        patient_relations = PatientDomainRelation.objects.filter(patient=patient)
        if patient_relations.exists():
            return patient_relations.first().domain

    except PatientUser.DoesNotExist:
        pass

    # Fall back to extracting from username if it's still in old format
    if "🐼" in user.username:
        domain_id = user.username.split("🐼")[-1]
        try:
            return UserDomain.objects.get(id=domain_id)
        except UserDomain.DoesNotExist:
            pass

    return None


def create_user_email_only(
    email: str,
    password: str,
    first_name: str,
    last_name: str,
    phone_number: Optional[str] = None,
    domain: Optional[UserDomain] = None,
) -> User:
    """
    Create a new user with email-only username (no domain scoping).

    Args:
        email: The user's email address (will be used as username)
        password: The user's password
        first_name: The user's first name
        last_name: The user's last name
        phone_number: Optional phone number for domain inference
        domain: Optional explicit domain assignment

    Returns:
        The newly created User object
    """
    if not validate_password(password):
        raise Exception(
            "Password is not valid: must be at least 8 characters and cannot be entirely numeric"
        )

    # Check if user already exists
    if User.objects.filter(username=email).exists():
        raise Exception(f"User with email {email} already exists")

    if User.objects.filter(email=email).exists():
        raise Exception(f"User with email {email} already exists")

    try:
        user = User.objects.create_user(
            username=email,  # Username is now just the email
            email=email,
            password=password,
            first_name=first_name,
            last_name=last_name,
            is_active=False,  # Require email verification
        )
        logger.info(f"Created new user with email-only username: {email}")

        # Store phone number in UserContactInfo if provided
        if phone_number:
            from fhi_users.models import UserContactInfo

            UserContactInfo.objects.create(
                user=user, phone_number=normalize_phone_number(phone_number)
            )

        return user

    except Exception as e:
        logger.opt(exception=True).error(f"Failed to create user {email}: {str(e)}")
        raise


# ...existing code...
