from typing import TYPE_CHECKING

from django import forms

if TYPE_CHECKING:
    # Only so the checker knows a form is underneath; at runtime the mixin
    # must add no base of its own. Same shape as ReCaptchaOptionalMixin.
    from django.forms import BaseForm as _StyledWidgetsMixinBase
else:
    _StyledWidgetsMixinBase = object


# See https://docs.djangoproject.com/en/5.1/topics/http/file-uploads/
class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultipleFileField(forms.FileField):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("widget", MultipleFileInput())
        super().__init__(*args, **kwargs)

    def clean(self, data, initial=None):
        single_file_clean = super().clean
        if isinstance(data, (list, tuple)):
            result = [single_file_clean(d, initial) for d in data]
        else:
            result = [single_file_clean(data, initial)]
        return result


#: Widget types that are not text boxes and take the tick-box class.
_TICKABLE = (forms.CheckboxInput, forms.RadioSelect, forms.CheckboxSelectMultiple)
#: Widget types that get no class at all.
_UNTOUCHED = (forms.HiddenInput, forms.MultipleHiddenInput)

FIELD_CLASS = "fhi-field"
CHECK_CLASS = "fhi-check"


def style_widgets(fields) -> None:
    """Give each field's visible widget the site's own input class.

    A function rather than only a mixin, because not every form the flow
    renders is a class anybody can inherit from: the questions page builds
    one at runtime and copies fields into it, so the fields have to be
    stamped where they land.

    Appends rather than replaces, so a widget that already carries a class of
    its own keeps it, and skips a field already stamped so a field copied
    between forms does not collect the class twice.
    """
    for field in fields:
        widget = field.widget
        if isinstance(widget, _UNTOUCHED):
            continue
        if widget.__class__.__module__.startswith("django_recaptcha"):
            continue
        wanted = CHECK_CLASS if isinstance(widget, _TICKABLE) else FIELD_CLASS
        existing = widget.attrs.get("class", "")
        if wanted in existing.split():
            continue
        widget.attrs["class"] = f"{existing} {wanted}".strip()


class StyledWidgetsMixin(_StyledWidgetsMixinBase):
    """Give every visible control on a form the site's own input class.

    Three templates in the appeal flow hand a whole form to Django and render
    whatever the widgets emit. Those forms set no class, so the controls came
    out with the browser's own geometry while the hand-written pages around
    them carried a styled box: different heights, a fixed width that ignored
    the screen, square corners against rounded ones. Stamping the class here
    rather than in each template means a field added later is styled by
    existing, and there is one place to change when the class changes.

    Appends rather than replaces, so a widget that already carries a class of
    its own keeps it. Hidden inputs and the captcha are left alone: neither
    is a box anybody types in, and the captcha's markup is not ours.
    """

    #: Kept as attributes so tests and subclasses can read them off the
    #: mixin; the values live at module level, with style_widgets.
    FIELD_CLASS: str = FIELD_CLASS
    CHECK_CLASS: str = CHECK_CLASS

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        style_widgets(self.fields.values())


def magic_combined_form(
    forms_to_merge: list[forms.Form], existing_answers: dict[str, str]
) -> forms.Form:
    """Merge several question forms into the one form the page renders.

    The merge contract:

    * ``existing_answers`` is the decoded ``qa_context``, keyed by field
      name.  Whatever it holds for a field beats that field's default.
    * A per-form default passed as ``SomeForm(initial={...})`` lives on the
      FORM, not on the field, so ``field.initial`` cannot see it;
      ``get_initial_for_field`` is the accessor that reads both.
    * On a name two forms declare, the first to declare it owns the field.
      A later form only fills a default it left empty; the two are
      unrelated sentences and must never be combined.
    """
    combined_form = forms.Form()

    for f in forms_to_merge:
        for field_name, field in f.fields.items():
            source_initial = f.get_initial_for_field(field, field_name)
            if field_name not in combined_form.fields:
                combined_form.fields[field_name] = field
                field.initial = source_initial
                if field_name in existing_answers:
                    value = existing_answers[field_name]
                    if isinstance(field, forms.BooleanField):
                        # The field's own coercion: a browser posts "on" for
                        # a ticked box and nothing at all for an unticked
                        # one, never "True"/"False".
                        field.initial = field.to_python(value)
                    else:
                        field.initial = value
            else:
                kept = combined_form.fields[field_name]
                if (
                    kept.initial is None or kept.initial == ""
                ) and source_initial not in (
                    None,
                    "",
                ):
                    kept.initial = source_initial

    # The fields came from forms that may or may not carry the mixin, and
    # some are added at runtime, so the merged form is stamped here: this is
    # the one the questions page actually renders.
    style_widgets(combined_form.fields.values())
    return combined_form
