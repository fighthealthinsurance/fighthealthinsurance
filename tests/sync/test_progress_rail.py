"""The appeal flow's progress rail: five stages, the current one marked.

The flow has eight pages. The rail groups them into the stages a patient
feels and shows it on the upload page too, where the flow starts.
"""

from bs4 import BeautifulSoup
from django.template.loader import render_to_string
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from tests.sync.test_flow_step_headings import FLOW_PAGES

STAGES = ["Upload", "Check details", "Answer questions", "Get your appeal", "Send"]

# The stage each flow page belongs to.
STAGE_OF_PAGE = {
    "health_history.html": "Upload",
    "plan_documents.html": "Upload",
    "entity_extract.html": "Check details",
    "categorize.html": "Check details",
    "find_next_steps_loading.html": "Answer questions",
    "outside_help.html": "Answer questions",
    "appeals.html": "Get your appeal",
    "appeal.html": "Send",
}


def _rail(html):
    rail = BeautifulSoup(html, "html.parser").find("ol", class_="fhi-progress")
    assert rail is not None, "no progress rail on the page"
    return rail


def _labels(rail):
    return [
        step.find(class_="fhi-progress-label").find(string=True, recursive=False)
        for step in rail.find_all("li")
    ]


def _current(rail):
    return [
        step.find(class_="fhi-progress-label").find(string=True, recursive=False)
        for step in rail.find_all(attrs={"aria-current": True})
    ]


def _render(step):
    return render_to_string("partials/flow_progress.html", {"current_step": step})


class TheUploadPageShowsTheRailTest(TestCase):
    def test_the_upload_page_shows_the_five_stages_with_upload_current(self):
        rail = _rail(self.client.get(reverse("scan")).content.decode())
        self.assertEqual(rail["aria-label"], "Appeal progress")
        self.assertEqual(_labels(rail), STAGES)
        self.assertEqual(_current(rail), ["Upload"])
        current = rail.find(attrs={"aria-current": True})
        self.assertEqual(current["aria-current"], "step")


class TheRailGroupsThePagesIntoStagesTest(SimpleTestCase):
    def test_each_flow_page_marks_its_own_stage(self):
        for template, (_, context) in FLOW_PAGES.items():
            with self.subTest(template=template):
                rail = _rail(render_to_string(template, context))
                self.assertEqual(_labels(rail), STAGES)
                self.assertEqual(_current(rail), [STAGE_OF_PAGE[template]])

    def test_stages_before_the_current_one_say_they_are_done(self):
        rail = _rail(_render(6))
        steps = rail.find_all("li")
        for step in steps[:2]:
            self.assertIn("fhi-progress-done", step["class"])
            self.assertIn("(done)", step.get_text())
        for step in steps[2:]:
            self.assertNotIn("fhi-progress-done", step["class"])
            self.assertNotIn("(done)", step.get_text())

    def test_a_page_outside_the_flow_marks_no_stage(self):
        for step in (None, "", 0, 9, "not a step"):
            with self.subTest(step=step):
                self.assertEqual(_current(_rail(_render(step))), [])

    def test_the_rail_carries_no_styles_of_its_own(self):
        html = _render(1)
        self.assertNotIn("<style", html)
        self.assertNotIn("style=", html)
