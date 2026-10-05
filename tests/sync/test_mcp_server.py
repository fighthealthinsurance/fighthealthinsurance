"""The read-only MCP server for AI assistants at /mcp (mcp_server.py).

Every test drives the server the way an assistant would: the MCP SDK's own
client, over HTTP, through the same dispatcher asgi.py mounts in front of
Django, all in-process (httpx's ASGI transport, no sockets). The SDK's
session manager can start only once per server, so each test builds a fresh
one and enters its lifespan itself, as uvicorn would.
"""

import ast
import contextlib
import json
import logging
import os
import re
import socket
import time
from fnmatch import fnmatch
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock, skipIf

import httpx
import yaml
from asgiref.sync import async_to_sync
from channels.routing import ProtocolTypeRouter
from datetime import datetime, timedelta
from django.core.handlers.asgi import ASGIHandler
from django.db import DatabaseError, connection
from prometheus_client import REGISTRY
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import NoReverseMatch, reverse
from django.utils import timezone
from loguru import logger
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from fighthealthinsurance import (
    agent_docs,
    assistant_handoff,
    glossary,
    mcp_server,
    models,
)
from fighthealthinsurance import settings as fhi_settings
from fighthealthinsurance.regulatory_citations import FEDERAL_HOOKS, STATE_HOOKS

SITE = "https://www.fighthealthinsurance.com"
BASE_URL = "http://localhost"

MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "fhi-test", "version": "0"},
    },
}

_DJANGO_APP = None


def django_app():
    """One Django ASGI app for the module: it loads the middleware once.
    ASGIHandler() rather than get_asgi_application(), which would run
    django.setup() again and, with it, a logging config that switches off
    every logger that already exists, the MCP SDK's included."""
    global _DJANGO_APP
    if _DJANGO_APP is None:
        _DJANGO_APP = ASGIHandler()
    return _DJANGO_APP


@contextlib.asynccontextmanager
async def running_app(routes=None):
    """The dispatcher asgi.py mounts, with the MCP lifespan running."""
    routes = routes or mcp_server.mcp_asgi_routes(django_app())
    app = ProtocolTypeRouter(routes)
    lifespan_app = routes.get("lifespan")
    async with contextlib.AsyncExitStack() as stack:
        if lifespan_app is not None:
            await stack.enter_async_context(
                lifespan_app.router.lifespan_context(lifespan_app)
            )
        transport = httpx.ASGITransport(app=app)
        http = await stack.enter_async_context(
            httpx.AsyncClient(transport=transport, base_url=BASE_URL)
        )
        yield http


@contextlib.asynccontextmanager
async def mcp_session(routes=None):
    """An initialized SDK client session against a fresh server."""
    async with running_app(routes) as http:
        async with streamable_http_client(f"{BASE_URL}/mcp", http_client=http) as (
            read,
            write,
            _,
        ):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                session.init_result = init  # type: ignore[attr-defined]
                yield session


async def call(tool: str, arguments: dict[str, Any], routes=None):
    async with mcp_session(routes) as session:
        return await session.call_tool(tool, arguments)


def text_of(result) -> str:
    return "\n".join(getattr(c, "text", "") for c in result.content)


PLAN_SOURCES = list(mcp_server.PlanSourceName.__args__)
TEMPLATES = Path(mcp_server.__file__).parent / "templates"
STATIC_JS = Path(mcp_server.__file__).parent / "static" / "js"


def writes_in(queries: CaptureQueriesContext) -> list[str]:
    return [
        q["sql"]
        for q in queries.captured_queries
        if q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
    ]


class ProtocolTest(TestCase):
    async def test_initialize_names_the_server_and_carries_the_welcome(self):
        async with mcp_session() as session:
            init = session.init_result
        self.assertEqual(init.serverInfo.name, "fight-health-insurance")
        self.assertEqual(init.instructions, mcp_server.INSTRUCTIONS)

    def test_the_welcome_says_what_fhi_is_and_keeps_health_details_on_the_site(
        self,
    ):
        welcome = mcp_server.INSTRUCTIONS
        self.assertIn("Fight Health Insurance is a free tool", welcome)
        self.assertIn("belong on the site", welcome)
        for kept_out in ("denial letter", "names", "member IDs", "medical history"):
            with self.subTest(kept_out=kept_out):
                self.assertIn(kept_out, welcome)

    def test_the_welcome_allows_a_short_treatment_word_for_the_guide_tools(self):
        # find_treatment_guide and find_financial_help ask for one, so the
        # welcome must not tell assistants to keep every medical word out.
        welcome = mcp_server.INSTRUCTIONS
        self.assertIn("A short treatment or condition word", welcome)
        self.assertIn("is fine to share", welcome)
        self.assertNotIn("medical details belong", welcome)

    async def test_every_tool_is_read_only_and_closed_world(self):
        async with mcp_session() as session:
            tools = (await session.list_tools()).tools
        self.assertEqual(len(tools), 10)
        for tool in tools:
            with self.subTest(tool=tool.name):
                self.assertTrue(tool.annotations.readOnlyHint)
                self.assertFalse(tool.annotations.destructiveHint)
                self.assertFalse(tool.annotations.openWorldHint)

    async def test_every_tool_has_a_title_and_a_description_that_says_fhi_is_free(
        self,
    ):
        async with mcp_session() as session:
            tools = (await session.list_tools()).tools
        for tool in tools:
            with self.subTest(tool=tool.name):
                self.assertTrue(tool.title)
                self.assertGreater(len(tool.description or ""), 150)
                self.assertIn("free", tool.description.lower())

    async def test_every_tool_schema_refuses_undeclared_arguments(self):
        async with mcp_session() as session:
            tools = (await session.list_tools()).tools
        for tool in tools:
            with self.subTest(tool=tool.name):
                self.assertIs(tool.inputSchema.get("additionalProperties"), False)

    async def test_an_undeclared_argument_is_an_error_not_silently_dropped(self):
        result = await call("search_site", {"query": "turning 26", "letter": "x"})
        self.assertTrue(result.isError)
        self.assertIn("does not take letter", text_of(result))


class StartAppealTest(TestCase):
    async def test_start_appeal_with_the_handoff_off_never_names_prepare_appeal(self):
        data = (await call("start_appeal", {})).structuredContent
        self.assertNotIn("prepare_appeal", data["privacy"])

    async def test_start_appeal_says_the_link_opens_the_first_step(self):
        data = (await call("start_appeal", {})).structuredContent
        self.assertIn("opens the first step", data["tell_the_person"])
        self.assertIn("aren't written yet", data["tell_the_person"])

    async def test_start_appeal_with_no_topic_links_to_the_intake(self):
        result = await call("start_appeal", {})
        self.assertFalse(result.isError)
        self.assertEqual(result.structuredContent["url"], f"{SITE}/scan")
        self.assertGreaterEqual(len(result.structuredContent["steps"]), 5)

    async def test_start_appeal_with_a_guide_topic_prefills_the_treatment(self):
        result = await call("start_appeal", {"topic": "mri-denial"})
        self.assertFalse(result.isError)
        url = result.structuredContent["url"]
        self.assertTrue(url.startswith(f"{SITE}/scan?"), url)
        self.assertIn("microsite_slug=mri-denial", url)
        self.assertIn("default_procedure=MRI%20Scan", url)

    async def test_start_appeal_with_an_unknown_topic_is_an_error(self):
        result = await call("start_appeal", {"topic": "no-such-guide"})
        self.assertTrue(result.isError)
        self.assertIn("Unknown topic", text_of(result))

    async def test_start_appeal_takes_nothing_but_a_topic(self):
        async with mcp_session() as session:
            tools = {t.name: t for t in (await session.list_tools()).tools}
        schema = tools["start_appeal"].inputSchema
        self.assertEqual(set(schema["properties"]), {"topic"})
        self.assertEqual(schema.get("required", []), [])

    async def test_start_appeal_refuses_denial_letter_text(self):
        letter = (
            "Dear member, your claim for an MRI was denied as not medically necessary."
        )
        result = await call("start_appeal", {"denial_text": letter})
        self.assertTrue(result.isError)
        self.assertNotIn("your claim for an MRI", text_of(result))

    async def test_start_appeal_refuses_free_text_in_the_topic(self):
        # The pattern, not the unknown-slug check, refuses it: free text
        # would also fail as an unknown topic, so assert the pattern's message.
        result = await call(
            "start_appeal", {"topic": "My insurer denied my MRI because of my back"}
        )
        self.assertTrue(result.isError)
        self.assertIn("takes a treatment-guide slug", text_of(result))
        self.assertNotIn("Unknown topic", text_of(result))

    async def test_a_treatment_name_as_the_topic_points_to_find_treatment_guide(self):
        result = await call("start_appeal", {"topic": "Ozempic"})
        self.assertTrue(result.isError)
        self.assertIn("find_treatment_guide", text_of(result))
        self.assertNotIn("pydantic", text_of(result))

    async def test_start_appeal_privacy_says_what_the_scan_page_shows(self):
        result = await call("start_appeal", {})
        data = result.structuredContent
        said = data["privacy"] + " " + " ".join(data["steps"])
        self.assertIn("'Use outside AI services to get more appeal drafts'", said)
        self.assertIn("ticked by default", said)
        self.assertIn("can untick it", said)
        self.assertIn("name and street address the page asks for stay in", said)
        self.assertIn("joins the mailing list, which keeps the real email and", said)
        self.assertIn("kept with their real email for the mailing list", said)
        self.assertIn("The ZIP code is sent", said)
        self.assertIn("works out the state from it, and keeps only its first", said)
        # The site is dropping the example companies (PR #1126).
        for company in ("Google", "Anthropic", "OpenAI"):
            with self.subTest(company=company):
                self.assertNotIn(company, said)

    def test_the_scan_page_still_matches_what_start_appeal_says(self):
        """Binds the privacy text to the page and its scripts: the box's
        wording and default, which fields have no name (so stay in the
        browser) and which one is sent."""
        page = (TEMPLATES / "scrub.html").read_text()
        box = re.search(r'<input[^>]*id="use_external_models"[^>]*>', page)
        self.assertIsNotNone(box)
        self.assertRegex(box.group(0), r"\bchecked\b")
        self.assertIn("Use outside AI services to get more appeal drafts.", page)
        for kept in ("store_fname", "store_lname", "store_street"):
            with self.subTest(field=kept):
                field = re.search(rf'<input[^>]*id="{kept}"[^>]*>', page)
                self.assertIsNotNone(field)
                self.assertNotIn("name=", field.group(0))
        zip_field = re.search(r'<input[^>]*id="store_zip"[^>]*>', page)
        self.assertIn('name="zip"', zip_field.group(0))
        # The name goes to the server only for the mailing list, and the
        # Remove personal details button uses the stored fields.
        form_script = (STATIC_JS / "scrub_client_side_form.ts").read_text()
        self.assertIn("if (form.subscribe.checked)", form_script)
        scrub_script = (STATIC_JS / "scrub_scrub.ts").read_text()
        self.assertIn('node.id.startsWith("store_")', scrub_script)
        # The mailing list keeps the real email and the name.
        self.assertIn("If you join the mailing list, we keep your name and", page)
        # The state comes from the whole ZIP code, then only ZIP3 is stored.
        intake = (TEMPLATES.parent / "common_view_logic.py").read_text()
        self.assertIn("cls.zip_engine.by_zipcode(zip).state", intake)
        self.assertIn("denial.service_zip = zip[:3]", intake)

    async def medicare_chat_start(self):
        guide = SimpleNamespace(
            slug="some-medicare-guide",
            title="Some Medicare guide",
            default_procedure="Home health care",
            default_condition=None,
            medicare=True,
            wip=False,
        )
        with mock.patch.object(
            mcp_server.microsites, "get_microsite", return_value=guide
        ):
            result = await call("start_appeal", {"topic": "some-medicare-guide"})
        self.assertFalse(result.isError)
        return result.structuredContent

    async def test_start_appeal_for_a_medicare_guide_says_the_link_opens_the_chat(
        self,
    ):
        data = await self.medicare_chat_start()
        self.assertIn("opens Fight Health Insurance's chat", data["tell_the_person"])
        self.assertNotIn("first step of the appeal form", data["tell_the_person"])

    async def test_start_appeal_for_a_medicare_guide_goes_to_the_chat(self):
        url = (await self.medicare_chat_start())["url"]
        self.assertTrue(url.startswith(f"{SITE}/chat?"), url)
        self.assertIn("medicare=true", url)

    async def test_the_chat_path_privacy_says_what_the_consent_page_asks_for(self):
        data = await self.medicare_chat_start()
        said = data["privacy"] + " " + " ".join(data["steps"])
        self.assertIn("a first and last name and an email address (required)", said)
        self.assertIn("a phone number, street address, city, state and ZIP", said)
        self.assertIn("such as their phone number, out of their messages", said)
        self.assertIn("The form is also sent to the site", said)
        self.assertIn("'Let outside AI services help answer' is on by default", said)
        for company in ("Google", "Anthropic", "OpenAI"):
            with self.subTest(company=company):
                self.assertNotIn(company, said)

    def test_the_chat_consent_page_still_matches_what_start_appeal_says(self):
        """Binds the chat path's privacy text to the consent form: which
        fields are required, the outside AI box's default, the note about
        the phone number, and what the server keeps from the form."""
        from fighthealthinsurance.chat_forms import UserConsentForm

        fields = UserConsentForm().fields
        for name in ("first_name", "last_name", "email"):
            with self.subTest(field=name):
                self.assertTrue(fields[name].required)
        for name in ("phone", "address", "city", "state", "zip_code"):
            with self.subTest(field=name):
                self.assertFalse(fields[name].required)
        self.assertIs(fields["use_external_models"].initial, True)
        partial = (TEMPLATES / "partials" / "user_consent_form_fields.html").read_text()
        self.assertIn("We try to remove the name, email, street address, city", partial)
        self.assertIn("such as your phone number", partial)
        views = (TEMPLATES.parent / "views.py").read_text()
        self.assertIn('session["email"] = email', views)
        self.assertIn('"phone": form.cleaned_data.get("phone", "")', views)


class ExplainDenialReasonTest(TestCase):
    async def test_explain_denial_reason_matches_a_common_phrase(self):
        result = await call("explain_denial_reason", {"phrase": "medical necessity"})
        self.assertFalse(result.isError)
        self.assertTrue(result.structuredContent["exact_match"])
        self.assertEqual(result.structuredContent["reason"], "Not medically necessary")
        self.assertEqual(result.structuredContent["url"], f"{SITE}/denial-language/")

    async def test_one_distinctive_word_is_still_a_match(self):
        result = await call("explain_denial_reason", {"phrase": "experimental"})
        self.assertTrue(result.structuredContent["exact_match"])
        self.assertEqual(
            result.structuredContent["reason"], "Experimental or investigational"
        )

    async def test_a_weak_match_lists_candidates_instead_of_one_answer(self):
        for phrase in (
            "not covered",
            "no prior auth was obtained",
            "benefit exclusion",
        ):
            with self.subTest(phrase=phrase):
                result = await call("explain_denial_reason", {"phrase": phrase})
                self.assertFalse(result.isError)
                data = result.structuredContent
                self.assertFalse(data["exact_match"])
                self.assertNotIn("reason", data)
                self.assertNotIn("how_to_counter", data)
                self.assertTrue(data["candidates"])
                self.assertEqual(data["url"], f"{SITE}/denial-language/")

    async def test_explain_denial_reason_with_an_unknown_phrase_lists_the_known_ones(
        self,
    ):
        result = await call("explain_denial_reason", {"phrase": "zzzz qqqq"})
        self.assertTrue(result.isError)
        self.assertIn("Step therapy required", text_of(result))


class StateHelpTest(TestCase):
    async def test_get_state_help_by_two_letter_code(self):
        result = await call("get_state_help", {"state": "ny"})
        self.assertFalse(result.isError)
        data = result.structuredContent
        self.assertEqual(data["state"], "New York")
        self.assertEqual(data["url"], f"{SITE}/state-help/new-york/")
        self.assertIn("insurance_department", data)
        self.assertTrue(data["external_review"]["hand_verified"])

    async def test_get_state_help_by_full_name(self):
        result = await call("get_state_help", {"state": "New Mexico"})
        self.assertFalse(result.isError)
        self.assertEqual(result.structuredContent["abbreviation"], "NM")

    async def test_get_state_help_with_an_unknown_state_is_an_error(self):
        result = await call("get_state_help", {"state": "ZZ"})
        self.assertTrue(result.isError)
        self.assertIn("Unknown state", text_of(result))

    async def test_get_state_help_leaves_out_placeholder_deadlines(self):
        result = await call("get_state_help", {"state": "WA"})
        self.assertFalse(result.isError)
        self.assertNotIn("deadline", result.structuredContent["external_review"])

    async def test_external_review_carries_the_state_pages_own_link(self):
        result = await call("get_state_help", {"state": "CA"})
        review = result.structuredContent["external_review"]
        # state_help.json's link, as the California page shows it, beside
        # the hand-verified DMHC route.
        self.assertEqual(
            review["state_page_link"],
            "https://www.insurance.ca.gov/01-consumers/101-help/15-imr/",
        )
        self.assertIn("dmhc.ca.gov", review["how_to_apply_url"])

    async def test_last_verified_is_left_out_when_the_state_is_not_hand_verified(
        self,
    ):
        wyoming = await call("get_state_help", {"state": "WY"})
        new_york = await call("get_state_help", {"state": "NY"})
        self.assertFalse(wyoming.structuredContent["external_review"]["hand_verified"])
        self.assertNotIn("last_verified", wyoming.structuredContent["external_review"])
        self.assertIn("last_verified", new_york.structuredContent["external_review"])


class AppealRightsTest(TestCase):
    async def test_get_appeal_rights_for_a_private_employer_plan_names_erisa(self):
        result = await call(
            "get_appeal_rights", {"plan_source": "Employer -- Private", "state": "CA"}
        )
        self.assertFalse(result.isError)
        data = result.structuredContent
        self.assertIn("ERISA governs", data["appeal_law"][0])
        self.assertIn("180 days", data["deadlines"])
        self.assertEqual(data["url"], f"{SITE}/state-help/california/")

    async def test_get_appeal_rights_for_medicare_advantage_lists_only_its_rules(self):
        result = await call(
            "get_appeal_rights", {"plan_source": "Medicare Advantage", "state": "MA"}
        )
        self.assertFalse(result.isError)
        names = [h["name"] for h in result.structuredContent["laws_that_may_help"]]
        self.assertTrue(names)
        self.assertFalse(
            any("Massachusetts" in n for n in names),
            "a state insurance law does not bind Medicare Advantage",
        )

    async def test_get_appeal_rights_rejects_an_unknown_plan_source(self):
        result = await call("get_appeal_rights", {"plan_source": "Gold PPO"})
        self.assertTrue(result.isError)

    async def test_a_bad_plan_source_gets_a_short_message_with_the_plan_types(self):
        result = await call("get_appeal_rights", {"plan_source": "Gold PPO"})
        text = text_of(result)
        self.assertIn("'Medicare Regular' (Original Medicare", text)
        self.assertIn("'Don't know'", text)
        self.assertNotIn("pydantic", text)
        self.assertNotIn("Gold PPO", text)

    async def test_the_plan_source_description_explains_medicare_regular(self):
        async with mcp_session() as session:
            tools = {t.name: t for t in (await session.list_tools()).tools}
        schema = tools["get_appeal_rights"].inputSchema
        self.assertIn(
            "'Medicare Regular' (Original Medicare",
            json.dumps(schema["properties"]["plan_source"]),
        )

    async def appeal_rights(self, plan_source: str, state: str = "CA") -> dict:
        result = await call(
            "get_appeal_rights", {"plan_source": plan_source, "state": state}
        )
        self.assertFalse(result.isError, text_of(result))
        return result.structuredContent

    async def test_deadlines_follow_the_kind_of_plan(self):
        expected = {
            "Medicaid": ("60 calendar days", "180 days"),
            "Medicare Regular": ("120 days", "180 days"),
            "Medicare Advantage": ("about 65 days", "180 days"),
            "Employer -- Federal Government": ("6 months", "180 days"),
            "Union": ("180 days", "60 calendar days"),
        }
        for plan_source, (said, not_said) in expected.items():
            with self.subTest(plan_source=plan_source):
                deadlines = (await self.appeal_rights(plan_source))["deadlines"]
                self.assertIn(said, deadlines)
                self.assertNotIn(not_said, deadlines)

    async def test_va_and_unclear_plans_get_their_own_process_and_no_number(self):
        for plan_source in ("Veterans Affairs", "Other", "Don't know"):
            with self.subTest(plan_source=plan_source):
                data = await self.appeal_rights(plan_source)
                self.assertIn("check the denial letter", data["deadlines"])
                self.assertIsNone(re.search(r"\d", data["deadlines"]))

    async def test_every_deadline_number_has_a_source(self):
        for plan_source in PLAN_SOURCES:
            with self.subTest(plan_source=plan_source):
                data = await self.appeal_rights(plan_source)
                if re.search(r"\d", data["deadlines"]):
                    self.assertTrue(data.get("deadline_sources"))
                for cited, url in (
                    ("405.942", mcp_server.ORIGINAL_MEDICARE_DEADLINE_URL),
                    ("890.105", mcp_server.FEHB_DEADLINE_URL),
                ):
                    if cited in data["deadlines"]:
                        self.assertIn(url, data["deadline_sources"])

    def test_deadline_wording_is_quoted_from_fhis_own_pages(self):
        internal_appeal = glossary.get_term("internal-appeal").definition
        for quoted in (
            mcp_server._GLOSSARY_COMMERCIAL_DEADLINE,
            mcp_server._GLOSSARY_MEDICARE_ADVANTAGE_DEADLINE,
            mcp_server._GLOSSARY_MEDICAID_DEADLINE,
            mcp_server._GLOSSARY_CHECK_THE_LETTER,
        ):
            with self.subTest(quoted=quoted[:40]):
                self.assertIn(quoted, internal_appeal)
        coverage_changes = (TEMPLATES / "coverage_changes.html").read_text()
        self.assertIn(mcp_server._SITE_EXPEDITED, coverage_changes)

    async def test_an_employer_plan_gets_no_rules_written_for_public_programs(self):
        data = await self.appeal_rights("Employer -- Private")
        names = " ".join(h["name"] for h in data["laws_that_may_help"])
        self.assertNotIn("CMS-0057-F", names)
        self.assertNotIn("artificial intelligence in coverage", names)
        self.assertIn("California", names)
        about = data["about_these_laws"]
        self.assertIn(mcp_server.LAWS_FRAMING, about)
        self.assertIn("Self-funded (ERISA) employer plans are usually exempt", about)

    async def test_an_unknown_plan_leaves_out_rules_for_one_program(self):
        data = await self.appeal_rights("Don't know")
        names = " ".join(h["name"] for h in data["laws_that_may_help"])
        self.assertNotIn("CMS-0057-F", names)
        self.assertNotIn("artificial intelligence in coverage", names)
        self.assertIn(
            "left out until the kind of plan is known", data["about_these_laws"]
        )

    async def test_a_marketplace_plan_gets_the_cms_rule_with_its_healthcare_gov_scope(
        self,
    ):
        data = await self.appeal_rights("State Marketplace / Affordable Care Act")
        names = " ".join(h["name"] for h in data["laws_that_may_help"])
        self.assertIn("CMS-0057-F", names)
        self.assertNotIn("artificial intelligence in coverage", names)
        self.assertIn(
            "not plans from a state's own marketplace", data["about_these_laws"]
        )

    async def test_original_medicare_gets_neither_cms_rule(self):
        data = await self.appeal_rights("Medicare Regular")
        self.assertEqual(data.get("laws_that_may_help", []), [])
        self.assertIn("Original Medicare coverage", data["about_these_laws"])

    async def test_medicaid_is_told_a_state_licensed_plan_may_have_state_rules(self):
        # California licenses Medi-Cal managed care plans (DMHC), and their
        # members can ask for its independent medical review.
        about = (await self.appeal_rights("Medicaid"))["about_these_laws"]
        self.assertIn("also licensed by the state", about)
        self.assertIn("Medicaid has its own federal appeal rules", about)
        self.assertNotIn("State insurance laws", about)

    async def test_appeal_rights_are_written_for_the_person_not_the_letter(self):
        drafting = (
            "patient",
            "Argue the medical case",
            "Name one of them",
            "Name ERISA only",
            "say so",
            "Use whichever",
            "Demand",
            "Cite it",
            "Insist",
            "Use it to",
        )
        async with mcp_session() as session:
            replies = {
                (state, plan_source): await session.call_tool(
                    "get_appeal_rights",
                    {"plan_source": plan_source, "state": state},
                )
                for state in ("CA", "MA", "WA", "TX", "MD", "IN", "GA", "WV")
                for plan_source in PLAN_SOURCES
            }
        for (state, plan_source), reply in replies.items():
            data = reply.structuredContent
            said = json.dumps(
                [
                    data["appeal_law"],
                    # The text, not the source URLs ("patient-consumer-
                    # protections" is in KFF's).
                    [
                        (law["name"], law["what_it_says"])
                        for law in data.get("laws_that_may_help", [])
                    ],
                    data.get("about_these_laws", ""),
                    data["deadlines"],
                ]
            )
            for phrase in drafting:
                with self.subTest(state=state, plan=plan_source, phrase=phrase):
                    self.assertNotIn(phrase, said)

    def test_every_law_has_a_summary_for_the_person(self):
        for hook in (*FEDERAL_HOOKS, *STATE_HOOKS):
            with self.subTest(hook=hook.name):
                self.assertGreater(len(hook.plain_summary), 80)
                self.assertNotIn("patient", hook.plain_summary)
                self.assertNotRegex(hook.plain_summary, r"(^|\. )(Demand|Cite|Insist)")

    async def test_the_plan_sources_offered_match_the_intake_list(self):
        fixture = (
            Path(mcp_server.__file__).parent / "fixtures" / "plan_source.yaml"
        ).read_text()
        names = {row["fields"]["name"].strip() for row in yaml.safe_load(fixture)}
        self.assertEqual(set(mcp_server.PlanSourceName.__args__), names)


class InsurerAppealContactsTest(TestCase):
    fixtures = ["plan_source", "insurance_companies"]

    async def test_find_insurer_appeal_contacts_for_a_known_insurer(self):
        result = await call("find_insurer_appeal_contacts", {"insurer": "aetna"})
        self.assertFalse(result.isError)
        company = result.structuredContent["company"]
        self.assertEqual(company["name"], "Aetna")
        self.assertEqual(company["appeal_contacts"]["fax"], "859-425-3379")
        self.assertNotIn("notes", company)

    async def test_find_insurer_appeal_contacts_with_a_state_lists_its_plans(self):
        result = await call(
            "find_insurer_appeal_contacts",
            {"insurer": "UnitedHealthcare", "state": "NY"},
        )
        self.assertFalse(result.isError)
        plans = result.structuredContent["plans"]
        self.assertEqual(len(plans), 2)
        self.assertTrue(all(p["state"] == "NY" for p in plans))

    async def test_find_insurer_appeal_contacts_with_an_unknown_insurer_is_an_error(
        self,
    ):
        result = await call(
            "find_insurer_appeal_contacts", {"insurer": "Zzyzx Mutual Benefit"}
        )
        self.assertTrue(result.isError)
        self.assertIn("appeal address on the denial letter", text_of(result))

    def test_find_insurer_appeal_contacts_writes_nothing(self):
        # Sync, so the query capture can wrap the whole call; the tool's
        # database work runs back on this thread.
        with CaptureQueriesContext(connection) as queries:
            result = async_to_sync(call)(
                "find_insurer_appeal_contacts",
                {"insurer": "UnitedHealthcare", "state": "NY"},
            )
        self.assertFalse(result.isError)
        self.assertTrue(queries.captured_queries, "the lookup never reached the DB")
        self.assertEqual(writes_in(queries), [])


class TreatmentGuideTest(TestCase):
    async def test_find_treatment_guide_finds_the_mri_guide(self):
        result = await call("find_treatment_guide", {"query": "MRI"})
        self.assertFalse(result.isError)
        data = result.structuredContent
        self.assertEqual(data["slug"], "mri-denial")
        self.assertEqual(data["url"], f"{SITE}/microsite/mri-denial/")
        self.assertIn("microsite_slug=mri-denial", data["start_appeal_url"])

    async def test_find_treatment_guide_with_no_match_points_to_the_directory(self):
        result = await call("find_treatment_guide", {"query": "zzzz qqqq"})
        self.assertTrue(result.isError)
        self.assertIn(f"{SITE}/treatments/", text_of(result))

    async def test_find_treatment_guide_never_serves_a_held_back_guide(self):
        result = await call(
            "find_treatment_guide", {"query": "medicare-work-requirements-denial"}
        )
        served = [] if result.isError else [result.structuredContent["slug"]]
        if not result.isError:
            served += [
                m["slug"] for m in result.structuredContent.get("other_matches", [])
            ]
        self.assertNotIn("medicare-work-requirements-denial", served)

    async def test_medicare_does_not_find_a_medication_guide(self):
        result = await call("find_treatment_guide", {"query": "Medicare"})
        self.assertFalse(result.isError, text_of(result))
        data = result.structuredContent
        self.assertNotEqual(data["slug"], "biologic-denial")
        self.assertIn("medicare", (data["title"] + " " + data["tagline"]).lower())

    def test_matching_keeps_whole_words_apart(self):
        terms = mcp_server._terms("Medicare Medicaid medical medication")
        self.assertEqual(terms, {"medicare", "medicaid", "medical", "medication"})

    def test_matching_treats_the_listed_forms_of_a_word_as_one(self):
        same = [
            ("medically necessary", "medical necessity"),
            ("prior auth", "prior authorization"),
            ("physical therapies", "physical therapy"),
            ("appeals for drugs", "drug"),
            ("diabetic", "diabetes"),
            ("diabetics", "diabetes"),
            ("pregnant", "pregnancy"),
            ("depressed", "depression"),
            ("psychiatric", "psychiatry"),
        ]
        for one, other in same:
            with self.subTest(one=one):
                self.assertEqual(mcp_server._terms(one), mcp_server._terms(other))

    async def test_a_persons_word_for_a_condition_finds_its_guide(self):
        expected = {
            "diabetic": {
                "glp1-denial-tirzepatide-mounjaro",
                "insulin-pump-cgm-denial-dexcom",
                "insulin-pump-cgm-denial-freestyle-libre",
            },
            "pregnant": {"prenatal-denial"},
            "depressed": {"flow-tdcs-depression-denial"},
            "psychiatric": {"mental-health-denial"},
        }
        async with mcp_session() as session:
            replies = {
                query: await session.call_tool("find_treatment_guide", {"query": query})
                for query in expected
            }
        for query, slugs in expected.items():
            with self.subTest(query=query):
                reply = replies[query]
                self.assertFalse(reply.isError, text_of(reply))
                data = reply.structuredContent
                others = data.get("other_matches", [])
                served = {data["slug"]} | {m["slug"] for m in others}
                self.assertLessEqual(slugs, served)

    async def test_earlier_queries_still_find_the_same_guides(self):
        expected = {
            "MRI": "mri-denial",
            "Ozempic": "glp1-denial-semaglutide-ozempic",
            "sleep apnea": "sleep-study-denial",
            "Medicare": "home-health-denial",
            "medication": "biologic-denial",
        }
        async with mcp_session() as session:
            replies = {
                query: await session.call_tool("find_treatment_guide", {"query": query})
                for query in expected
            }
        for query, slug in expected.items():
            with self.subTest(query=query):
                self.assertFalse(replies[query].isError, text_of(replies[query]))
                self.assertEqual(replies[query].structuredContent["slug"], slug)

    def test_medicaid_matches_no_medicare_or_medication_guide(self):
        for guide in mcp_server._matching_guides("Medicaid"):
            with self.subTest(guide=guide.slug):
                words = mcp_server._terms(
                    " ".join(
                        (
                            guide.title,
                            guide.slug.replace("-", " "),
                            guide.default_procedure,
                            guide.default_condition or "",
                            guide.tagline,
                        )
                    )
                )
                self.assertIn("medicaid", words)

    async def test_find_financial_help_for_a_drug_and_a_state(self):
        result = await call("find_financial_help", {"drug": "Humira", "state": "CA"})
        self.assertFalse(result.isError)
        data = result.structuredContent
        self.assertTrue(data["specific_matches"])
        self.assertIn("state_medicaid", data)
        self.assertTrue(data["url"].startswith(SITE))

    async def test_find_financial_help_with_nothing_to_go_on_is_an_error(self):
        result = await call("find_financial_help", {})
        self.assertTrue(result.isError)
        self.assertIn("at least one", text_of(result))


class SiteSearchAndPagesTest(TestCase):
    async def test_search_site_finds_turning_26(self):
        result = await call("search_site", {"query": "turning 26"})
        self.assertFalse(result.isError)
        urls = [r["url"] for r in result.structuredContent["results"]]
        self.assertIn(f"{SITE}/turning-26", urls)

    async def test_search_site_finds_a_glossary_term(self):
        result = await call("search_site", {"query": "adverse benefit determination"})
        self.assertFalse(result.isError)
        first = result.structuredContent["results"][0]
        self.assertEqual(first["section"], "Glossary")

    async def test_search_site_finds_the_coverage_changes_page_for_medicare(self):
        result = await call("search_site", {"query": "medicare appeal"})
        urls = [r["url"] for r in result.structuredContent["results"]]
        self.assertIn(f"{SITE}/coverage-changes", urls)
        self.assertFalse(any("medicaid" in u for u in urls), urls)

    async def test_search_site_for_medicaid_lists_no_medicare_page(self):
        result = await call("search_site", {"query": "Medicaid"})
        urls = [r["url"] for r in result.structuredContent["results"]]
        self.assertIn(f"{SITE}/faq/medicaid/", urls)
        self.assertFalse(any("medicare" in u for u in urls), urls)

    async def test_search_site_finds_pages_from_a_persons_word_for_a_condition(self):
        for query, url in (
            ("diabetic", f"{SITE}/microsite/insulin-pump-cgm-denial/"),
            ("pregnant", f"{SITE}/microsite/prenatal-denial/"),
        ):
            with self.subTest(query=query):
                result = await call("search_site", {"query": query})
                urls = [r["url"] for r in result.structuredContent["results"]]
                self.assertIn(url, urls)

    async def test_search_site_never_lists_a_held_back_guide(self):
        result = await call("search_site", {"query": "medicare work requirements"})
        urls = [r["url"] for r in result.structuredContent["results"]]
        self.assertFalse(
            any("medicare-work-requirements-denial" in u for u in urls), urls
        )

    async def test_search_site_with_a_too_long_query_is_an_error(self):
        result = await call("search_site", {"query": "x" * 101})
        self.assertTrue(result.isError)

    async def test_get_page_reads_a_public_page_as_markdown(self):
        result = await call("get_page", {"url": f"{SITE}/turning-26"})
        self.assertFalse(result.isError, text_of(result))
        data = result.structuredContent
        self.assertEqual(data["url"], f"{SITE}/turning-26")
        self.assertIn(
            "- URL: https://www.fighthealthinsurance.com/turning-26", data["markdown"]
        )
        self.assertNotIn("<div", data["markdown"])

    async def test_get_page_reads_a_blog_post_with_its_title(self):
        post = agent_docs._blog_posts_from_sources()[0]
        result = await call("get_page", {"url": f"/blog/{post['slug']}/"})
        self.assertFalse(result.isError, text_of(result))
        self.assertEqual(result.structuredContent["title"], post["title"])
        self.assertEqual(
            result.structuredContent["url"], f"{SITE}/blog/{post['slug']}/"
        )

    async def test_get_page_reads_a_glossary_term(self):
        result = await call(
            "get_page", {"url": "/glossary/adverse-benefit-determination/"}
        )
        self.assertFalse(result.isError)
        self.assertTrue(
            result.structuredContent["markdown"].startswith(
                "# Adverse Benefit Determination"
            )
        )

    async def test_get_page_refuses_a_page_inside_the_appeal_flow(self):
        with mock.patch.object(mcp_server, "_get_in_process") as fetch:
            result = await call("get_page", {"url": "/scan"})
        self.assertTrue(result.isError)
        self.assertIn("enters their own denial or details", text_of(result))
        self.assertIn("start_appeal", text_of(result))
        fetch.assert_not_called()

    async def test_get_page_refuses_a_held_back_guide_and_its_text_version(self):
        for url in (
            "/microsite/medicare-work-requirements-denial/",
            f"{SITE}/microsite/medicare-work-requirements-denial/index.md",
            # The guide's own redirect, from the slug without "-denial".
            "/microsite/medicare-work-requirements/",
        ):
            with self.subTest(url=url):
                with mock.patch.object(mcp_server, "_get_in_process") as fetch:
                    result = await call("get_page", {"url": url})
                self.assertTrue(result.isError)
                self.assertIn("being corrected", text_of(result))
                fetch.assert_not_called()

    async def test_the_treatments_list_leaves_out_held_back_guides(self):
        result = await call("get_page", {"url": "/treatments/"})
        self.assertFalse(result.isError, text_of(result))
        markdown = result.structuredContent["markdown"]
        self.assertIn("/microsite/mri-denial/", markdown)
        self.assertNotIn("medicare-work-requirements", markdown)
        self.assertNotIn("Understanding Medicare Work Requirements", markdown)
        # The rest of its card: the tagline and the Medicare badge.
        self.assertNotIn("Navigating the new Medicare work requirements", markdown)
        self.assertNotIn("\nMedicare\n", markdown)

    async def test_the_treatments_list_keeps_what_follows_the_last_card(self):
        # The held-back guide's card is the last one, so this is what a
        # card running on to the next heading would take with it.
        result = await call("get_page", {"url": "/treatments/"})
        markdown = result.structuredContent["markdown"]
        self.assertIn("**Don't see your treatment?** Our main", markdown)
        self.assertIn(f"[Start Your Appeal]({SITE}/scan)", markdown)
        self.assertIn(f"[Chat with AI]({SITE}/chat)", markdown)

    def test_a_held_back_card_between_two_others_leaves_only_itself(self):
        slug = "medicare-work-requirements-denial"
        guide = mcp_server.microsites.get_microsite(slug)
        path = reverse("microsite", kwargs={"slug": slug})
        before = "### [A](/microsite/a/)\n\nTagline A\n\n"
        after = "### [B](/microsite/b/)\n\nTagline B\n"
        card = f"### [{guide.title}]({SITE}{path})\n\n{guide.tagline}\n\nMedicare\n\n"
        self.assertEqual(
            mcp_server._without_held_back(before + card + after), before + after
        )

    async def test_get_page_follows_the_sites_permanent_redirects(self):
        for url, lands_at in (
            ("/preparing-for-2026", f"{SITE}/coverage-changes"),
            ("/media-references/", f"{SITE}/media-references"),
            ("/microsite/mri/", f"{SITE}/microsite/mri-denial/"),
        ):
            with self.subTest(url=url):
                result = await call("get_page", {"url": url})
                self.assertFalse(result.isError, text_of(result))
                self.assertEqual(result.structuredContent["url"], lands_at)

    async def test_get_page_refusals_say_why_for_each_case(self):
        cases = {
            "/glossary/": "one term at a time",
            "/no-such-page-here": "There is no page at /no-such-page-here",
            "/microsite/no-such-guide-denial/": "no treatment guide at",
            "/timbit/sentry-debug/x": "not one of the site's public reading pages",
        }
        for url, said in cases.items():
            with self.subTest(url=url):
                result = await call("get_page", {"url": url})
                self.assertTrue(result.isError)
                self.assertIn(said, text_of(result))
                self.assertNotIn("appeal flow", text_of(result))

    async def test_get_page_sends_the_person_to_other_resources_instead_of_reading_it(
        self,
    ):
        with mock.patch.object(mcp_server, "_get_in_process") as fetch:
            result = await call("get_page", {"url": "/other-resources"})
        self.assertTrue(result.isError)
        self.assertIn(f"{SITE}/other-resources", text_of(result))
        fetch.assert_not_called()

    async def test_reading_every_public_page_makes_no_outbound_connection(self):
        attempts: list[Any] = []

        def refuse(*args, **kwargs):
            attempts.append(args[1] if len(args) > 1 else args)
            raise OSError("no outbound connections in this test")

        urls = []
        for name in sorted(agent_docs.static_twin_url_names()):
            try:
                urls.append(reverse(name))
            except NoReverseMatch:
                continue  # needs a slug: guides, states and posts share a view
        self.assertIn(reverse("other-resources"), urls)
        with (
            mock.patch.object(socket.socket, "connect", refuse),
            mock.patch.object(socket, "getaddrinfo", refuse),
        ):
            async with mcp_session() as session:
                read = {
                    url: not (await session.call_tool("get_page", {"url": url})).isError
                    for url in urls
                }
        self.assertEqual(attempts, [])
        # Every page was read, apart from the one sent elsewhere.
        unread = [url for url, ok in read.items() if not ok]
        self.assertEqual(unread, [reverse("other-resources")])

    async def test_get_page_refuses_other_sites(self):
        result = await call("get_page", {"url": "https://example.com/about"})
        self.assertTrue(result.isError)
        self.assertIn("only reads fighthealthinsurance.com", text_of(result))

    async def test_get_page_never_reaches_a_route_outside_the_twins(self):
        with mock.patch.object(mcp_server, "_get_in_process") as fetch:
            result = await call("get_page", {"url": "/timbit/sentry-debug/x"})
        self.assertTrue(result.isError)
        fetch.assert_not_called()

    def test_get_page_writes_nothing(self):
        with CaptureQueriesContext(connection) as queries:
            result = async_to_sync(call)("get_page", {"url": "/turning-26"})
        self.assertFalse(result.isError, text_of(result))
        self.assertEqual(writes_in(queries), [])


def enable_sdk_loggers() -> None:
    """Switch the MCP SDK's loggers back on. In production asgi.py runs
    django.setup() before the SDK is imported, so they are live; here any
    test that imports asgi.py runs it again, and the test logging config
    (disable_existing_loggers) switches off every logger that exists."""
    for name, found in logging.root.manager.loggerDict.items():
        if isinstance(found, logging.Logger) and (
            name == "mcp" or name.startswith("mcp.")
        ):
            found.disabled = False


class ToolArgumentsAreNotLoggedTest(TestCase):
    async def test_a_condition_keyword_never_reaches_the_logs(self):
        marker = "zebrafinchitis"
        lines: list[str] = []
        sdk_records: list[str] = []
        sink = logger.add(lambda m: lines.append(str(m)), level="DEBUG")

        class Collect(logging.Handler):
            def emit(self, record):
                lines.append(record.getMessage())
                if record.name.startswith("mcp.server"):
                    sdk_records.append(record.name)

        enable_sdk_loggers()
        handler = Collect(level=logging.DEBUG)
        root = logging.getLogger()
        old_level = root.level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        try:
            await call("find_financial_help", {"condition": marker})
            await call("find_treatment_guide", {"query": marker})
            await call("explain_denial_reason", {"phrase": marker})
        finally:
            root.setLevel(old_level)
            root.removeHandler(handler)
            logger.remove(sink)
        # The SDK's own loggers have to be heard from (the session manager's
        # start line at least), or a silenced SDK would pass this test.
        self.assertTrue(
            sdk_records, "the MCP SDK logged nothing, so this proves nothing"
        )
        self.assertEqual([line for line in lines if marker in line], [])


class MountingTest(TestCase):
    async def test_mcp_is_a_plain_404_when_the_flag_is_off(self):
        from fighthealthinsurance.asgi import http_and_lifespan_routes

        with override_settings(MCP_SERVER_ENABLED=False):
            routes = http_and_lifespan_routes(django_app())
            self.assertEqual(set(routes), {"http"})
            async with running_app(routes) as http:
                response = await http.post(
                    "/mcp", content=json.dumps(INITIALIZE), headers=MCP_HEADERS
                )
        self.assertEqual(response.status_code, 404)

    async def test_the_flag_adds_the_mcp_lifespan(self):
        from fighthealthinsurance.asgi import http_and_lifespan_routes

        with override_settings(MCP_SERVER_ENABLED=True):
            routes = http_and_lifespan_routes(django_app())
        self.assertEqual(set(routes), {"http", "lifespan"})

    @skipIf("MCP_SERVER_ENABLED" in os.environ, "the flag is set in this environment")
    def test_the_flag_is_off_in_production_unless_turned_on(self):
        self.assertFalse(fhi_settings.Prod.MCP_SERVER_ENABLED)

    async def test_other_paths_still_reach_django(self):
        async with running_app() as http:
            llms = await http.get("/llms.txt")
            lookalike = await http.get("/mcpfoo")
        self.assertEqual(llms.status_code, 200)
        self.assertTrue(llms.text.startswith("# Fight Health Insurance\n"))
        self.assertEqual(lookalike.status_code, 404)

    async def test_get_on_mcp_is_method_not_allowed(self):
        async with running_app() as http:
            response = await http.get("/mcp", headers={"Accept": "text/event-stream"})
        self.assertEqual(response.status_code, 405)
        self.assertEqual(response.headers["allow"], "POST")

    async def test_mcp_with_a_trailing_slash_is_served_in_place(self):
        async with running_app() as http:
            response = await http.post(
                "/mcp/", content=json.dumps(INITIALIZE), headers=MCP_HEADERS
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["result"]["serverInfo"]["name"], "fight-health-insurance"
        )

    async def test_an_unknown_host_is_refused(self):
        async with running_app() as http:
            response = await http.post(
                "/mcp",
                content=json.dumps(INITIALIZE),
                headers={**MCP_HEADERS, "Host": "evil.example"},
            )
        self.assertEqual(response.status_code, 421)

    async def test_a_request_with_an_origin_header_is_served(self):
        """The dispatcher drops Origin, so a platform that sends one connects.
        The answer carries no CORS headers, so a page still couldn't read it."""
        async with running_app() as http:
            for origin in ("https://claude.ai", "https://chatgpt.com"):
                with self.subTest(origin=origin):
                    response = await http.post(
                        "/mcp",
                        content=json.dumps(INITIALIZE),
                        headers={**MCP_HEADERS, "Origin": origin},
                    )
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(
                        response.json()["result"]["serverInfo"]["name"],
                        "fight-health-insurance",
                    )
                    self.assertNotIn("access-control-allow-origin", response.headers)

    async def test_a_browser_preflight_still_gets_405(self):
        """A page's cross-site JSON POST needs this preflight to pass first."""
        async with running_app() as http:
            response = await http.options(
                "/mcp",
                headers={
                    "Origin": "https://evil.example",
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": "content-type",
                },
            )
        self.assertEqual(response.status_code, 405)
        self.assertEqual(response.headers["allow"], "POST")
        self.assertNotIn("access-control-allow-origin", response.headers)

    async def test_a_post_a_page_can_send_without_a_preflight_gets_400(self):
        async with running_app() as http:
            response = await http.post(
                "/mcp",
                content=json.dumps(INITIALIZE),
                headers={
                    **MCP_HEADERS,
                    "Content-Type": "text/plain",
                    "Origin": "https://evil.example",
                },
            )
        self.assertEqual(response.status_code, 400)

    async def test_a_bad_host_still_gets_421_with_an_origin(self):
        """DNS rebinding: a page whose name now points at us sends that name
        as Host (and as Origin). Dropping Origin leaves the Host check."""
        async with running_app() as http:
            response = await http.post(
                "/mcp",
                content=json.dumps(INITIALIZE),
                headers={
                    **MCP_HEADERS,
                    "Host": "rebound.example",
                    "Origin": "http://rebound.example",
                },
            )
        self.assertEqual(response.status_code, 421)

    async def test_an_oversized_request_is_refused(self):
        body = json.dumps({**INITIALIZE, "padding": "x" * (70 * 1024)})
        async with running_app() as http:
            response = await http.post("/mcp", content=body, headers=MCP_HEADERS)
        self.assertEqual(response.status_code, 413)

    def test_sentry_ignores_every_mcp_sdk_logger(self):
        """asgi.py's Sentry block ignores the SDK's loggers, by a pattern
        Sentry's fnmatch check applies to each of them."""
        tree = ast.parse((Path(mcp_server.__file__).parent / "asgi.py").read_text())
        ignored = [
            node.args[0].value
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and getattr(node.func, "id", None) == "ignore_logger"
            and node.args
            and isinstance(node.args[0], ast.Constant)
        ]
        for name in (
            "mcp.server.streamable_http",
            "mcp.server.lowlevel.server",
            "mcp.server.streamable_http_manager",
        ):
            with self.subTest(logger=name):
                self.assertTrue(any(fnmatch(name, p) for p in ignored), ignored)
        self.assertFalse(any(fnmatch("fighthealthinsurance.views", p) for p in ignored))

    def test_production_hosts_come_from_allowed_hosts(self):
        with override_settings(
            ALLOWED_HOSTS=["www.fighthealthinsurance.com", ".example.org"]
        ):
            allowed = mcp_server.transport_security().allowed_hosts
        self.assertIn("www.fighthealthinsurance.com", allowed)
        self.assertIn("www.fighthealthinsurance.com:*", allowed)
        self.assertNotIn("localhost", allowed)
        self.assertFalse(any("example.org" in h for h in allowed))


class LlmsTxtTest(TestCase):
    def test_llms_txt_lists_the_mcp_server_when_it_is_on(self):
        with override_settings(MCP_SERVER_ENABLED=True):
            body = agent_docs.build_llms_txt()
        self.assertIn(f"- [MCP server]({SITE}/mcp)", body)

    def test_llms_txt_leaves_the_mcp_server_out_when_it_is_off(self):
        with override_settings(MCP_SERVER_ENABLED=False):
            body = agent_docs.build_llms_txt()
        self.assertNotIn("[MCP server]", body)


# ---------------------------------------------------------------------------
# Stage 2: prepare_appeal fills in the appeal form for the person to submit
# ---------------------------------------------------------------------------

PREPARE_ON = {"MCP_SERVER_ENABLED": True, "MCP_PREPARE_APPEAL_ENABLED": True}
LETTER = (
    "Dear {{FIRST_NAME}} {{LAST_NAME}}, Acme Health has denied your request "
    "for an MRI of the lower back as not medically necessary."
)
HANDOFF_LINK = re.compile(
    r"^https://www\.fighthealthinsurance\.com/from-your-assistant#(?P<code>[A-Za-z0-9_-]{43})$"
)


def prepare_routes():
    """asgi.py's routes as a server built with prepare_appeal on."""
    with override_settings(**PREPARE_ON):
        return mcp_server.mcp_asgi_routes(django_app())


async def prepare(arguments: dict[str, Any]):
    return await call("prepare_appeal", arguments, routes=prepare_routes())


async def tool_names(routes) -> set[str]:
    async with mcp_session(routes) as session:
        return {t.name for t in (await session.list_tools()).tools}


@override_settings(**PREPARE_ON)
class PrepareAppealListingTest(TestCase):
    async def test_prepare_appeal_is_not_listed_with_either_flag_off(self):
        for server, prepare_flag in ((True, False), (False, True)):
            with self.subTest(server=server, prepare=prepare_flag):
                with override_settings(
                    MCP_SERVER_ENABLED=server, MCP_PREPARE_APPEAL_ENABLED=prepare_flag
                ):
                    routes = mcp_server.mcp_asgi_routes(django_app())
                names = await tool_names(routes)
                self.assertNotIn("prepare_appeal", names)
                self.assertIn("start_appeal", names)

    async def test_with_it_on_prepare_appeal_is_the_only_tool_that_is_not_read_only(
        self,
    ):
        async with mcp_session(prepare_routes()) as session:
            tools = (await session.list_tools()).tools
        self.assertEqual(len(tools), 11)
        writers = [t.name for t in tools if not t.annotations.readOnlyHint]
        self.assertEqual(writers, ["prepare_appeal"])
        prepare_tool = next(t for t in tools if t.name == "prepare_appeal")
        self.assertFalse(prepare_tool.annotations.idempotentHint)
        self.assertFalse(prepare_tool.annotations.destructiveHint)
        self.assertFalse(prepare_tool.annotations.openWorldHint)
        self.assertEqual(
            prepare_tool.description, mcp_server.PREPARE_APPEAL_DESCRIPTION
        )
        schema = prepare_tool.inputSchema
        self.assertEqual(
            set(schema["properties"]), {"letter_text", "procedure", "condition"}
        )
        self.assertEqual(schema["required"], ["letter_text"])
        self.assertEqual(schema["properties"]["letter_text"]["maxLength"], 20_000)

    def test_the_description_uses_fhis_own_placeholders(self):
        """The site's Remove personal details writes these, and the finished
        appeal page fills them in from its personal details panel."""
        scrubber = (STATIC_JS / "scrub_scrub.ts").read_text()
        # appeal.ts fills in the IDs itself and the names through
        # user_info_storage.ts's restorePersonalInfo.
        appeal = (STATIC_JS / "appeal.ts").read_text() + (
            STATIC_JS / "user_info_storage.ts"
        ).read_text()
        self.assertIn("restorePersonalInfo(text, userInfo)", appeal)
        for placeholder in ("{{FIRST_NAME}}", "{{LAST_NAME}}", "{{SCSID}}", "{{GPID}}"):
            with self.subTest(placeholder=placeholder):
                self.assertIn(placeholder, mcp_server.PREPARE_APPEAL_DESCRIPTION)
                self.assertIn(placeholder, scrubber)
                # As the regular expression that replaces it.
                pattern = placeholder.replace("{", "\\{").replace("}", "\\}")
                self.assertIn(f"/{pattern}/g", appeal)

    def test_the_description_says_to_ask_first_and_to_share_what_was_sent(self):
        said = mcp_server.PREPARE_APPEAL_DESCRIPTION
        self.assertIn(
            "Ask the person first whether they want you to send the letter", said
        )
        self.assertIn("send it only if they say yes", said)
        self.assertIn("tell_the_person", said)
        self.assertIn("share that note with the person", said)
        self.assertIn("in your own words if you like", said)
        self.assertIn("without leaving out what was sent or how long it's kept", said)
        self.assertIn("give them the link exactly as returned", said)

    async def test_the_welcome_and_the_refusal_say_where_the_letter_may_go(self):
        routes = prepare_routes()
        async with mcp_session(routes) as session:
            init = session.init_result
            refused = await session.call_tool(
                "search_site", {"query": "turning 26", "letter": "x"}
            )
        self.assertEqual(init.instructions, mcp_server.INSTRUCTIONS_WITH_PREPARE)
        self.assertIn(
            "already shared their denial letter in this chat, offer to load it",
            init.instructions,
        )
        self.assertIn("ask before calling it", init.instructions)
        self.assertIn("opens the first step", init.instructions)
        self.assertIn(
            "offer to load it into the form with prepare_appeal", init.instructions
        )
        self.assertIn("names, member IDs and medical history", init.instructions)
        self.assertTrue(refused.isError)
        self.assertIn(
            "Denial letters go only in prepare_appeal's letter_text", text_of(refused)
        )

    async def test_start_appeal_points_to_prepare_appeal_only_while_it_is_on(self):
        async with mcp_session(prepare_routes()) as session:
            on = {t.name: t for t in (await session.list_tools()).tools}
        with override_settings(MCP_PREPARE_APPEAL_ENABLED=False):
            routes = mcp_server.mcp_asgi_routes(django_app())
        async with mcp_session(routes) as session:
            off = {t.name: t for t in (await session.list_tools()).tools}
        self.assertIn("offer prepare_appeal instead", on["start_appeal"].description)
        self.assertNotIn("prepare_appeal", off["start_appeal"].description)
        # It still takes no letter.
        self.assertEqual(set(on["start_appeal"].inputSchema["properties"]), {"topic"})

    async def test_start_appeal_still_refuses_a_letter_while_it_is_on(self):
        result = await call(
            "start_appeal", {"denial_text": LETTER}, routes=prepare_routes()
        )
        self.assertTrue(result.isError)
        self.assertNotIn("Acme Health", text_of(result))


@override_settings(**PREPARE_ON)
class PrepareAppealTest(TestCase):
    async def test_it_returns_a_link_that_works_once_for_two_hours(self):
        result = await prepare(
            {"letter_text": LETTER, "procedure": "MRI", "condition": "back pain"}
        )
        self.assertFalse(result.isError, text_of(result))
        data = result.structuredContent
        self.assertRegex(data["url"], HANDOFF_LINK)
        self.assertEqual(data["works"], "once")
        self.assertEqual(data["expires_in_minutes"], 120)
        expires = datetime.fromisoformat(data["expires_at"])
        left = expires - timezone.now()
        self.assertTrue(timedelta(minutes=119) < left <= timedelta(minutes=120), left)
        self.assertEqual(
            data["received"],
            {
                "letter_characters": len(LETTER),
                "procedure": "MRI",
                "condition": "back pain",
            },
        )
        # The letter itself never comes back.
        self.assertNotIn("Acme Health", json.dumps(data))
        self.assertIn(f"{SITE}/scan", data["if_the_link_stops_working"])

    def test_the_link_opens_the_form_with_the_letter_in_the_box_once(self):
        denials = models.Denial.objects.count()
        result = async_to_sync(prepare)({"letter_text": LETTER, "procedure": "MRI"})
        url = result.structuredContent["url"]
        path, code = url[len(SITE) :].split("#")
        self.assertEqual(path, "/from-your-assistant")
        landing = self.client.get(path)
        self.assertEqual(landing.status_code, 200)
        self.assertContains(landing, "Open my appeal form")
        opened = self.client.post(path, {"token": code})
        self.assertEqual(opened.status_code, 200)
        self.assertTemplateUsed(opened, "scrub.html")
        box = re.search(
            r'<textarea name="denial_text"[^>]*>(.*?)</textarea>',
            opened.content.decode(),
            re.S,
        )
        self.assertEqual(box.group(1), LETTER)
        self.assertEqual(self.client.post(path, {"token": code}).status_code, 404)
        self.assertEqual(models.Denial.objects.count(), denials)

    def test_no_denial_exists_until_the_person_submits_the_form(self):
        result = async_to_sync(prepare)({"letter_text": LETTER})
        code = result.structuredContent["url"].split("#")[1]
        self.assertEqual(models.Denial.objects.count(), 0)
        self.client.post("/from-your-assistant", {"token": code})
        self.assertEqual(models.Denial.objects.count(), 0)
        edited = LETTER.replace("{{FIRST_NAME}} {{LAST_NAME}}", "member")
        self.client.post(
            reverse("scan"),
            {
                "email": "handoff-submit@example.com",
                "denial_text": edited,
                "zip": "94103",
                "pii": "on",
                "tos": "on",
                "privacy": "on",
                "personalonly": "on",
            },
        )
        denial = models.Denial.objects.get()
        # Only what was in the box when the person pressed Submit.
        self.assertEqual(denial.denial_text, edited)

    def test_the_text_is_cleaned_before_it_is_kept(self):
        messy = "  Dear member,\r\nyour claim\x00 was\x07 denied.\r\n\tReason: not covered.\x1b  "
        result = async_to_sync(prepare)(
            {"letter_text": messy, "procedure": "  MRI\tscan "}
        )
        self.assertFalse(result.isError, text_of(result))
        code = result.structuredContent["url"].split("#")[1]
        content = assistant_handoff.claim_handoff(code)
        self.assertEqual(
            content.letter,
            "Dear member,\nyour claim was denied.\n\tReason: not covered.",
        )
        self.assertEqual(content.procedure, "MRI scan")

    async def test_a_short_field_loses_invisible_format_characters(self):
        """U+202E reverses how the text after it reads, wherever the site
        shows it later."""
        result = await prepare(
            {
                "letter_text": LETTER,
                "procedure": "MRI\u202e of the back",
                "condition": "\u2066migraine\u2069\ufeff",
            }
        )
        self.assertFalse(result.isError, text_of(result))
        received = result.structuredContent["received"]
        self.assertEqual(received["procedure"], "MRI of the back")
        self.assertEqual(received["condition"], "migraine")

    def test_a_lone_surrogate_never_reaches_the_page(self):
        """json.loads keeps a body's lone "\\ud800" escape as text UTF-8 can't
        encode, and pydantic passes it in letter_text (procedure and condition
        have a length limit, whose check refuses one). Kept, it would break
        the page when the person opens the link, after the link was used
        up."""
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "prepare_appeal",
                    "arguments": {"letter_text": LETTER + " \ud800"},
                },
            }
        )
        self.assertIn("\\ud800", body)

        async def send():
            async with running_app(prepare_routes()) as http:
                return await http.post("/mcp", content=body, headers=MCP_HEADERS)

        response = async_to_sync(send)()
        self.assertEqual(response.status_code, 200)
        result = response.json()["result"]
        self.assertFalse(result["isError"], response.text[:300])
        path, code = result["structuredContent"]["url"][len(SITE) :].split("#")
        opened = self.client.post(path, {"token": code})
        self.assertEqual(opened.status_code, 200)
        self.assertContains(opened, LETTER)

    async def test_each_refusal_is_short_and_never_repeats_the_input(self):
        cases = {
            "short": ({"letter_text": "Acme denied it."}, "too short"),
            "long": ({"letter_text": "Acme denied it. " * 1300}, "at most 20,000"),
            "controls only": ({"letter_text": "\x00" * 40 + "Acme"}, "too short"),
            "two-line procedure": (
                {"letter_text": LETTER, "procedure": "Acme MRI\nlumbar"},
                "procedure must be one line",
            ),
            "long condition": (
                {"letter_text": LETTER, "condition": "Acme " * 41},
                "condition is too long",
            ),
            "no letter": ({"procedure": "Acme MRI"}, "letter_text is required"),
        }
        for name, (arguments, expected) in cases.items():
            with self.subTest(case=name):
                result = await prepare(arguments)
                self.assertTrue(result.isError)
                said = text_of(result)
                self.assertIn(expected, said)
                self.assertNotIn("Acme", said)
                self.assertNotIn("pydantic", said)
                self.assertLess(len(said), 300)
        self.assertEqual(await models.AssistantHandoff.objects.acount(), 0)

    async def test_a_letter_counted_after_cleaning_fits_at_the_limit(self):
        letter = ("x" * 99 + "\r\n") * 200  # 20,200 raw, 20,000 cleaned
        result = await prepare({"letter_text": letter})
        self.assertFalse(result.isError, text_of(result))
        self.assertEqual(
            result.structuredContent["received"]["letter_characters"], 20_000 - 1
        )

    @override_settings(MCP_PREPARE_APPEAL_MAX_LIVE=1)
    async def test_at_the_live_cap_it_points_to_start_appeal(self):
        self.assertFalse((await prepare({"letter_text": LETTER})).isError)
        refused = await prepare({"letter_text": LETTER})
        self.assertTrue(refused.isError)
        self.assertIn(mcp_server.AT_CAPACITY, text_of(refused))
        self.assertNotIn("Acme", text_of(refused))

    @override_settings(MCP_PREPARE_APPEAL_MAX_PER_MINUTE=1)
    async def test_at_the_per_minute_cap_it_points_to_start_appeal(self):
        self.assertFalse((await prepare({"letter_text": LETTER})).isError)
        refused = await prepare({"letter_text": LETTER})
        self.assertTrue(refused.isError)
        self.assertIn(mcp_server.AT_CAPACITY, text_of(refused))

    async def test_its_steps_are_the_scan_pages_after_its_own_two(self):
        data = (await prepare({"letter_text": LETTER})).structuredContent
        steps = data["steps"]
        scan = list(mcp_server.SCAN_STEPS)
        self.assertEqual(steps[2:], scan[2:])
        self.assertIn("within 2 hours", steps[0])
        self.assertIn("works once", steps[0])
        self.assertIn("'Open my appeal form'", steps[1])
        landing = (TEMPLATES / "assistant_handoff.html").read_text()
        self.assertIn(">Open my appeal form</button>", landing)
        self.assertEqual(
            assistant_handoff.HANDOFF_TTL, timedelta(hours=2), "the steps say 2 hours"
        )
        # start_appeal still gives the same list for the scan page.
        started = (await call("start_appeal", {})).structuredContent["steps"]
        self.assertEqual(started, scan)

    async def test_its_privacy_note_says_what_happens_to_the_letter(self):
        data = (await prepare({"letter_text": LETTER})).structuredContent
        said = data["privacy"]
        self.assertIn("encrypted, with a key only the link carries", said)
        # What happens to a link nobody opens, and to backups, said exactly:
        # it isn't deleted the moment it runs out.
        self.assertIn("Opening the link deletes it.", said)
        self.assertIn(
            "stops working after 2 hours, and what it held is deleted soon after", said
        )
        self.assertIn("backups made before then keep a locked copy", said)
        self.assertNotIn("at most 2 hours", said)
        self.assertIn("Nothing becomes an appeal until the person presses Submit", said)
        self.assertIn("'Use outside AI services to get more appeal drafts'", said)
        self.assertIn("ticked by default", said)
        # The same wording the scan page uses for what the site keeps.
        page = (TEMPLATES / "scrub.html").read_text()
        self.assertIn(
            "We keep this text to improve our AI, and people on our team may read it.",
            page,
        )

    def test_it_tells_the_person_what_was_sent_and_how_long_it_is_kept(self):
        marker = "zebrafinchitis"
        letter = f"{LETTER} {marker} " + "The plan excludes it. " * 60
        result = async_to_sync(prepare)(
            {"letter_text": letter, "procedure": "MRI", "condition": "back pain"}
        )
        self.assertFalse(result.isError, text_of(result))
        data = result.structuredContent
        tell = data["tell_the_person"]
        self.assertIn("aren't written yet", tell)
        characters = data["received"]["letter_characters"]
        self.assertGreater(characters, 1000)
        self.assertIn(f"(about {characters:,} characters)", tell)
        self.assertIn(
            ', plus the treatment "MRI" and the condition "back pain", so it '
            "could fill in its free appeal form.",
            tell,
        )
        # The lifetime the link really has.
        self.assertEqual(data["expires_in_minutes"], 120)
        self.assertIn("stops working after 2 hours", tell)
        # The real privacy policy page, from urls.py.
        policy = reverse("privacy_policy")
        self.assertTrue(tell.endswith(f"privacy policy: {SITE}{policy}"), tell)
        self.assertEqual(self.client.get(policy).status_code, 200)
        # Said the way the privacy note and the dead-link page say it.
        for words in (
            "and what it held is deleted soon after",
            "keep a locked copy, which can't be opened without the link",
        ):
            with self.subTest(words=words):
                self.assertIn(words, tell)
                self.assertIn(words, data["privacy"])
        self.assertIn("encrypted, with a key only the", tell)
        self.assertIn("deletes it when you open the link", tell)
        self.assertIn("Nothing becomes part of an appeal unless you submit", tell)
        landing = (TEMPLATES / "assistant_handoff.html").read_text()
        self.assertIn(
            "as soon as the link is opened, or soon after it runs out", landing
        )
        # Counted, never quoted.
        for piece in (
            "Acme",
            "{{FIRST_NAME}}",
            "medically necessary",
            marker,
            "plan excludes",
        ):
            with self.subTest(piece=piece):
                self.assertNotIn(piece, tell)

    async def test_it_names_the_treatment_and_condition_only_when_they_were_sent(
        self,
    ):
        after_count = f"(about {len(LETTER)} characters)"
        cases = {
            "neither": ({}, f"{after_count} so it could fill in"),
            # Blank ones are kept as nothing, so they weren't sent.
            "blank ones": (
                {"procedure": "  ", "condition": "\t"},
                f"{after_count} so it could fill in",
            ),
            "treatment": (
                {"procedure": "MRI"},
                f'{after_count}, plus the treatment "MRI", so it',
            ),
            "condition": (
                {"condition": "back pain"},
                f'{after_count}, plus the condition "back pain", so it',
            ),
            "both": (
                {"procedure": "MRI", "condition": "back pain"},
                f'{after_count}, plus the treatment "MRI" and the condition '
                '"back pain", so it',
            ),
        }
        for name, (fields, expected) in cases.items():
            with self.subTest(case=name):
                result = await prepare({"letter_text": LETTER, **fields})
                self.assertFalse(result.isError, text_of(result))
                tell = result.structuredContent["tell_the_person"]
                self.assertIn(expected, tell)
                sent = result.structuredContent["received"]
                self.assertEqual("treatment" in tell, "procedure" in sent)
                self.assertEqual("condition" in tell, "condition" in sent)

    async def test_the_lifetime_it_tells_comes_from_the_setting(self):
        for lifetime, words in (
            (timedelta(hours=1), "after 1 hour,"),
            (timedelta(minutes=90), "after 90 minutes,"),
            (timedelta(hours=3), "after 3 hours,"),
        ):
            with self.subTest(lifetime=lifetime):
                with mock.patch.object(assistant_handoff, "HANDOFF_TTL", lifetime):
                    result = await prepare({"letter_text": LETTER})
                self.assertFalse(result.isError, text_of(result))
                self.assertIn(words, result.structuredContent["tell_the_person"])
        self.assertEqual(mcp_server._lifetime_words(timedelta(hours=2)), "2 hours")

    async def test_the_body_cap_is_128_kib_only_while_it_is_on(self):
        body = json.dumps({**INITIALIZE, "padding": "x" * (100 * 1024)})
        async with running_app(prepare_routes()) as http:
            on = await http.post("/mcp", content=body, headers=MCP_HEADERS)
            too_big = await http.post(
                "/mcp",
                content=json.dumps({**INITIALIZE, "padding": "x" * (130 * 1024)}),
                headers=MCP_HEADERS,
            )
        with override_settings(MCP_PREPARE_APPEAL_ENABLED=False):
            routes = mcp_server.mcp_asgi_routes(django_app())
        async with running_app(routes) as http:
            off = await http.post("/mcp", content=body, headers=MCP_HEADERS)
        self.assertEqual(on.status_code, 200)
        self.assertEqual(too_big.status_code, 413)
        self.assertEqual(off.status_code, 413)

    async def test_a_twenty_thousand_character_letter_in_another_script_fits(self):
        # Cyrillic, sent the way json.dumps sends it: each letter escaped as
        # six bytes, so about 120 KB, over the read-only tools' 64 KiB.
        call_body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "prepare_appeal",
                    "arguments": {"letter_text": "\u0434" * 20_000},
                },
            }
        )
        self.assertGreater(len(call_body), 64 * 1024)
        async with running_app(prepare_routes()) as http:
            response = await http.post("/mcp", content=call_body, headers=MCP_HEADERS)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["result"]["isError"], response.text[:300])

    async def test_get_page_never_reads_the_handoff_page(self):
        for url in ("/from-your-assistant", "/from-your-assistant/"):
            with self.subTest(url=url):
                with mock.patch.object(mcp_server, "_get_in_process") as fetch:
                    result = await call(
                        "get_page", {"url": url}, routes=prepare_routes()
                    )
                self.assertTrue(result.isError)
                fetch.assert_not_called()

    def test_the_link_can_point_at_a_local_server_for_trying_it(self):
        with override_settings(MCP_HANDOFF_ORIGIN="http://127.0.0.1:8000/"):
            result = async_to_sync(prepare)({"letter_text": LETTER})
        self.assertRegex(
            result.structuredContent["url"],
            r"^http://127\.0\.0\.1:8000/from-your-assistant#[A-Za-z0-9_-]{43}$",
        )


@override_settings(**PREPARE_ON)
class PrepareAppealIsNotLoggedTest(TestCase):
    async def collect_logs(self, arguments_list) -> tuple[list[str], list[str]]:
        lines: list[str] = []
        sdk_records: list[str] = []
        sink = logger.add(lambda m: lines.append(str(m)), level="DEBUG")

        class Collect(logging.Handler):
            def emit(self, record):
                lines.append(record.getMessage())
                if record.exc_info:
                    lines.append(logging.Formatter().formatException(record.exc_info))
                if record.name.startswith("mcp.server"):
                    sdk_records.append(record.name)

        enable_sdk_loggers()
        handler = Collect(level=logging.DEBUG)
        root = logging.getLogger()
        old_level = root.level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        results = []
        try:
            for arguments in arguments_list:
                results.append(await prepare(arguments))
        finally:
            root.setLevel(old_level)
            root.removeHandler(handler)
            logger.remove(sink)
        self.assertTrue(
            sdk_records, "the MCP SDK logged nothing, so this proves nothing"
        )
        return lines, results

    async def test_a_marker_letter_never_reaches_the_logs(self):
        marker = "zebrafinchitis"
        lines, results = await self.collect_logs(
            [
                {"letter_text": f"{LETTER} {marker}", "condition": marker},
                {"letter_text": marker},  # refused as too short
            ]
        )
        self.assertFalse(results[0].isError)
        self.assertTrue(results[1].isError)
        self.assertEqual([line for line in lines if marker in line], [])

    async def test_nor_when_storing_it_fails(self):
        marker = "zebrafinchitis"
        with mock.patch.object(
            models.AssistantHandoff.objects,
            "create",
            side_effect=DatabaseError(f"insert failed near {marker}"),
        ):
            lines, results = await self.collect_logs(
                [{"letter_text": f"{LETTER} {marker}", "procedure": marker}]
            )
        self.assertTrue(results[0].isError)
        self.assertIn("something went wrong on our side", text_of(results[0]))
        self.assertNotIn(marker, text_of(results[0]))
        self.assertTrue(
            any("prepare_appeal failed with DatabaseError" in line for line in lines),
            "the failure itself is logged, by its type",
        )
        self.assertEqual([line for line in lines if marker in line], [])

    async def test_nor_when_the_call_is_malformed(self):
        # The SDK's session logs a message it can't validate through the
        # root logger, not an mcp.* one, quoting the message: a tools/call
        # sent without an id (so it reads as a notification) in full, and a
        # request whose params don't fit by pydantic's error text, which
        # quotes the start and end of the input.
        marker = "zebrafinchitis"
        letter = f"{LETTER} {marker}"
        malformed = [
            {
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {
                    "name": "prepare_appeal",
                    "arguments": {"letter_text": letter},
                },
            },
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {"name": "prepare_appeal", "arguments": letter},
            },
        ]
        lines: list[str] = []
        records: list[logging.LogRecord] = []

        class Collect(logging.Handler):
            def emit(self, record):
                records.append(record)
                lines.append(record.getMessage())
                if record.exc_info:
                    lines.append(logging.Formatter().formatException(record.exc_info))

        enable_sdk_loggers()
        handler = Collect(level=logging.DEBUG)
        root = logging.getLogger()
        old_level = root.level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        statuses = []
        try:
            async with running_app(prepare_routes()) as http:
                for message in malformed:
                    response = await http.post(
                        "/mcp", json=message, headers=MCP_HEADERS
                    )
                    statuses.append(response.status_code)
        finally:
            root.setLevel(old_level)
            root.removeHandler(handler)
        self.assertEqual(statuses, [202, 200])
        self.assertEqual([line for line in lines if marker in line], [])
        withheld = [
            r for r in records if r.name == "root" and "withheld" in r.getMessage()
        ]
        # Both warnings still happen, so a broken client shows up in the logs.
        self.assertGreaterEqual(
            len([r for r in withheld if r.levelno == logging.WARNING]), 2, lines
        )


class AppealChecklistTest(TestCase):
    async def checklist(self, arguments: dict[str, Any], routes=None) -> dict:
        result = await call("get_appeal_checklist", arguments, routes=routes)
        self.assertFalse(result.isError, text_of(result))
        return result.structuredContent

    async def test_each_item_for_the_form_is_quoted_from_its_page(self):
        data = await self.checklist({})
        sources = {
            mcp_server.SCAN_EVERY_PAGE: "scrub.html",
            mcp_server.SCAN_EMAIL_WHY: "scrub.html",
            mcp_server.HEALTH_HISTORY_EXAMPLES: "health_history.html",
            mcp_server.PLAN_DOCUMENTS_EXAMPLES: "plan_documents.html",
        }
        quoted = {i.get("from_the_site") for i in data["for_the_form"]}
        for quote, template in sources.items():
            with self.subTest(template=template):
                self.assertIn(quote, quoted)
                page = " ".join((TEMPLATES / template).read_text().split())
                self.assertIn(quote, page)
        items = {i["item"]: i for i in data["for_the_form"]}
        self.assertEqual(
            items["Claim ID / Reference Number"]["from_the_site"],
            "From your denial letter or Explanation of Benefits (EOB).",
        )
        self.assertIn("How do you get your insurance?", items)
        self.assertIn("When was the denial letter dated?", items)
        for item in data["for_the_form"]:
            with self.subTest(item=item["item"]):
                self.assertEqual(item["url"], f"{SITE}/scan")

    async def test_what_is_worth_gathering_is_quoted_from_the_glossary(self):
        data = await self.checklist({})
        gathered = {i["item"]: i for i in data["worth_gathering"]}
        for slug in (
            "explanation-of-benefits",
            "letter-of-medical-necessity",
            "evidence-of-coverage",
            "summary-of-benefits-and-coverage",
            "medical-policy",
        ):
            term = glossary.get_term(slug)
            with self.subTest(slug=slug):
                self.assertEqual(gathered[term.term]["what_it_is"], term.short)
                self.assertEqual(gathered[term.term]["url"], f"{SITE}/glossary/{slug}/")
        letter = gathered["Letter of Medical Necessity"]["why"]
        self.assertIn(
            letter, glossary.get_term("letter-of-medical-necessity").definition
        )
        self.assertIn("deciding factor", letter)
        records = gathered["Medical records"]["from_the_site"]
        self.assertIn(records, glossary.get_term("hipaa").definition)
        self.assertEqual(
            data["expedited_appeal"]["what_it_is"],
            glossary.get_term("expedited-appeal").short,
        )

    async def test_a_named_reason_gets_the_librarys_own_list(self):
        data = await self.checklist({"denial_reason": "experimental"})
        reason = data["for_this_denial_reason"]
        self.assertTrue(reason["exact_match"])
        library = mcp_server._denial_phrases()[reason["reason"]]
        self.assertEqual(reason["how_to_counter"], library["how_to_counter"][:10])

    async def test_a_weak_match_gets_candidates_not_a_guess(self):
        data = await self.checklist({"denial_reason": "benefit exclusion"})
        reason = data["for_this_denial_reason"]
        self.assertFalse(reason["exact_match"])
        self.assertTrue(reason["candidates"])
        self.assertNotIn("how_to_counter", reason)
        self.assertNotIn("benefit exclusion", json.dumps(reason))

    async def test_an_unclear_plan_gets_no_deadline_number(self):
        for plan in (None, "Don't know", "Other", "Veterans Affairs"):
            with self.subTest(plan=plan):
                data = await self.checklist({"plan_source": plan} if plan else {})
                self.assertNotRegex(data["deadlines"], r"\d")
                self.assertIn("check the denial letter", data["deadlines"])
        employer = await self.checklist({"plan_source": "Employer -- Private"})
        self.assertIn("180 days", employer["deadlines"])

    async def test_a_state_gets_its_help_page(self):
        data = await self.checklist({"state": "CA"})
        self.assertEqual(data["state_help"]["url"], f"{SITE}/state-help/california/")
        self.assertIn("external_review", data["state_help"])

    async def test_it_mentions_prepare_appeal_only_while_it_is_on(self):
        off = await self.checklist({})
        on = await self.checklist({}, routes=prepare_routes())
        self.assertNotIn("prepare_appeal", off["start"])
        self.assertIn("prepare_appeal", on["start"])
        self.assertEqual(on["start"]["url"], f"{SITE}/scan")

    def test_it_writes_nothing(self):
        with CaptureQueriesContext(connection) as queries:
            result = async_to_sync(call)(
                "get_appeal_checklist",
                {
                    "plan_source": "Medicaid",
                    "state": "NY",
                    "denial_reason": "experimental",
                },
            )
        self.assertFalse(result.isError, text_of(result))
        self.assertEqual(writes_in(queries), [])


class PlanKindAndPeerToPeerTest(TestCase):
    """The two things the first live test wanted and couldn't get."""

    async def test_appeal_rights_say_how_to_tell_an_insured_plan_from_a_self_funded_one(
        self,
    ):
        data = (
            await call(
                "get_appeal_rights",
                {"plan_source": "Employer -- Private", "state": "CA"},
            )
        ).structuredContent
        note = data["how_to_tell_which_kind_of_plan"]
        self.assertIn("self-funded", note["why_it_matters"])
        self.assertEqual(len(note["how_to_tell"]), 3)
        self.assertIn(
            "Is our plan fully insured or self-funded?", note["how_to_tell"][2]
        )
        self.assertTrue(note["erisa"]["url"].endswith("/glossary/erisa/"))

    async def test_the_checklist_mentions_peer_to_peer(self):
        data = (await call("get_appeal_checklist", {})).structuredContent
        self.assertIn("doctor", data["peer_to_peer"]["what_it_is"])
        self.assertTrue(
            data["peer_to_peer"]["url"].endswith("/glossary/peer-to-peer-review/")
        )

    async def test_medical_necessity_explains_peer_to_peer_and_timely_filing_does_not(
        self,
    ):
        nmn = (
            await call("explain_denial_reason", {"phrase": "not medically necessary"})
        ).structuredContent
        self.assertIn("peer_to_peer", nmn)
        late = (
            await call(
                "explain_denial_reason", {"phrase": "timely filing limit exceeded"}
            )
        ).structuredContent
        self.assertNotIn("peer_to_peer", late)

    async def test_start_appeal_says_the_guide_already_gives_its_link(self):
        async with mcp_session() as session:
            tools = {t.name: t for t in (await session.list_tools()).tools}
        said = " ".join(tools["start_appeal"].description.split())
        self.assertIn("find_treatment_guide returns this same link", said)


class ToolCallCountersTest(TestCase):
    """Every call is counted by tool and outcome, never by what was sent."""

    @staticmethod
    def _count(tool, outcome):
        return (
            REGISTRY.get_sample_value(
                "fhi_mcp_tool_calls_total", {"tool": tool, "outcome": outcome}
            )
            or 0
        )

    async def test_a_good_call_counts_as_ok_and_is_timed(self):
        before = self._count("get_state_help", "ok")
        timed = (
            REGISTRY.get_sample_value(
                "fhi_mcp_tool_call_seconds_count", {"tool": "get_state_help"}
            )
            or 0
        )
        result = await call("get_state_help", {"state": "CA"})
        self.assertFalse(result.isError)
        self.assertEqual(self._count("get_state_help", "ok"), before + 1)
        self.assertEqual(
            REGISTRY.get_sample_value(
                "fhi_mcp_tool_call_seconds_count", {"tool": "get_state_help"}
            ),
            timed + 1,
        )

    async def test_an_undeclared_argument_counts_as_refused_and_is_timed(self):
        before = self._count("get_state_help", "refused")
        timed = (
            REGISTRY.get_sample_value(
                "fhi_mcp_tool_call_seconds_count", {"tool": "get_state_help"}
            )
            or 0
        )
        result = await call("get_state_help", {"state": "CA", "letter": "x"})
        self.assertTrue(result.isError)
        self.assertEqual(self._count("get_state_help", "refused"), before + 1)
        self.assertEqual(
            REGISTRY.get_sample_value(
                "fhi_mcp_tool_call_seconds_count", {"tool": "get_state_help"}
            ),
            timed + 1,
        )

    async def test_a_page_the_site_cannot_serve_counts_as_failed_not_refused(self):
        before_failed = self._count("get_page", "failed")
        before_refused = self._count("get_page", "refused")
        with mock.patch.object(mcp_server, "_get_in_process", return_value=(500, "")):
            result = await call("get_page", {"url": f"{SITE}/about-us"})
        self.assertTrue(result.isError)
        self.assertIn("something went wrong on our side", text_of(result))
        self.assertEqual(self._count("get_page", "failed"), before_failed + 1)
        self.assertEqual(self._count("get_page", "refused"), before_refused)

    async def test_a_page_that_is_not_there_counts_as_refused(self):
        before = self._count("get_page", "refused")
        with mock.patch.object(mcp_server, "_get_in_process", return_value=(404, "")):
            result = await call("get_page", {"url": f"{SITE}/about-us"})
        self.assertTrue(result.isError)
        self.assertEqual(self._count("get_page", "refused"), before + 1)

    async def test_an_unknown_tool_is_counted_under_unknown_not_its_name(self):
        before = self._count("unknown", "refused")
        result = await call("no_such_tool", {})
        self.assertTrue(result.isError)
        self.assertEqual(self._count("unknown", "refused"), before + 1)
        self.assertIsNone(
            REGISTRY.get_sample_value(
                "fhi_mcp_tool_calls_total",
                {"tool": "no_such_tool", "outcome": "refused"},
            )
        )


# ---------------------------------------------------------------------------
# Stage 3: the chat path, letters drafted and brought back to the chat
# ---------------------------------------------------------------------------

CHAT_ON = {
    **PREPARE_ON,
    "MCP_DRAFT_IN_CHAT_ENABLED": True,
    "MCP_HANDOFF_V2_ENABLED": True,
    "TEMPORAL_ENABLED": True,
    "TEMPORAL_APPEAL_JOURNEY_ENABLED": True,
    "TEMPORAL_PAYLOAD_KEY": "test-key",
}
CHAT_TOOLS = {"draft_appeal_in_chat", "get_appeal_drafts", "answer_appeal_questions"}
NOT_YOURS = "Don't open the link or fill in the form yourself"
SIGNAL = "fighthealthinsurance.temporal_client.signal_assistant_answers_filed"


def chat_routes():
    """asgi.py's routes as a server built with the chat path on."""
    with override_settings(**CHAT_ON):
        return mcp_server.mcp_asgi_routes(django_app())


def chat_off_routes():
    """prepare_appeal on, the chat path off."""
    with override_settings(**{**CHAT_ON, "MCP_DRAFT_IN_CHAT_ENABLED": False}):
        return mcp_server.mcp_asgi_routes(django_app())


def a_chat_denial():
    return models.Denial.objects.create(
        hashed_email=models.Denial.get_hashed_email("chat@example.com"),
        denial_text="The MRI was denied as not medically necessary.",
        insurance_company="Example Health",
        channel="assistant",
    )


class ChatPathListingTest(TestCase):
    def setUp(self):
        # After the conftest fixture that turns Temporal off.
        self.enterContext(override_settings(**CHAT_ON))

    async def test_the_chat_tools_are_listed_only_with_every_flag_on(self):
        self.assertLessEqual(CHAT_TOOLS, await tool_names(chat_routes()))
        for flag in CHAT_ON:
            with self.subTest(flag=flag):
                with override_settings(**{**CHAT_ON, flag: False}):
                    routes = mcp_server.mcp_asgi_routes(django_app())
                names = await tool_names(routes)
                self.assertFalse(CHAT_TOOLS & names)
                self.assertIn("start_appeal", names)

    async def test_their_hints(self):
        async with mcp_session(chat_routes()) as session:
            tools = {t.name: t for t in (await session.list_tools()).tools}
        self.assertEqual(len(tools), 14)
        hints = {
            name: (
                t.annotations.readOnlyHint,
                t.annotations.destructiveHint,
                t.annotations.idempotentHint,
                t.annotations.openWorldHint,
            )
            for name, t in tools.items()
        }
        self.assertEqual(hints["draft_appeal_in_chat"], (False, False, False, False))
        self.assertEqual(hints["get_appeal_drafts"], (True, False, True, False))
        self.assertEqual(hints["answer_appeal_questions"], (False, False, True, True))
        writers = {name for name, h in hints.items() if not h[0]}
        self.assertEqual(
            writers, {"prepare_appeal", "draft_appeal_in_chat", "answer_appeal_questions"}
        )

    async def test_their_inputs(self):
        async with mcp_session(chat_routes()) as session:
            tools = {t.name: t for t in (await session.list_tools()).tools}
        draft = tools["draft_appeal_in_chat"].inputSchema
        self.assertEqual(
            set(draft["properties"]), {"letter_text", "procedure", "condition"}
        )
        get = tools["get_appeal_drafts"].inputSchema
        self.assertEqual(set(get["properties"]), {"draft_id", "seen", "wait"})
        self.assertEqual(get["properties"]["wait"]["maximum"], 40)
        answer = tools["answer_appeal_questions"].inputSchema
        self.assertEqual(set(answer["properties"]), {"draft_id", "answers"})
        self.assertEqual(answer["properties"]["answers"]["maxItems"], 60)
        for tool in tools.values():
            with self.subTest(tool=tool.name):
                self.assertIs(tool.inputSchema.get("additionalProperties"), False)
                self.assertIn("free", tool.description.lower())

    async def test_each_new_description_says_not_to_open_the_link(self):
        async with mcp_session(chat_routes()) as session:
            tools = {t.name: t for t in (await session.list_tools()).tools}
        for name in CHAT_TOOLS:
            with self.subTest(tool=name):
                self.assertIn(NOT_YOURS, tools[name].description)

    async def test_the_welcome_and_the_refusal_name_both_paths(self):
        async with mcp_session(chat_routes()) as session:
            init = session.init_result
            refused = await session.call_tool(
                "search_site", {"query": "turning 26", "letter": "x"}
            )
        self.assertEqual(init.instructions, mcp_server.INSTRUCTIONS_WITH_CHAT)
        self.assertIn("draft_appeal_in_chat", init.instructions)
        self.assertIn("prepare_appeal", init.instructions)
        self.assertIn("ask before calling either", init.instructions)
        self.assertIn(mcp_server.LINK_OPENS_FIRST_STEP, init.instructions)
        self.assertIn(mcp_server.UNDECLARED_NOTE_WITH_CHAT, text_of(refused))

    def test_with_the_chat_path_off_nothing_names_it(self):
        for text in (
            mcp_server.INSTRUCTIONS,
            mcp_server.INSTRUCTIONS_WITH_PREPARE,
            mcp_server.UNDECLARED_NOTE,
            mcp_server.UNDECLARED_NOTE_WITH_PREPARE,
        ):
            with self.subTest(text=text[:40]):
                self.assertNotIn("draft_appeal_in_chat", text)
                self.assertNotIn("answer_appeal_questions", text)

    async def test_start_appeal_names_the_chat_path_and_caregivers_only_while_on(self):
        async with mcp_session(chat_routes()) as session:
            on = {t.name: t for t in (await session.list_tools()).tools}
            data = (await session.call_tool("start_appeal", {})).structuredContent
        async with mcp_session(chat_off_routes()) as session:
            off = {t.name: t for t in (await session.list_tools()).tools}
        self.assertIn("offer draft_appeal_in_chat", on["start_appeal"].description)
        self.assertIn("helping a family member", on["start_appeal"].description)
        self.assertIn("draft_appeal_in_chat", data["privacy"])
        self.assertNotIn("draft_appeal_in_chat", off["start_appeal"].description)

    async def test_the_checklist_names_the_chat_path_only_while_on(self):
        on = (await call("get_appeal_checklist", {}, routes=chat_routes())).structuredContent
        off = (
            await call("get_appeal_checklist", {}, routes=chat_off_routes())
        ).structuredContent
        self.assertIn("draft_appeal_in_chat", on["start"])
        self.assertNotIn("draft_appeal_in_chat", off["start"])


class ChatPathToolsTest(TestCase):
    def setUp(self):
        self.enterContext(override_settings(**CHAT_ON))
        mcp_server._WAITING_ON.clear()

    def _draft(self, letters: int = 0, status: str = "drafting", **fields):
        from fighthealthinsurance import assistant_drafts

        denial = a_chat_denial()
        for i in range(letters):
            models.ProposedAppeal.objects.create(
                for_denial=denial,
                appeal_text="Dear Example Health, I am writing to appeal the "
                "denial of my MRI, which my doctor ordered as medically "
                f"necessary. Please reverse it. Sincerely, {{{{FIRST_NAME}}}} {i}",
            )
        new = assistant_drafts.create_draft(denial)
        models.AssistantDraft.objects.filter(pk=new.draft.pk).update(
            status=status, status_at=timezone.now(), **fields
        )
        return denial, new.draft_id

    def test_draft_appeal_in_chat_returns_a_link_and_a_draft_id(self):
        result = async_to_sync(call)(
            "draft_appeal_in_chat",
            {"letter_text": LETTER, "procedure": "MRI"},
            routes=chat_routes(),
        )
        self.assertFalse(result.isError, text_of(result))
        data = result.structuredContent
        self.assertEqual(
            set(data),
            {
                "status",
                "url",
                "draft_id",
                "tell_the_person",
                "steps",
                "expires_at",
                "privacy",
                "next",
            },
        )
        self.assertEqual(data["status"], "waiting_for_agreement")
        self.assertEqual(data["next"], "stop_and_tell_the_person")
        self.assertRegex(data["url"], HANDOFF_LINK)
        self.assertEqual(data["privacy"], mcp_server.DRAFT_IN_CHAT_PRIVACY)
        self.assertIn("becomes an appeal", data["tell_the_person"])
        self.assertNotIn("Acme Health", json.dumps(data))
        code = data["url"].split("#")[1]
        content = assistant_handoff.claim_handoff(code, consume=False)
        self.assertEqual(content.kind, "chat")
        self.assertEqual(
            models.AssistantDraft.objects.get().pk, content.draft
        )

    def test_paused_it_returns_site_only_with_a_site_link_in_the_same_call(self):
        with override_settings(MCP_DRAFT_IN_CHAT_PAUSED=True):
            result = async_to_sync(call)(
                "draft_appeal_in_chat", {"letter_text": LETTER}, routes=chat_routes()
            )
        data = result.structuredContent
        self.assertEqual(data["status"], "site_only")
        self.assertEqual(data["next"], "finish_on_site")
        self.assertNotIn("draft_id", data)
        self.assertEqual(data["steps"], list(mcp_server.PREPARED_FORM_STEPS))
        self.assertTrue(data["tell_the_person"].startswith(mcp_server.PAUSED_FOR_THE_CHAT))
        content = assistant_handoff.claim_handoff(data["url"].split("#")[1], consume=False)
        self.assertEqual(content.kind, "site")
        self.assertFalse(models.AssistantDraft.objects.exists())

    def test_with_the_budget_spent_it_returns_site_only(self):
        with mock.patch(
            "fighthealthinsurance.ml.spend.assistant_budget_left", return_value=False
        ):
            result = async_to_sync(call)(
                "draft_appeal_in_chat", {"letter_text": LETTER}, routes=chat_routes()
            )
        self.assertEqual(result.structuredContent["status"], "site_only")

    def test_an_unknown_draft_id_returns_at_once_even_with_a_wait(self):
        started = time.monotonic()
        result = async_to_sync(call)(
            "get_appeal_drafts",
            {"draft_id": "x" * 43, "seen": "expired", "wait": 40},
            routes=chat_routes(),
        )
        self.assertLess(time.monotonic() - started, 20)
        self.assertEqual(result.structuredContent["status"], "expired")
        self.assertEqual(result.structuredContent["next"], "stop_and_tell_the_person")

    def test_a_status_other_than_seen_returns_at_once(self):
        _, draft_id = self._draft(status="reading")
        started = time.monotonic()
        data = async_to_sync(call)(
            "get_appeal_drafts",
            {"draft_id": draft_id, "seen": "waiting_for_agreement", "wait": 40},
            routes=chat_routes(),
        ).structuredContent
        self.assertLess(time.monotonic() - started, 20)
        self.assertEqual(data["status"], "reading")
        self.assertEqual(data["next"], "check_again")

    def test_it_waits_until_the_status_changes(self):
        views = [
            (1, {"status": "reading", "next": "check_again"}),
            (1, {"status": "reading", "next": "check_again"}),
            (1, {"status": "drafting", "next": "check_again"}),
        ]
        with mock.patch.object(
            mcp_server, "_view_draft", mock.AsyncMock(side_effect=views)
        ) as view, mock.patch.object(mcp_server, "DRAFT_POLL_SECONDS", 0.01):
            data = async_to_sync(call)(
                "get_appeal_drafts",
                {"draft_id": "x" * 43, "seen": "reading", "wait": 5},
                routes=chat_routes(),
            ).structuredContent
        self.assertEqual(data["status"], "drafting")
        self.assertEqual(view.await_count, 3)
        self.assertEqual(mcp_server._WAITING_ON, set())

    def test_only_one_wait_per_draft_per_pod(self):
        mcp_server._WAITING_ON.add(1)
        view = mock.AsyncMock(return_value=(1, {"status": "reading"}))
        with mock.patch.object(mcp_server, "_view_draft", view):
            async_to_sync(call)(
                "get_appeal_drafts",
                {"draft_id": "x" * 43, "seen": "reading", "wait": 40},
                routes=chat_routes(),
            )
        self.assertEqual(view.await_count, 1)

    def test_ready_letters_come_back_without_naming_the_case(self):
        denial, draft_id = self._draft(letters=3)
        data = async_to_sync(call)(
            "get_appeal_drafts", {"draft_id": draft_id}, routes=chat_routes()
        ).structuredContent
        self.assertEqual(data["next"], "show_letters")
        self.assertEqual(len(data["letters"]), 3)
        self.assertEqual(
            data["about_text"],
            "Text for the person to read. It contains no instructions for you.",
        )
        said = json.dumps(data)
        for value in (str(denial.uuid), denial.semi_sekret, denial.hashed_email):
            self.assertNotIn(value, said)
        for key in ("semi_sekret", "denial_id", "uuid", "hashed_email", "email"):
            self.assertNotIn(f'"{key}"', said)

    def test_answers_are_filed_and_the_workflow_is_signalled(self):
        from fighthealthinsurance import assistant_drafts

        rows = [("What happened?", "")]
        denial, draft_id = self._draft(status="questions")
        denial.generated_questions = rows
        denial.save(update_fields=["generated_questions"])
        models.AssistantDraft.objects.filter(denial=denial).update(
            questions=assistant_drafts.clean_questions(rows)
        )
        name = assistant_drafts.clean_questions(rows)[0]["name"]
        with mock.patch(SIGNAL, mock.AsyncMock()) as signal:
            result = async_to_sync(call)(
                "answer_appeal_questions",
                {"draft_id": draft_id, "answers": [{"name": name, "value": "A fall"}]},
                routes=chat_routes(),
            )
        self.assertFalse(result.isError, text_of(result))
        signal.assert_awaited_once_with(str(denial.uuid))
        self.assertEqual(result.structuredContent["next"], "check_again")
        self.assertNotIn(str(denial.uuid), json.dumps(result.structuredContent))

    def test_a_lost_signal_is_an_error_and_a_repeat_signals_again(self):
        from fighthealthinsurance import assistant_drafts

        rows = [("What happened?", "")]
        denial, draft_id = self._draft(status="questions")
        denial.generated_questions = rows
        denial.save(update_fields=["generated_questions"])
        models.AssistantDraft.objects.filter(denial=denial).update(
            questions=assistant_drafts.clean_questions(rows)
        )
        arguments = {
            "draft_id": draft_id,
            "answers": [
                {"name": assistant_drafts.clean_questions(rows)[0]["name"], "value": "x"}
            ],
        }
        with mock.patch(SIGNAL, mock.AsyncMock(side_effect=RuntimeError)):
            failed = async_to_sync(call)(
                "answer_appeal_questions", arguments, routes=chat_routes()
            )
        self.assertTrue(failed.isError)
        self.assertIn("Please try again", text_of(failed))
        with mock.patch(SIGNAL, mock.AsyncMock()) as signal:
            again = async_to_sync(call)(
                "answer_appeal_questions", arguments, routes=chat_routes()
            )
        self.assertFalse(again.isError, text_of(again))
        signal.assert_awaited_once_with(str(denial.uuid))

    def test_an_unknown_answer_name_is_refused_by_name(self):
        from fighthealthinsurance import assistant_drafts

        rows = [("What happened?", "")]
        denial, draft_id = self._draft(status="questions")
        denial.generated_questions = rows
        denial.save(update_fields=["generated_questions"])
        models.AssistantDraft.objects.filter(denial=denial).update(
            questions=assistant_drafts.clean_questions(rows)
        )
        with mock.patch(SIGNAL, mock.AsyncMock()) as signal:
            result = async_to_sync(call)(
                "answer_appeal_questions",
                {"draft_id": draft_id, "answers": [{"name": "q_made_up", "value": "x"}]},
                routes=chat_routes(),
            )
        self.assertTrue(result.isError)
        self.assertIn("q_made_up", text_of(result))
        signal.assert_not_awaited()
