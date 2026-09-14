"""The extraction and escalation streams must not report work that did not happen.

Two things are pinned here. The wire: every frame is a JSON object carrying a
task and an outcome, and exactly one run-level outcome arrives per run. And
that the outcomes are distinguishable, because the opposite lie is as cheap as
the first one: a row whose only extraction state is
``extract_procedure_diagnosis_finished`` must be told we read the letter and
found nothing, not congratulated on details it does not have.

The page-side rules are asserted twice. What is here reads the source text,
which catches a rule being deleted and nothing more: a rewrite that keeps the
shape while changing what the page does walks straight past it. The real
page-side tests run the code, in ``tests/sync/test_entity_fetcher_behaviour.py``
and ``tests/sync/test_escalation_packet_behaviour.py``. Put new page-side
claims over there.
"""

import contextlib
import json
import pathlib
import re
from unittest.mock import AsyncMock, patch

import pytest
from asgiref.sync import sync_to_async
from channels.testing import WebsocketCommunicator

from fighthealthinsurance import common_view_logic
from fighthealthinsurance.common_view_logic import (
    EXTRACTION_OUTCOME_CACHED,
    EXTRACTION_OUTCOME_FAILED,
    EXTRACTION_OUTCOME_FOUND,
    EXTRACTION_OUTCOME_KEPT_EXISTING,
    EXTRACTION_OUTCOME_NOTHING_FOUND,
    EXTRACTION_RUN_ALREADY_HAVE_DETAILS,
    EXTRACTION_RUN_FAILED,
    EXTRACTION_RUN_FINISHED,
    EXTRACTION_RUN_KEPT_YOUR_DETAILS,
    EXTRACTION_RUN_OUT_OF_ATTEMPTS,
    EXTRACTION_RUN_READ_AND_FOUND_NOTHING,
    EXTRACTION_TASK_CLAIM_ID,
    EXTRACTION_TASK_DATE_OF_SERVICE,
    EXTRACTION_TASK_DENIAL_TYPE,
    EXTRACTION_TASK_INSURANCE_COMPANY,
    EXTRACTION_TASK_PLAN_ID,
    EXTRACTION_TASK_PROCEDURE_AND_DIAGNOSIS,
    DenialCreatorHelper,
    EscalationPacketHelper,
)
from fighthealthinsurance.escalation_addresses import EscalationRecipient
from fighthealthinsurance.ml.ml_plan_doc_helper import MLPlanDocHelper
from fighthealthinsurance.models import (
    DataSource,
    Denial,
    DenialTypes,
    DenialTypesRelation,
)
from fighthealthinsurance.websockets import StreamingEntityBackend

pytestmark = pytest.mark.django_db(transaction=True)

EMAIL = "someone@example.com"
SEKRET = "the-real-secret"

REPO = pathlib.Path(__file__).resolve().parents[2] / "fighthealthinsurance"
FETCHER = REPO / "static" / "js" / "entity_fetcher.ts"
ENTITY_TEMPLATE = REPO / "templates" / "entity_extract.html"
ESCALATION_TEMPLATE = REPO / "templates" / "escalation_packet.html"

# Every step except the one that reads the procedure and the diagnosis.
OTHER_STEPS = (
    "extract_set_fax_number",
    "extract_set_insurance_company",
    "match_insurance_plan_from_regex",
    "extract_set_plan_id",
    "extract_set_claim_id",
    "extract_set_date_of_service",
    "extract_set_regulator",
    "extract_set_triage",
    "extract_set_denialtype",
)


async def _swallow(coro, *args, **kwargs):
    """Stand in for fire_and_forget_in_new_threadpool without leaving coroutines."""
    close = getattr(coro, "close", None)
    if close is not None:
        close()
    return None


@contextlib.contextmanager
def _only_the_letter_reader():
    with contextlib.ExitStack() as stack:
        for name in OTHER_STEPS:
            stack.enter_context(
                patch.object(
                    DenialCreatorHelper, name, new=AsyncMock(return_value=None)
                )
            )
        stack.enter_context(
            patch.object(
                MLPlanDocHelper,
                "generate_plan_documents_summary",
                new=AsyncMock(return_value=None),
            )
        )
        stack.enter_context(
            patch.object(
                DenialCreatorHelper,
                "_maybe_dispatch_ucr",
                new=AsyncMock(return_value=None),
            )
        )
        stack.enter_context(
            patch.object(
                common_view_logic, "fire_and_forget_in_new_threadpool", _swallow
            )
        )
        yield


@contextlib.contextmanager
def _the_model_answers(**answers):
    """Real setters, stubbed model.

    ``_only_the_letter_reader`` replaces the other extractors with AsyncMocks,
    so no test through it can see what their writes do to the row. This stubs
    the ``appealGenerator`` roundtrips instead and lets the real setters run.
    """
    defaults = {
        "get_procedure_and_diagnosis": (None, None),
        "get_insurance_company": None,
        "get_plan_id": None,
        "get_claim_id": None,
        "get_date_of_service": None,
        "get_fax_number": None,
    }
    defaults.update(answers)
    with contextlib.ExitStack() as stack:
        for name, value in defaults.items():
            stack.enter_context(
                patch(
                    f"fighthealthinsurance.common_view_logic.appealGenerator.{name}",
                    new=AsyncMock(return_value=value),
                )
            )
        stack.enter_context(
            patch.object(
                MLPlanDocHelper,
                "generate_plan_documents_summary",
                new=AsyncMock(return_value=None),
            )
        )
        stack.enter_context(
            patch.object(
                DenialCreatorHelper,
                "_maybe_dispatch_ucr",
                new=AsyncMock(return_value=None),
            )
        )
        stack.enter_context(
            patch.object(
                common_view_logic, "fire_and_forget_in_new_threadpool", _swallow
            )
        )
        yield

async def _regex_source() -> None:
    """Make the ``regex`` DataSource real for this test's database.

    ``DenialCreatorHelper.regex_src`` caches the row on the class and the
    database is flushed between these tests, so a row cached by an earlier test
    points at a primary key that no longer exists.
    """
    DenialCreatorHelper._regex_src = None
    await sync_to_async(DataSource.objects.get_or_create)(name="regex")


async def _make_denial(**kwargs) -> Denial:
    fields = dict(
        denial_text="A denial letter that needs reading.",
        hashed_email=Denial.get_hashed_email(EMAIL),
        semi_sekret=SEKRET,
    )
    fields.update(kwargs)
    return await sync_to_async(Denial.objects.create)(**fields)


async def _reload(denial: Denial) -> Denial:
    return await sync_to_async(Denial.objects.get)(denial_id=denial.denial_id)


async def _drive(payload: dict) -> list[str]:
    """Drive the consumer with one payload; return the raw text frames."""
    communicator = WebsocketCommunicator(
        StreamingEntityBackend.as_asgi(), "/ws/streaming-entity-backend/"
    )
    connected, _ = await communicator.connect()
    assert connected
    frames: list[str] = []
    try:
        await communicator.send_to(text_data=json.dumps(payload))
        while True:
            try:
                output = await communicator.receive_output(timeout=20)
            except Exception:
                break
            if output.get("type") == "websocket.close":
                break
            if "text" in output:
                frames.append(output["text"])
    finally:
        await communicator.disconnect()
    return frames


async def _run(denial: Denial, retry: bool = False) -> list[dict]:
    payload = {
        "denial_id": denial.denial_id,
        "email": EMAIL,
        "semi_sekret": SEKRET,
    }
    if retry:
        payload["retry"] = True
    raw = await _drive(payload)
    return _parsed(raw)


def _parsed(raw_frames: list[str]) -> list[dict]:
    """Every frame is a JSON object with a task and an outcome, or this fails."""
    records = []
    for frame in raw_frames:
        assert frame.strip(), f"a whitespace-only frame reached the wire: {frame!r}"
        record = json.loads(frame)
        assert isinstance(record, dict), f"frame is not an object: {frame!r}"
        assert record.get("task"), f"frame carries no task: {frame!r}"
        assert record.get("outcome"), f"frame carries no outcome: {frame!r}"
        records.append(record)
    return records


def _run_outcomes(records: list[dict]) -> list[str]:
    return [r["outcome"] for r in records if r.get("type") == "run"]


def _the_run_outcome(records: list[dict]) -> str:
    outcomes = _run_outcomes(records)
    assert len(outcomes) == 1, f"expected exactly one run-level outcome, got {outcomes}"
    return outcomes[0]


def _outcome_for(records: list[dict], task: str) -> str | None:
    for record in records:
        if record.get("task") == task:
            return record["outcome"]
    return None


def _brace_block(src: str, open_at: int) -> str:
    assert src[open_at] == "{", src[open_at : open_at + 40]
    depth = 0
    for i in range(open_at, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[open_at : i + 1]
    raise AssertionError("unbalanced braces")


def _function_body(src: str, name: str) -> str:
    match = re.search(r"function %s\([^)]*\)\s*(?::[^{]*)?\{" % re.escape(name), src)
    assert match, f"{name} is gone from entity_fetcher.ts"
    return _brace_block(src, match.end() - 1)


# ---------------------------------------------------------------------------
# The stream, driven through the consumer.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_normal_run_says_it_read_the_letter():
    denial = await _make_denial()
    with _only_the_letter_reader(), patch(
        "fighthealthinsurance.common_view_logic.appealGenerator."
        "get_procedure_and_diagnosis",
        new=AsyncMock(return_value=("knee MRI", "knee pain")),
    ):
        records = await _run(denial)

    assert (
        _outcome_for(records, EXTRACTION_TASK_PROCEDURE_AND_DIAGNOSIS)
        == EXTRACTION_OUTCOME_FOUND
    )
    assert _the_run_outcome(records) == EXTRACTION_RUN_FINISHED
    fresh = await _reload(denial)
    assert fresh.procedure == "knee MRI"


@pytest.mark.asyncio
async def test_a_row_that_already_has_the_details_is_told_so():
    denial = await _make_denial(procedure="knee MRI")
    with _only_the_letter_reader(), patch(
        "fighthealthinsurance.common_view_logic.appealGenerator."
        "get_procedure_and_diagnosis",
        new=AsyncMock(return_value=("should not run", "should not run")),
    ) as reader:
        records = await _run(denial)

    assert reader.await_count == 0, "the already-done gate re-read the letter"
    assert _the_run_outcome(records) == EXTRACTION_RUN_ALREADY_HAVE_DETAILS
    blob = json.dumps(records).lower()
    assert "extraction complete" not in blob, records


@pytest.mark.asyncio
async def test_the_finished_flag_with_empty_fields_is_not_the_same_news():
    """The gate is an OR, and this is its other arm.

    ``extract_procedure_diagnosis_finished`` is set True whenever the model
    call returns without raising, including a return of ``(None, None)``. A row
    in that state has no procedure and no diagnosis, so telling the person the
    details are already there is the opposite lie to the one this work removes.
    """
    denial = await _make_denial(
        extract_procedure_diagnosis_finished=True, procedure="", diagnosis=""
    )
    with _only_the_letter_reader(), patch(
        "fighthealthinsurance.common_view_logic.appealGenerator."
        "get_procedure_and_diagnosis",
        new=AsyncMock(return_value=("should not run", "should not run")),
    ):
        records = await _run(denial)

    outcome = _the_run_outcome(records)
    assert outcome == EXTRACTION_RUN_READ_AND_FOUND_NOTHING
    assert outcome != EXTRACTION_RUN_ALREADY_HAVE_DETAILS
    # Both ways out are named in the words the page shows.
    label = [r for r in records if r.get("type") == "run"][0]["label"].lower()
    assert "try again" in label, label
    assert "type them in yourself" in label, label


@pytest.mark.asyncio
async def test_an_exhausted_attempt_budget_says_so():
    denial = await _make_denial(extract_attempts=3)
    with _only_the_letter_reader(), patch(
        "fighthealthinsurance.common_view_logic.appealGenerator."
        "get_procedure_and_diagnosis",
        new=AsyncMock(return_value=("should not run", "should not run")),
    ) as reader:
        records = await _run(denial)

    assert reader.await_count == 0
    assert _the_run_outcome(records) == EXTRACTION_RUN_OUT_OF_ATTEMPTS


@pytest.mark.asyncio
async def test_a_failure_underneath_the_extractor_reaches_the_page():
    """The criterion a fix that only repairs the task wrapper cannot meet.

    ``extract_set_denial_and_diagnosis`` catches every exception out of
    ``get_procedure_and_diagnosis``, bumps ``extract_attempts`` and returns
    normally, so the wrapper around it sees a clean return. Reporting the
    failure requires the extractor itself to say what happened.
    """
    denial = await _make_denial()
    with _only_the_letter_reader(), patch(
        "fighthealthinsurance.common_view_logic.appealGenerator."
        "get_procedure_and_diagnosis",
        new=AsyncMock(side_effect=RuntimeError("the model is down")),
    ):
        records = await _run(denial)

    assert (
        _outcome_for(records, EXTRACTION_TASK_PROCEDURE_AND_DIAGNOSIS)
        == EXTRACTION_OUTCOME_FAILED
    )
    assert not [
        r for r in records if r.get("outcome") == EXTRACTION_OUTCOME_FOUND
    ], records
    assert _the_run_outcome(records) == EXTRACTION_RUN_FAILED
    fresh = await _reload(denial)
    assert fresh.extract_attempts == 1
    assert fresh.extract_procedure_diagnosis_finished is False


@pytest.mark.asyncio
async def test_an_authorized_retry_reads_the_letter_again_within_the_cap():
    """The retry clears what the gate reads, leaves what the person typed
    alone, and spends one of the letter's attempts on its way through."""
    denial = await _make_denial()
    with _only_the_letter_reader(), patch(
        "fighthealthinsurance.common_view_logic.appealGenerator."
        "get_procedure_and_diagnosis",
        new=AsyncMock(side_effect=RuntimeError("the model is down")),
    ):
        await _run(denial)

    # The person gave up waiting and typed the procedure in themselves, and a
    # stale candidate mirror from the failed run is sitting on the row.
    await sync_to_async(Denial.objects.filter(denial_id=denial.denial_id).update)(
        procedure="typed by the person",
        extract_procedure_diagnosis_finished=True,
        candidate_procedure="stale guess",
    )

    with _only_the_letter_reader(), patch(
        "fighthealthinsurance.common_view_logic.appealGenerator."
        "get_procedure_and_diagnosis",
        new=AsyncMock(return_value=(None, "knee pain")),
    ) as reader:
        records = await _run(denial, retry=True)

    assert reader.await_count == 1, "the retry did not read the letter again"
    # The model found a diagnosis and the row had none, so this run really did
    # fill something in and may say so.
    assert _the_run_outcome(records) == EXTRACTION_RUN_FINISHED
    fresh = await _reload(denial)
    assert fresh.procedure == "typed by the person", "the retry overwrote their answer"
    assert fresh.diagnosis == "knee pain"
    assert fresh.candidate_procedure is None, "the stale mirror survived the retry"
    # The failed first run spent one attempt and the retry spent the second.
    assert fresh.extract_attempts == 2, "the retry did not spend an attempt"

    # A fourth attempt is refused: the retry never resets the attempt budget.
    await sync_to_async(Denial.objects.filter(denial_id=denial.denial_id).update)(
        extract_attempts=3
    )
    with _only_the_letter_reader(), patch(
        "fighthealthinsurance.common_view_logic.appealGenerator."
        "get_procedure_and_diagnosis",
        new=AsyncMock(return_value=("should not run", "should not run")),
    ) as refused:
        records = await _run(denial, retry=True)

    assert refused.await_count == 0, "a fourth attempt was allowed through"
    assert _the_run_outcome(records) == EXTRACTION_RUN_OUT_OF_ATTEMPTS


@pytest.mark.asyncio
async def test_a_find_we_could_not_write_down_is_not_reported_as_filled_in():
    """A run can read the letter, find a procedure, and write nothing, because
    the write declines on a column that already holds what the person typed.
    ``run_finished`` says "we filled in what we found", and this run did not."""
    denial = await _make_denial(procedure="typed by the person", diagnosis="theirs too")
    with _only_the_letter_reader(), patch(
        "fighthealthinsurance.common_view_logic.appealGenerator."
        "get_procedure_and_diagnosis",
        new=AsyncMock(return_value=("knee MRI", "knee pain")),
    ) as reader:
        records = await _run(denial, retry=True)

    assert reader.await_count == 1, "the retry did not read the letter again"
    assert (
        _outcome_for(records, EXTRACTION_TASK_PROCEDURE_AND_DIAGNOSIS)
        == EXTRACTION_OUTCOME_KEPT_EXISTING
    )
    assert _the_run_outcome(records) == EXTRACTION_RUN_KEPT_YOUR_DETAILS
    label = [r for r in records if r.get("type") == "run"][0]["label"].lower()
    assert "filled in" not in label, label

    fresh = await _reload(denial)
    assert fresh.procedure == "typed by the person"
    assert fresh.diagnosis == "theirs too"
    # What the model produced is still kept where it belongs, so the review
    # page can offer it: the mirrors are ours to write.
    assert fresh.candidate_procedure == "knee MRI"


@pytest.mark.asyncio
async def test_the_retry_button_cannot_be_pressed_forever():
    """The cap has to count reads, not just the reads that raised.

    ``extract_attempts`` is bumped by the extractor's except path, so on a
    letter the model reads cleanly it never moves, and the retry clears the
    finished flag: without an attempt of its own every press re-runs eleven
    steps and the fan-out behind them.
    """
    denial = await _make_denial()
    with _only_the_letter_reader(), patch(
        "fighthealthinsurance.common_view_logic.appealGenerator."
        "get_procedure_and_diagnosis",
        new=AsyncMock(return_value=(None, None)),
    ) as reader:
        records = await _run(denial)
        assert _the_run_outcome(records) == EXTRACTION_RUN_READ_AND_FOUND_NOTHING
        # A clean read that found nothing does not move the counter, which is
        # why the retry has to.
        assert (await _reload(denial)).extract_attempts == 0

        outcomes = []
        for _ in range(6):
            outcomes.append(_the_run_outcome(await _run(denial, retry=True)))

    assert EXTRACTION_RUN_OUT_OF_ATTEMPTS in outcomes, outcomes
    assert outcomes[-1] == EXTRACTION_RUN_OUT_OF_ATTEMPTS, outcomes
    assert reader.await_count <= 4, f"the letter was read {reader.await_count} times"
    fresh = await _reload(denial)
    assert fresh.extract_attempts == 3, fresh.extract_attempts



@pytest.mark.asyncio
async def test_the_retry_does_not_overwrite_the_details_the_person_corrected():
    """The retry lifts the gate that normally stops a second read.

    Plan ID, claim ID, date of service and the insurer name are all editable on
    the review page, and their extractors wrote unconditionally because no
    other run reaches them twice. So the person could correct what the first
    run got wrong, press Back, press retry, and get the model's answers again
    in place of their own. The real setters run here; stubbing them is what
    hid it.
    """
    await _regex_source()
    denial = await _make_denial(
        denial_text="Denied. Plan XYZ-1. Claim 998877. Served 2026-01-02.",
    )
    with _the_model_answers(
        get_procedure_and_diagnosis=(None, None),
        get_plan_id="AB12345",
        get_claim_id="CD67890",
        get_date_of_service="2026-01-02",
        get_insurance_company="Model Insurance Co",
    ):
        await _run(denial)

    # What the person fixed on the review page, in every column the model also
    # answers.
    await sync_to_async(Denial.objects.filter(denial_id=denial.denial_id).update)(
        plan_id="ZZ99911",
        claim_id="YY88822",
        date_of_service="2025-12-31",
        insurance_company="Their Insurance Co",
    )

    with _the_model_answers(
        get_procedure_and_diagnosis=(None, "knee pain"),
        get_plan_id="AB12345",
        get_claim_id="CD67890",
        get_date_of_service="2026-01-02",
        get_insurance_company="Model Insurance Co",
    ):
        records = await _run(denial, retry=True)

    fresh = await _reload(denial)
    assert fresh.plan_id == "ZZ99911", "the retry overwrote the plan ID"
    assert fresh.claim_id == "YY88822", "the retry overwrote the claim ID"
    assert fresh.date_of_service == "2025-12-31", "the retry overwrote the date"
    assert (
        fresh.insurance_company == "Their Insurance Co"
    ), "the retry overwrote the insurer"

    # And the page says what happened rather than claiming a fill-in.
    for task in (
        EXTRACTION_TASK_PLAN_ID,
        EXTRACTION_TASK_CLAIM_ID,
        EXTRACTION_TASK_DATE_OF_SERVICE,
        EXTRACTION_TASK_INSURANCE_COMPANY,
    ):
        assert _outcome_for(records, task) == EXTRACTION_OUTCOME_KEPT_EXISTING, task


@pytest.mark.asyncio
async def test_an_empty_row_still_gets_filled_in_by_the_real_setters():
    """The other half of the conditional write: it still writes."""
    await _regex_source()
    denial = await _make_denial(denial_text="Denied. Plan XYZ-1. Claim 998877.")
    with _the_model_answers(
        get_plan_id="XYZ-1",
        get_claim_id="998877",
        get_date_of_service="2026-01-02",
        get_insurance_company="Model Insurance Co",
    ):
        records = await _run(denial)

    fresh = await _reload(denial)
    assert fresh.plan_id == "XYZ-1"
    assert fresh.claim_id == "998877"
    assert fresh.date_of_service == "2026-01-02"
    assert fresh.insurance_company == "Model Insurance Co"
    for task in (
        EXTRACTION_TASK_PLAN_ID,
        EXTRACTION_TASK_CLAIM_ID,
        EXTRACTION_TASK_DATE_OF_SERVICE,
        EXTRACTION_TASK_INSURANCE_COMPANY,
    ):
        assert _outcome_for(records, task) == EXTRACTION_OUTCOME_FOUND, task


@pytest.mark.asyncio
async def test_a_step_that_blew_up_is_not_reported_as_absent_from_the_letter():
    """These four setters swallow the model's exceptions, so a read that failed
    returned the same ``None`` as a letter with no plan ID in it, and the page
    said "not in this letter" for a step that never got an answer."""
    await _regex_source()
    denial = await _make_denial()
    boom = AsyncMock(side_effect=RuntimeError("the model is down"))
    with _the_model_answers(), patch(
        "fighthealthinsurance.common_view_logic.appealGenerator.get_plan_id", new=boom
    ), patch(
        "fighthealthinsurance.common_view_logic.appealGenerator.get_claim_id", new=boom
    ), patch(
        "fighthealthinsurance.common_view_logic.appealGenerator.get_date_of_service",
        new=boom,
    ), patch(
        "fighthealthinsurance.common_view_logic.appealGenerator.get_insurance_company",
        new=boom,
    ):
        records = await _run(denial)

    for task in (
        EXTRACTION_TASK_PLAN_ID,
        EXTRACTION_TASK_CLAIM_ID,
        EXTRACTION_TASK_DATE_OF_SERVICE,
        EXTRACTION_TASK_INSURANCE_COMPANY,
    ):
        assert _outcome_for(records, task) == EXTRACTION_OUTCOME_FAILED, task


@pytest.mark.asyncio
async def test_the_denial_reason_is_not_reported_missing_after_it_was_stored():
    """The label on this step is "Reason they gave for the denial", and a bare
    ``None`` return is read as nothing-found, so a run that stored two denial
    types told the person the reason was not in their letter.

    The second run pins the get-or-create: the relation table carries no unique
    constraint, so a retry adds a second copy of every type.
    """
    await _regex_source()
    kinds = [
        await sync_to_async(DenialTypes.objects.create)(
            name="Medical necessity", regex="necessity", diagnosis_regex=""
        ),
        await sync_to_async(DenialTypes.objects.create)(
            name="Prior authorization", regex="prior auth", diagnosis_regex=""
        ),
    ]

    denial = await _make_denial()

    async def _two_types(**kwargs):
        return kinds

    with _the_model_answers(), patch.object(
        DenialCreatorHelper.regex_denial_processor,
        "get_denialtype",
        new=AsyncMock(side_effect=_two_types),
    ):
        records = await _run(denial)
        assert (
            _outcome_for(records, EXTRACTION_TASK_DENIAL_TYPE)
            == EXTRACTION_OUTCOME_FOUND
        ), records
        stored = await sync_to_async(
            DenialTypesRelation.objects.filter(denial=denial).count
        )()
        assert stored == len(kinds)

        records = await _run(denial, retry=True)

    again = await sync_to_async(
        DenialTypesRelation.objects.filter(denial=denial).count
    )()
    assert again == len(kinds), "the retry stored a second copy of every denial type"
    assert (
        _outcome_for(records, EXTRACTION_TASK_DENIAL_TYPE) == EXTRACTION_OUTCOME_CACHED
    ), records

@pytest.mark.asyncio
async def test_a_case_that_does_not_resolve_uses_the_same_envelope():
    """The rejection frame is not a second shape the client has to know."""
    denial = await _make_denial()
    raw = await _drive(
        {
            "denial_id": denial.denial_id,
            "email": EMAIL,
            "semi_sekret": "wrong",
        }
    )
    records = _parsed(raw)
    assert len(records) == 1, records
    assert records[0]["type"] == "error"
    assert records[0]["outcome"] == EXTRACTION_RUN_FAILED
    assert records[0]["label"]


# ---------------------------------------------------------------------------
# Replacing the letter drops what was read out of the old one.
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_replacing_the_letter_clears_the_candidate_mirrors():
    denial = Denial.objects.create(
        denial_text="Letter A.",
        hashed_email=Denial.get_hashed_email(EMAIL),
        semi_sekret=SEKRET,
        candidate_procedure="from letter A",
        candidate_diagnosis="also from letter A",
        procedure="typed by the person",
        extract_procedure_diagnosis_finished=True,
        extract_attempts=2,
        appeal_deadline_label="30 days from the denial",
    )
    denial.denial_text = "Letter B."
    denial.save()

    DenialCreatorHelper._invalidate_denial_text_artifacts(denial)

    fresh = Denial.objects.get(denial_id=denial.denial_id)
    assert fresh.candidate_procedure is None
    assert fresh.candidate_diagnosis is None
    assert fresh.extract_procedure_diagnosis_finished is False
    assert fresh.extract_attempts == 0
    # The triage column this already cleared is still cleared.
    assert not fresh.appeal_deadline_label
    # What the person typed is theirs, and a new letter is not a reason to
    # throw it away.
    assert fresh.procedure == "typed by the person"


# ---------------------------------------------------------------------------
# The page: structural rules the TypeScript has to keep.
# ---------------------------------------------------------------------------


def test_no_step_name_can_reach_the_page():
    """A frame the server gave no label for is not rendered at all."""
    src = FETCHER.read_text()
    body = _function_body(src, "renderStep")
    assert "task" not in body, body
    finish_body = _function_body(src, "finish")
    assert "task" not in finish_body, finish_body


def test_the_words_extraction_complete_cannot_reach_the_dom():
    assert "extraction complete" not in FETCHER.read_text().lower()
    assert "extraction complete" not in ENTITY_TEMPLATE.read_text().lower()
    server_copy = json.dumps(
        [
            common_view_logic.EXTRACTION_RUN_LABELS,
            common_view_logic.EXTRACTION_TASK_LABELS,
        ]
    ).lower()
    assert "extraction complete" not in server_copy


def test_the_extraction_page_never_navigates_for_the_person():
    """Stated about this page, not about the flow:
    ``find_next_steps_loading.html`` auto-submits by design.

    A shape rather than a list of spellings, because three literals let
    ``form.requestSubmit()`` walk straight through.
    """
    src = FETCHER.read_text()
    movers = re.findall(
        r"\.\s*(click|submit|requestSubmit|assign|replace|reload|forward|go)\s*\(",
        src,
    )
    assert not movers, f"something moves the person: {movers}"
    assert not re.search(r"location\s*(\.\s*href)?\s*=", src), src
    assert "window.open" not in src
    assert "button.type = submits ? 'submit' : 'button'" in src


def test_the_page_does_not_say_two_things_at_once_when_the_run_ends():
    """``#waiting-msg`` says "Analyzing your denial..." behind an endless
    spinner and nothing else hides it, so a terminal state has to, or the page
    ends every run saying two different things at once."""
    src = FETCHER.read_text()
    finish_body = _function_body(src, "finish")
    assert "waiting-msg" in finish_body, finish_body
    hide = re.search(
        r"waitingMsg\.style\.display\s*=\s*'none'|waitingMsg\.hidden\s*=\s*true",
        finish_body,
    )
    assert hide, finish_body
    # The block is still there for the run itself.
    assert 'id="waiting-msg"' in ENTITY_TEMPLATE.read_text()


def test_a_good_run_is_not_offered_the_typing_words():
    """Both controls are on every terminal state; only the words on the submit
    one turn on whether the run went well."""
    src = FETCHER.read_text()
    finish_body = _function_body(src, "finish")
    assert "Continue to the next page" in finish_body, finish_body
    assert "Continue and type it in myself" in finish_body, finish_body
    assert "calm ?" in finish_body, finish_body
    assert "run_kept_your_details" in src, src


def test_nothing_paints_over_a_terminal_state():
    """``finish`` guards on ``settled``, but a late frame reaching
    ``renderStep`` would still append a step line under the final words."""
    src = FETCHER.read_text()
    handle = _function_body(src, "handleFrame")
    assert "if (settled)" in handle, handle
    # And the guard is the first thing it does, before anything is rendered.
    assert handle.index("if (settled)") < handle.index("renderStep"), handle


def test_the_bundle_url_moves_when_the_frames_change():
    """Static files here are served from unhashed URLs with no manifest
    storage, so without a marker in the URL a browser holding the previous
    bundle keeps it and renders none of the new frames."""
    src = ENTITY_TEMPLATE.read_text()
    match = re.search(
        r'\{% static "js/dist/entity_fetcher\.bundle\.js" %\}\?frames=(\d+)', src
    )
    assert match, src
    assert int(match.group(1)) >= 2


def test_every_terminal_state_offers_both_ways_out():
    src = FETCHER.read_text()
    finish_body = _function_body(src, "finish")
    start = finish_body.index("Try reading the letter again")
    end = finish_body.index("actions.style.display")
    between = finish_body[start:end]
    assert "Continue and type it in myself" in between, finish_body
    # No branch between the two. The words on the continue button turn on the
    # outcome through a ternary on its label, not a branch around the control.
    assert "if (" not in between, between
    assert between.count("actions.appendChild") == 2, between


def test_a_dead_socket_lands_on_the_could_not_read_state():
    src = FETCHER.read_text()
    match = re.search(r"const INACTIVITY_MS = (\d+);", src)
    assert match, "the inactivity timeout is gone"
    assert int(match.group(1)) <= 60000
    cap = re.search(r"const HARD_CAP_MS = (\d+);", src)
    assert cap, "the hard cap is gone"
    assert int(cap.group(1)) <= 120000
    # The close path resolves into the failure state rather than success.
    settle = src[src.index("const settleConnection") :]
    settle = settle[: settle.index("ws.onopen")]
    assert "COULD_NOT_READ" in settle, settle
    assert "run_failed" in settle, settle


def test_the_socket_scheme_follows_the_page_scheme():
    """Extraction has to work under the local http dev recipe as well as https."""
    src = ENTITY_TEMPLATE.read_text()
    assert "`wss://${window.location.host}" not in src
    assert "window.location.protocol === 'https:' ? 'wss:' : 'ws:'" in src


def test_the_page_offers_a_way_through_without_javascript():
    src = ENTITY_TEMPLATE.read_text()
    assert "<noscript>" in src
    noscript = src[src.index("<noscript>") : src.index("</noscript>")]
    assert "type the procedure and the diagnosis in yourself" in noscript


# ---------------------------------------------------------------------------
# The escalation packet: the same rule, the same defect.
# ---------------------------------------------------------------------------


def _recipient(kind: str, name: str) -> EscalationRecipient:
    return EscalationRecipient(recipient_type=kind, name=name, rationale="because")


async def _escalation_frames(denial: Denial, recipients, letter_for) -> list[dict]:
    with patch(
        "fighthealthinsurance.escalation_addresses.get_recipients_for_denial",
        return_value=recipients,
    ), patch(
        "fighthealthinsurance.generate_regulator_letter.generate_regulator_letter",
        new=AsyncMock(side_effect=lambda d, r, **kw: letter_for(r)),
    ):
        out = []
        async for line in EscalationPacketHelper.generate_escalation_letters(
            {
                "denial_id": denial.denial_id,
                "email": EMAIL,
                "semi_sekret": SEKRET,
            }
        ):
            for part in line.splitlines():
                if part.strip():
                    out.append(json.loads(part))
        return out


def _done_frame(records: list[dict]) -> dict | None:
    for record in records:
        if record.get("type") == "status" and record.get("phase") == "done":
            return record
    return None


@pytest.mark.asyncio
async def test_two_of_four_letters_failing_is_not_all_letters_generated():
    denial = await _make_denial(your_state="CA")
    recipients = [_recipient(f"kind{i}", f"Recipient {i}") for i in range(4)]

    def letter_for(recipient):
        return "" if recipient.name in ("Recipient 1", "Recipient 3") else "A letter."

    records = await _escalation_frames(denial, recipients, letter_for)
    done = _done_frame(records)
    assert done is not None, records
    assert done["total"] == 4
    assert done["generated"] == 2
    assert done["failed"] == 2
    assert done["complete"] is False
    assert sorted(done["failed_names"]) == ["Recipient 1", "Recipient 3"]
    assert "all" not in done["message"].lower(), done["message"]


@pytest.mark.asyncio
async def test_the_total_covers_cached_letters_as_well_as_new_ones():
    denial = await _make_denial(your_state="CA")
    recipients = [_recipient("doi", "Recipient A"), _recipient("md", "Recipient B")]
    await sync_to_async(common_view_logic.RegulatorEscalation.objects.create)(
        for_denial=denial,
        hashed_email=Denial.get_hashed_email(EMAIL),
        recipient_type="doi",
        recipient_name="Recipient A",
        letter_text="An earlier draft.",
    )

    records = await _escalation_frames(denial, recipients, lambda r: "A letter.")
    generating = [
        r
        for r in records
        if r.get("type") == "status" and r.get("phase") == "generating"
    ]
    assert generating, records
    assert generating[0]["total"] == 2, generating[0]
    assert generating[0]["cached"] == 1
    done = _done_frame(records)
    assert done is not None
    assert done["total"] == 2
    assert done["cached"] == 1
    assert done["generated"] == 1
    assert done["complete"] is True


@pytest.mark.asyncio
async def test_no_eligible_recipients_says_so():
    denial = await _make_denial()
    records = await _escalation_frames(denial, [], lambda r: "A letter.")
    assert _done_frame(records) is None, records
    assert any(r.get("type") == "error" for r in records), records


def test_the_escalation_page_does_not_read_a_close_as_a_finished_packet():
    """A tripwire, not the coverage.

    This passes against a close handler that hides the block under a different
    spelling (``setAttribute('style', 'display:none')``). What actually holds
    the rule is ``tests/sync/test_escalation_packet_behaviour.py``, which runs
    the rendered script and reads the screen.
    """
    src = ESCALATION_TEMPLATE.read_text()
    close_handler = src[src.index("ws.onclose") : src.index("ws.onerror")]
    assert "style.display = 'none'" not in close_handler, close_handler
    assert "doneSeen" in close_handler
    assert "failureShown" in close_handler
    # Only a done frame that says every letter is there may hide the block.
    assert "parsed.complete === true" in src


def test_the_escalation_page_keeps_a_connection_error_visible():
    """Also a tripwire. The behaviour is held in the sync harness."""
    src = ESCALATION_TEMPLATE.read_text()
    error_handler = src[src.index("ws.onerror") :]
    assert "showFailure" in error_handler, error_handler
    close_handler = src[src.index("ws.onclose") : src.index("ws.onerror")]
    assert "if (doneSeen || failureShown)" in close_handler


def test_no_em_dash_survives_on_the_escalation_page():
    assert "—" not in ESCALATION_TEMPLATE.read_text()
    assert "—" not in FETCHER.read_text()
    assert "—" not in ENTITY_TEMPLATE.read_text()
