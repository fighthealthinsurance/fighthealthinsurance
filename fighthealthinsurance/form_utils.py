import os

from django import forms
from django.core.exceptions import ValidationError


# See https://docs.djangoproject.com/en/5.1/topics/http/file-uploads/
class MultipleFileInput(forms.ClearableFileInput):
    """Widget that enables multiple file uploads."""
    allow_multiple_selected = True


class MultipleFileField(forms.FileField):
    """
    A FileField subclass that supports multiple file uploads with enhanced security.

    This field uses the MultipleFileInput widget and validates each uploaded file's
    extension and size if restrictions are provided.

    Args:
        allowed_extensions (list[str], optional): List of allowed file extensions 
            (e.g., ['.jpg', '.png']). If empty, no extension check is performed.
        max_file_size (int, optional): Maximum allowed file size in bytes. If None,
            no file size check is performed.
    """

    def __init__(self, *args, allowed_extensions=None, max_file_size=None, **kwargs):
        self.allowed_extensions = allowed_extensions or []
        self.max_file_size = max_file_size
        kwargs.setdefault("widget", MultipleFileInput())
        super().__init__(*args, **kwargs)

    def clean(self, data, initial=None):
        """
        Validate each uploaded file for type and size before cleaning.

        Args:
            data: A single file or a list/tuple of uploaded files.
            initial: The initial value for the field (if any).

        Returns:
            list: A list of cleaned files.

        Raises:
            ValidationError: If a file has an unsupported extension or exceeds the allowed size.
        """
        cleaned_files = []
        single_file_clean = super().clean
        files = data if isinstance(data, (list, tuple)) else [data]

        for file in files:
            if file is None:
                continue

            # Validate file extension if restrictions are provided.
            if self.allowed_extensions:
                ext = os.path.splitext(file.name)[1].lower()
                if ext not in self.allowed_extensions:
                    raise ValidationError(
                        f"Unsupported file extension: {ext}. "
                        f"Allowed extensions: {self.allowed_extensions}"
                    )

            # Validate file size if a maximum is specified.
            if self.max_file_size and file.size > self.max_file_size:
                raise ValidationError(
                    f"File too large. Maximum file size is {self.max_file_size} bytes."
                )

            cleaned_file = single_file_clean(file, initial)
            cleaned_files.append(cleaned_file)

        return cleaned_files


def magic_combined_form(
    forms_to_merge: list[forms.Form], existing_answers: dict[str, str]
) -> forms.Form:
    """
    Combine multiple Django forms into a single form.

    The function merges the fields of the provided forms and sets initial values
    from the existing_answers dictionary when available.

    Args:
        forms_to_merge (list[forms.Form]): List of Django form instances to merge.
        existing_answers (dict[str, str]): Dictionary of initial answers keyed by field names.

    Returns:
        forms.Form: A combined form with merged fields and their initial values.
    """
    combined_form = forms.Form()

    def get_initial(field, value):
        """Return the properly typed initial value based on the field type."""
        if isinstance(field, forms.BooleanField):
            return True if value == "True" else False if value == "False" else value
        return value

    for form in forms_to_merge:
        for field_name, field in form.fields.items():
            if field_name not in combined_form.fields:
                combined_form.fields[field_name] = field
                if field_name in existing_answers:
                    combined_form.fields[field_name].initial = get_initial(
                        field, existing_answers[field_name]
                    )
            elif field.initial is not None:
                # Append additional initial value if already present.
                current_initial = combined_form.fields[field_name].initial
                if current_initial is not None:
                    combined_form.fields[field_name].initial = current_initial + field.initial
                else:
                    combined_form.fields[field_name].initial = field.initial

    return combined_form
