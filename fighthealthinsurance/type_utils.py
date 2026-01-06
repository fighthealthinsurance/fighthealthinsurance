"""Shared type utilities for Fight Health Insurance.

This module provides common type patterns used across the application
to reduce code duplication.
"""

import typing

from django.contrib.auth import get_user_model

# Common pattern for User model type checking
# This ensures proper type hints during static analysis while using
# the correct user model at runtime
if typing.TYPE_CHECKING:
    from django.contrib.auth.models import User
else:
    User = get_user_model()

__all__ = ["User"]
