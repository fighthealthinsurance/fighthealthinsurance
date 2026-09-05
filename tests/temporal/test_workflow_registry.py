"""The shared workflow registry must reproduce the worker's exact semantics.

This registry was extracted so the replay gate could derive its list from what
production actually registers instead of a hand-kept copy. That refactor
touches PRODUCTION behaviour -- which workflows a worker hosts -- and getting
it wrong is worse than the gap it closed: registering IntakeJourneyWorkflow
while its flag is off would make the flag stop being an execution kill switch,
so a direct Temporal start (or a task queued before the flag flipped) would
run on a worker that is supposed to be dark.
"""

from fighthealthinsurance.workflows import registry


def _names(workflows):
    return [w.__name__ for w in workflows]


def test_fax_role_hosts_only_the_fax_workflow():
    assert _names(
        registry.workflows_for_role(
            "fax", journey_enabled=False, intake_enabled=False
        )
    ) == ["SendFaxWorkflow"]


def test_appeal_role_registers_nothing_while_the_journey_flag_is_off():
    """The flag is an execution kill switch, not just a dispatch switch."""
    assert (
        registry.workflows_for_role(
            "appeal", journey_enabled=False, intake_enabled=False
        )
        == []
    )


def test_intake_workflow_appears_only_with_its_own_flag():
    journey_only = registry.workflows_for_role(
        "appeal", journey_enabled=True, intake_enabled=False
    )
    assert _names(journey_only) == ["GenerateAppealWorkflow"]

    both = registry.workflows_for_role(
        "appeal", journey_enabled=True, intake_enabled=True
    )
    assert _names(both) == ["GenerateAppealWorkflow", "IntakeJourneyWorkflow"]


def test_all_role_is_the_union_of_fax_and_appeal():
    both = registry.workflows_for_role("all", journey_enabled=True, intake_enabled=True)
    assert _names(both) == [
        "SendFaxWorkflow",
        "GenerateAppealWorkflow",
        "IntakeJourneyWorkflow",
    ]
    # ...and with the journey off, "all" is just the fax worker's set.
    assert _names(
        registry.workflows_for_role(
            "all", journey_enabled=False, intake_enabled=False
        )
    ) == ["SendFaxWorkflow"]


def test_all_enabled_is_the_maximal_set_the_replay_gate_must_cover():
    assert set(_names(registry.all_enabled_workflows())) == {
        "SendFaxWorkflow",
        "GenerateAppealWorkflow",
        "IntakeJourneyWorkflow",
    }


def test_an_unknown_role_registers_nothing():
    assert (
        registry.workflows_for_role(
            "nonsense", journey_enabled=True, intake_enabled=True
        )
        == []
    )
