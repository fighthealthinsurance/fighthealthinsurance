"""Encrypted CharField using the project's existing django_encrypted_filefield
crypto infrastructure.

The repo's `django-encrypted-model-fields` fork only ships file-encryption
support (it's actually a fork of `tlc-django-encrypted-filefield`). There is no
`EncryptedCharField` in the dependency tree. Rather than add a new dependency,
this module provides a minimal scalar `EncryptedAmountField` that reuses the
already-configured `Cryptographer` (DEFF_SALT / DEFF_PASSWORD env keys, see
settings.py).

Used for PHI-adjacent billing amounts on Denial (UCR plan §10.3).
"""

from __future__ import annotations

from typing import Any, Optional

from cryptography.fernet import InvalidToken
from django.db import models
from loguru import logger

from django_encrypted_filefield.crypt import Cryptographer

# Fernet token wrapping a small int (e.g. "25000" -> ~100 base64 chars). 512
# leaves slack for longer plaintext and key rotation. Enforced in __init__ so
# future callers can't accidentally declare a too-narrow column.
_FERNET_MIN_MAX_LENGTH = 512


def amount_to_int(value: Any) -> Optional[int]:
    """Convert a stored encrypted-amount value to an int, or None if blank.

    Tolerates corrupt/non-numeric ciphertext fallbacks by logging and returning
    None instead of letting a bad column 500 the entire detail view.
    """
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        logger.warning(
            "EncryptedAmountField: value could not be parsed as int (length={})",
            len(str(value)),
        )
        return None


def amount_to_str(value: Optional[int]) -> str:
    """Serialize an int amount for storage. Empty string for None."""
    return "" if value is None else str(int(value))


class EncryptedAmountField(models.CharField):
    """Stores a string at rest as a Fernet ciphertext.

    Empty / None values are stored as an empty string with no encryption so
    callers can distinguish "no value" from "encrypted value". Anything else
    is encrypted on save and decrypted on load.

    The field is intentionally a CharField subclass (not BinaryField) so the
    column is portable across SQLite, PostgreSQL, and MySQL.
    """

    description = "An encrypted scalar string (Fernet ciphertext base64)."

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        # A Fernet token is at least ~100 base64 chars even for a 5-byte int.
        # Reject narrower columns so a future caller can't silently lose data
        # under PostgreSQL (SQLite would accept and CI wouldn't catch it).
        if "max_length" not in kwargs or kwargs["max_length"] < _FERNET_MIN_MAX_LENGTH:
            kwargs["max_length"] = _FERNET_MIN_MAX_LENGTH
        super().__init__(*args, **kwargs)

    def from_db_value(
        self, value: Optional[str], expression: Any, connection: Any
    ) -> str:
        if value is None or value == "":
            return ""
        try:
            decrypted: bytes = Cryptographer.decrypted(value.encode("utf-8"))
            return decrypted.decode("utf-8")
        except (InvalidToken, ValueError):
            # Backwards-compat: column was written before encryption was wired
            # in (e.g. legacy fixtures). Anything unexpected — including a key
            # misconfiguration — surfaces as a logged warning instead of being
            # silently swallowed.
            logger.warning(
                "EncryptedAmountField: decryption failed (raw passthrough); "
                "check DEFF_SALT/DEFF_PASSWORD if you see this in production. "
                "raw_length={}",
                len(value),
            )
            return str(value)

    def to_python(self, value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, bytes):
            return value.decode("utf-8")
        return str(value)

    def get_prep_value(self, value: Any) -> str:
        if value is None or value == "":
            return ""
        if isinstance(value, bytes):
            value = value.decode("utf-8")
        encrypted: bytes = Cryptographer.encrypted(str(value).encode("utf-8"))
        return encrypted.decode("utf-8")
