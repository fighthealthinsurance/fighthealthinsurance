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

    ``existing_answers`` is the decoded ``qa_context``, keyed by field name.
    Whatever it holds for a field wins over that field's default, so the
    page comes back filled in.

    Two things this used to get wrong:

    * A per-form default passed as ``SomeForm(initial={...})`` lives on the
      FORM, not on the field, so reading ``field.initial`` never saw it.
      The form's own ``get_initial_for_field`` is the accessor that reads
      both.  This is about reading a default correctly, not about putting
      one in front of the person: the questions page passes no per-form
      initial, because the only thing it ever passed was the denial type's
      canned ``appeal_text`` and that is not a sentence the person wrote.
    * On a field name declared by two of the forms, the old code did
      ``combined.fields[name].initial += field.initial``, which concatenates
      two unrelated defaults into one string and raises ``TypeError`` when
      the kept field has no default at all.  ``find_next_steps`` recovered
      from that raise by rebuilding the whole form with an empty answers
      dict, so a name clash between two denial types wiped every answer the
      person had given.  The first form to declare a name now owns the
      field, and a later form only fills a gap it left.
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
                        # The field's own coercion, not a "True"/"False"
                        # string compare: an unchecked box posts nothing and
                        # a checked one posts "on", so a compare against
                        # "True" left every checkbox the person had ticked
                        # rendering back unticked.
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
