"""The underscore-prefix contract: request payloads cannot smuggle
internal-only keys into the appeal generator (see
``fighthealthinsurance.utils.strip_internal_keys`` and its call sites in
rest_views/websockets)."""

from fighthealthinsurance.utils import strip_internal_keys


def test_underscore_keys_are_removed():
    cleaned = strip_internal_keys(
        {"denial_id": 1, "_internal_hashed_email": "h", "_background": True}
    )
    assert cleaned == {"denial_id": 1}


def test_public_keys_pass_through_unchanged():
    params = {"denial_id": 1, "email": "a@b.c", "semi_sekret": "s"}
    assert strip_internal_keys(params) == params


def test_original_dict_is_not_mutated():
    params = {"_internal_hashed_email": "h"}
    strip_internal_keys(params)
    assert "_internal_hashed_email" in params
