"""The review form's two pick-several fields are tick boxes.

Type of denial and how the person gets their insurance used to be
<select multiple> boxes, which take a modifier key to pick more than one.
Each choice is now a tick box with its label beside it.
"""

from bs4 import BeautifulSoup
from django.template.loader import render_to_string
from django.test import TestCase

from fighthealthinsurance.forms import PostInferedForm
from fighthealthinsurance.models import DenialTypes, PlanSource

# field name -> (the model behind it, the label that names the group)
PICKERS = {
    "denial_type": (DenialTypes, "Type of denial"),
    "plan_source": (PlanSource, "How do you get your insurance?"),
}


def _page(form):
    html = render_to_string(
        "categorize.html", {"post_infered_form": form, "back_url": "/back/"}
    )
    return BeautifulSoup(html, "html.parser")


class TheReviewFormPicksWithTickBoxesTest(TestCase):
    fixtures = [
        "fighthealthinsurance/fixtures/initial.yaml",
        "fighthealthinsurance/fixtures/plan_source.yaml",
    ]

    def test_each_choice_is_a_tick_box_with_its_own_label(self):
        page = _page(PostInferedForm())
        for name, (model, _) in PICKERS.items():
            with self.subTest(field=name):
                self.assertIsNone(page.find("select", attrs={"name": name}))
                boxes = page.find_all("input", attrs={"name": name})
                choices = model.objects.values_list("pk", flat=True)
                self.assertEqual(
                    sorted(box["value"] for box in boxes),
                    sorted(str(pk) for pk in choices),
                )
                for box in boxes:
                    self.assertEqual(box["type"], "checkbox")
                    self.assertIn("fhi-check", box["class"])
                    label = page.find("label", attrs={"for": box["id"]})
                    self.assertIsNotNone(label, f"{box['id']} has no label")
                    row = box.parent
                    self.assertEqual(row["class"], ["fhi-check-row"])
                    self.assertIs(label.parent, row)

    def test_each_list_is_a_group_named_after_its_field(self):
        page = _page(PostInferedForm())
        for name, (_, label) in PICKERS.items():
            with self.subTest(field=name):
                group = page.find(id=f"id_{name}")
                self.assertEqual(group["class"], ["fhi-check-list"])
                self.assertEqual(group["role"], "group")
                self.assertEqual(group["aria-label"], label)
                help_id = group["aria-describedby"]
                self.assertIsNotNone(page.find(id=help_id), f"no element {help_id}")

    def test_what_we_already_know_starts_ticked(self):
        denial_type = DenialTypes.objects.order_by("pk").first()
        plan_source = PlanSource.objects.order_by("pk").last()
        page = _page(
            PostInferedForm(
                initial={"denial_type": [denial_type], "plan_source": [plan_source]}
            )
        )
        chosen_by_field = {"denial_type": denial_type, "plan_source": plan_source}
        for name, chosen in chosen_by_field.items():
            with self.subTest(field=name):
                ticked = [
                    box["value"]
                    for box in page.find_all("input", attrs={"name": name})
                    if box.has_attr("checked")
                ]
                self.assertEqual(ticked, [str(chosen.pk)])

    def test_several_ticked_boxes_come_back_as_several_choices(self):
        two_types = list(DenialTypes.objects.order_by("pk")[:2])
        two_sources = list(PlanSource.objects.order_by("pk")[:2])
        form = PostInferedForm(
            data={
                "denial_id": 1,
                "email": "someone@example.com",
                "semi_sekret": "sekret",
                "denial_type": [str(row.pk) for row in two_types],
                "plan_source": [str(row.pk) for row in two_sources],
            }
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(set(form.cleaned_data["denial_type"]), set(two_types))
        self.assertEqual(set(form.cleaned_data["plan_source"]), set(two_sources))
