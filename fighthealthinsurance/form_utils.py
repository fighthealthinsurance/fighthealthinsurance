from django import forms


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

    return combined_form
