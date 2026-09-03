"""Tests for Temporal payload encryption and the no-PHI payload contract.

The data-protection posture has two layers: workflow inputs carry only opaque
identifiers (enforced here as a contract test so nobody adds case content
to a payload dataclass later), and with TEMPORAL_PAYLOAD_KEY set every
payload is Fernet-encrypted client-side so Temporal's database and UI hold
ciphertext until namespace retention (720h) expires it.
"""

import dataclasses

import pytest
from cryptography.fernet import Fernet, InvalidToken

from temporalio.api.common.v1 import Payload

from fighthealthinsurance.temporal_codec import ENCODING, EncryptionCodec

_KEY = Fernet.generate_key().decode()


def _payload(data: bytes) -> Payload:
    return Payload(metadata={"encoding": b"json/plain"}, data=data)


@pytest.mark.asyncio
async def test_roundtrip_encrypts_and_restores():
    codec = EncryptionCodec(_KEY)
    original = _payload(b'{"hashed_email": "abc", "fax_uuid": "u"}')
    (encoded,) = await codec.encode([original])
    assert encoded.metadata["encoding"] == ENCODING
    assert b"abc" not in encoded.data  # ciphertext, not readable ids
    (decoded,) = await codec.decode([encoded])
    assert decoded == original


@pytest.mark.asyncio
async def test_plaintext_history_passes_through():
    """Histories written before the key existed must keep replaying."""
    codec = EncryptionCodec(_KEY)
    legacy = _payload(b'{"fax_uuid": "u"}')
    (decoded,) = await codec.decode([legacy])
    assert decoded == legacy


def test_key_of_only_separators_is_a_clear_config_error():
    """A set-but-unusable TEMPORAL_PAYLOAD_KEY (only commas/whitespace) must
    fail at construction with a message naming the setting, not surface as
    MultiFernet's bare ValueError."""
    with pytest.raises(ValueError, match="TEMPORAL_PAYLOAD_KEY"):
        EncryptionCodec(",,,")


@pytest.mark.asyncio
async def test_wrong_key_fails_loudly_not_silently():
    codec = EncryptionCodec(_KEY)
    (encoded,) = await codec.encode([_payload(b"x")])
    other = EncryptionCodec(Fernet.generate_key().decode())
    with pytest.raises(InvalidToken):
        await other.decode([encoded])


def test_payload_dataclasses_carry_only_opaque_identifiers():
    """Tripwire: workflow inputs must stay claim-check style. Any new field
    that could carry case content (text, letters, names, emails) needs a
    design conversation, not a quiet addition."""
    from fighthealthinsurance.workflows import types as wf_types

    allowed = {"hashed_email", "fax_uuid", "denial_uuid", "delay_send"}
    for klass in (wf_types.SendFaxInput, wf_types.GenerateAppealInput):
        fields = {f.name for f in dataclasses.fields(klass)}
        assert fields <= allowed, (
            f"{klass.__name__} gained fields {fields - allowed}: Temporal "
            "payloads may only carry opaque identifiers (see temporal_codec.py)"
        )


@pytest.mark.asyncio
async def test_key_rotation_old_ciphertext_still_decodes():
    """Comma-separated keys: first encrypts, all decrypt."""
    old = Fernet.generate_key().decode()
    new = Fernet.generate_key().decode()
    encoded_old = (await EncryptionCodec(old).encode([_payload(b"x")]))[0]
    rotated = EncryptionCodec(f"{new},{old}")
    (decoded,) = await rotated.decode([encoded_old])
    assert decoded == _payload(b"x")
    # and new writes are NOT readable by the old key alone
    encoded_new = (await rotated.encode([_payload(b"y")]))[0]
    with pytest.raises(InvalidToken):
        await EncryptionCodec(old).decode([encoded_new])


def test_data_converter_encodes_failure_attributes():
    """The failure converter must move exception text into encodable
    payloads; the default leaves message/stack as plaintext protobuf."""
    from temporalio.converter import DefaultFailureConverterWithEncodedAttributes

    from fighthealthinsurance.temporal_client import _encrypting_data_converter

    conv = _encrypting_data_converter(Fernet.generate_key().decode())
    assert conv.failure_converter_class is DefaultFailureConverterWithEncodedAttributes
