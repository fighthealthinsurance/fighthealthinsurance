"""The Claude/ChatGPT setup page serves only while the MCP server is on."""

from django.test import TestCase, override_settings
from django.urls import reverse

PAGE = "/ai-assistants"
ADDRESS = "https://www.fighthealthinsurance.com/mcp"
INSTALL_LINK = "claude.ai/customize/connectors?modal=add-custom-connector"


@override_settings(MCP_SERVER_ENABLED=True, MCP_PREPARE_APPEAL_ENABLED=False)
class PageOnTest(TestCase):
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
        self.assertNotIn("It opens once.", self.page)

    def test_the_footer_and_resources_link_to_it(self):
        self.assertIn(reverse("ai-assistants"), self.client.get("/").content.decode())
        self.assertIn(
            reverse("ai-assistants"), self.client.get("/other-resources").content.decode()
        )

    def test_the_sitemap_and_llms_txt_list_it(self):
        self.assertIn(PAGE, self.client.get("/sitemap.xml").content.decode())
        self.assertIn(PAGE, self.client.get("/llms.txt").content.decode())


@override_settings(MCP_SERVER_ENABLED=True, MCP_PREPARE_APPEAL_ENABLED=True)
class HandoffOnTest(TestCase):
    def test_the_handoff_section_explains_the_link(self):
        page = self.client.get(PAGE).content.decode()
        self.assertIn("It opens once.", page)
        self.assertIn("It lasts two hours.", page)


@override_settings(MCP_SERVER_ENABLED=False)
class PageOffTest(TestCase):
    def test_the_page_is_404_and_nothing_links_to_it(self):
        self.assertEqual(self.client.get(PAGE).status_code, 404)
        for path in ("/", "/other-resources", "/sitemap.xml", "/llms.txt"):
            self.assertNotIn("ai-assistants", self.client.get(path).content.decode(), path)
