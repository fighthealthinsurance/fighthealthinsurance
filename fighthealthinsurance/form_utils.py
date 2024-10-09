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


def magic_combined_form(forms_to_merge):
    combined_form = forms.Form()
    for f in forms_to_merge:
        for field_name, field in f.fields.items():
            if field_name not in combined_form.fields:
                combined_form.fields[field_name] = field
            elif field.initial is not None:
                combined_form.fields[field_name].initial += field.initial
    return combined_form
