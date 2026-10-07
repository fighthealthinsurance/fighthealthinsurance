"""The AI assistants page serves only while the MCP server is on, and says
only what the appeal flags that are on make true."""

from bs4 import BeautifulSoup
from django.test import TestCase, override_settings
from django.urls import reverse

PAGE = "/ai-assistants"
ADDRESS = "https://www.fighthealthinsurance.com/mcp"
INSTALL_LINK = "claude.ai/customize/connectors?modal=add-custom-connector"
TITLE = "Use Fight Health Insurance with your AI assistant"
HOME_HEADING = "Lowering the appeal bar even more"
HEADLINE = "Now Your AI Can Work With Ours"
# Links to these sections are out in the world; a rewrite keeps them landing.
OLD_ANCHORS = (
    "address",
    "claude",
    "chatgpt",
    "what-it-does",
    "keep-out",
    "start-an-appeal",
    "other-assistants",
    "how-it-works",
)
EM_DASHES = ("\u2014", "&mdash;", "&#8212;", "&#x2014;")

# One marker for each flag branch of the template, so a branch that renders in
# the wrong state, or two branches that both render, fails a test. Each is
# checked against the page's text (tags stripped, spaces collapsed) and its
# HTML, so image files and the meta description count too.
ONLY_WITH_BOTH_OFF = (
    "get the link to start a free appeal.",
    "give you the link to start an appeal here",
    "When you're ready, it gives you the link to start an appeal here.",
    "ChatGPT runs the look-up tools without asking.",
    "The assistant never takes the letter.",
    "It lists the look-up tools described below.",
    "images/ai-assistants/claude-3-tool-permissions.webp",
    "images/ai-assistants/chatgpt-3-answer.webp",
    "your denial letter, your name, member ID",
)
ONLY_WITH_PREPARE_ONLY = (
    "fill in our appeal form with your letter for you to check and submit here",
    "There are two ways.",
    "Or share your denial letter with your assistant and ask it to fill in our form",
    "Open it in the browser you'll finish in.",
    "It lists 11 tools.",
    "The only tool that takes your denial letter is the one that fills in our form.",
    "We delete it when you press Open my appeal form, or soon after",
    "When you submit our form, the letter",
)
ONLY_WITH_CHAT = (
    "bring up to three draft appeal letters back to your chat",
    "To get letters back in your chat, we ask for your email address.",
    "There are three ways.",
    "For the other two, share your denial letter",
    "It works only in the browser where you first open it",
    "bring them back to the chat",
    "images/ai-assistants/terms-page.webp",
    "when we've reached our daily limit",
    "It lists 14 tools.",
    ", or get draft letters back in the chat",
    "The only tools that take your denial letter are the ones that start an appeal.",
    "(for letters in your chat)",
    "When you agree on our page, or submit our form,",
    "When letters are drafted for your chat, that record also says",
    "so the emailed link can check it's you",
    "The letters are drafts for you to read",
    "The letters are written as the patient",
    "There's a daily limit on letters drafted for chats.",
    "Three Ways It Works With Us",
    "images/ai-assistants/assistant-choices.webp",
    "images/ai-assistants/assistant-1-ask.webp",
    "images/ai-assistants/claude-3-tool-permissions-14.webp",
    "images/ai-assistants/assistant-3-questions.webp",
    "images/ai-assistants/assistant-4-letters.webp",
)
WITH_EITHER_APPEAL_TOOL = (
    "start a free appeal from your chat. No sign-in.",
    "help you start an appeal from your chat",
    "and Claude and ChatGPT ask your permission first",
    "the tools that send us something",
    "opens once.",
    "It lasts two hours.",
    "Finish on our site",
    "Needs approval",
    "Here's my denial letter.",
    "Start an appeal</h3>",
    "Keep everything else personal out of these tools: your name, member ID and",
    "We keep what your assistant sent encrypted",
)


class EveryStateChecks:
    """Held true whichever appeal flags are on; mixed into each state below."""

    page: str
    shows: tuple[str, ...]
    hides: tuple[str, ...]

    def test_each_branch_shows_only_in_its_own_state(self):
        text = " ".join(BeautifulSoup(self.page, "html.parser").get_text().split())
        on_page = [m for m in self.shows + self.hides if m in self.page or m in text]
        self.assertEqual([m for m in self.shows if m not in on_page], [], "missing")
        self.assertEqual([m for m in self.hides if m in on_page], [], "from another state")

    def test_the_top_says_what_it_is_and_how_to_set_it_up(self):
        for text in (
            TITLE,
            HEADLINE,
            "Insurers are betting you won't appeal.",
            "Rather not use an AI assistant? That's fine too.",
            "Plug Us In With MCP",
            "MCP (Model Context Protocol) is an open standard",
            "Ways It Works With Us",
            "Set It Up Once",
            ADDRESS,
            INSTALL_LINK,
        ):
            self.assertIn(text, self.page)

    def test_old_links_into_the_page_still_land(self):
        ids = {tag["id"] for tag in BeautifulSoup(self.page, "html.parser").select("[id]")}
        for anchor in OLD_ANCHORS:
            self.assertIn(anchor, ids)

    def test_every_jump_link_has_a_section_to_land_on(self):
        soup = BeautifulSoup(self.page, "html.parser")
        ids = {tag["id"] for tag in soup.select("[id]")}
        jumps = [a["href"][1:] for a in soup.select("main a[href^='#']")]
        self.assertTrue(jumps)
        for target in jumps:
            self.assertIn(target, ids)

    def test_there_is_no_em_dash_on_the_page(self):
        for dash in EM_DASHES:
            self.assertNotIn(dash, self.page)


@override_settings(MCP_SERVER_ENABLED=True, MCP_PREPARE_APPEAL_ENABLED=False)
class PageOnTest(EveryStateChecks, TestCase):
    shows = ONLY_WITH_BOTH_OFF
    hides = ONLY_WITH_PREPARE_ONLY + ONLY_WITH_CHAT + WITH_EITHER_APPEAL_TOOL

    def setUp(self):
        self.page = self.client.get(PAGE).content.decode()

    def test_the_page_serves(self):
        self.assertEqual(self.client.get(PAGE).status_code, 200)

    def test_the_address_and_both_clients_are_on_it(self):
        for text in (
            ADDRESS,
            "Set it up in Claude",
            "Set it up in ChatGPT",
            INSTALL_LINK,
            "claude mcp add --transport http",
            "codex mcp add",
        ):
            self.assertIn(text, self.page)

    def test_it_says_what_to_keep_out_in_the_servers_words(self):
        self.assertIn("Keep everything else personal out of these tools", self.page)
        self.assertIn("member ID", self.page)

    def test_the_handoff_section_waits_for_its_flag(self):
        self.assertIn("The assistant never takes the letter.", self.page)
        self.assertNotIn("opens once.", self.page)
        self.assertNotIn("bring them back to the chat", self.page)

    def test_with_both_appeal_tools_off_it_offers_only_the_link(self):
        self.assertIn("give you the link to start an appeal here", self.page)
        self.assertIn("your denial letter, your name, member ID", self.page)
        self.assertIn("It lists the look-up tools described below.", self.page)
        for text in (
            "help you start an appeal from your chat",
            "Here's my denial letter.",
            "Start an appeal</h3>",
            "Needs approval",
            "We keep what your assistant sent encrypted",
        ):
            self.assertNotIn(text, self.page)

    def test_chatgpt_is_not_said_to_ask_before_tools_that_are_not_there(self):
        self.assertEqual(
            self.page.count("ChatGPT runs the look-up tools without asking."), 1
        )

    def test_the_footer_and_resources_link_to_it(self):
        link = f"a[href='{reverse('ai-assistants')}']"
        home = BeautifulSoup(self.client.get("/").content.decode(), "html.parser")
        footer_link = home.select_one(f"footer {link}")
        self.assertIsNotNone(footer_link)
        self.assertEqual(footer_link.get_text(strip=True), "Your AI Assistant")
        resources = BeautifulSoup(
            self.client.get("/other-resources").content.decode(), "html.parser"
        )
        self.assertIsNotNone(resources.select_one(f"main {link}"))

    def test_resources_offers_only_the_link_too(self):
        resources = self.client.get("/other-resources").content.decode()
        self.assertIn("give you the link to start a free appeal here", resources)
        self.assertNotIn("help you start a free appeal from your chat", resources)

    def test_the_sitemap_and_llms_txt_list_it(self):
        self.assertIn(PAGE, self.client.get("/sitemap.xml").content.decode())
        self.assertIn(PAGE, self.client.get("/llms.txt").content.decode())


@override_settings(MCP_SERVER_ENABLED=True, MCP_PREPARE_APPEAL_ENABLED=True)
class HandoffOnTest(EveryStateChecks, TestCase):
    shows = ONLY_WITH_PREPARE_ONLY + WITH_EITHER_APPEAL_TOOL
    hides = ONLY_WITH_BOTH_OFF + ONLY_WITH_CHAT

    def setUp(self):
        self.page = self.client.get(PAGE).content.decode()

    def test_the_handoff_section_explains_the_link(self):
        self.assertIn("It opens once.", self.page)
        self.assertIn("It lasts two hours.", self.page)

    def test_it_offers_two_ways_and_the_form_but_not_the_chat(self):
        self.assertIn("help you start an appeal from your chat", self.page)
        self.assertIn("There are two ways.", self.page)
        self.assertIn("ask it to fill in our form for you", self.page)
        self.assertIn("Finish on our site", self.page)
        self.assertNotIn("bring them back to the chat", self.page)
        self.assertNotIn("terms-page", self.page)

    def test_it_only_promises_a_browser_the_link_is_not_tied_to(self):
        self.assertIn("Open it in the browser you'll finish in.", self.page)
        self.assertNotIn("It works only in the browser where you first open it", self.page)

    def test_claude_lists_the_one_tool_that_takes_the_letter(self):
        self.assertIn("It lists 11 tools.", self.page)
        self.assertIn("The only tool that takes your denial letter", self.page)
        self.assertNotIn("It lists 14 tools.", self.page)

    def test_the_letter_example_and_start_an_appeal_card_appear(self):
        self.assertIn("Here's my denial letter.", self.page)
        self.assertIn("Start an appeal</h3>", self.page)

    def test_resources_says_it_can_help_start_an_appeal(self):
        resources = self.client.get("/other-resources").content.decode()
        self.assertIn("help you start a free appeal from your chat", resources)


CHAT_ON = dict(
    MCP_SERVER_ENABLED=True,
    MCP_PREPARE_APPEAL_ENABLED=True,
    MCP_DRAFT_IN_CHAT_ENABLED=True,
    MCP_HANDOFF_V2_ENABLED=True,
    TEMPORAL_ENABLED=True,
    TEMPORAL_APPEAL_JOURNEY_ENABLED=True,
    TEMPORAL_PAYLOAD_KEY="test-key",
)


class ChatPathTest(EveryStateChecks, TestCase):
    shows = ONLY_WITH_CHAT + WITH_EITHER_APPEAL_TOOL
    hides = ONLY_WITH_BOTH_OFF + ONLY_WITH_PREPARE_ONLY

    def setUp(self):
        # After the conftest fixture that turns Temporal off.
        self.enterContext(override_settings(**CHAT_ON))
        self.page = self.client.get(PAGE).content.decode()

    def test_the_chat_path_is_explained_when_it_is_on(self):
        self.assertIn("bring them back to the chat", self.page)
        self.assertIn("opens again in that browser until you press", self.page)
        self.assertIn("There are three ways.", self.page)
        self.assertIn("Get letters back in your chat", self.page)

    def test_it_says_the_link_is_tied_to_the_first_browser(self):
        self.assertIn(
            "It works only in the browser where you first open it", self.page
        )

    def test_claude_lists_the_three_tools_that_send_something(self):
        self.assertIn("It lists 14 tools.", self.page)
        for tool in (
            "Fill in an appeal form for the person to check",
            "Draft appeal letters to bring back to this chat",
            "Send the person's answers and start the letters",
        ):
            self.assertIn(tool, self.page)

    def test_it_does_not_promise_a_record_of_which_assistant_sent_it(self):
        # The MCP server is stateless, so a tool call never sees the name
        # the app gives itself at initialize, and the record stores none.
        self.assertNotIn("which assistant sent the letter", self.page)

    def test_it_says_what_the_assistant_sent_besides_the_letter_is_kept(self):
        self.assertIn(
            "we also keep the few words your assistant sent on what was denied",
            self.page,
        )

    def test_send_me_news_keeps_the_name_too(self):
        self.assertIn("we keep your name and email for our mailing list", self.page)

    def test_the_terms_page_screenshot_has_a_narrow_version(self):
        self.assertIn("images/ai-assistants/terms-page.webp", self.page)
        self.assertIn("images/ai-assistants/terms-page-narrow.webp", self.page)


class DraftWithoutNewerLinksTest(EveryStateChecks, TestCase):
    """Drafting in the chat on, but the links that tie to one browser off:
    the page waits for them and reads as prepare only."""

    shows = ONLY_WITH_PREPARE_ONLY + WITH_EITHER_APPEAL_TOOL
    hides = ONLY_WITH_BOTH_OFF + ONLY_WITH_CHAT

    def setUp(self):
        self.enterContext(
            override_settings(**{**CHAT_ON, "MCP_HANDOFF_V2_ENABLED": False})
        )
        self.page = self.client.get(PAGE).content.decode()


class HomeSectionTest(TestCase):
    def _home_section(self):
        soup = BeautifulSoup(self.client.get("/").content.decode(), "html.parser")
        return soup.find(id="ai-assistants-home")

    @override_settings(MCP_SERVER_ENABLED=True, MCP_PREPARE_APPEAL_ENABLED=True)
    def test_home_announces_the_page_while_the_server_is_on(self):
        section = self._home_section()
        self.assertIsNotNone(section)
        self.assertEqual(section.find("h2").get_text(strip=True), HOME_HEADING)
        self.assertIn("Now your AI can talk to our AI.", str(section))
        self.assertIn("images/ai-assistants/fhi-connector-icon.webp", str(section))
        link = section.find("a", href=reverse("ai-assistants"))
        self.assertEqual(link.get_text(strip=True), "Set up your AI assistant")
        for dash in EM_DASHES:
            self.assertNotIn(dash, str(section))
        self.assertIn("help you start a free appeal from your chat", str(section))

    @override_settings(MCP_SERVER_ENABLED=True, MCP_PREPARE_APPEAL_ENABLED=False)
    def test_home_offers_only_the_link_while_both_appeal_tools_are_off(self):
        section = str(self._home_section())
        self.assertIn("give you the link to start a free appeal here", section)
        self.assertNotIn("from your chat", section)

    @override_settings(MCP_SERVER_ENABLED=False)
    def test_home_says_nothing_while_the_server_is_off(self):
        self.assertIsNone(self._home_section())
        self.assertNotIn(HOME_HEADING, self.client.get("/").content.decode())


@override_settings(MCP_SERVER_ENABLED=False)
class PageOffTest(TestCase):
    def test_the_page_is_404_and_nothing_links_to_it(self):
        self.assertEqual(self.client.get(PAGE).status_code, 404)
        for path in ("/", "/other-resources", "/sitemap.xml", "/llms.txt"):
            self.assertNotIn("ai-assistants", self.client.get(path).content.decode(), path)
