"""Who regulates this plan, and is it time to go to them?

The model side of the regulator question. ``process_denial.ProcessDenialRegex``
matches the few regulators whose fixture rows carry a regex (ERISA, CMS); this
module is for the letters those regexes miss, where a fine-tuned model reads
the letter instead. The prompt and the answer format are the contract the
training data is built to (``dataset_tools/regulator_routing.py`` in
healthinsurance-llm-internal): keep ``ROUTING_PROMPT`` identical there and
here, since a model trained on one wording is asked with the same wording.

The model answers four lines::

    Escalate now: Yes
    Regulator: Texas Department of Insurance
    Plan kind: marketplace_individual
    Next step: Ask the Texas Department of Insurance for an independent ...

:func:`parse_routing_answer` reads them; :func:`regulator_defaults` turns the
regulator's name into the fields of a ``Regulator`` row, using the fixture's
federal rows where one exists and ``state_help.json`` for the state insurance
departments and Medicaid agencies, so what we store has the contact details
the outside-help page shows. ``Plan kind`` uses the denial-triage vocabulary
(``ml/denial_triage.PLAN_SOURCE_FOR_REGULATION``) plus ``tricare`` and
``unknown``.

Names are matched loosely (case, punctuation, "the"/"of"/"State", an
abbreviation in parentheses, and the other names a body goes by), since the
model was trained on hand-written names that differ a little from ours.

The letter is not the only evidence. Once the person tells us about their
plan, :func:`plan_source_regulator` places the plans whose source settles
the regulator (Medicare, Medicaid, a marketplace plan, federal employees,
the VA), and :func:`plan_facts` writes the rest out for the model to read
with the letter (``DenialCreatorHelper.extract_set_regulator_from_plan``).
"""

import re
from typing import Any, Iterable, NamedTuple, Optional

from fighthealthinsurance.state_help import (
    get_state_help_by_abbreviation,
    load_state_help,
)

#: The user turn, verbatim from the training data. The denial text follows it.
ROUTING_PROMPT = (
    "Read this health insurance denial or other communication from the plan. "
    "Should the patient take it to a regulator or outside reviewer now, and if "
    "so which one? Answer with four lines: 'Escalate now: Yes' or 'Escalate "
    "now: No'; 'Regulator: <the body that oversees this plan, or Unknown>'; "
    "'Plan kind: <one of employer_private, employer_state_gov, "
    "employer_other_gov, federal_employee, union, marketplace_individual, "
    "medicare_advantage, medicare, medicaid, va, tricare, unknown>'; and "
    "'Next step: <one or two sentences>'."
)

UNKNOWN = "Unknown"

PLAN_KINDS = frozenset(
    {
        "employer_private",
        "employer_state_gov",
        "employer_other_gov",
        "federal_employee",
        "union",
        "marketplace_individual",
        "medicare_advantage",
        "medicare",
        "medicaid",
        "va",
        "tricare",
        "unknown",
    }
)


def _spaced(text: str) -> str:
    """``text`` lowercased with each run of whitespace as one space."""
    return " ".join(text.lower().split())


def letter_names_regulator(denial: Any) -> bool:
    """Whether the denial letter itself names the regulator linked to it.

    A regex match (``ProcessDenialRegex.get_regulator``) means the letter
    names the regulator; a regulator the model inferred from the letter is
    linked with no match, so it is only who likely oversees the plan. A row
    with no regex (the state departments and agencies ``_regulator_row_for``
    creates) is named when its name is in the letter. Text that says "your
    letter names X", and signals documented as "the letter named ERISA",
    must check this rather than the bare link.
    """
    regulator = getattr(denial, "regulator", None)
    if regulator is None:
        return False
    text = getattr(denial, "denial_text", "") or ""
    positive: Any = getattr(regulator, "regex", None)
    negative: Any = getattr(regulator, "negative_regex", None)
    try:
        # An empty pattern compiles to match-everything, so the row's name
        # stands in for it.
        if not getattr(positive, "pattern", ""):
            name = _spaced(getattr(regulator, "name", "") or "")
            return bool(name) and name in _spaced(text)
        if positive.search(text) is None:
            return False
        if getattr(negative, "pattern", "") and negative.search(text) is not None:
            return False
    except Exception:
        return False
    return True


class RoutingAnswer(NamedTuple):
    #: Whether the model says to go to the regulator now.
    escalate: bool
    #: The regulator's name as the model wrote it, or ``Unknown``.
    regulator: str
    #: One of :data:`PLAN_KINDS`, or ``None`` when the line was missing or off-list.
    plan_kind: Optional[str]
    next_step: Optional[str]

    @property
    def regulator_known(self) -> bool:
        return self.regulator.lower() != UNKNOWN.lower()


_THINK_BLOCK = re.compile(r"<(think|thinking)>.*?</\1>", re.S | re.I)
_ANSWER_TAG = re.compile(r"<answer>\s*(.*?)\s*</answer>", re.S | re.I)
_ANSWER_LINE = re.compile(
    r"^\s*\**(Escalate now|Regulator|Plan kind|Next step)\**\s*:\s*\**(.*?)\**\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_NOT_WORD = re.compile(r"[^a-z0-9]+")


def parse_routing_answer(text: Optional[str]) -> Optional[RoutingAnswer]:
    """The four fields of a model answer, or ``None`` without the first two.

    A thinking block is skipped and an ``<answer>`` tag wins if there is one.
    ``Next step`` keeps the rest of the text, since the sentence may wrap.
    Markdown emphasis around the labels is tolerated; the model is asked for
    plain lines but a hosted backup may bold them anyway.
    """
    body = _THINK_BLOCK.sub("", text or "").strip()
    tagged = _ANSWER_TAG.search(body)
    if tagged:
        body = tagged.group(1).strip()
    matches = list(_ANSWER_LINE.finditer(body))
    escalate: Optional[bool] = None
    regulator = ""
    plan_kind: Optional[str] = None
    next_step: Optional[str] = None
    for i, m in enumerate(matches):
        label = m.group(1).lower()
        value = m.group(2).strip()
        if label == "escalate now":
            word = value.rstrip(".").lower()
            if word not in ("yes", "no"):
                return None
            escalate = word == "yes"
        elif label == "regulator":
            regulator = value.rstrip(".")
        elif label == "plan kind":
            kind = value.rstrip(".").lower()
            plan_kind = kind if kind in PLAN_KINDS else None
        else:
            end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
            next_step = " ".join((value + body[m.end() : end]).split()) or None
    if escalate is None or not regulator:
        return None
    return RoutingAnswer(escalate, regulator, plan_kind, next_step)


def normalize_name(name: str) -> str:
    """Fold case, punctuation and spacing so near-identical names compare equal."""
    return _NOT_WORD.sub(" ", (name or "").lower()).strip()


class RegulatorDefaults(NamedTuple):
    """Fields for a ``Regulator`` row, as :func:`regulator_defaults` resolves them."""

    name: str
    alt_name: str
    website: str
    phone: str
    #: ``alt_name`` of a fixture row this stands for (ERISA, CMS, CDMC), when
    #: one exists. The caller links that row rather than creating another.
    fixture_alt_name: Optional[str] = None


#: The regulators that don't depend on the state, by the names the training
#: data uses. ERISA, CMS and the DMHC are fixture rows (fixtures/initial.yaml);
#: the rest are created on first use.
FEDERAL_REGULATORS: dict[str, RegulatorDefaults] = {
    "U.S. Department of Labor, Employee Benefits Security Administration (EBSA)": RegulatorDefaults(
        "Federal Department of Labor",
        "ERISA",
        "https://www.askebsa.dol.gov/WebIntake/",
        "1-866-444-3272",
        fixture_alt_name="ERISA",
    ),
    "Centers for Medicare & Medicaid Services (CMS)": RegulatorDefaults(
        "Centers for Medicare and Medicaid",
        "CMS",
        "https://www.cms.gov/",
        "1-800-633-4227",
        fixture_alt_name="CMS",
    ),
    "California Department of Managed Health Care (DMHC)": RegulatorDefaults(
        "California Department of Managed Care",
        "CDMC",
        "https://www.dmhc.ca.gov/",
        "1-888-466-2219",
        fixture_alt_name="CDMC",
    ),
    "California Department of Insurance (CDI)": RegulatorDefaults(
        "California Department of Insurance",
        "CDI",
        "https://www.insurance.ca.gov/01-consumers/101-help/",
        "1-800-927-4357",
    ),
    "U.S. Office of Personnel Management (OPM)": RegulatorDefaults(
        "U.S. Office of Personnel Management",
        "OPM",
        "https://www.opm.gov/healthcare-insurance/healthcare/reference-materials/#url=Disputed-Claims",
        "",
    ),
    "Defense Health Agency (DHA)": RegulatorDefaults(
        "Defense Health Agency",
        "TRICARE",
        "https://tricare.mil/appeals",
        "",
    ),
    "U.S. Department of Veterans Affairs (VA)": RegulatorDefaults(
        "U.S. Department of Veterans Affairs",
        "VA",
        "https://www.va.gov/health-care/",
        "1-877-881-7618",
    ),
}

#: Words a model drops or adds to a body's name without naming a different
#: body ("Washington State Office of the Insurance Commissioner" and
#: "Washington Office of the Insurance Commissioner" are the same office).
_FILLER_WORDS = frozenset({"the", "of", "and", "state", "for"})
_PARENTHETICAL = re.compile(r"\(([^)]*)\)")
#: A parenthetical that is only one of these says what kind of body it is,
#: not which one ("Louisiana Department of Health (Medicaid)").
_GENERIC_ASIDES = frozenset({"medicaid", "medicare", "insurance"})


def _loose(name: str) -> str:
    """``normalize_name`` with the filler words dropped and ``&`` as ``and``."""
    words = normalize_name((name or "").replace("&", " and ")).split()
    return " ".join(w for w in words if w not in _FILLER_WORDS)


def _name_keys(name: str) -> list[str]:
    """Every key a name answers to, most specific first.

    The name as given, the name without its parentheticals ("Texas
    Department of Insurance (TDI)"), and each parenthetical that names the
    body on its own ("(Medi-Cal)", "(EBSA)", "(Texas Medicaid)").
    """
    keys = [_loose(name), _loose(_PARENTHETICAL.sub(" ", name or ""))]
    for aside in _PARENTHETICAL.findall(name or ""):
        key = _loose(aside)
        if key and key not in _GENERIC_ASIDES:
            keys.append(key)
    seen: list[str] = []
    for key in keys:
        if key and key not in seen:
            seen.append(key)
    return seen


def _register(
    table: dict[str, RegulatorDefaults], name: str, value: RegulatorDefaults
) -> None:
    """Add every key of ``name`` to ``table``; a key already taken stays."""
    for key in _name_keys(name):
        table.setdefault(key, value)


_FEDERAL_BY_KEY: dict[str, RegulatorDefaults] = {}
for _name, _defaults in FEDERAL_REGULATORS.items():
    _register(_FEDERAL_BY_KEY, _name, _defaults)
# The fixture rows' own names and alt names resolve too, so a model that
# says "Federal Department of Labor" or "ERISA" lands on the same row.
for _defaults in list(FEDERAL_REGULATORS.values()):
    _register(_FEDERAL_BY_KEY, _defaults.name, _defaults)
    _register(_FEDERAL_BY_KEY, _defaults.alt_name, _defaults)
# The other names the agencies go by.
for _alias, _key in (
    ("U.S. Department of Labor", "ERISA"),
    ("Department of Labor", "ERISA"),
    ("Employee Benefits Security Administration", "ERISA"),
    ("Centers for Medicare and Medicaid Services", "CMS"),
    ("Medicare", "CMS"),
    ("TRICARE", "TRICARE"),
    ("Veterans Health Administration", "VA"),
):
    _register(
        _FEDERAL_BY_KEY,
        _alias,
        next(d for d in FEDERAL_REGULATORS.values() if d.alt_name == _key),
    )


def _department_defaults(state: Any) -> Optional[RegulatorDefaults]:
    """The state's insurance department as ``Regulator`` fields, or None."""
    department = state.insurance_department
    if not department.name:
        return None
    return RegulatorDefaults(
        department.name,
        f"{state.abbreviation} insurance department",
        department.complaint_url or department.url,
        department.consumer_line or department.phone or "",
    )


def _medicaid_defaults(state: Any) -> Optional[RegulatorDefaults]:
    """The state's Medicaid agency as ``Regulator`` fields, or None."""
    medicaid = state.medicaid
    if not medicaid.agency_name:
        return None
    return RegulatorDefaults(
        medicaid.agency_name,
        f"{state.abbreviation} Medicaid",
        medicaid.agency_url or "",
        medicaid.agency_phone or "",
    )


def _state_regulators() -> dict[str, RegulatorDefaults]:
    """Every state insurance department and Medicaid agency, by name key.

    Besides the names in ``state_help.json``, a department answers to the
    name the external-review table uses for it and to "<State> Department of
    Insurance" / "<State> Insurance Department", and a Medicaid agency to
    "<State> Medicaid": the model was trained on names written by hand, and
    a state's regulator under a slightly different name is still that one.
    """
    # Imported here: external_review is only needed to build the table.
    from fighthealthinsurance.external_review import STATE_CONFIG

    states = list(load_state_help().values())
    # (name, body, is_the_bodys_own_name), the names as written first.
    names: list[tuple[str, RegulatorDefaults, bool]] = []
    for state in states:
        for body in (_department_defaults(state), _medicaid_defaults(state)):
            if body is not None:
                names.append((body.name, body, True))
    for state in states:
        department = _department_defaults(state)
        if department is not None:
            config = STATE_CONFIG.get(state.abbreviation) or {}
            # California's external-review body is the DMHC, a fixture row
            # the federal table already resolves.
            if state.abbreviation != "CA" and config.get("regulator_name"):
                names.append((config["regulator_name"], department, False))
            for pattern in (
                "{} Department of Insurance",
                "{} Insurance Department",
                "{} Division of Insurance",
            ):
                names.append((pattern.format(state.name), department, False))
        medicaid = _medicaid_defaults(state)
        if medicaid is not None:
            names.append((f"{state.name} Medicaid", medicaid, False))
    # A key two bodies answer to says neither: "(Medical Assistance)" is in
    # both Minnesota's and Pennsylvania's Medicaid agency names. Such a key
    # is dropped, unless it is one body's own full name.
    claimants: dict[str, set[str]] = {}
    own_names: dict[str, RegulatorDefaults] = {}
    for name, body, own in names:
        keys = _name_keys(name)
        if own and keys:
            own_names.setdefault(keys[0], body)
        for key in keys:
            claimants.setdefault(key, set()).add(body.alt_name)
    table: dict[str, RegulatorDefaults] = dict(own_names)
    for name, body, _own in names:
        for key in _name_keys(name):
            if len(claimants[key]) == 1:
                table.setdefault(key, body)
    return table


_STATE_BY_KEY: Optional[dict[str, RegulatorDefaults]] = None


def _state_table() -> dict[str, RegulatorDefaults]:
    global _STATE_BY_KEY
    if _STATE_BY_KEY is None:
        table = _state_regulators()
        if not table:
            # state_help.json could not be read; try again next time rather
            # than resolving no state body for the life of the process.
            return table
        _STATE_BY_KEY = table
    return _STATE_BY_KEY


def regulator_defaults(name: str) -> Optional[RegulatorDefaults]:
    """The ``Regulator`` row fields for a regulator the model named.

    ``None`` for ``Unknown`` and for a name nothing here recognises: a name
    we can't attach contact details to is not stored, since the outside-help
    page would show it with nothing to do about it. Case, punctuation,
    "the"/"of"/"State" and a trailing abbreviation in parentheses don't
    matter.
    """
    keys = _name_keys(name)
    if not keys or keys[0] == _loose(UNKNOWN):
        return None
    for table in (_FEDERAL_BY_KEY, _state_table()):
        for key in keys:
            found = table.get(key)
            if found is not None:
                return found
    return None


# What the person says about their coverage (the ``PlanSource`` fixture rows,
# fixtures/plan_source.yaml), by a word in the row's name. Only the sources
# whose regulator the source alone settles are here: an employer or union
# plan is overseen by the Department of Labor or the state depending on how
# it is funded, and a California marketplace plan by the DMHC or the CDI
# depending on the plan, so those go to the model with what the person said.
_PLAN_SOURCE_FEDERAL: tuple[tuple[str, str], ...] = (
    ("medicare", "CMS"),
    ("federal government", "OPM"),
    ("veterans", "VA"),
)


def plan_source_regulator(
    plan_sources: Iterable[str], state: Optional[str]
) -> Optional[RegulatorDefaults]:
    """The regulator the person's own answers about their plan settle, or None.

    ``plan_sources`` are the names of the ``PlanSource`` rows they picked and
    ``state`` their two-letter state. Medicare (either kind) is CMS, a federal
    employee plan OPM, VA care the VA; Medicaid is the state's Medicaid
    agency and a marketplace plan the state's insurance department, when we
    know the state. More than one source that disagree, or a source that
    doesn't settle it, is None.
    """
    found: list[RegulatorDefaults] = []
    for raw in plan_sources:
        label = " ".join((raw or "").lower().split())
        if not label:
            continue
        candidate: Optional[RegulatorDefaults] = None
        for marker, alt_name in _PLAN_SOURCE_FEDERAL:
            if marker in label:
                candidate = next(
                    d for d in FEDERAL_REGULATORS.values() if d.alt_name == alt_name
                )
                break
        else:
            help_for_state = (
                get_state_help_by_abbreviation(state.strip())
                if state and state.strip()
                else None
            )
            if help_for_state is not None and "medicaid" in label:
                candidate = _medicaid_defaults(help_for_state)
            elif (
                help_for_state is not None
                and help_for_state.abbreviation != "CA"
                and ("marketplace" in label or "affordable care" in label)
            ):
                candidate = _department_defaults(help_for_state)
        if candidate is None:
            # A source we can't place ("Employer -- Private", "Don't know")
            # beside one we can leaves it open.
            return None
        if candidate not in found:
            found.append(candidate)
    return found[0] if len(found) == 1 else None


def plan_facts(
    plan_sources: Iterable[str],
    state: Optional[str],
    employer_name: Optional[str],
    insurance_company: Optional[str] = None,
) -> str:
    """What the person told us about their plan, as text for the model.

    Appended to the letter when the model is asked again after the person
    answers our questions (``extract_set_regulator`` with ``plan_info``), so
    a letter that never says what kind of plan it is can still be placed.
    Empty when they told us nothing.
    """
    lines: list[str] = []
    sources = [
        " ".join(s.split())
        for s in plan_sources
        if s and s.strip() and "don't know" not in s.lower()
    ]
    if sources:
        lines.append(f"Where the coverage comes from: {', '.join(sources)}.")
    if employer_name and employer_name.strip():
        lines.append(f"Employer: {employer_name.strip()}.")
    if insurance_company and insurance_company.strip():
        lines.append(f"Insurance company: {insurance_company.strip()}.")
    if state and state.strip():
        help_for_state = get_state_help_by_abbreviation(state.strip())
        name = help_for_state.name if help_for_state is not None else state.strip()
        lines.append(f"The patient lives in {name}.")
    if not lines:
        return ""
    return "What the patient told us about their plan:\n" + "\n".join(lines)
