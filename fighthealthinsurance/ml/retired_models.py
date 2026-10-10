"""Models FHI no longer calls, by name, whatever the settings say.

Retiring by model name, not by backend class, leaves the backend's slot free
for the next model. To bring a model back, remove its entry and set its host.

An entry may be keyed by the last part of a served path ("/models/<name>"),
which is how our own vLLM slots are named. Outside providers' "org/name" ids
are matched only in full, so retiring one of ours never retires a hosted
model that happens to end the same way.
"""

import datetime
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Retirement:
    retired_on: datetime.date
    reason: str

    def describe(self) -> str:
        return f"Retired {self.retired_on.isoformat()}: {self.reason}"


RETIRED_MODELS: dict[str, Retirement] = {
    "fhi-2025-may-0.3-float16-q8-vllm-compressed": Retirement(
        datetime.date(2026, 10, 10), "parked 2026-10-08, replaced by Gemma 4 26B"
    ),
}


def retirement(
    name: Optional[str], internal_name: Optional[str] = None
) -> Optional[Retirement]:
    """The entry for a model, matched on its name, its wire name, or the last
    part of a wire path ("/models/<name>")."""
    candidates = [name, internal_name]
    # Only a served path's tail: an outside "google/<name>" id is not ours.
    if internal_name and internal_name.startswith("/"):
        candidates.append(internal_name.rstrip("/").rsplit("/", 1)[-1])
    for candidate in candidates:
        if candidate and candidate in RETIRED_MODELS:
            return RETIRED_MODELS[candidate]
    return None
