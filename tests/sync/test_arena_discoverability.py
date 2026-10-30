"""The Appeal Arena and its rankings can be found: from the menu and the
footer on every page, from the home page, from About our AI, from the
sitemap, and from llms.txt and the rankings page's markdown twin."""

from bs4 import BeautifulSoup
from django.core.cache import cache
from django.test import Client, TestCase
from django.urls import reverse

from fighthealthinsurance import agent_docs
from fighthealthinsurance.sitemap import StaticViewSitemap


def _soup(name: str) -> BeautifulSoup:
    response = Client().get(reverse(name))
    assert response.status_code == 200, name
    return BeautifulSoup(response.content, "html.parser")


def _text(node) -> str:
    return " ".join(node.get_text(" ", strip=True).split())


class ArenaInTheShellTest(TestCase):
    """The header and the footer, which every page carries."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.soup = _soup("about")

    def test_the_resources_menu_lists_the_arena_then_its_rankings(self):
        resources = next(
            s for s in self.soup.select("summary.fhi-nav-link") if _text(s) == "Resources"
        )
        links = [
            (_text(a), a["href"]) for a in resources.find_next_sibling("ul").find_all("a")
        ]
        arena = links.index(("Appeal Arena", reverse("chooser")))
        rankings = links.index(("Which AI Wins: Rankings", reverse("chooser_rankings")))
        self.assertEqual(rankings, arena + 1)
        self.assertGreater(arena, links.index(("Blog", reverse("blog"))))

    def test_help_stays_its_own_link(self):
        """The Arena went under Resources, not into a Help dropdown."""
        nav = self.soup.find(id="navbar")
        help_link = nav.find("a", href=reverse("how-to-help"))
        self.assertEqual(help_link.get("class"), ["fhi-nav-link"])
        self.assertEqual(len(nav.find_all("a", href=reverse("chooser"))), 1)

    def test_the_footer_links_both(self):
        footer = self.soup.find("footer")
        self.assertEqual(_text(footer.find("a", href=reverse("chooser"))), "Appeal Arena")
        self.assertEqual(
            _text(footer.find("a", href=reverse("chooser_rankings"))), "AI Rankings"
        )


class ArenaOnTheContentPagesTest(TestCase):
    def test_the_home_page_has_an_arena_section_with_both_doors(self):
        section = _soup("root").find(id="arena-home")
        self.assertEqual(
            _text(section.find("h2")), "Which AI writes the best appeal? You decide."
        )
        self.assertEqual(
            [(_text(a), a["href"]) for a in section.find_all("a")],
            [
                ("Step into the Arena", reverse("chooser")),
                ("See which AI is winning", reverse("chooser_rankings")),
            ],
        )

    def test_about_our_ai_says_how_the_models_are_compared(self):
        soup = _soup("about-ai")
        heading = soup.find(id="which-model-wins")
        self.assertEqual(_text(heading), "Which model writes the best appeal?")
        section = heading.find_next_sibling("p")
        self.assertIsNotNone(section.find("a", href=reverse("chooser")))
        self.assertIsNotNone(section.find("a", href=reverse("chooser_rankings")))

    def test_how_to_help_names_what_the_rankings_show(self):
        # Inside the page, not the menu, which now links the rankings too.
        section = _soup("how-to-help").find(id="help-patients")
        link = section.find("a", href=reverse("chooser_rankings"))
        self.assertEqual(_text(link), "Appeal Arena rankings")


class ArenaForCrawlersAndAgentsTest(TestCase):
    def tearDown(self):
        cache.clear()

    def test_both_pages_are_in_the_static_sitemap(self):
        items = StaticViewSitemap().items()
        self.assertIn("chooser", items)
        self.assertIn("chooser_rankings", items)

    def test_the_rankings_page_has_a_markdown_twin_and_the_app_does_not(self):
        self.assertTrue(agent_docs.twin_eligible("chooser_rankings"))
        self.assertFalse(agent_docs.twin_eligible("chooser"))

        twin = self.client.get(agent_docs.twin_path_for(reverse("chooser_rankings")))
        self.assertEqual(twin.status_code, 200)
        body = twin.content.decode()
        self.assertTrue(body.startswith("# Appeal Arena Rankings\n"), body[:60])
        self.assertIn("## Who is winning", body)

        self.assertEqual(
            self.client.get(agent_docs.twin_path_for(reverse("chooser"))).status_code,
            404,
        )

    def test_llms_txt_lists_the_rankings_twin_and_the_arena_itself(self):
        body = self.client.get(reverse("llms_txt")).content.decode()
        origin = agent_docs.CANONICAL_ORIGIN
        rankings_twin = agent_docs.twin_path_for(reverse("chooser_rankings"))
        self.assertIn(f"- [Appeal Arena rankings]({origin}{rankings_twin}): ", body)
        self.assertIn(f"- [Appeal Arena]({origin}{reverse('chooser')}): ", body)
        self.assertNotIn(agent_docs.twin_path_for(reverse("chooser")), body)
