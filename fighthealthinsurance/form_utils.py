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
    combined_form = forms.Form()
    seen_fields = set()

    for f in forms_to_merge:
        for field_name, field in f.fields.items():
            # Only add field if we haven't seen it before
            if field_name not in seen_fields:
                seen_fields.add(field_name)
                combined_form.fields[field_name] = field
                # Check if this field has a value in existing_answers
                if field_name in existing_answers:
                    value = existing_answers[field_name]

                    # Handle boolean fields
                    if isinstance(field, forms.BooleanField):
                        if value == "True":
                            combined_form.fields[field_name].initial = True
                        elif value == "False":
                            combined_form.fields[field_name].initial = False
                    # Handle string fields and others
                    else:
                        combined_form.fields[field_name].initial = value
            # For existing fields, append initial value if not None
            elif field.initial is not None:
                combined_form.fields[field_name].initial += field.initial
    return combined_form
