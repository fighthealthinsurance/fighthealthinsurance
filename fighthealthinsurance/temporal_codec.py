"""Payload encryption for Temporal, so workflow history never stores
readable identifiers.

Workflow inputs are already claim-check style -- opaque ``(hashed_email,
uuid)`` pairs, never case content -- but a hashed email is still a
user-linked identifier, and it sits in Temporal's own database until
namespace retention (720h, see ``k8s/temporal/README.md``) expires it. With ``TEMPORAL_PAYLOAD_KEY`` set, every payload (inputs,
results, and the values inside errors) is Fernet-encrypted by the client
before it reaches the Temporal server, so:

- the Temporal database and UI hold ciphertext only;
- retention expiry remains the storage bound (no long-term storage);
- rotating away or destroying the key renders every history unreadable at
  once, an immediate backstop for anything retention has not yet removed.

Decoding passes unencrypted payloads through untouched, so histories
written before the key was configured keep replaying during rollout.
"""

import base64
from typing import Any, Iterable, List

from cryptography.fernet import Fernet, MultiFernet

from temporalio.api.common.v1 import Payload
from temporalio.converter import PayloadCodec

ENCODING = b"binary/encrypted-fernet"


class EncryptionCodec(PayloadCodec):
    """``key`` may be a comma-separated list: the FIRST key encrypts, every
    key decrypts -- so rotation is "prepend the new key, keep the old one
    until retention has expired everything it encrypted"."""

    def __init__(self, key: str) -> None:
        keys = [k.strip() for k in key.split(",") if k.strip()]
        if not keys:
            # A set-but-unusable key (e.g. only commas/whitespace) would
            # otherwise surface as MultiFernet's bare ValueError long after
            # the config mistake was made.
            raise ValueError(
                "TEMPORAL_PAYLOAD_KEY is set but contains no usable key "
                "after splitting on commas; provide at least one Fernet key"
            )
        self._fernet = MultiFernet([Fernet(k) for k in keys])

    async def encode(self, payloads: Iterable[Payload]) -> List[Payload]:
        return [
            Payload(
                metadata={"encoding": ENCODING},
                data=self._fernet.encrypt(p.SerializeToString()),
            )
            for p in payloads
        ]

    async def decode(self, payloads: Iterable[Payload]) -> List[Payload]:
        out: List[Payload] = []
        for p in payloads:
            if p.metadata.get("encoding") == ENCODING:
                out.append(Payload.FromString(self._fernet.decrypt(p.data)))
            else:
                # Pre-codec history (or another producer without the key):
                # pass through so old workflows keep replaying.
                out.append(p)
        return out


def decode_history_json(history: Any, key: str) -> Any:
    """A ``temporal workflow show --output json`` history with every
    encrypted payload replaced by its plaintext, for redacting a capture."""
    fernet = EncryptionCodec(key)._fernet
    marker = base64.b64encode(ENCODING).decode()

    def walk(node: Any) -> Any:
        if isinstance(node, list):
            return [walk(v) for v in node]
        if not isinstance(node, dict):
            return node
        if (node.get("metadata") or {}).get("encoding") == marker and "data" in node:
            plain = Payload.FromString(fernet.decrypt(base64.b64decode(node["data"])))
            return {
                "metadata": {
                    k: base64.b64encode(v).decode() for k, v in plain.metadata.items()
                },
                "data": base64.b64encode(plain.data).decode(),
            }
        return {k: walk(v) for k, v in node.items()}

    return walk(history)
