"""The review page's denial type list shows the common types up front.

Thirty-odd tick boxes was a lot to read. The types marked ``core`` show up
front, along with anything already ticked (say, read from the letter) and
the sub-types of a ticked type; the rest sit under a "More denial types"
<details> that needs no script and whose boxes still submit.
"""

from bs4 import BeautifulSoup
from django.template.loader import render_to_string
from django.test import TestCase

from fighthealthinsurance.forms import PostInferedForm
from fighthealthinsurance.models import DenialTypes


def _page(form):
    html = render_to_string(
        "categorize.html", {"post_infered_form": form, "back_url": "/back/"}
    )
    return BeautifulSoup(html, "html.parser")


def _values(container):
    return {
        box["value"]
        for box in container.find_all("input", attrs={"name": "denial_type"})
    }


def _folded(page):
    more = page.find(id="id_denial_type").find("details", class_="fhi-check-more")
    return _values(more) if more is not None else set()


def _up_front(page):
    return _values(page.find(id="id_denial_type")) - _folded(page)


class DenialTypesCoreFirstTest(TestCase):
    fixtures = ["fighthealthinsurance/fixtures/initial.yaml"]

    def test_with_nothing_ticked_only_core_types_are_up_front(self):
        core = {
            str(pk)
            for pk in DenialTypes.objects.filter(core=True).values_list("pk", flat=True)
        }
        self.assertEqual(_up_front(_page(PostInferedForm())), core)

    def test_non_core_types_are_folded_under_more(self):
        rest = {
            str(pk)
            for pk in DenialTypes.objects.filter(core=False).values_list(
                "pk", flat=True
            )
        }
        self.assertEqual(_folded(_page(PostInferedForm())), rest)

    def test_the_fold_is_labelled_with_how_many_it_holds(self):
        summary = (
            _page(PostInferedForm()).find("details", class_="fhi-check-more").summary
        )
        count = DenialTypes.objects.filter(core=False).count()
        self.assertEqual(summary.get_text(strip=True), f"More denial types ({count})")

    def test_a_ticked_non_core_type_is_up_front(self):
        inferred = DenialTypes.objects.filter(core=False).order_by("pk").first()
        page = _page(PostInferedForm(initial={"denial_type": [inferred]}))
        self.assertIn(str(inferred.pk), _up_front(page))

    def test_children_of_a_ticked_type_are_up_front(self):
        child = DenialTypes.objects.filter(core=False, parent__isnull=False).first()
        page = _page(PostInferedForm(initial={"denial_type": [child.parent]}))
        self.assertIn(str(child.pk), _up_front(page))

    def test_without_any_core_types_the_list_is_flat(self):
        DenialTypes.objects.update(core=False)
        page = _page(PostInferedForm())
        self.assertIsNone(page.find("details", class_="fhi-check-more"))

    def test_a_folded_type_still_submits(self):
        folded = DenialTypes.objects.filter(core=False).order_by("pk").first()
        form = PostInferedForm(
            data={
                "denial_id": 1,
                "email": "someone@example.com",
                "semi_sekret": "sekret",
                "denial_type": [str(folded.pk)],
            }
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(list(form.cleaned_data["denial_type"]), [folded])
