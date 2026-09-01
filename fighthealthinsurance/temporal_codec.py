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

from typing import Iterable, List

from cryptography.fernet import Fernet

from temporalio.api.common.v1 import Payload
from temporalio.converter import PayloadCodec

ENCODING = b"binary/encrypted-fernet"


class EncryptionCodec(PayloadCodec):
    def __init__(self, key: str) -> None:
        self._fernet = Fernet(key)

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
