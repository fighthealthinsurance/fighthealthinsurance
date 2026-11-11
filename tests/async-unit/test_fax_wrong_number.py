"""Faxes to numbers that don't take faxes: known voice lines, and failures the
fax backends say were a person / phone menu, a dead number or a bad number.

Covers the classifier (fax_utils.classify_fax_failure), the per-send report
the backends note on, the known-number lookup (fax_number_check), what
send_fax_via_vendor keeps on the row, the notes finalize_fax puts in the
support and patient emails, the fax forms' "is this really the fax number?"
check, and the fixture seeding 866-348-1286.
"""

from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.template.loader import render_to_string

from fighthealthinsurance import fax_send_core
from fighthealthinsurance.fax_number_check import (
    lookup_non_fax_number,
    normalize_phone_digits,
)
from fighthealthinsurance.fax_status import (
    FAILURE_BUSY,
    FAILURE_DISCONNECTED,
    FAILURE_INVALID_NUMBER,
    FAILURE_NO_ANSWER,
    FAILURE_OUR_SIDE,
    FAILURE_VOICE,
)
from fighthealthinsurance.fax_utils import (
    FaxSenderBase,
    FaxSendReport,
    FlexibleFaxMagic,
    HylaFaxClient,
    classify_fax_failure,
    note_fax_failure,
)
from fighthealthinsurance.forms import FaxResendForm
from fighthealthinsurance.models import (
    Denial,
    FaxesToSend,
    InsuranceCompany,
    KnownNonFaxNumber,
)

VOICE_LINE = "866-348-1286"


# --- classifying what the backends say ---------------------------------------


@pytest.mark.parametrize(
    "text,reason",
    [
        ("No carrier detected", FAILURE_VOICE),
        ("Ringback detected, no answer without CED", FAILURE_VOICE),
        ("Busy signal detected", FAILURE_BUSY),
        ("No answer from remote", FAILURE_NO_ANSWER),
        ("Invalid dialstring", FAILURE_INVALID_NUMBER),
        ("The number you have dialed is not in service", FAILURE_DISCONNECTED),
        (
            '<td class="s">failed</td><td>Number disconnected</td>',
            FAILURE_DISCONNECTED,
        ),
        ("<td>failed</td><td>Voice answered</td>", FAILURE_VOICE),
        ("No local dialtone", FAILURE_OUR_SIDE),
        ("Modem busy", FAILURE_OUR_SIDE),
    ],
)
def test_classify_fax_failure_recognises_backend_messages(text, reason):
    assert classify_fax_failure(text) == reason


def test_classify_fax_failure_returns_none_for_unrecognised_text():
    assert classify_fax_failure("<td>failed</td><td>12:01</td>") is None


def test_report_prefers_a_wrong_number_reason_over_a_later_busy():
    report = FaxSendReport()
    report.note("Busy signal detected")
    report.note("No carrier detected")
    report.note("No answer from remote")
    assert report.failure_reason == FAILURE_VOICE


def test_report_prefers_what_a_call_reached_over_our_own_trouble():
    report = FaxSendReport()
    report.note("SonicFax: login failed", FAILURE_OUR_SIDE, classify=False)
    report.note("Busy signal detected")
    assert report.failure_reason == FAILURE_BUSY


def test_report_falls_back_to_otherwise_for_unrecognised_text():
    report = FaxSendReport()
    report.note("ssh: connect to host fax port 22", otherwise=FAILURE_OUR_SIDE)
    assert report.failure_reason == FAILURE_OUR_SIDE


def test_report_does_not_classify_text_marked_as_not_a_call_result():
    report = FaxSendReport()
    report.note("Sonic login page: Internet, Voice and TV", classify=False)
    assert report.failure_reason is None


def test_hylafax_job_failure_reads_the_faxstat_status_column():
    report = FaxSendReport()
    faxstat = (
        "JID  Pri S  Owner Number       Pages Dials     TTS Status\n"
        "41   127 D  fax   18665551234   0:2   1:12         \n"
        "42   127 F  fax   18663481286   0:2   3:12         No carrier detected\n"
    )
    with patch("fighthealthinsurance.fax_utils._current_report") as current:
        current.get.return_value = report
        HylaFaxClient._note_job_failure("42", faxstat)
    assert report.failure_reason == FAILURE_VOICE


class _NotingBackend(FaxSenderBase):
    """A backend whose send fails with ``message`` noted, as a real one does."""

    def __init__(self, message: str):
        self.message = message

    async def send_fax_blocking(self, destination, path, dest_name=None):
        note_fax_failure(self.message)
        return False


async def _send_with_report(backend, destination="14255555555"):
    report = FaxSendReport()

    async def _one_transmission(*args, **kwargs):
        return ["/tmp/does-not-matter.pdf"]

    with (
        patch.object(FlexibleFaxMagic, "assemble_outputs", _one_transmission),
        patch("fighthealthinsurance.fax_utils.PdfReader") as reader,
    ):
        reader.return_value.pages = [object()]
        sent = await FlexibleFaxMagic([backend]).send_fax(
            input_paths=["/tmp/letter.pdf"],
            extra="",
            destination=destination,
            blocking=True,
            professional=False,
            report=report,
        )
    return sent, report


@pytest.mark.asyncio
async def test_send_fax_report_carries_the_backends_failure_reason():
    sent, report = await _send_with_report(_NotingBackend("No carrier detected"))
    assert (sent, report.failure_reason) == (False, FAILURE_VOICE)


class _RaisingBackend(FaxSenderBase):
    async def send_fax_blocking(self, destination, path, dest_name=None):
        raise RuntimeError("Error logging into sonic: Internet, Voice and TV")


@pytest.mark.asyncio
async def test_send_fax_report_blames_our_side_when_a_backend_raises():
    _sent, report = await _send_with_report(_RaisingBackend())
    assert report.failure_reason == FAILURE_OUR_SIDE


@pytest.mark.asyncio
async def test_send_fax_report_marks_a_refused_number_invalid():
    # 415-211-xxxx: an N11 exchange, refused before anything is dialed.
    _sent, report = await _send_with_report(
        _NotingBackend("unused"), destination="4152115555"
    )
    assert report.failure_reason == FAILURE_INVALID_NUMBER


# --- the known-number lookup ---------------------------------------------------


@pytest.mark.parametrize(
    "raw,digits",
    [
        ("1 (866) 348-1286", "8663481286"),
        ("866.348.1286", "8663481286"),
        ("348-1286", None),
        ("", None),
    ],
)
def test_normalize_phone_digits(raw, digits):
    assert normalize_phone_digits(raw) == digits


@pytest.mark.django_db
def test_known_non_fax_number_is_stored_as_ten_digits():
    row = KnownNonFaxNumber.objects.create(number="1-866-348-1286")
    assert row.number == "8663481286"


@pytest.mark.django_db
def test_lookup_finds_a_known_number_however_it_is_typed():
    KnownNonFaxNumber.objects.create(number=VOICE_LINE, label="Acme member services")
    found = lookup_non_fax_number("1 (866) 348-1286")
    assert found is not None and found.label == "Acme member services"


@pytest.mark.django_db
def test_lookup_treats_a_carriers_phone_number_as_a_voice_line():
    InsuranceCompany.objects.create(
        name="Acme Health Test", appeal_phone_number="(800) 555-0100"
    )
    found = lookup_non_fax_number("8005550100")
    assert found is not None and found.label == "Acme Health Test member services"


@pytest.mark.django_db
def test_lookup_ignores_a_carrier_phone_that_is_also_a_fax_on_file():
    InsuranceCompany.objects.create(
        name="Acme Health Test", appeal_phone_number="800-555-0100"
    )
    InsuranceCompany.objects.create(
        name="Other Health Test", appeal_fax_number="800-555-0100"
    )
    assert lookup_non_fax_number("8005550100") is None


@pytest.mark.django_db
def test_lookup_returns_none_for_an_unknown_number():
    assert lookup_non_fax_number("4255551234") is None


@pytest.mark.django_db
def test_fixture_seeds_the_reported_voice_line():
    call_command("loaddata", "known_non_fax_numbers", verbosity=0)
    assert lookup_non_fax_number(VOICE_LINE) is not None


# --- what a send keeps, and the notes in the emails -----------------------------


@pytest.fixture
def denial(db):
    email = "wrong_number_test@example.com"
    return Denial.objects.create(
        denial_text="Test denial",
        hashed_email=Denial.get_hashed_email(email),
        raw_email=email,
        health_history="",
    )


def _make_fax(denial, destination="4255551234", **overrides):
    return FaxesToSend.objects.create(
        hashed_email=denial.hashed_email,
        paid=True,
        email=denial.raw_email,
        name="Test User",
        appeal_text="Test appeal text",
        denial_id=denial,
        destination=destination,
        sent=False,
        **overrides,
    )


@pytest.mark.django_db
@patch("fighthealthinsurance.fax_send_core.flexible_fax_magic.send_fax")
def test_failed_send_keeps_the_failure_reason_on_the_row(mock_send, denial):
    async def _fail(**kwargs):
        kwargs["report"].note("No carrier detected")
        return False

    mock_send.side_effect = _fail
    fax = _make_fax(denial)
    with patch.object(fax, "get_temporary_document_path", return_value="/tmp/x.pdf"):
        fax_send_core.send_fax_via_vendor(fax)
    fax.refresh_from_db()
    assert fax.failure_reason == FAILURE_VOICE


@pytest.mark.django_db
@patch("fighthealthinsurance.fax_send_core.flexible_fax_magic.send_fax")
def test_a_send_that_raises_is_recorded_as_our_side(mock_send, denial):
    mock_send.side_effect = RuntimeError("pandoc fell over")
    fax = _make_fax(denial)
    with (
        patch.object(fax, "get_temporary_document_path", return_value="/tmp/x.pdf"),
        pytest.raises(RuntimeError),
    ):
        fax_send_core.send_fax_via_vendor(fax)
    fax.refresh_from_db()
    assert fax.failure_reason == FAILURE_OUR_SIDE


@pytest.mark.django_db
@patch("fighthealthinsurance.fax_send_core.flexible_fax_magic.send_fax")
def test_successful_send_clears_an_earlier_failure_reason(mock_send, denial):
    mock_send.return_value = True
    fax = _make_fax(denial, failure_reason=FAILURE_VOICE, failure_detail="old")
    with patch.object(fax, "get_temporary_document_path", return_value="/tmp/x.pdf"):
        fax_send_core.send_fax_via_vendor(fax)
    fax.refresh_from_db()
    assert (fax.failure_reason, fax.failure_detail) == (None, None)


@pytest.fixture
def support_mail():
    with (
        patch("fighthealthinsurance.fax_send_core.send_mail") as send,
        patch(
            "fighthealthinsurance.fax_send_core.get_env_variable", return_value="true"
        ),
    ):
        yield send


@pytest.mark.django_db
def test_support_email_flags_a_known_voice_line_as_a_wrong_number(
    denial, support_mail
):
    KnownNonFaxNumber.objects.create(number=VOICE_LINE)
    fax = _make_fax(denial, destination=VOICE_LINE)
    with patch("fighthealthinsurance.fax_send_core.build_fallback_email"):
        fax_send_core.finalize_fax(fax, False, missing_destination=False)
    subject, body = support_mail.call_args[0][:2]
    assert "likely wrong number" in subject and "not a fax problem" in body


@pytest.mark.django_db
def test_support_email_includes_what_the_fax_line_reported(denial, support_mail):
    fax = _make_fax(
        denial, failure_reason=FAILURE_DISCONNECTED, failure_detail="Not in service"
    )
    fax_send_core.send_fax_status_notification(fax, False, False)
    body = support_mail.call_args[0][1]
    assert "disconnected" in body and "Not in service" in body


@pytest.mark.django_db
def test_support_email_flags_our_own_fax_system(denial, support_mail):
    fax = _make_fax(denial, failure_reason=FAILURE_OUR_SIDE, failure_detail="login")
    fax_send_core.send_fax_status_notification(fax, False, False)
    subject, body = support_mail.call_args[0][:2]
    assert "likely OUR fax system" in subject and "worth investigating" in body


@pytest.mark.django_db
def test_support_email_says_a_busy_line_is_probably_not_us(denial, support_mail):
    fax = _make_fax(denial, failure_reason=FAILURE_BUSY)
    fax_send_core.send_fax_status_notification(fax, False, False)
    assert "not our fax system" in support_mail.call_args[0][1]


@pytest.mark.django_db
def test_patient_email_says_we_think_it_is_their_voice_line(denial, support_mail):
    KnownNonFaxNumber.objects.create(number=VOICE_LINE, label="Acme member services")
    fax = _make_fax(denial, destination=VOICE_LINE)
    with (
        patch("fighthealthinsurance.fax_send_core.build_fallback_email") as email,
        patch(
            "fighthealthinsurance.email_utils.is_blocked_email", return_value=False
        ),
    ):
        fax_send_core.finalize_fax(fax, False, missing_destination=False)
    context = email.call_args[0][2]
    text = render_to_string("emails/fax_followup.txt", context)
    assert "voice phone line (Acme member services)" in text


FAILED = {
    "success": False,
    "missing_destination": False,
    "fax_redo_link": "https://example.com/redo",
    "fax_number": "425-555-1234",
}


@pytest.mark.parametrize("template", ["fax_followup.txt", "fax_followup.html"])
def test_every_failed_fax_email_asks_to_double_check_the_fax_number(template):
    rendered = render_to_string(f"emails/{template}", FAILED)
    assert "is the fax number for appeals, not the phone number" in rendered


def test_failed_fax_email_explains_a_voice_answer():
    rendered = render_to_string(
        "emails/fax_followup.txt", {**FAILED, "failure_reason": FAILURE_VOICE}
    )
    assert "a person or a phone menu answered" in rendered


@pytest.mark.parametrize("template", ["fax_followup.txt", "fax_followup.html"])
@pytest.mark.parametrize(
    "reason",
    [
        FAILURE_VOICE,
        FAILURE_DISCONNECTED,
        FAILURE_INVALID_NUMBER,
        FAILURE_BUSY,
        FAILURE_NO_ANSWER,
        FAILURE_OUR_SIDE,
    ],
)
def test_failed_fax_email_stars_its_guess_and_says_we_could_be_wrong(
    template, reason
):
    rendered = render_to_string(
        f"emails/{template}", {**FAILED, "failure_reason": reason}
    )
    assert ".*" in rendered or "*," in rendered
    assert "* This is our best guess" in rendered and "we could be wrong" in rendered


def test_failed_fax_email_without_a_reason_has_no_footnote():
    rendered = render_to_string("emails/fax_followup.txt", FAILED)
    assert "we could be wrong" not in rendered


def test_failed_fax_email_says_when_it_is_our_fax_system():
    rendered = render_to_string(
        "emails/fax_followup.txt", {**FAILED, "failure_reason": FAILURE_OUR_SIDE}
    )
    assert "a problem with our fax system*, not with the number" in rendered


def test_successful_fax_email_has_no_double_check_note():
    rendered = render_to_string(
        "emails/fax_followup.txt", {**FAILED, "success": True}
    )
    assert "not the phone number" not in rendered


# --- the fax forms ---------------------------------------------------------------


def _resend_form(fax_phone, **extra):
    return FaxResendForm(data={"fax_phone": fax_phone, "fax_ref": "ref", **extra})


@pytest.mark.django_db
def test_resend_form_turns_back_a_known_voice_line():
    KnownNonFaxNumber.objects.create(number=VOICE_LINE)
    form = _resend_form(VOICE_LINE)
    assert not form.is_valid()
    assert form.has_error("fax_phone", "known_non_fax_number")


@pytest.mark.django_db
def test_resend_form_offers_the_box_only_for_a_known_voice_line():
    KnownNonFaxNumber.objects.create(number=VOICE_LINE)
    plain, voice = _resend_form("4255551234"), _resend_form(VOICE_LINE)
    plain.is_valid(), voice.is_valid()
    assert ("fax_number_is_right" in plain.fields) is False
    assert "fax_number_is_right" in voice.fields


@pytest.mark.django_db
def test_resend_form_sends_a_known_voice_line_once_the_box_is_ticked():
    KnownNonFaxNumber.objects.create(number=VOICE_LINE)
    assert _resend_form(VOICE_LINE, fax_number_is_right="on").is_valid()


@pytest.mark.django_db
def test_resend_form_accepts_an_ordinary_fax_number():
    assert _resend_form("4255551234").is_valid()


# --- reading the fax number off a denial letter -------------------------------------


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_extraction_check_rejects_a_known_voice_line():
    from fighthealthinsurance.common_view_logic import _is_known_non_fax_number

    await KnownNonFaxNumber.objects.acreate(number=VOICE_LINE)
    assert await _is_known_non_fax_number("(866) 348-1286") is True
