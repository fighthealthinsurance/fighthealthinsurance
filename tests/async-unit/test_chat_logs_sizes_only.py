"""Chat tool, scoring and summarization logs carry sizes and error classes.

Tool payloads are part of the model's reply, and replies, queries, document
names, payload keys, URL paths and queries, socket frames, usernames and
exception text raised while handling them are patient data. Each test plants
a sentinel in one of them and checks that no log line written while it was in
flight contains it. Where a line attaches a traceback, the
sentinel may appear there (the deployed sinks keep tracebacks but not frame
variables); the log message itself carries only the exception class.

Session keys identify anonymous chats and work as their passwords, so the
chat consumer logs at most the first 8 characters of one, and ids and flags
the client sends as a bounded repr or a truth value.
"""

import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from channels.testing import WebsocketCommunicator
from loguru import logger

from fighthealthinsurance.chat.context_manager import _summarize_history
from fighthealthinsurance.chat.document_processor import summarize_chunks
from fighthealthinsurance.chat.llm_client import score_llm_response
from fighthealthinsurance.chat.tools.appeal_tool import AppealTool
from fighthealthinsurance.chat.tools.clinical_trials_tool import ClinicalTrialsTool
from fighthealthinsurance.chat.tools.doc_fetcher_tool import DocFetcherTool
from fighthealthinsurance.chat.tools.medicaid_gov_tool import MedicaidGovLookupTool
from fighthealthinsurance.chat.tools.medicaid_tool import (
    MedicaidEligibilityTool,
    MedicaidInfoTool,
)
from fighthealthinsurance.chat.tools.prior_auth_tool import PriorAuthTool
from fighthealthinsurance.chat.tools.pubmed_tool import PubMedTool
from fighthealthinsurance.chat.tools.rxnorm_tool import RxNormLookupTool
from fighthealthinsurance.chat.tools.uspstf_tool import USPSTFLookupTool
from fighthealthinsurance.medicaid_api import MedicaidDataUnavailableError
from fighthealthinsurance.models import Appeal, Denial, PriorAuthRequest
from fighthealthinsurance.websockets import (
    OngoingChatConsumer,
    _parse_json_or_close,
    resolve_chat_type,
)


@contextlib.contextmanager
def _captured_logs():
    """Capture every record at DEBUG and above, plus each line as a sink that
    keeps tracebacks but not frame variables (as the deployed sinks do) would
    write it."""
    records: list = []
    lines: list = []

    def _sink(msg):
        records.append(msg.record)
        lines.append(str(msg))

    sink_id = logger.add(
        _sink,
        level="DEBUG",
        format="{message}\n{exception}",
        backtrace=False,
        diagnose=False,
    )
    try:
        yield records, lines
    finally:
        logger.remove(sink_id)


def _assert_absent_from_lines(lines, sentinel):
    leaked = [line for line in lines if sentinel in line]
    assert leaked == [], f"{sentinel} reached a log line: {leaked}"


def _assert_absent_from_messages(records, sentinel):
    leaked = [r["message"] for r in records if sentinel in r["message"]]
    assert leaked == [], f"{sentinel} reached a log message: {leaked}"


def _messages(records):
    return [r["message"] for r in records]


def _has_message(records, *parts):
    return any(all(p in m for p in parts) for m in _messages(records))


async def _followup(*args, **kwargs):
    return ("SENTINEL-followup reply text", "follow-up context")


class TestMedicaidInfoToolLogs:
    @pytest.mark.asyncio
    async def test_payload_info_and_followup_logged_as_sizes(self):
        tool = MedicaidInfoTool(AsyncMock(), call_llm_callback=_followup)
        text = '**medicaid_info {"state": "California", "note": "SENTINEL-mi-1"}**'
        match = tool.detect(text)
        assert match is not None
        with _captured_logs() as (records, lines):
            with patch(
                "fighthealthinsurance.medicaid_api.get_medicaid_info",
                return_value="Official contact info SENTINEL-mi-info",
            ):
                await tool.execute(
                    match,
                    text,
                    "",
                    model_backends=["backend"],
                    current_message_for_llm="Am I covered?",
                    history_for_llm=[],
                )
        _assert_absent_from_lines(lines, "SENTINEL-mi-1")
        _assert_absent_from_lines(lines, "SENTINEL-mi-info")
        _assert_absent_from_lines(lines, "SENTINEL-followup")
        assert _has_message(records, "Medicaid tool call detected (payload_chars=")
        assert _has_message(records, "Parsed Medicaid tool payload (type=dict)")
        assert _has_message(records, "Got Medicaid info response (info_chars=")
        assert _has_message(
            records, "Medicaid with intro/conclusion (additional_chars="
        )

    @pytest.mark.asyncio
    async def test_unavailable_logs_class_and_fixed_reason(self):
        tool = MedicaidInfoTool(AsyncMock())
        text = '**medicaid_info {"state": "Guam"}**'
        match = tool.detect(text)
        assert match is not None
        with _captured_logs() as (records, lines):
            with patch(
                "fighthealthinsurance.medicaid_api.get_medicaid_info",
                side_effect=MedicaidDataUnavailableError(
                    "SENTINEL-mi-state", "no resources row for this state"
                ),
            ):
                await tool.execute(match, text, "")
        _assert_absent_from_lines(lines, "SENTINEL-mi-state")
        assert (
            "Medicaid info unavailable: MedicaidDataUnavailableError "
            "(no resources row for this state)"
        ) in _messages(records)

    @pytest.mark.asyncio
    async def test_invalid_payload_logged_as_size(self):
        tool = MedicaidInfoTool(AsyncMock())
        text = '**medicaid_info {"state": SENTINEL-mi-bad}**'
        with _captured_logs() as (records, lines):
            _, _, handled = await tool.handle(text, "")
        assert handled is True
        _assert_absent_from_lines(lines, "SENTINEL-mi-bad")
        assert _has_message(records, "Invalid JSON in medicaid_info token")
        assert "Error executing Medicaid Info tool: JSONDecodeError" in _messages(
            records
        )

    @pytest.mark.asyncio
    async def test_processing_error_logs_class_name(self):
        tool = MedicaidInfoTool(AsyncMock())
        text = '**medicaid_info {"state": "California"}**'
        with _captured_logs() as (records, lines):
            with patch(
                "fighthealthinsurance.medicaid_api.get_medicaid_info",
                side_effect=RuntimeError("SENTINEL-mi-err"),
            ):
                await tool.handle(text, "")
        _assert_absent_from_messages(records, "SENTINEL-mi-err")
        assert "Error processing Medicaid info data: RuntimeError" in _messages(records)
        assert "Error executing Medicaid Info tool: RuntimeError" in _messages(records)


class TestMedicaidEligibilityToolLogs:
    @pytest.mark.asyncio
    async def test_payload_logged_as_sizes_and_error_as_class(self):
        tool = MedicaidEligibilityTool(AsyncMock())
        text = '**medicaid_eligibility {"state": "CA", "note": "SENTINEL-el-1"}**'
        match = tool.detect(text)
        assert match is not None
        with _captured_logs() as (records, lines):
            with patch(
                "fighthealthinsurance.medicaid_api.is_eligible",
                side_effect=RuntimeError("SENTINEL-el-err"),
            ):
                await tool.execute(match, text, "")
        _assert_absent_from_lines(lines, "SENTINEL-el-1")
        _assert_absent_from_messages(records, "SENTINEL-el-err")
        assert _has_message(
            records, "Medicaid eligibility tool call detected (match_chars="
        )
        assert _has_message(records, "Extracted eligibility payload (payload_chars=")
        assert "Parsed eligibility payload (type=dict)" in _messages(records)
        assert (
            "Error parsing params for medicaid eligibility tool: RuntimeError"
            in _messages(records)
        )

    @pytest.mark.asyncio
    async def test_invalid_payload_logged_as_size(self):
        tool = MedicaidEligibilityTool(AsyncMock())
        text = '**medicaid_eligibility {"state": SENTINEL-el-bad}**'
        match = tool.detect(text)
        assert match is not None
        with _captured_logs() as (records, lines):
            await tool.execute(match, text, "")
        _assert_absent_from_lines(lines, "SENTINEL-el-bad")
        assert _has_message(
            records,
            "Invalid JSON in medicaid_eligibility token (payload_chars=",
            "): JSONDecodeError",
        )


class TestAppealAndPriorAuthToolLogs:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "tool_cls,token,label",
        [
            (AppealTool, "create_or_update_appeal", "Appeal"),
            (PriorAuthTool, "create_or_update_prior_auth", "Prior Auth"),
        ],
    )
    async def test_invalid_payload_logged_as_size(self, tool_cls, token, label):
        tool = tool_cls(AsyncMock(), AsyncMock())
        text = f'{token} {{"patient_name": SENTINEL-rec-bad}}'
        with _captured_logs() as (records, lines):
            _, _, handled = await tool.handle(text, "", chat=MagicMock())
        assert handled is True
        _assert_absent_from_lines(lines, "SENTINEL-rec-bad")
        assert _has_message(records, f"Invalid JSON in {token} token (payload_chars=")
        assert f"Error executing {label} tool: JSONDecodeError" in _messages(records)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "tool_cls,token,helper,expected",
        [
            (
                AppealTool,
                "create_or_update_appeal",
                "_get_or_create_appeal",
                "Error processing appeal data: RuntimeError",
            ),
            (
                PriorAuthTool,
                "create_or_update_prior_auth",
                "_get_or_create_prior_auth",
                "Error processing prior auth data: RuntimeError",
            ),
        ],
    )
    async def test_processing_error_logs_class_name(
        self, tool_cls, token, helper, expected
    ):
        tool = tool_cls(AsyncMock(), AsyncMock())
        text = f'{token} {{"patient_name": "SENTINEL-rec-name"}}'
        with _captured_logs() as (records, lines):
            with patch.object(
                tool, helper, AsyncMock(side_effect=RuntimeError("SENTINEL-rec-err"))
            ):
                await tool.handle(text, "", chat=MagicMock())
        _assert_absent_from_lines(lines, "SENTINEL-rec-name")
        _assert_absent_from_messages(records, "SENTINEL-rec-err")
        assert expected in _messages(records)

    @pytest.mark.asyncio
    async def test_appeal_rejected_keys_logged_as_count(self):
        tool = AppealTool(AsyncMock(), AsyncMock())
        payload = {
            "SENTINEL-apkey-a": "value a",
            "_SENTINEL-apkey-b": "value b",
            "SENTINEL-apkey-c_id": "value c",
            "appeal_text": "Please reconsider.",
        }
        with _captured_logs() as (records, lines):
            await tool._update_appeal_fields(Appeal(), Denial(), payload)
        _assert_absent_from_lines(lines, "apkey")
        assert (
            "Skipped payload keys not settable on Appeal or Denial model "
            "(rejected_keys=3)"
        ) in _messages(records)

    @pytest.mark.asyncio
    async def test_prior_auth_rejected_keys_logged_as_count(self):
        tool = PriorAuthTool(AsyncMock(), AsyncMock())
        payload = {
            "sentinel-pakey-a": "value a",
            "_sentinel-pakey-b": "value b",
            "medication": "metformin",
        }
        with _captured_logs() as (records, lines):
            await tool._update_prior_auth_fields(PriorAuthRequest(), payload)
        _assert_absent_from_lines(lines, "pakey")
        assert (
            "Skipped payload keys not settable on Prior Auth model (rejected_keys=2)"
            in _messages(records)
        )


class TestDocFetcherToolLogs:
    @pytest.mark.asyncio
    async def test_invalid_payload_logged_as_size(self):
        tool = DocFetcherTool(AsyncMock())
        text = '**fetch_doc {"url": SENTINEL-df-bad}**'
        match = tool.detect(text)
        assert match is not None
        with _captured_logs() as (records, lines):
            await tool.execute(match, text, "")
        _assert_absent_from_lines(lines, "SENTINEL-df-bad")
        assert _has_message(
            records, "Invalid JSON in fetch_doc (payload_chars=", "): JSONDecodeError"
        )

    @pytest.mark.asyncio
    async def test_validation_error_logs_url_sizes_and_class_name(self):
        text = (
            '**fetch_doc {"url": "https://sentinel-df-host.local'
            '/SENTINEL-df-vpath/a.pdf?q=SENTINEL-df-vquery"}**'
        )
        status = AsyncMock()
        tool = DocFetcherTool(status)
        with _captured_logs() as (records, lines):
            await tool.execute(tool.detect(text), text, "")
        for sentinel in ("sentinel-df-host", "SENTINEL-df-vpath", "SENTINEL-df-vquery"):
            _assert_absent_from_lines(lines, sentinel)
        assert (
            "URL validation failed for fetch_doc (scheme=https, host=unlisted, "
            "path_chars=24, query_chars=20): ValueError"
        ) in _messages(records)
        # The user still sees why their link was refused.
        status.assert_awaited_with(
            "Cannot fetch document: Cannot fetch from local addresses: "
            "sentinel-df-host.local"
        )

    @pytest.mark.asyncio
    async def test_fetch_and_store_errors_log_class_names(self):
        text = (
            '**fetch_doc {"url": "https://example.org'
            '/SENTINEL-df-path/policy.pdf?name=SENTINEL-df-query"}**'
        )

        tool = DocFetcherTool(AsyncMock())
        tool.fetcher = MagicMock()
        tool.fetcher.fetch_and_extract_text = AsyncMock(
            side_effect=RuntimeError("SENTINEL-df-fetch")
        )
        with _captured_logs() as (records, lines):
            with patch(
                "fighthealthinsurance.chat.tools.doc_fetcher_tool.validate_url",
                new=AsyncMock(return_value=None),
            ):
                await tool.execute(tool.detect(text), text, "")
        _assert_absent_from_lines(lines, "SENTINEL-df-fetch")
        _assert_absent_from_lines(lines, "SENTINEL-df-path")
        _assert_absent_from_lines(lines, "SENTINEL-df-query")
        assert (
            "Failed to fetch document (scheme=https, host=unlisted, "
            "path_chars=28, query_chars=22): RuntimeError"
        ) in _messages(records)

        tool = DocFetcherTool(AsyncMock(), chat=MagicMock())
        tool.fetcher = MagicMock()
        tool.fetcher.fetch_and_extract_text = AsyncMock(
            return_value=("Fetched text SENTINEL-df-text", "pdf")
        )
        with _captured_logs() as (records, lines):
            with patch(
                "fighthealthinsurance.chat.tools.doc_fetcher_tool.validate_url",
                new=AsyncMock(return_value=None),
            ), patch(
                "fighthealthinsurance.chat.tools.doc_fetcher_tool.process_uploaded_document",
                new=AsyncMock(side_effect=RuntimeError("SENTINEL-df-store")),
            ):
                await tool.execute(tool.detect(text), text, "")
        _assert_absent_from_lines(lines, "SENTINEL-df-text")
        _assert_absent_from_lines(lines, "SENTINEL-df-store")
        _assert_absent_from_lines(lines, "SENTINEL-df-path")
        _assert_absent_from_lines(lines, "SENTINEL-df-query")
        assert "Failed to store fetched document: RuntimeError" in _messages(records)


class TestJsonFollowupToolLogs:
    @pytest.mark.asyncio
    async def test_invalid_payload_logged_as_size(self):
        tool = USPSTFLookupTool(AsyncMock())
        text = '**uspstf_lookup {"query": SENTINEL-us-bad}**'
        match = tool.detect(text)
        assert match is not None
        with _captured_logs() as (records, lines):
            await tool.execute(match, text, "")
        _assert_absent_from_lines(lines, "SENTINEL-us-bad")
        assert _has_message(
            records, "Invalid JSON in USPSTF Lookup tool call (length=", "): "
        )
        assert _has_message(records, "JSONDecodeError")

    @pytest.mark.asyncio
    async def test_lookup_error_logs_class_name(self):
        tool = USPSTFLookupTool(AsyncMock())
        text = '**uspstf_lookup {"query": "SENTINEL-us-query"}**'
        match = tool.detect(text)
        assert match is not None
        with _captured_logs() as (records, lines):
            with patch.object(
                tool, "run", AsyncMock(side_effect=RuntimeError("SENTINEL-us-err"))
            ):
                await tool.execute(match, text, "")
        _assert_absent_from_lines(lines, "SENTINEL-us-query")
        _assert_absent_from_messages(records, "SENTINEL-us-err")
        assert "USPSTF Lookup lookup failed: RuntimeError" in _messages(records)


class TestMedicaidGovToolLogs:
    @pytest.mark.asyncio
    async def test_invalid_payload_logs_class_name(self):
        tool = MedicaidGovLookupTool(AsyncMock())
        text = '**medicaid_gov_lookup {"query": SENTINEL-mg-bad}**'
        match = tool.detect(text)
        assert match is not None
        with _captured_logs() as (records, lines):
            await tool.execute(match, text, "")
        _assert_absent_from_lines(lines, "SENTINEL-mg-bad")
        assert "Invalid JSON in medicaid_gov_lookup: JSONDecodeError" in _messages(
            records
        )

    @pytest.mark.asyncio
    async def test_fetch_error_logs_class_name(self):
        tool = MedicaidGovLookupTool(AsyncMock())
        tool.fetcher = MagicMock()
        tool.fetcher.fetch_and_extract_text = AsyncMock(
            side_effect=RuntimeError("SENTINEL-mg-err")
        )
        text = '**medicaid_gov_lookup {"query": "work requirements"}**'
        match = tool.detect(text)
        assert match is not None
        with _captured_logs() as (records, lines):
            with patch.object(
                tool,
                "_resolve_target",
                return_value=(
                    "https://www.medicaid.gov/SENTINEL-mg-path/index.html"
                    "?q=SENTINEL-mg-query",
                    [],
                    "the URL provided",
                ),
            ):
                await tool.execute(match, text, "")
        _assert_absent_from_lines(lines, "SENTINEL-mg-err")
        _assert_absent_from_lines(lines, "SENTINEL-mg-path")
        _assert_absent_from_lines(lines, "SENTINEL-mg-query")
        assert (
            "medicaid_gov_lookup failed to fetch (scheme=https, "
            "host=www.medicaid.gov, path_chars=28, query_chars=19): RuntimeError"
        ) in _messages(records)

    @pytest.mark.parametrize(
        "url,expected",
        [
            (
                "https://sentinel-mg-host.example/SENTINEL-mg-offpath"
                "?x=SENTINEL-mg-offquery",
                "scheme=https, host=unlisted, path_chars=20, query_chars=22",
            ),
            (
                "http://www.medicaid.gov/SENTINEL-mg-offpath"
                "?x=SENTINEL-mg-offquery",
                "scheme=http, host=www.medicaid.gov, path_chars=20, "
                "query_chars=22",
            ),
        ],
    )
    def test_refused_url_logged_without_path_or_query(self, url, expected):
        with _captured_logs() as (records, lines):
            target = MedicaidGovLookupTool._resolve_target({"url": url})
        assert target == (None, [], "")
        for sentinel in ("sentinel-mg-host", "SENTINEL-mg-offpath", "SENTINEL-mg-offquery"):
            _assert_absent_from_lines(lines, sentinel)
        assert (
            f"medicaid_gov_lookup refused off-allowlist URL ({expected})"
            in _messages(records)
        )


class TestSearchToolPlaceholderLogs:
    @pytest.mark.asyncio
    async def test_pubmed_placeholder_logged_as_size(self):
        tool = PubMedTool(AsyncMock())
        text = "**pubmed_query: your search terms SENTINEL-pm**"
        match = tool.detect(text)
        assert match is not None
        with _captured_logs() as (records, lines):
            await tool.execute(match, text, "")
        _assert_absent_from_lines(lines, "SENTINEL-pm")
        assert _has_message(records, "Got placeholder PubMed query (query_chars=")

    @pytest.mark.asyncio
    async def test_pubmed_normalization_error_logs_class_name(self):
        rxnorm = MagicMock()
        rxnorm.normalize = AsyncMock(side_effect=RuntimeError("SENTINEL-pm-err"))
        tool = PubMedTool(AsyncMock(), rxnorm_tools=rxnorm)
        with _captured_logs() as (records, lines):
            assert await tool._normalize_drug_terms("metformin") == "metformin"
        _assert_absent_from_messages(records, "SENTINEL-pm-err")
        assert "RxNorm normalization failed: RuntimeError" in _messages(records)

    @pytest.mark.asyncio
    async def test_clinical_trials_placeholder_logged_as_size(self):
        tool = ClinicalTrialsTool(AsyncMock())
        text = "**clinical_trials_query: your search terms SENTINEL-ct**"
        match = tool.detect(text)
        assert match is not None
        with _captured_logs() as (records, lines):
            await tool.execute(match, text, "")
        _assert_absent_from_lines(lines, "SENTINEL-ct")
        assert _has_message(
            records, "Got placeholder ClinicalTrials query (query_chars="
        )

    @pytest.mark.asyncio
    async def test_rxnorm_placeholder_logged_as_size(self):
        tool = RxNormLookupTool(AsyncMock(), rxnorm_tools=MagicMock())
        text = "**rxnorm_lookup: drug name SENTINEL-rx**"
        match = tool.detect(text)
        assert match is not None
        with _captured_logs() as (records, lines):
            await tool.execute(match, text, "")
        _assert_absent_from_lines(lines, "SENTINEL-rx")
        assert _has_message(
            records, "Ignoring empty/placeholder rxnorm_lookup (name_chars="
        )


class TestScoringAndSummaryLogs:
    def test_document_name_bonus_logged_without_the_name(self):
        history = [
            {
                "role": "user",
                "content": "I've uploaded a document: SENTINEL-scorename.pdf "
                "(1,200 characters).",
            },
        ]
        reply = (
            "I read SENTINEL-scorename.pdf. It says the MRI needs prior "
            "authorization, and here is how to appeal that decision."
        )
        with _captured_logs() as (records, lines):
            score_llm_response(
                (reply, "context"),
                100,
                chat_history=history,
                current_message="What does it say?",
            )
        _assert_absent_from_lines(lines, "SENTINEL-scorename")
        assert (
            "Response references an uploaded document by name, boosting score"
            in _messages(records)
        )

    @pytest.mark.asyncio
    async def test_history_summary_error_logs_class_name(self):
        with _captured_logs() as (records, lines):
            with patch(
                "fighthealthinsurance.ml.ml_router.MLRouter.summarize_chat_history",
                new=AsyncMock(side_effect=RuntimeError("SENTINEL-sum-err")),
            ):
                result = await _summarize_history(
                    [{"role": "user", "content": "SENTINEL-sum-history"}],
                    "existing summary",
                )
        assert result == "existing summary"
        _assert_absent_from_lines(lines, "SENTINEL-sum-history")
        _assert_absent_from_lines(lines, "SENTINEL-sum-err")
        assert "Failed to summarize chat history: RuntimeError" in _messages(records)

    @pytest.mark.asyncio
    async def test_document_summary_errors_log_class_names(self):
        doc = MagicMock()
        doc.full_text = "Denial letter SENTINEL-doc-text"
        doc.asave = AsyncMock(side_effect=[None, RuntimeError("SENTINEL-doc-save")])
        chat_document = MagicMock()
        chat_document.objects.aget = AsyncMock(return_value=doc)
        chat_document.DoesNotExist = type("DoesNotExist", (Exception,), {})
        with _captured_logs() as (records, lines):
            with patch(
                "fighthealthinsurance.chat.document_processor.ChatDocument",
                chat_document,
            ), patch(
                "fighthealthinsurance.chat.document_processor.chunk_document",
                side_effect=RuntimeError("SENTINEL-doc-chunk"),
            ):
                await summarize_chunks(4242)
        _assert_absent_from_lines(lines, "SENTINEL-doc-text")
        _assert_absent_from_messages(records, "SENTINEL-doc-chunk")
        _assert_absent_from_lines(lines, "SENTINEL-doc-save")
        messages = _messages(records)
        assert "Failed to summarize ChatDocument 4242: RuntimeError" in messages
        assert "Could not persist failed status: RuntimeError" in messages


class TestChatConsumerLogs:
    @pytest.mark.asyncio
    async def test_enqueue_failure_logs_class_name(self):
        consumer = OngoingChatConsumer()
        consumer.chat_interface = MagicMock()
        consumer.chat_id = "sizelog-chat"
        with _captured_logs() as (records, lines):
            with patch(
                "fighthealthinsurance.websockets.enqueue_denied_items_analysis",
                new=AsyncMock(side_effect=RuntimeError("SENTINEL-ws-err")),
            ):
                await consumer.disconnect(1000)
        _assert_absent_from_messages(records, "SENTINEL-ws-err")
        assert (
            "Failed to enqueue denied-item analysis for chat sizelog-chat: "
            "RuntimeError"
        ) in _messages(records)

    @pytest.mark.asyncio
    async def test_invalid_frame_logged_as_size_and_class_name(self):
        consumer = MagicMock()
        consumer.send = AsyncMock()
        consumer.close = AsyncMock()
        frame = '{"message": "My MRI was denied SENTINEL-ws-frame'
        with _captured_logs() as (records, lines):
            data = await _parse_json_or_close(
                consumer, frame, consumer_name="ongoing chat"
            )
        assert data is None
        consumer.close.assert_awaited_once()
        _assert_absent_from_lines(lines, "SENTINEL-ws-frame")
        assert (
            "Invalid JSON received in ongoing chat websocket "
            f"(frame_chars={len(frame)}, error_pos=12): JSONDecodeError"
        ) in _messages(records)

    @pytest.mark.asyncio
    async def test_non_professional_user_logged_by_id_not_username(self):
        user = MagicMock()
        user.pk = 4321
        user.username = "SENTINEL-username@example.com"
        with _captured_logs() as (records, lines):
            chat_type, professional = await resolve_chat_type(
                user=user,
                is_authenticated=True,
                session_key=None,
                get_professional_user_fn=lambda u: None,
            )
        assert professional is None
        _assert_absent_from_lines(lines, "SENTINEL-username")
        assert (
            "User 4321 is not a professional user, treating as patient"
            in _messages(records)
        )

    # django_db: the consumer's dispatch sweeps DB connections on disconnect
    # (see test_chat_ws_error_shape.py).
    @pytest.mark.django_db
    @pytest.mark.asyncio
    async def test_receive_line_logs_key_prefix_and_bounded_client_values(self):
        # Each value carries a sentinel past the point its log form keeps.
        session_key = "sk8head-SENTINEL-ws-session-key"
        pad = "x" * 64
        chat_id = f"chat\nforged {pad}SENTINEL-ws-chat-id"
        appeal_id = f"appeal {pad}SENTINEL-ws-appeal-id"
        prior_auth_id = f"prior auth {pad}SENTINEL-ws-prior-auth-id"
        frame = {
            "content": "hello",
            "session_key": session_key,
            "chat_id": chat_id,
            "iterate_on_appeal": appeal_id,
            "iterate_on_prior_auth": prior_auth_id,
            "is_patient": "SENTINEL-ws-is-patient",
            "replay": "SENTINEL-ws-replay",
            "use_external_models": "SENTINEL-ws-external",
        }
        with _captured_logs() as (records, lines):
            # Stop the turn right after the receive line.
            with patch(
                "fighthealthinsurance.websockets.resolve_chat_type",
                side_effect=RuntimeError("stop"),
            ):
                communicator = WebsocketCommunicator(
                    OngoingChatConsumer.as_asgi(), "/ws/ongoing-chat/"
                )
                connected, _ = await communicator.connect()
                assert connected
                try:
                    await communicator.send_json_to(frame)
                    reply = await communicator.receive_json_from(timeout=10)
                finally:
                    await communicator.disconnect()
        assert "error" in reply
        # No line carries more than the key's first 8 characters.
        _assert_absent_from_lines(lines, session_key[:9])
        for sentinel in (
            "SENTINEL-ws-chat-id",
            "SENTINEL-ws-appeal-id",
            "SENTINEL-ws-prior-auth-id",
            "SENTINEL-ws-is-patient",
            "SENTINEL-ws-replay",
            "SENTINEL-ws-external",
        ):
            _assert_absent_from_lines(lines, sentinel)
        _assert_absent_from_messages(records, "\nforged")
        assert (
            "chat ws: msg_len=5 replay=True "
            f"chat_id={chat_id[:64]!r} "
            f"iterate_on_appeal={appeal_id[:64]!r} "
            f"iterate_on_prior_auth={prior_auth_id[:64]!r} "
            "is_patient=True session_key='sk8head-' microsite_slug=None "
            "use_external_models=True"
        ) in _messages(records)
