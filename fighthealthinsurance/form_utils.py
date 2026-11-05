from typing import TYPE_CHECKING, Any, Optional

from django import forms
from django.template.loader import render_to_string
from django.utils.html import format_html
from django.utils.safestring import SafeString, mark_safe

if TYPE_CHECKING:
    # Only so the checker knows a form is underneath; at runtime the mixin
    # must add no base of its own. Same shape as ReCaptchaOptionalMixin.
    from django.forms import BaseForm as _StyledWidgetsMixinBase
else:
    _StyledWidgetsMixinBase = object


def append_help_text(field: forms.Field, note: Any) -> None:
    """Show ``note`` under whatever help text ``field`` already has. Plain
    text is escaped; pass a SafeString for markup."""
    field.help_text = (
        format_html("{}<br>{}", field.help_text, note)
        if field.help_text
        else format_html("{}", note)
    )


def from_your_letter_hint() -> SafeString:
    """The note under a field whose value was read from the person's letter
    rather than told to us (partials/from_your_letter_hint.html)."""
    # The partial's own text, autoescaped when it rendered; strip() hands
    # back a plain str, so it is marked safe again.
    return mark_safe(render_to_string("partials/from_your_letter_hint.html").strip())


#: The id of the agreement under the share-for-research box.
SHARE_FOR_RESEARCH_TERMS_ID = "share-for-research-terms"


def share_for_research_field() -> forms.BooleanField:
    """The box that also shares a copy of a page's uploads for research,
    drawn by partials/share_for_research.html with its agreement as the help
    text. Unticked until the person ticks it."""
    from fighthealthinsurance import consent

    return forms.BooleanField(
        required=False,
        initial=False,
        label=consent.SHARING_OPT_IN_CHECKBOX,
        help_text=consent.SHARING_OPT_IN_TERMS,
        widget=forms.CheckboxInput(
            attrs={
                "class": "fhi-check fhi-check-large",
                "id": "share_for_research",
                "aria-describedby": SHARE_FOR_RESEARCH_TERMS_ID,
            }
        ),
    )


class FileDropInput(forms.FileInput):
    """A file field drawn as a box that takes the file dropped as well as
    chosen (partials/file_drop_box.html), like every upload on the site."""

    template_name = "widgets/file_drop.html"


# See https://docs.djangoproject.com/en/5.1/topics/http/file-uploads/
class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True
    template_name = FileDropInput.template_name


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


class CheckboxList(forms.CheckboxSelectMultiple):
    """A pick-several field as tick boxes, one row each (widgets/check_list.html)."""

    template_name = "widgets/check_list.html"
    option_template_name = "widgets/check_list_option.html"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.group_label = ""
        self.group_help = False

    def name_from(self, field: forms.Field) -> None:
        """Name the group after its field: the table layout's label has no for=."""
        self.group_label = str(field.label or "")
        self.group_help = bool(field.help_text)

    def get_context(self, name, value, attrs):
        context = super().get_context(name, value, attrs)
        context["widget"]["group_label"] = self.group_label
        context["widget"]["group_help"] = self.group_help
        return context


class RadioList(forms.RadioSelect):
    """A pick-one field drawn like CheckboxList: a column of round boxes,
    each with its label beside it (widgets/check_list.html)."""

    template_name = CheckboxList.template_name
    option_template_name = CheckboxList.option_template_name

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.group_label = ""
        self.group_help = False

    def name_from(self, field: forms.Field) -> None:
        """Name the group after its field: the table layout's label has no for=."""
        self.group_label = str(field.label or "")
        self.group_help = bool(field.help_text)

    def get_context(self, name, value, attrs):
        context = super().get_context(name, value, attrs)
        context["widget"]["group_label"] = self.group_label
        context["widget"]["group_help"] = self.group_help
        return context


class CoreFirstCheckboxList(CheckboxList):
    """A CheckboxList over a long model list that shows the common choices
    first and folds the rest under a "More ..." disclosure.

    Shown up front: every choice whose instance has ``core`` set, every
    ticked choice (so nothing pre-ticked from the letter is ever out of
    sight), and the children of a ticked choice (through ``parent``), which
    narrow it. The rest go in a <details>, so it needs no script and the
    folded boxes still submit. When either part would be empty the list
    renders flat, as a plain CheckboxList.
    """

    template_name = "widgets/check_list_core_first.html"

    def __init__(self, *args, more_label: str = "More choices", **kwargs):
        super().__init__(*args, **kwargs)
        self.more_label = more_label

    @staticmethod
    def _instance(option):
        # ModelChoiceIteratorValue carries the row; plain choices have none.
        return getattr(option["value"], "instance", None)

    def get_context(self, name, value, attrs):
        context = super().get_context(name, value, attrs)
        options = [
            option
            for _group, group_options, _index in context["widget"]["optgroups"]
            for option in group_options
        ]
        ticked = {
            instance.pk
            for option in options
            if option["selected"] and (instance := self._instance(option))
        }
        shown: list[dict] = []
        more: list[dict] = []
        for option in options:
            instance = self._instance(option)
            up_front = option["selected"] or (
                instance is not None
                and (
                    getattr(instance, "core", False)
                    or getattr(instance, "parent_id", None) in ticked
                )
            )
            (shown if up_front else more).append(option)
        if not shown or not more:
            shown, more = options, []
        context["widget"]["shown_options"] = shown
        context["widget"]["more_options"] = more
        context["widget"]["more_label"] = self.more_label
        return context


# The two-step plan picker's groups (PlanSourcePicker), each a first answer
# with the PlanSource rows (fixtures/plan_source.yaml) it narrows to, matched
# by name, never by primary key. A group of several rows asks a follow-up
# question; a group of one row is that row. A row no group names gets a
# group of its own, after these, so a new fixture row is still offered.
PLAN_SOURCE_GROUPS: tuple[tuple[str, str, str, tuple[tuple[str, str], ...]], ...] = (
    (
        "job",
        "Through a job: mine, or a spouse's or parent's (including COBRA and "
        "retiree coverage)",
        "What kind of employer?",
        (
            ("Employer -- Private", "A private company or nonprofit"),
            (
                "Employer -- State Government",
                "A state government, including state universities",
            ),
            (
                "Employer -- Other Government",
                "A city, county, school district or other public employer",
            ),
            (
                "Employer -- Federal Government",
                "The federal government (FEHB)",
            ),
            ("Union", "A union plan or union trust fund"),
            ("Other Group", "Another group, like a professional association"),
        ),
    ),
    (
        "medicare",
        "Medicare",
        "Which kind of Medicare?",
        (
            (
                "Medicare Regular",
                "Original Medicare: you use the red, white and blue Medicare card",
            ),
            (
                "Medicare Advantage",
                "Medicare Advantage (Part C): a plan from a private company, "
                "with that company's card, in place of Original Medicare",
            ),
        ),
    ),
    (
        "medicaid",
        "Medicaid or CHIP (your state may call it something else, like Medi-Cal)",
        "",
        (("Medicaid", ""),),
    ),
    (
        "marketplace",
        "A plan I bought myself, on HealthCare.gov, a state marketplace or "
        "from an insurer",
        "",
        (("State Marketplace / Affordable Care Act", ""),),
    ),
    ("va", "VA health care", "", (("Veterans Affairs", ""),)),
    ("other", "Something else", "", (("Other", ""),)),
    ("unknown", "I'm not sure", "", (("Don't know", ""),)),
)


class PlanSourcePicker(forms.RadioSelect):
    """Where a plan comes from, asked in two steps (widgets/plan_source_picker.html).

    First the broad answer (through a job, Medicare, Medicaid, ...); then, for
    an answer that covers several PlanSource rows, a follow-up such as which
    kind of employer. The follow-up is in the page from the start and the
    stylesheet shows only the one under the ticked answer (:has), so it
    needs no script; a browser without :has shows them all.

    The first answer posts as ``<name>_group``; a follow-up's answer as
    ``<name>``. A post with no ``<name>_group`` (the REST API, an older
    page) is read as plain ``<name>`` values. ``multiple`` is for a
    ModelMultipleChoiceField, which wants a list back.
    """

    template_name = "widgets/plan_source_picker.html"

    def __init__(self, *args, multiple: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        self.allow_multiple_selected = multiple
        self.group_label = ""
        self.group_help = False
        # The first answer a post ticked, so a page drawn again after an
        # error keeps it ticked even when no kind under it was picked and
        # the follow-up the error asks about stays in sight. Each form gets
        # its own copy of the widget, so this is per form.
        self.posted_group = ""

    def name_from(self, field: forms.Field) -> None:
        """Name the group after its field: the table layout's label has no for=."""
        self.group_label = str(field.label or "")
        self.group_help = bool(field.help_text)

    def _rows(self) -> list[tuple[str, str]]:
        """(pk, PlanSource name) for each choice, in the field's order. The
        name's whitespace is tidied: some fixture rows end in spaces."""
        rows = []
        for value, label in self.choices:
            if value == "":
                continue
            instance = getattr(value, "instance", None)
            name = getattr(instance, "name", None) or str(label)
            rows.append((str(value), " ".join(name.split())))
        return rows

    def groups(self) -> list[dict]:
        """The groups with their rows, each row as {"pk", "name", "label"}.
        A group none of whose rows exist is left out."""
        by_name = {name: pk for pk, name in self._rows()}
        placed: set[str] = set()
        groups = []
        for key, label, question, members in PLAN_SOURCE_GROUPS:
            rows = [
                {"pk": by_name[name], "name": name, "label": sub_label or name}
                for name, sub_label in members
                if name in by_name
            ]
            if not rows:
                continue
            placed.update(row["name"] for row in rows)
            groups.append(
                {"key": key, "label": label, "question": question, "rows": rows}
            )
        extra = [
            {
                "key": f"row-{pk}",
                "label": name,
                "question": "",
                "rows": [{"pk": pk, "name": name, "label": name}],
            }
            for pk, name in self._rows()
            if name not in placed
        ]
        # Before "I'm not sure", which reads best last.
        at = next(
            (i for i, group in enumerate(groups) if group["key"] == "unknown"),
            len(groups),
        )
        return groups[:at] + extra + groups[at:]

    def _group(self, key: str) -> Optional[dict]:
        return next((g for g in self.groups() if g["key"] == key), None)

    def missing_follow_up(self, data, name: str) -> str:
        """The follow-up question left unanswered under the ticked first
        answer, or "" when there is none."""
        group = self._group(data.get(f"{name}_group") or "")
        if group is None or len(group["rows"]) < 2:
            return ""
        picked = set(self._picked(data, name))
        if picked & {row["pk"] for row in group["rows"]}:
            return ""
        return str(group["question"])

    @staticmethod
    def _picked(data, name: str) -> list[str]:
        if hasattr(data, "getlist"):
            return [str(v) for v in data.getlist(name)]
        value = data.get(name)
        if value is None:
            return []
        if isinstance(value, (list, tuple)):
            return [str(v) for v in value]
        return [str(value)]

    def value_from_datadict(self, data, files, name):
        key = data.get(f"{name}_group")
        self.posted_group = key or ""
        if not key:
            return super().value_from_datadict(data, files, name)
        group = self._group(key)
        chosen: list[str] = []
        if group is not None:
            pks = [row["pk"] for row in group["rows"]]
            if len(pks) == 1:
                chosen = pks
            else:
                chosen = [pk for pk in self._picked(data, name) if pk in pks][:1]
        if self.allow_multiple_selected:
            return chosen
        return chosen[0] if chosen else None

    def value_omitted_from_data(self, data, files, name):
        return name not in data and f"{name}_group" not in data

    def get_context(self, name, value, attrs):
        context = super().get_context(name, value, attrs)
        selected = [str(v) for v in context["widget"]["value"] if v not in ("", None)]
        groups = self.groups()
        # One answer is shown ticked: the first group holding a selected row
        # (an older denial can hold more than one), and in it that row.
        ticked_group = None
        ticked_row = None
        for group in groups:
            for row in group["rows"]:
                if row["pk"] in selected:
                    ticked_group, ticked_row = group["key"], row["pk"]
                    break
            if ticked_group:
                break
        if ticked_group is None and any(
            group["key"] == self.posted_group for group in groups
        ):
            ticked_group = self.posted_group
        base_id = context["widget"]["attrs"].get("id") or f"id_{name}"
        for group in groups:
            group["id"] = f"{base_id}_group_{group['key']}"
            group["checked"] = group["key"] == ticked_group
            for index, row in enumerate(group["rows"]):
                row["id"] = f"{base_id}_{group['key']}_{index}"
                row["checked"] = row["pk"] == ticked_row
        context["widget"]["groups"] = groups
        context["widget"]["group_name"] = f"{name}_group"
        context["widget"]["group_label"] = self.group_label
        context["widget"]["group_help"] = self.group_help
        context["widget"]["base_id"] = base_id
        return context


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
        if isinstance(widget, (CheckboxList, RadioList, PlanSourcePicker)):
            widget.name_from(field)
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
