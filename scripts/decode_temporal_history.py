"""Decode an encrypted Temporal history export (tests/temporal/histories/README.md).

    TEMPORAL_PAYLOAD_KEY=... python scripts/decode_temporal_history.py \
        < /tmp/history-raw.json > /tmp/history-plain.json

The key is read from the environment and never printed.
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fighthealthinsurance.temporal_codec import decode_history_json  # noqa: E402

json.dump(
    decode_history_json(
        json.load(sys.stdin), os.environ.get("TEMPORAL_PAYLOAD_KEY", "")
    ),
    sys.stdout,
    indent=2,
)
