"""About Us thanks the companies that give us free resources, and ends the
section with a link to How to Help so the next one knows where to start."""

from bs4 import BeautifulSoup
from django.test import Client, TestCase
from django.urls import reverse


class AboutUsAcknowledgementsTest(TestCase):
    SUPPORTERS = ("Microsoft Azure", "GitHub", "Anthropic", "OpenAI", "TypeSafe", "Hugging Face")

    def setUp(self):
        body = Client().get(reverse("about")).content.decode("utf-8")
        self.page = BeautifulSoup(body, "html.parser")
        self.section = self.page.find("div", id="acknowledgements")
        self.assertIsNotNone(self.section, "acknowledgements section missing")

    def test_every_supporter_is_named(self):
        text = self.section.get_text(" ")
        for name in self.SUPPORTERS:
            self.assertIn(name, text)

    def test_hugging_face_is_thanked_for_hosting_the_model_files(self):
        # It hosts the files behind the better text recognition option; the
        # thanks say so and link to it, like the others.
        text = self.section.get_text(" ")
        self.assertIn("hosts the model files behind the better text recognition option", text)
        link = self.section.find("a", href="https://huggingface.co/")
        self.assertIsNotNone(link, "Hugging Face is named without a link")
        self.assertEqual(link.get_text(strip=True), "Hugging Face")

    def test_the_section_ends_with_a_real_link_to_how_to_help(self):
        paragraphs = self.section.find_all("p")
        self.assertGreaterEqual(len(paragraphs), 2)
        links = paragraphs[-1].find_all("a", href=True)
        self.assertEqual([a["href"] for a in links], [reverse("how-to-help")])

    def test_thanks_are_a_subsection_of_the_page_heading(self):
        heading = self.section.find(["h1", "h2", "h3", "h4", "h5", "h6"])
        self.assertIsNotNone(heading)
        self.assertEqual(heading.name, "h4")
        self.assertEqual(heading.get_text(strip=True), "Thank you")
