# Different clinics will have different "domains" and the django username will be the user visible username + a clinic name seperated by a panda unicode
# This may seem kind of silly -- but given the requirement that username be unique
# _even_ if you define a custom user model this feels like the most reasonable workaround.

from fhi_users.models import UserDomain


def validate_username(username: str) -> bool:
    return "🐼" not in username


def is_valid_domain(domain: str) -> bool:
    return UserDomain.objects.filter(name=domain).exists()


def combine_domain_and_username(username: str, domain: str) -> str:
    domain_id = UserDomain.objects.get(name=domain).id
    return f"{username}🐼{domain_id}"
