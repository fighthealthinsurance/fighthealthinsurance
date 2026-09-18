"""Every box a patient types into looks like every other one.

The flow had two families. Pages whose markup was written by hand carried a
styled box; three pages hand a whole form to Django and rendered whatever the
widgets emit, which is the browser's own control: shorter, a fixed width that
ignores the screen, square against rounded. Which one a patient got depended
only on how that page happened to be built.

``StyledWidgetsMixin`` stamps the site's own class onto every visible widget,
so a field added later is styled by existing rather than by remembering.
"""

from django import forms
from django.test import TestCase

from fighthealthinsurance import forms as core_forms
from fighthealthinsurance.form_utils import StyledWidgetsMixin

FIELD = StyledWidgetsMixin.FIELD_CLASS
CHECK = StyledWidgetsMixin.CHECK_CLASS

# The forms a patient meets walking the flow.
FLOW_FORMS = (
    core_forms.HealthHistory,
    core_forms.PlanDocumentsForm,
    core_forms.PostInferedForm,
    core_forms.ChooseAppealForm,
    core_forms.FaxForm,
)

TICKABLE = (forms.CheckboxInput, forms.RadioSelect, forms.CheckboxSelectMultiple)
UNSTYLED = (forms.HiddenInput, forms.MultipleHiddenInput)


def _visible_widgets(form):
    for name, field in form.fields.items():
        widget = field.widget
        if isinstance(widget, UNSTYLED):
            continue
        if widget.__class__.__module__.startswith("django_recaptcha"):
            continue
        yield name, widget


class EveryControlInTheFlowIsOursTest(TestCase):
    def test_the_forms_are_reached_at_all(self):
        """A form list that stops resolving would make this vacuous."""
        total = sum(len(list(_visible_widgets(f()))) for f in FLOW_FORMS)
        self.assertGreater(total, 20, "only found %d controls" % total)

    def test_each_visible_control_carries_the_class_for_its_kind(self):
        for form_class in FLOW_FORMS:
            form = form_class()
            for name, widget in _visible_widgets(form):
                with self.subTest(form=form_class.__name__, field=name):
                    classes = widget.attrs.get("class", "").split()
                    wanted = CHECK if isinstance(widget, TICKABLE) else FIELD
                    self.assertIn(
                        wanted,
                        classes,
                        "%s.%s renders as %s with class %r"
                        % (
                            form_class.__name__,
                            name,
                            widget.__class__.__name__,
                            widget.attrs.get("class", ""),
                        ),
                    )

    def test_a_class_a_field_already_had_is_kept(self):
        """Two widgets carry selectors the page's own script hangs off."""

        class Existing(StyledWidgetsMixin, forms.Form):
            picked = forms.CharField(
                widget=forms.TextInput(attrs={"class": "insurance-company-select"})
            )

        widget = Existing().fields["picked"].widget
        self.assertIn("insurance-company-select", widget.attrs["class"].split())
        self.assertIn(FIELD, widget.attrs["class"].split())

    def test_a_hidden_field_is_left_alone(self):
        class Hidden(StyledWidgetsMixin, forms.Form):
            ref = forms.CharField(widget=forms.HiddenInput())

        self.assertNotIn("class", Hidden().fields["ref"].widget.attrs)

    def test_stamping_twice_does_not_repeat_the_class(self):
        """Two forms is not two stamps. A field copied between forms is.

        The questions page builds its form at runtime out of fields from
        other forms, so a field genuinely arrives already carrying the class
        and is stamped again. Constructing two fresh forms exercises none of
        that: each gets one stamp, and the guard could be gone.
        """
        from fighthealthinsurance.form_utils import style_widgets

        field = forms.CharField(widget=forms.TextInput(attrs={"class": FIELD}))

        style_widgets([field])
        style_widgets([field])

        self.assertEqual(field.widget.attrs["class"].split().count(FIELD), 1)

    def test_a_field_carried_into_the_questions_form_is_styled_once(self):
        """The real path: merged at runtime, out of forms of mixed origin."""
        from fighthealthinsurance.form_utils import magic_combined_form

        class Asked(StyledWidgetsMixin, forms.Form):
            already = forms.CharField()

        class Plain(forms.Form):
            never_stamped = forms.CharField()
            ticked = forms.BooleanField(required=False)

        combined = magic_combined_form([Asked(), Plain()], {})

        self.assertEqual(
            combined.fields["already"].widget.attrs["class"].split().count(FIELD), 1
        )
        self.assertIn(
            FIELD, combined.fields["never_stamped"].widget.attrs["class"].split()
        )
        self.assertIn(CHECK, combined.fields["ticked"].widget.attrs["class"].split())


class TheStylesheetDefinesThemTest(TestCase):
    def test_both_classes_are_styled_from_the_tokens(self):
        from pathlib import Path

        css = (
            Path(__file__).resolve().parent.parent.parent
            / "fighthealthinsurance"
            / "static"
            / "css"
            / "custom.css"
        ).read_text()

        self.assertIn(".%s {" % FIELD, css)
        self.assertIn(".%s {" % CHECK, css)
        # Built from tokens, not from literals, or the next scale change
        # leaves the inputs behind.
        block = css[css.index(".%s {" % FIELD) :][:600]
        self.assertIn("var(--fhi-text-input)", block)
        self.assertIn("var(--fhi-radius)", block)
        self.assertIn("var(--fhi-space-", block)

    def test_the_focus_ring_covers_them(self):
        from pathlib import Path

        css = (
            Path(__file__).resolve().parent.parent.parent
            / "fighthealthinsurance"
            / "static"
            / "css"
            / "custom.css"
        ).read_text()

        self.assertIn(".%s:focus-visible" % FIELD, css)
        self.assertIn(".%s:focus-visible" % CHECK, css)
