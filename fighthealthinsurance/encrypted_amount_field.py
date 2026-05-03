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

from django.db import models

from django_encrypted_filefield.crypt import Cryptographer


class EncryptedAmountField(models.CharField):
    """Stores a string at rest as a Fernet ciphertext.

    Empty / None values are stored as an empty string with no encryption so
    callers can distinguish "no value" from "encrypted value". Anything else
    is encrypted on save and decrypted on load.

    The field is intentionally a CharField subclass (not BinaryField) so the
    column is portable across SQLite, PostgreSQL, and MySQL.
    """

    description = "An encrypted scalar string (Fernet ciphertext base64)."

    def from_db_value(
        self, value: Optional[str], expression: Any, connection: Any
    ) -> str:
        if value is None or value == "":
            return ""
        try:
            decrypted: bytes = Cryptographer.decrypted(value.encode("utf-8"))
            return decrypted.decode("utf-8")
        except Exception:
            # Backwards-compat: if the column was written before encryption
            # was wired in (e.g. unit-test fixtures), pass the raw value
            # through rather than crashing.
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
