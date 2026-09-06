"""The single source of truth for which workflows a worker role registers.

Both ``run_temporal_worker`` and the replay-safety tests import this. Keeping
one list means a workflow can never be enabled in production without the
replay gate noticing: the test asks this function for the maximal set and
requires a recorded history for every entry (external review -- previously the
test hard-coded the names, so a newly registered workflow would have been
replay-unchecked and the gap invisible).

Pure and flag-driven: no Django settings are read here, so the test can ask
for the fully-enabled set without touching configuration.
"""

from typing import List

from fighthealthinsurance.workflows.generate_appeal import GenerateAppealWorkflow
from fighthealthinsurance.workflows.intake_journey import IntakeJourneyWorkflow
from fighthealthinsurance.workflows.send_fax import SendFaxWorkflow

QUEUE_ROLES = ("fax", "appeal", "all")


def fax_workflows() -> List[type]:
    """Workflows hosted on the fax task queue."""
    return [SendFaxWorkflow]


def appeal_workflows(*, intake_enabled: bool) -> List[type]:
    """Workflows hosted on the appeal task queue.

    IntakeJourneyWorkflow is registered only when the intake flag is on, so
    the flag stays a real execution kill switch: with unconditional
    registration a direct Temporal start (or a task queued before the flag
    flipped) would still run on a "dark" worker.
    """
    workflows: List[type] = [GenerateAppealWorkflow]
    if intake_enabled:
        workflows.append(IntakeJourneyWorkflow)
    return workflows


def workflows_for_role(
    role: str, *, journey_enabled: bool, intake_enabled: bool
) -> List[type]:
    """Every workflow a worker with this role registers under these flags."""
    registered: List[type] = []
    if role in ("fax", "all"):
        registered.extend(fax_workflows())
    if role in ("appeal", "all") and journey_enabled:
        registered.extend(appeal_workflows(intake_enabled=intake_enabled))
    return registered


def all_enabled_workflows() -> List[type]:
    """The maximal set: everything a fully-enabled fleet runs.

    What the replay gate must have a history for.
    """
    return workflows_for_role("all", journey_enabled=True, intake_enabled=True)
