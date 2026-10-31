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
"""

import re
from typing import NamedTuple, Optional

from fighthealthinsurance.state_help import load_state_help

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

_FEDERAL_BY_KEY = {normalize_name(k): v for k, v in FEDERAL_REGULATORS.items()}
# The fixture rows' own names and alt names resolve too, so a model that
# says "Federal Department of Labor" or "ERISA" lands on the same row.
for _defaults in list(FEDERAL_REGULATORS.values()):
    _FEDERAL_BY_KEY.setdefault(normalize_name(_defaults.name), _defaults)
    _FEDERAL_BY_KEY.setdefault(normalize_name(_defaults.alt_name), _defaults)


def _state_regulators() -> dict[str, RegulatorDefaults]:
    """Every state insurance department and Medicaid agency, by normalized name."""
    table: dict[str, RegulatorDefaults] = {}
    for state in load_state_help().values():
        department = state.insurance_department
        if department.name:
            table[normalize_name(department.name)] = RegulatorDefaults(
                department.name,
                f"{state.abbreviation} insurance department",
                department.complaint_url or department.url,
                department.consumer_line or department.phone or "",
            )
        medicaid = state.medicaid
        if medicaid.agency_name:
            table[normalize_name(medicaid.agency_name)] = RegulatorDefaults(
                medicaid.agency_name,
                f"{state.abbreviation} Medicaid",
                medicaid.agency_url or "",
                medicaid.agency_phone or "",
            )
    return table


_STATE_BY_KEY: Optional[dict[str, RegulatorDefaults]] = None


def regulator_defaults(name: str) -> Optional[RegulatorDefaults]:
    """The ``Regulator`` row fields for a regulator the model named.

    ``None`` for ``Unknown`` and for a name nothing here recognises: a name
    we can't attach contact details to is not stored, since the outside-help
    page would show it with nothing to do about it.
    """
    global _STATE_BY_KEY
    key = normalize_name(name)
    if not key or key == normalize_name(UNKNOWN):
        return None
    found = _FEDERAL_BY_KEY.get(key)
    if found is not None:
        return found
    if _STATE_BY_KEY is None:
        _STATE_BY_KEY = _state_regulators()
    return _STATE_BY_KEY.get(key)
