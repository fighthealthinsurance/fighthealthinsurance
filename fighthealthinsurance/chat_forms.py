import ipaddress
import re
import typing
from typing import Any, Optional
from urllib.parse import urlsplit

from django import forms
from django.core.files.uploadedfile import UploadedFile
from django.core.validators import URLValidator

from fighthealthinsurance import consent
from fighthealthinsurance.form_utils import (
    FileDropInput,
    MultipleFileField,
    MultipleFileInput,
    StyledWidgetsMixin,
    share_for_research_field,
)
from fighthealthinsurance.forms import REFERRAL_SOURCE_CHOICES
from fighthealthinsurance.models import Denial, PolicyDocument
from fighthealthinsurance.state_help import get_states_sorted_by_name

SUPPORTED_POLICY_EXTENSIONS = {".pdf", ".docx", ".txt"}
MAX_POLICY_DOCUMENT_BYTES = 20 * 1024 * 1024

# Magic bytes for file type validation
_PDF_MAGIC = b"%PDF"
_DOCX_MAGIC = b"PK"  # ZIP-based format


def policy_document_extension(name: str) -> str:
    """The file's extension, lowercased with its dot, or "" when it has none."""
    return "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""


def validate_policy_document(file: UploadedFile) -> None:
    """Raise ValidationError unless ``file`` is a policy document we can read:
    a supported extension, at most MAX_POLICY_DOCUMENT_BYTES, and for PDF and
    DOCX the right first bytes."""
    if file.size is not None and file.size > MAX_POLICY_DOCUMENT_BYTES:
        raise forms.ValidationError(
            "File size must be less than 20MB. Please upload a smaller file."
        )
    ext = policy_document_extension(file.name or "")
    if not ext:
        raise forms.ValidationError(
            f"File must have an extension. Please upload a {', '.join(sorted(SUPPORTED_POLICY_EXTENSIONS))} file."
        )
    if ext not in SUPPORTED_POLICY_EXTENSIONS:
        raise forms.ValidationError(
            f"Unsupported file type. Please upload one of: {', '.join(sorted(SUPPORTED_POLICY_EXTENSIONS))}"
        )
    # Magic byte check for PDF and DOCX (stronger than MIME type)
    header = file.read(8)
    file.seek(0)
    if ext == ".pdf" and not header.startswith(_PDF_MAGIC):
        raise forms.ValidationError("The file does not appear to be a valid PDF.")
    if ext == ".docx" and not header.startswith(_DOCX_MAGIC):
        raise forms.ValidationError(
            "The file does not appear to be a valid DOCX document."
        )


# A Word file locked with a password, or an old .doc, is an OLE file, not a zip.
_OLE_MAGIC = b"\xd0\xcf\x11\xe0"
# A real .docx holds a few dozen parts; thousands means it isn't one.
_MAX_DOCX_ENTRIES = 2000
# A text file with a NUL byte near the start is a binary file renamed.
_TEXT_SNIFF_BYTES = 64 * 1024
# The most pages a shared PDF may have, and how long counting them may take.
MAX_SHARED_PDF_PAGES = 3000
_PDF_CHECK_SECONDS = 15

LOCKED_WORD_MESSAGE = (
    "This Word file is locked with a password or is an old .doc file. Please "
    "save it as a .docx without a password, or as a PDF."
)
LOCKED_PDF_MESSAGE = (
    "This PDF is locked with a password. Please upload a copy without one."
)
UNREADABLE_PDF_MESSAGE = (
    "We couldn't open this PDF. Please save it again as a PDF and try once more."
)


def validate_shared_document(file: UploadedFile) -> None:
    """validate_policy_document, then a closer look, for a file we keep:
    a .docx must be a Word document with no macros or locked parts, a .txt
    must be text, and a PDF must open in a separate process without a
    password, within MAX_SHARED_PDF_PAGES pages. Leaves the file rewound."""
    ext = policy_document_extension(file.name or "")
    file.seek(0)
    if ext == ".docx" and file.read(4) == _OLE_MAGIC:
        file.seek(0)
        raise forms.ValidationError(LOCKED_WORD_MESSAGE)
    file.seek(0)
    validate_policy_document(file)
    try:
        if ext == ".docx":
            _check_docx(file)
        elif ext == ".txt":
            if b"\x00" in file.read(_TEXT_SNIFF_BYTES):
                raise forms.ValidationError(
                    "This doesn't look like a text file. Please upload a PDF, "
                    "Word (.docx) or text file."
                )
        elif ext == ".pdf":
            _check_pdf(file.read())
    finally:
        file.seek(0)


def _check_docx(file: UploadedFile) -> None:
    import zipfile

    not_word = forms.ValidationError(
        "The file does not appear to be a valid DOCX document."
    )
    try:
        with zipfile.ZipFile(file) as archive:
            entries = archive.infolist()
    except (zipfile.BadZipFile, OSError, ValueError):
        raise not_word from None
    if len(entries) > _MAX_DOCX_ENTRIES:
        raise not_word
    names = {entry.filename for entry in entries}
    if not {"[Content_Types].xml", "word/document.xml"} <= names:
        raise not_word
    if any(entry.flag_bits & 0x1 for entry in entries):
        raise forms.ValidationError(LOCKED_WORD_MESSAGE)
    if any(name.lower().endswith("vbaproject.bin") for name in names):
        raise forms.ValidationError(
            "This Word file has macros. Please save it as a .docx without "
            "macros, or as a PDF."
        )


def _check_pdf(content: bytes) -> None:
    from asgiref.sync import async_to_sync

    from fighthealthinsurance.pdf_text import acount_pdf_pages

    try:
        pages = async_to_sync(acount_pdf_pages)(content, _PDF_CHECK_SECONDS)
    except TimeoutError:
        raise forms.ValidationError(UNREADABLE_PDF_MESSAGE) from None
    except ValueError as error:
        if "encrypted" in str(error):
            raise forms.ValidationError(LOCKED_PDF_MESSAGE) from None
        # pypdf is stricter than the reader that analyses files later.
        return
    except Exception:
        return
    if pages > MAX_SHARED_PDF_PAGES:
        raise forms.ValidationError(
            f"This PDF has more than {MAX_SHARED_PDF_PAGES:,} pages. Please split "
            "it and share the parts."
        )


# ---- Links to a plan's documents --------------------------------------------

#: The most links one share takes, and the longest link we keep.
MAX_SHARED_LINKS = 5
MAX_SHARED_LINK_CHARS = 2000

LINK_TOO_LONG_MESSAGE = f"Links can be up to {MAX_SHARED_LINK_CHARS:,} characters."
LINK_SCHEME_MESSAGE = "Links must start with https:// or http://."
LINK_SIGN_IN_MESSAGE = "Links can't include a user name or password."
LINK_NO_SITE_MESSAGE = "This link is missing the website's name."
LINK_NOT_PUBLIC_MESSAGE = (
    "Links must go to a public website, not an IP address or localhost."
)
LINK_NOT_A_LINK_MESSAGE = "This doesn't look like a link to a web page."

_LINK_SHAPE = URLValidator(schemes=["http", "https"])
# A last part a browser reads as part of an address: decimal, or hex like 0x7f.
_NUMERIC_LABEL = re.compile(r"^(?:0x[0-9a-f]*|[0-9]+)$")


def _not_a_public_name(host: str) -> bool:
    """True for an IP address in any form a browser takes, localhost, or a
    name with no dot."""
    host = host.lower()
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    labels = host.split(".")
    return (
        len(labels) < 2
        or labels[-1] == "localhost"
        or bool(_NUMERIC_LABEL.match(labels[-1]))
    )


def clean_shared_link(link: str) -> str:
    """``link`` trimmed, when it is one we keep: http or https, a website's
    name, no user name or password, at most MAX_SHARED_LINK_CHARS. Raises
    ValidationError otherwise. It only reads the text: nothing fetches it."""
    link = link.strip()
    if len(link) > MAX_SHARED_LINK_CHARS:
        raise forms.ValidationError(LINK_TOO_LONG_MESSAGE)
    try:
        parts = urlsplit(link)
    except ValueError:
        raise forms.ValidationError(LINK_NOT_A_LINK_MESSAGE) from None
    if parts.scheme.lower() not in ("http", "https"):
        raise forms.ValidationError(LINK_SCHEME_MESSAGE)
    if "@" in parts.netloc:
        raise forms.ValidationError(LINK_SIGN_IN_MESSAGE)
    host = (parts.hostname or "").rstrip(".")
    if not host:
        raise forms.ValidationError(LINK_NO_SITE_MESSAGE)
    if _not_a_public_name(host):
        raise forms.ValidationError(LINK_NOT_PUBLIC_MESSAGE)
    try:
        _LINK_SHAPE(link)
    except forms.ValidationError:
        raise forms.ValidationError(LINK_NOT_A_LINK_MESSAGE) from None
    return link


def _shown(link: str) -> str:
    """A link short enough to name in an error."""
    return link if len(link) <= 60 else link[:57] + "..."


class BaseConsentForm(forms.Form):
    """Shared consent fields for forms that collect user info + TOS agreement."""

    first_name = forms.CharField(
        max_length=100,
        required=True,
        widget=forms.TextInput(
            attrs={
                "class": "fhi-field",
                "id": "store_fname",
                "placeholder": "Enter your first name",
            }
        ),
    )

    last_name = forms.CharField(
        max_length=100,
        required=True,
        widget=forms.TextInput(
            attrs={
                "class": "fhi-field",
                "id": "store_lname",
                "placeholder": "Enter your last name",
            }
        ),
    )

    email = forms.EmailField(
        required=True,
        widget=forms.EmailInput(
            attrs={
                "class": "fhi-field",
                "id": "email",
                "placeholder": "Enter your email address",
            }
        ),
    )

    tos_agreement = forms.BooleanField(
        required=True,
        widget=forms.CheckboxInput(attrs={"class": "fhi-check", "id": "tos"}),
    )

    privacy_policy = forms.BooleanField(
        required=True,
        widget=forms.CheckboxInput(attrs={"class": "fhi-check", "id": "privacy"}),
    )

    # Unticked until the person ticks it: the site promises no dark patterns.
    subscribe = forms.BooleanField(
        required=False,
        initial=False,
        widget=forms.CheckboxInput(attrs={"class": "fhi-check", "id": "subscribe"}),
    )


class UnderstandPolicyForm(BaseConsentForm):
    """Form for uploading policy documents with consent"""

    policy_document = forms.FileField(
        required=True,
        widget=FileDropInput(
            attrs={
                "id": "policy_document",
                "accept": ".pdf,.docx,.txt",
            }
        ),
    )

    document_type = forms.ChoiceField(
        required=True,
        choices=PolicyDocument.DOCUMENT_TYPE_CHOICES,
        initial="summary_of_benefits",
        widget=forms.Select(
            attrs={
                "class": "fhi-field",
                "id": "document_type",
            }
        ),
    )

    PLAN_CATEGORY_CHOICES = (
        ("employer_erisa", "Employer Plan (ERISA) — regulated by Dept. of Labor"),
        (
            "employer_non_erisa",
            "Employer Plan (Non-ERISA, e.g. government/church employer)",
        ),
        ("aca_marketplace", "ACA Marketplace (Healthcare.gov / State Exchange)"),
        ("medicare_traditional", "Medicare (Traditional/Original)"),
        ("medicare_advantage", "Medicare Advantage (Part C)"),
        ("medicaid_chip", "Medicaid / CHIP"),
        ("tricare", "TRICARE (Military)"),
        ("va", "VA Health Care"),
        ("individual_off_exchange", "Individual Plan (Off-Exchange)"),
        ("short_term", "Short-Term Health Plan"),
        ("unknown", "I'm Not Sure"),
    )

    plan_category = forms.ChoiceField(
        required=True,
        choices=PLAN_CATEGORY_CHOICES,
        initial="unknown",
        widget=forms.Select(
            attrs={
                "class": "fhi-field",
                "id": "plan_category",
            }
        ),
    )

    user_question = forms.CharField(
        required=False,
        max_length=1000,
        widget=forms.Textarea(
            attrs={
                "class": "fhi-field",
                "id": "user_question",
                "rows": 3,
                "placeholder": "Optional: What specific question do you have about your policy? (e.g., 'Is my MRI covered?' or 'What are the exclusions for mental health?')",
            }
        ),
    )

    share_for_research = share_for_research_field()

    def clean_policy_document(self) -> Optional[UploadedFile]:
        """Validate the uploaded file type and size."""
        file = self.cleaned_data.get("policy_document")
        if file:
            validate_policy_document(file)
        return file


class _OptionalDecimalField(forms.DecimalField):
    """An optional amount whose invalid answer is dropped, not refused."""

    def clean(self, value: Any) -> Any:
        try:
            return super().clean(value)
        except forms.ValidationError:
            return None


class _OptionalChoiceField(forms.ChoiceField):
    """An optional choice whose answer off the list is dropped, not refused."""

    def clean(self, value: Any) -> Any:
        try:
            return super().clean(value)
        except forms.ValidationError:
            return ""


class SharePlanDocumentsForm(StyledWidgetsMixin, forms.Form):
    """Plan documents shared for research. A share needs files, links to the
    documents, or both, and the agreement: no name, and an email only so a
    removal request finds them."""

    MAX_FILES = 10
    MAX_LINKS = MAX_SHARED_LINKS
    NOTHING_TO_SHARE = "Choose at least one file or add a link to share."
    # All the files in one upload, with room under the proxy's 80MB limit.
    MAX_TOTAL_BYTES = 60 * 1024 * 1024
    # The optional fields, shown folded under "About the plan".
    ABOUT_THE_PLAN = (
        "insurer_name",
        "employer_name",
        "plan_year",
        "state",
        "premium_amount",
        "premium_period",
        "who_is_covered",
        "email",
    )
    # The most a premium can be, in dollars, however often it is paid.
    MAX_PREMIUM = 100_000

    # Optional when there are links: clean() asks for one or the other.
    plan_documents = MultipleFileField(
        required=False,
        label="Your plan documents",
        widget=MultipleFileInput(attrs={"accept": ".pdf,.docx,.txt"}),
    )
    plan_links = forms.CharField(
        required=False,
        label="Links to your plan's documents",
        help_text=(
            f"One link per line, up to {MAX_SHARED_LINKS}. Use public pages only, "
            "like an employer's benefits site or the plan's page on an "
            "insurer's site, not links that need you to sign in."
        ),
        widget=forms.Textarea(
            attrs={"rows": 3, "autocomplete": "off", "spellcheck": "false"}
        ),
    )
    plan_category = forms.ChoiceField(
        required=False,
        choices=PolicyDocument.PLAN_CATEGORY_CHOICES,
        initial="unknown",
        label="What kind of plan is it? (optional)",
    )
    insurer_name = forms.CharField(
        required=False, max_length=200, label="Insurance company"
    )
    employer_name = forms.CharField(
        required=False, max_length=200, label="Employer, if the plan is through a job"
    )
    plan_year = forms.IntegerField(
        required=False,
        min_value=2000,
        max_value=2099,
        label="Plan year",
        help_text=(
            "The year these documents are for. Past years help us show how "
            "plans change over time. Sharing more than one year? You can leave "
            "this blank."
        ),
        error_messages={
            "min_value": "Enter a year from 2000 to 2099.",
            "max_value": "Enter a year from 2000 to 2099.",
            "invalid": "Enter a year from 2000 to 2099.",
        },
    )
    state = forms.ChoiceField(required=False, choices=(), label="State")
    # A bad premium answer is dropped, never the share; the amount's limits
    # still give the box its min, max and step, so the browser asks first.
    premium_amount = _OptionalDecimalField(
        required=False,
        min_value=0,
        max_value=MAX_PREMIUM,
        max_digits=8,
        decimal_places=2,
        label="What you pay for the plan (your share of the premium), in dollars",
        help_text="If you know it. This helps us show what plans cost over time.",
    )
    premium_period = _OptionalChoiceField(
        required=False,
        choices=[("", "Choose how often")] + PolicyDocument.PREMIUM_PERIOD_CHOICES,
        label="How often you pay it",
    )
    who_is_covered = _OptionalChoiceField(
        required=False,
        choices=[("", "Choose who it covers")] + PolicyDocument.WHO_IS_COVERED_CHOICES,
        label="Who the plan covers",
    )
    email = forms.EmailField(
        required=False,
        max_length=300,
        label="Your email",
        help_text=(
            "We keep only a scrambled form of it, so we can find what you shared "
            "if you ask us to delete it. What we've learned about the plan, like "
            "what it costs, stays, because it describes the plan, not you. We "
            "can't use it to write to you."
        ),
    )
    agree = forms.BooleanField(
        required=True,
        label=consent.SHARING_CHECKBOX,
        error_messages={"required": "Tick the box to agree before you share."},
    )
    # Hidden from people; the view drops a post that fills it in.
    website = forms.CharField(required=False, widget=forms.HiddenInput())

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        state_field = typing.cast(forms.ChoiceField, self.fields["state"])
        state_field.choices = [("", "Choose a state")] + [
            (state.abbreviation, state.name) for state in get_states_sorted_by_name()
        ]

    def clean_plan_documents(self) -> list[UploadedFile]:
        files = [f for f in self.cleaned_data.get("plan_documents") or [] if f]
        if len(files) > self.MAX_FILES:
            raise forms.ValidationError(self.too_many_message())
        # Django measured each file as it arrived, so the sizes can be trusted.
        if sum(f.size or 0 for f in files) > self.MAX_TOTAL_BYTES:
            raise forms.ValidationError(self.too_big_message())
        problems: list[str] = []
        for file in files:
            try:
                validate_shared_document(file)
            except forms.ValidationError as error:
                problems.extend(f"{file.name}: {message}" for message in error.messages)
        if problems:
            raise forms.ValidationError(problems)
        return files

    def clean_plan_links(self) -> list[str]:
        """The links, one a line, trimmed, without repeats or blank lines."""
        links: list[str] = []
        for line in (self.cleaned_data.get("plan_links") or "").splitlines():
            link = line.strip()
            if link and link not in links:
                links.append(link)
                if len(links) > self.MAX_LINKS:
                    raise forms.ValidationError(self.too_many_links_message())
        problems: list[str] = []
        for link in links:
            try:
                clean_shared_link(link)
            except forms.ValidationError as error:
                problems.extend(
                    f"{_shown(link)}: {message}" for message in error.messages
                )
        if problems:
            raise forms.ValidationError(problems)
        return links

    def clean(self) -> dict[str, Any]:
        cleaned = super().clean() or {}
        if not (
            cleaned.get("plan_documents")
            or cleaned.get("plan_links")
            or self.has_error("plan_documents")
            or self.has_error("plan_links")
        ):
            self.add_error("plan_documents", self.NOTHING_TO_SHARE)
        # Only the scrambled form is kept, so the address goes no further.
        email = (cleaned.pop("email", "") or "").strip().lower()
        cleaned["hashed_email"] = Denial.get_hashed_email(email) if email else None
        return cleaned

    @classmethod
    def max_total_mb(cls) -> int:
        return cls.MAX_TOTAL_BYTES // (1024 * 1024)

    # The page's script shows the same words before anything is sent.
    @classmethod
    def too_many_message(cls) -> str:
        return (
            f"You can share up to {cls.MAX_FILES} files at a time. Have more? "
            "Send them in another upload."
        )

    @classmethod
    def too_many_links_message(cls) -> str:
        return (
            f"You can share up to {cls.MAX_LINKS} links at a time. Have more? "
            "Send them in another share."
        )

    @classmethod
    def too_big_message(cls) -> str:
        return (
            f"These files add up to more than {cls.max_total_mb()}MB. Share some "
            "of them now, then send the rest in another upload."
        )

    def about_the_plan_has_errors(self) -> bool:
        return any(name in self.errors for name in self.ABOUT_THE_PLAN)


class PlanDocumentsReminderForm(StyledWidgetsMixin, forms.Form):
    """The share page's opt-in reminder: an email, nothing else, sent with a
    button that says what happens. Its own form, so the share form's email,
    which we never write to, is never used for it. The page draws it with
    the prefix "reminder"."""

    PREFIX = "reminder"

    email = forms.EmailField(
        max_length=254,
        label="Your email",
        widget=forms.EmailInput(attrs={"autocomplete": "email"}),
    )
    # Hidden from people; the view drops a post that fills it in.
    website = forms.CharField(required=False, widget=forms.HiddenInput())


class UserConsentForm(BaseConsentForm):
    """Form for user TOS consent and personal information collection"""

    phone = forms.CharField(
        max_length=20,
        required=False,
        widget=forms.TextInput(
            attrs={
                "class": "fhi-field",
                "id": "phone",
                "placeholder": "Enter your phone number",
            }
        ),
    )

    address = forms.CharField(
        max_length=200,
        required=False,
        widget=forms.TextInput(
            attrs={
                "class": "fhi-field",
                "id": "store_street",
                "placeholder": "Enter your street address",
            }
        ),
    )

    city = forms.CharField(
        max_length=100,
        required=False,
        widget=forms.TextInput(
            attrs={
                "class": "fhi-field",
                "id": "store_city",
                "placeholder": "Enter your city",
            }
        ),
    )

    state = forms.CharField(
        max_length=50,
        required=False,
        widget=forms.TextInput(
            attrs={
                "class": "fhi-field",
                "id": "store_state",
                "placeholder": "Enter your state",
            }
        ),
    )

    zip_code = forms.CharField(
        max_length=20,
        required=False,
        widget=forms.TextInput(
            attrs={
                "class": "fhi-field",
                "id": "store_zip",
                "placeholder": "Enter your ZIP code",
            }
        ),
    )

    referral_source = forms.ChoiceField(
        required=False,
        choices=REFERRAL_SOURCE_CHOICES,
        widget=forms.Select(attrs={"class": "fhi-field", "id": "referral_source"}),
    )

    referral_source_details = forms.CharField(
        required=False,
        max_length=500,
        widget=forms.TextInput(
            attrs={
                "class": "fhi-field",
                "id": "referral_source_details",
                "placeholder": "E.g., which search engine, social media platform, or person's name",
            }
        ),
    )

    use_external_models = forms.BooleanField(
        required=False,
        initial=True,
        widget=forms.CheckboxInput(
            attrs={"class": "fhi-check", "id": "use_external_models"}
        ),
    )
