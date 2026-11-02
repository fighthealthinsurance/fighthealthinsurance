"""Gaps in the MCP server's tests (mcp_server.py): argument limits that keep
personal details out of the read-only tools, error paths for each tool, and
the shape of what each tool returns.

Drives the server through the SDK's own client, like test_mcp_server.py,
whose helpers this reuses (functions only, so no test class runs twice).
"""

from typing import Any
from unittest import mock

from asgiref.sync import async_to_sync
from django.test import TestCase, override_settings

from fighthealthinsurance import assistant_handoff, mcp_server, models
from tests.sync.test_mcp_server import (
    CHAT_ON,
    LETTER,
    SITE,
    call,
    chat_routes,
    mcp_session,
    prepare_routes,
    text_of,
)

# Obviously fake: a sentence an assistant might paste where only a few words
# belong. Named for Rebecca Lee Crumpler, the first Black woman to earn an
# MD in the United States.
PERSONAL = (
    "Rebecca Lee Crumpler, member ID ZZ0000000, born 1831-02-08, was denied "
    "an MRI of the lower back as not medically necessary"
)


def assert_short_refusal(test: TestCase, result: Any, expected: str) -> str:
    """A refusal that says what was wrong, quotes none of the input and
    names no validation library."""
    test.assertTrue(result.isError)
    said = text_of(result)
    test.assertIn(expected, said)
    test.assertNotIn("Crumpler", said)
    test.assertNotIn("ZZ0000000", said)
    test.assertNotIn("pydantic", said)
    test.assertLess(len(said), 400)
    return said


class FreeTextLimitsTest(TestCase):
    """Every read-only tool that takes words takes only a few: a pasted
    letter or medical history is refused, and the refusal never repeats it."""

    async def test_explain_denial_reason_refuses_a_sentence_about_the_person(self):
        result = await call("explain_denial_reason", {"phrase": PERSONAL})
        assert_short_refusal(self, result, "phrase is too long")

    async def test_explain_denial_reason_refuses_a_one_letter_phrase(self):
        result = await call("explain_denial_reason", {"phrase": "x"})
        assert_short_refusal(self, result, "phrase is too short")

    async def test_the_checklist_refuses_a_sentence_as_the_denial_reason(self):
        result = await call("get_appeal_checklist", {"denial_reason": PERSONAL})
        assert_short_refusal(self, result, "denial_reason is too long")

    async def test_financial_help_refuses_a_medical_history_as_the_condition(self):
        result = await call("find_financial_help", {"condition": PERSONAL})
        assert_short_refusal(self, result, "condition is too long")

    async def test_financial_help_refuses_a_sentence_as_the_drug(self):
        result = await call("find_financial_help", {"drug": PERSONAL})
        assert_short_refusal(self, result, "drug is too long")

    async def test_find_treatment_guide_refuses_a_sentence_about_the_person(self):
        result = await call("find_treatment_guide", {"query": PERSONAL})
        assert_short_refusal(self, result, "query is too long")

    async def test_search_site_refuses_a_one_letter_query(self):
        result = await call("search_site", {"query": "a"})
        assert_short_refusal(self, result, "query is too short")

    async def test_insurer_contacts_refuses_a_sentence_as_the_insurer(self):
        result = await call(
            "find_insurer_appeal_contacts", {"insurer": PERSONAL + " by Acme"}
        )
        assert_short_refusal(self, result, "insurer is too long")

    async def test_a_state_argument_refuses_a_sentence(self):
        result = await call("get_state_help", {"state": PERSONAL})
        assert_short_refusal(self, result, "state is too long")

    async def test_start_appeal_refuses_a_topic_past_its_limit(self):
        result = await call("start_appeal", {"topic": "a" * 81})
        assert_short_refusal(self, result, "topic is too long")

    async def test_start_appeal_refuses_a_capitalised_slug_with_the_topic_help(self):
        result = await call("start_appeal", {"topic": "MRI-Denial"})
        assert_short_refusal(self, result, mcp_server.TOPIC_HELP)

    async def test_a_number_where_text_belongs_is_refused_as_not_text(self):
        result = await call("explain_denial_reason", {"phrase": 12345})
        assert_short_refusal(self, result, "phrase must be text")


class StateArgumentTest(TestCase):
    async def test_a_page_slug_finds_the_state(self):
        result = await call("get_state_help", {"state": "new-york"})
        self.assertFalse(result.isError, text_of(result))
        self.assertEqual(result.structuredContent["abbreviation"], "NY")

    async def test_extra_spaces_and_case_in_a_name_still_find_the_state(self):
        result = await call("get_state_help", {"state": "  new   YORK "})
        self.assertFalse(result.isError, text_of(result))
        self.assertEqual(result.structuredContent["abbreviation"], "NY")

    async def test_dc_is_covered(self):
        result = await call("get_state_help", {"state": "DC"})
        self.assertFalse(result.isError, text_of(result))
        self.assertEqual(result.structuredContent["abbreviation"], "DC")

    async def test_a_territory_says_territories_are_not_covered_yet(self):
        result = await call("get_state_help", {"state": "Guam"})
        self.assertTrue(result.isError)
        self.assertIn("territories are not yet", text_of(result))

    async def test_every_tool_with_a_state_refuses_an_unknown_one(self):
        cases = {
            "get_appeal_rights": {"plan_source": "Employer -- Private", "state": "Atlantis"},
            "get_appeal_checklist": {"state": "Atlantis"},
            "find_financial_help": {"state": "Atlantis"},
        }
        async with mcp_session() as session:
            for tool, arguments in cases.items():
                with self.subTest(tool=tool):
                    result = await session.call_tool(tool, arguments)
                    self.assertTrue(result.isError)
                    self.assertIn("Unknown state 'Atlantis'", text_of(result))

    async def test_state_help_always_names_the_federal_contact_for_self_funded_plans(
        self,
    ):
        result = await call("get_state_help", {"state": "TX"})
        federal = result.structuredContent["self_funded_employer_plans"]
        self.assertEqual(federal["name"], mcp_server.DOL_EBSA_NAME)
        self.assertEqual(federal["url"], mcp_server.DOL_EBSA_URL)
        self.assertIn("ERISA", federal["note"])


class ExplainDenialReasonShapeTest(TestCase):
    async def test_a_named_reason_never_lists_itself_among_the_other_matches(self):
        result = await call(
            "explain_denial_reason", {"phrase": "not medically necessary"}
        )
        data = result.structuredContent
        self.assertTrue(data["exact_match"])
        self.assertNotIn(data["reason"], data.get("other_matches", []))
        self.assertLessEqual(len(data.get("other_matches", [])), 3)
        self.assertTrue(data["url"].startswith(f"{SITE}/denial-language"))

    async def test_counter_arguments_are_capped(self):
        result = await call(
            "explain_denial_reason", {"phrase": "not medically necessary"}
        )
        self.assertLessEqual(
            len(result.structuredContent["how_to_counter"]), mcp_server.MAX_LIST_ITEMS
        )


class AppealRightsShapeTest(TestCase):
    async def test_with_a_state_the_link_is_the_state_page(self):
        result = await call(
            "get_appeal_rights", {"plan_source": "Employer -- Private", "state": "Ohio"}
        )
        self.assertFalse(result.isError, text_of(result))
        data = result.structuredContent
        self.assertEqual(data["state"], "Ohio")
        self.assertEqual(data["url"], f"{SITE}/state-help/ohio/")

    async def test_without_a_state_the_link_is_coverage_changes(self):
        result = await call("get_appeal_rights", {"plan_source": "Employer -- Private"})
        data = result.structuredContent
        self.assertNotIn("state", data)
        self.assertTrue(data["url"].startswith(SITE))
        self.assertIn("coverage-changes", data["url"])

    async def test_plan_source_is_required(self):
        result = await call("get_appeal_rights", {})
        self.assertTrue(result.isError)
        self.assertIn("plan_source", text_of(result))


class AppealChecklistShapeTest(TestCase):
    async def test_with_nothing_given_it_still_lists_what_to_gather_and_the_link(self):
        result = await call("get_appeal_checklist", {})
        self.assertFalse(result.isError, text_of(result))
        data = result.structuredContent
        self.assertTrue(data["for_the_form"])
        self.assertEqual(data["start"]["url"], f"{SITE}/scan")
        self.assertNotIn("state_help", data)
        self.assertNotIn("for_this_denial_reason", data)

    async def test_with_the_appeal_tools_off_start_names_neither_of_them(self):
        result = await call("get_appeal_checklist", {})
        start = result.structuredContent["start"]
        self.assertEqual(set(start), {"url", "how"})


class InsurerContactsShapeTest(TestCase):
    fixtures = ["plan_source", "insurance_companies"]

    async def test_extra_spaces_in_the_name_still_find_the_insurer(self):
        result = await call("find_insurer_appeal_contacts", {"insurer": "  aetna  "})
        self.assertFalse(result.isError, text_of(result))
        self.assertEqual(result.structuredContent["company"]["name"], "Aetna")

    async def test_a_found_insurer_carries_the_start_link_and_no_lookup_flag(self):
        result = await call("find_insurer_appeal_contacts", {"insurer": "Aetna"})
        data = result.structuredContent
        self.assertNotIn("found", data)
        self.assertNotIn("suggestions", data)
        self.assertEqual(data["url"], f"{SITE}/scan")
        self.assertIn("denial letter", data["note"])

    async def test_a_state_with_no_plans_on_file_says_the_company_contacts_apply(
        self,
    ):
        result = await call(
            "find_insurer_appeal_contacts", {"insurer": "Aetna", "state": "WY"}
        )
        self.assertFalse(result.isError, text_of(result))
        data = result.structuredContent
        if data.get("plans"):
            self.skipTest("the fixture now has Wyoming plans for Aetna")
        self.assertIn("No Wyoming plans on file", data["plans_note"])

    async def test_an_unknown_insurer_with_a_known_word_offers_close_matches(self):
        # The name lookup is made to miss, so the suggestions come from the
        # words in the name alone.
        with mock.patch.object(
            mcp_server, "resolve_insurance_company_by_name", return_value=None
        ):
            result = await call(
                "find_insurer_appeal_contacts", {"insurer": "Zzyzx Aetna Mutual"}
            )
        said = text_of(result)
        self.assertTrue(result.isError)
        self.assertIn("Close matches:", said)
        self.assertIn("Aetna", said)


class FinancialHelpShapeTest(TestCase):
    async def test_a_condition_alone_is_enough(self):
        result = await call("find_financial_help", {"condition": "cancer"})
        self.assertFalse(result.isError, text_of(result))
        self.assertTrue(result.structuredContent["url"].startswith(SITE))

    async def test_a_state_alone_gives_its_medicaid_agency(self):
        result = await call("find_financial_help", {"state": "California"})
        self.assertFalse(result.isError, text_of(result))
        data = result.structuredContent
        self.assertIn("agency", data["state_medicaid"])
        self.assertEqual(data["url"], f"{SITE}/other-resources")

    async def test_a_drug_with_a_guide_links_to_the_guide(self):
        result = await call("find_financial_help", {"drug": "Ozempic"})
        self.assertFalse(result.isError, text_of(result))
        self.assertIn("/microsite/", result.structuredContent["url"])

    async def test_every_list_is_capped(self):
        result = await call("find_financial_help", {"condition": "cancer"})
        data = result.structuredContent
        for key in (
            "condition_programs",
            "manufacturer_programs",
            "general_directories",
            "safety_net",
        ):
            with self.subTest(key=key):
                self.assertLessEqual(len(data.get(key, [])), mcp_server.MAX_LIST_ITEMS)


class SearchSiteShapeTest(TestCase):
    async def test_nothing_found_gives_a_hint_and_an_empty_list(self):
        result = await call("search_site", {"query": "zzqx vvqz"})
        self.assertFalse(result.isError, text_of(result))
        data = result.structuredContent
        self.assertEqual(data["results"], [])
        self.assertIn("find_treatment_guide", data["hint"])

    async def test_a_broad_query_is_capped_and_every_result_is_on_the_site(self):
        result = await call("search_site", {"query": "medicare"})
        results = result.structuredContent["results"]
        self.assertTrue(results)
        self.assertLessEqual(len(results), mcp_server.MAX_SEARCH_RESULTS)
        for item in results:
            with self.subTest(url=item["url"]):
                self.assertTrue(item["url"].startswith(SITE))
                self.assertIn("title", item)

    async def test_a_query_that_finds_something_carries_no_hint(self):
        result = await call("search_site", {"query": "turning 26"})
        self.assertNotIn("hint", result.structuredContent)


class GetPageShapeTest(TestCase):
    async def test_an_unknown_glossary_term_points_to_search(self):
        result = await call("get_page", {"url": "/glossary/no-such-term-zzqx/"})
        self.assertTrue(result.isError)
        self.assertIn("search_site", text_of(result))

    async def test_a_long_page_is_cut_and_says_so(self):
        long_page = "# A long page\n\n" + "word " * mcp_server.MAX_PAGE_CHARS
        with mock.patch.object(
            mcp_server, "_get_in_process", return_value=(200, long_page)
        ):
            result = await call("get_page", {"url": f"{SITE}/about-us"})
        self.assertFalse(result.isError, text_of(result))
        data = result.structuredContent
        self.assertTrue(data["truncated"])
        self.assertEqual(len(data["markdown"]), mcp_server.MAX_PAGE_CHARS)
        self.assertEqual(data["title"], "A long page")

    async def test_a_short_page_says_it_is_whole(self):
        with mock.patch.object(
            mcp_server, "_get_in_process", return_value=(200, "# About\n\nHello.\n")
        ):
            result = await call("get_page", {"url": f"{SITE}/about-us"})
        self.assertFalse(result.structuredContent["truncated"])

    async def test_a_missing_page_sends_the_person_to_the_site_instead(self):
        with mock.patch.object(mcp_server, "_get_in_process", return_value=(404, "")):
            result = await call("get_page", {"url": f"{SITE}/about-us"})
        self.assertTrue(result.isError)
        self.assertIn(f"{SITE}/about-us", text_of(result))


class RegisteredToolNamesTest(TestCase):
    def test_with_every_appeal_flag_off_only_the_read_only_tools_are_named(self):
        with override_settings(
            MCP_SERVER_ENABLED=True,
            MCP_PREPARE_APPEAL_ENABLED=False,
            MCP_DRAFT_IN_CHAT_ENABLED=False,
        ):
            names = mcp_server.registered_tool_names()
        self.assertEqual(names, sorted(names))
        self.assertNotIn("prepare_appeal", names)
        self.assertNotIn("draft_appeal_in_chat", names)
        self.assertIn("start_appeal", names)
        self.assertIn("get_page", names)

    def test_with_the_chat_path_on_its_three_tools_are_named(self):
        with override_settings(**CHAT_ON):
            names = set(mcp_server.registered_tool_names())
        self.assertLessEqual(
            {
                "prepare_appeal",
                "draft_appeal_in_chat",
                "get_appeal_drafts",
                "answer_appeal_questions",
            },
            names,
        )


class ChatPathRefusalsTest(TestCase):
    """The chat path's own refusals: each short, none quoting the input, and
    none leaving a draft or link behind."""

    def setUp(self):
        self.enterContext(override_settings(**CHAT_ON))

    def _call(self, tool: str, arguments: dict[str, Any]):
        return async_to_sync(call)(tool, arguments, routes=chat_routes())

    def assertNothingKept(self):
        self.assertFalse(models.AssistantDraft.objects.exists())
        self.assertFalse(models.AssistantHandoff.objects.exists())

    def test_a_too_short_letter_keeps_nothing(self):
        result = self._call(
            "draft_appeal_in_chat", {"letter_text": "Crumpler denied."}
        )
        assert_short_refusal(self, result, "too short")
        self.assertNothingKept()

    def test_a_two_line_condition_keeps_nothing(self):
        result = self._call(
            "draft_appeal_in_chat",
            {"letter_text": LETTER, "condition": "Crumpler\nmigraine"},
        )
        assert_short_refusal(self, result, "condition must be one line")
        self.assertNothingKept()

    def test_at_capacity_it_points_to_start_appeal_and_keeps_nothing(self):
        with mock.patch.object(
            mcp_server.assistant_draft_tools,
            "start",
            side_effect=assistant_handoff.HandoffCapacityError(),
        ):
            result = self._call("draft_appeal_in_chat", {"letter_text": LETTER})
        self.assertTrue(result.isError)
        self.assertIn(mcp_server.AT_CAPACITY, text_of(result))
        self.assertNotIn("Acme", text_of(result))
        self.assertFalse(models.AssistantDraft.objects.exists())

    def test_get_appeal_drafts_refuses_a_wait_past_its_limit(self):
        result = self._call(
            "get_appeal_drafts",
            {"draft_id": "x" * 43, "wait": mcp_server.MAX_DRAFT_WAIT_SECONDS + 1},
        )
        self.assertTrue(result.isError)
        self.assertIn("wait", text_of(result))

    def test_get_appeal_drafts_refuses_a_negative_wait(self):
        result = self._call("get_appeal_drafts", {"draft_id": "x" * 43, "wait": -1})
        self.assertTrue(result.isError)
        self.assertIn("wait", text_of(result))

    def test_get_appeal_drafts_refuses_an_overlong_draft_id(self):
        result = self._call("get_appeal_drafts", {"draft_id": "x" * 65})
        assert_short_refusal(self, result, "draft_id is too long")

    def test_answer_appeal_questions_refuses_more_answers_than_a_draft_holds(self):
        answers = [
            {"name": f"q_{i}", "value": "yes"}
            for i in range(mcp_server.assistant_drafts.MAX_ANSWERS + 1)
        ]
        result = self._call(
            "answer_appeal_questions", {"draft_id": "x" * 43, "answers": answers}
        )
        self.assertTrue(result.isError)
        self.assertIn("answers", text_of(result))

    def test_answer_appeal_questions_refuses_an_undeclared_key_in_an_answer(self):
        result = self._call(
            "answer_appeal_questions",
            {
                "draft_id": "x" * 43,
                "answers": [
                    {"name": "q_0", "value": "yes", "member_id": "ZZ0000000"}
                ],
            },
        )
        self.assertTrue(result.isError)
        self.assertNotIn("ZZ0000000", text_of(result))

    def test_an_answer_too_long_says_to_shorten_it_with_the_person(self):
        value = "Crumpler " * (mcp_server.assistant_drafts.ANSWER_MAX_CHARS // 9 + 1)
        result = self._call(
            "answer_appeal_questions",
            {"draft_id": "x" * 43, "answers": [{"name": "q_0", "value": value}]},
        )
        assert_short_refusal(self, result, "shorten it with the person")

    def test_answers_for_an_unknown_draft_file_nothing_and_signal_nothing(self):
        with mock.patch(
            "fighthealthinsurance.temporal_client.signal_assistant_answers_filed",
            mock.AsyncMock(),
        ) as signal:
            result = self._call(
                "answer_appeal_questions",
                {"draft_id": "x" * 43, "answers": [{"name": "q_0", "value": "yes"}]},
            )
        signal.assert_not_awaited()
        self.assertFalse(result.isError, text_of(result))
        self.assertEqual(result.structuredContent["status"], "not_found")
        self.assertEqual(result.structuredContent["next"], "stop_and_tell_the_person")


class PrepareAppealShapeTest(TestCase):
    def test_prepare_appeal_refuses_an_undeclared_member_id(self):
        result = async_to_sync(call)(
            "prepare_appeal",
            {"letter_text": LETTER, "member_id": "ZZ0000000"},
            routes=prepare_routes(),
        )
        self.assertTrue(result.isError)
        self.assertNotIn("ZZ0000000", text_of(result))
        self.assertFalse(models.AssistantHandoff.objects.exists())
