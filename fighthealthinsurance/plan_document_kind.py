"""What kind of document a shared file's text is: a plan document, a
person's own papers, or neither.

People share bills, claim statements (EOBs), denial letters and ID cards
along with plan documents. Those are about one person, so their reading
keeps the encrypted file but no text (PROCESSING_PERSONAL). Text with no
health plan words at all, such as a recipe, is kept but not counted as a
plan (PROCESSING_NOT_A_PLAN). Anything in doubt stays a plan, as before.

Rules only, no model, in English and Spanish:

- A person's details: an ID, a birth date, a name (a "Dear" greeting
  counts as one) or a Social Security number (plan_document_scrub).
- A person's own papers have an "Explanation of Benefits" heading or
  "This is not a bill" near the top; or denial wording there ("has been
  denied") and one of their details; or two kinds of their details.
- A plan document's title near the top ("Summary of Benefits and
  Coverage", "Evidence of Coverage" and so on) keeps it a plan, even with
  a member's name and ID on it, unless it also has a personal heading and
  one of those details, or denial wording and two kinds of them.
- Neither: at least OTHER_MIN_WORDS words in the Latin alphabet with at
  most OTHER_MAX_PLAN_WORDS different health plan words.

Works on text as read and on text already cleaned, where a detail found
is "[removed]" after its label.
"""

import re

from fighthealthinsurance.plan_document_scrub import REMOVED, personal_detail_kinds

PLAN = "plan"
PERSONAL = "personal"
OTHER = "other"

#: How much of the text the details are looked for in: a person's own
#: papers are short, and the first pages are what matter.
SAMPLE_CHARS = 100_000
#: How far down a title counts as one.
TITLE_CHARS = 3_000
#: Neither a plan nor personal: at least this many words...
OTHER_MIN_WORDS = 80
#: ...with no more than this many different health plan words.
OTHER_MAX_PLAN_WORDS = 1

_FLAGS = re.IGNORECASE
# Every run of spaces has one possessive quantifier, so a long run can't
# make a pattern backtrack.
_S = r"\s++"
# Spaces or tabs, never a line break.
_SP = r"[^\S\n]"
# Between a label and its value: spaces, at most one of : # . -, and the
# value may start the next line.
_SEP = rf"{_SP}*+(?:[:#.-]{_SP}*+)?(?:\n{_SP}*+)?"

# A heading (a line that starts with it, never a contents line with dot
# leaders), or the line an EOB prints anywhere.
_PERSONAL_TITLE = re.compile(
    rf"^{_SP}*+(?:explanation{_S}of{_S}benefits"
    rf"|explicaci[oó]n{_S}de{_S}(?:los{_S})?beneficios)\b(?![^\n]*\.{{4}})"
    rf"|\bthis{_S}is{_S}not{_S}a{_S}bill\b|\besto{_S}no{_S}es{_S}una{_S}factura\b",
    _FLAGS | re.MULTILINE,
)
# Weaker than a title: plan documents explain denials too.
_DENIAL = re.compile(
    rf"\b(?:we{_S}have|we|has{_S}been|have{_S}been){_S}denied\b"
    rf"|\bwe{_S}(?:are|were){_S}unable{_S}to{_S}approve\b"
    rf"|\b(?:ha|han){_S}sido{_S}(?:denegad|negad|rechazad)[oa]s?\b"
    rf"|\bhemos{_S}(?:denegado|negado|rechazado)\b",
    _FLAGS,
)
# A plan document's own name, not words a letter uses about one ("your plan
# documents", "our formulary").
_PLAN_TITLE = re.compile(
    rf"\bsummary{_S}of{_S}benefits\b|\bbenefits?{_S}summary\b"
    rf"|\bsummary{_S}plan{_S}description\b|\bevidence{_S}of{_S}coverage\b"
    rf"|\bcertificate{_S}of{_S}coverage\b|\bschedule{_S}of{_S}benefits\b"
    rf"|\bbenefits?{_S}booklet\b|\bmember{_S}handbook\b"
    rf"|\bresumen{_S}de{_S}(?:los{_S})?beneficios\b"
    rf"|\bdescripci[oó]n{_S}resumida{_S}del{_S}plan\b"
    rf"|\b(?:evidencia|certificado){_S}de{_S}cobertura\b"
    rf"|\bmanual{_S}del{_S}(?:miembro|afiliado)\b",
    _FLAGS,
)

# A person's details in Spanish, which plan_document_scrub doesn't know:
# a label, then a value with a digit (or a capital letter for a name).
_SPANISH_DETAILS = {
    "id": re.compile(
        rf"\b(?:n[uú]mero|no\.?|n[º°]){_SP}*+de{_SP}++(?:miembro|afiliado|socio"
        rf"|reclamo|reclamaci[oó]n|paciente|cuenta|asegurado){_SEP}"
        rf"[a-z0-9-]{{0,20}}\d",
        _FLAGS,
    ),
    "birth_date": re.compile(rf"\bfecha{_SP}++de{_SP}++nacimiento{_SEP}\d", _FLAGS),
    "name": re.compile(
        rf"(?i:\bnombre{_SP}++del?{_SP}++(?:paciente|miembro|afiliado|asegurado)"
        rf"{_SEP})[A-ZÁÉÍÓÚÑ]"
        rf"|^{_SP}*+(?i:estimad[oa]s?){_SP}++(?!(?i:miembros?|afiliados?|clientes?"
        rf"|asegurados?|pacientes?|participantes?|socios?)\b)[A-ZÁÉÍÓÚÑ]",
        re.MULTILINE,
    ),
}

# In text already cleaned: a person's label with "[removed]" after it, on
# its line or the next, the labels plan_document_scrub reads.
_GONE = re.escape(REMOVED)
_PEOPLE = (
    rf"(?:member|patient|subscriber|insured|enrollee|cardholder|beneficiary"
    rf"|dependents?|recipient|guarantor|policy{_SP}?holder)"
)
_REMOVED_DETAILS = {
    "id": re.compile(
        rf"(?:\b(?:{_PEOPLE}|claim|account|case|medicare|medicaid)(?:'s)?{_SP}*+"
        rf"(?:id(?:entification)?|identifier|number|no\b\.?|#)"
        rf"|\b(?:mbi|mrn|cin)\b|^{_SP}*+id\b){_SEP}{_GONE}",
        _FLAGS | re.MULTILINE,
    ),
    "birth_date": re.compile(
        rf"(?:\bdate{_SP}++of{_SP}++birth|\bbirth{_SP}*+date|\bd\.?o\.?b\b\.?)"
        rf"(?:{_SP}*+\([^()\n]{{0,20}}\))?{_SEP}{_GONE}",
        _FLAGS,
    ),
    "ssn": re.compile(
        rf"\b(?:ssn|ss#|social{_SP}++security(?:{_SP}++(?:number|no\b\.?|#))?)"
        rf"{_SEP}{_GONE}",
        _FLAGS,
    ),
    "name": re.compile(
        rf"(?:\b{_PEOPLE}(?:'s)?{_SP}++name{_SP}*+:?|\b{_PEOPLE}{_SP}*+:"
        rf"|^{_SP}*+name{_SP}*+:){_SP}*+(?:\n{_SP}*+)?{_GONE}"
        rf"|^{_SP}*+dear{_SP}++{_GONE}",
        _FLAGS | re.MULTILINE,
    ),
}

_WORD = re.compile(r"[^\W\d_]+")
# Words a health plan's papers use; "red" (network) and "plan" are left out,
# as everyday words elsewhere.
_PLAN_WORDS = frozenset(
    """insurance insurer health coverage covered benefit benefits medical
    premium premiums member members provider providers network claim claims
    copay copays copayment coinsurance deductible deductibles drug drugs
    pharmacy prescription prescriptions hospital formulary tier enrollment
    eligibility hmo ppo epo pos medicare medicaid dental vision preventive
    seguro salud cobertura beneficio beneficios médico médica prima primas
    miembro miembros proveedor proveedores reclamo reclamos copago coseguro
    deducible medicamento medicamentos farmacia afiliado afiliados""".split()
)


def _detail_kinds(sample: str) -> frozenset[str]:
    kinds = set(personal_detail_kinds(sample))
    for rules in (_SPANISH_DETAILS, _REMOVED_DETAILS):
        kinds.update(kind for kind, rule in rules.items() if rule.search(sample))
    return frozenset(kinds)


def _has_no_plan_words(sample: str) -> bool:
    words = _WORD.findall(sample.lower())
    if len(words) < OTHER_MIN_WORDS:
        return False
    letters = "".join(words)
    if sum(1 for ch in letters if ord(ch) > 0x24F) * 2 > len(letters):
        # Mostly not the Latin alphabet: too little to tell.
        return False
    return len(_PLAN_WORDS.intersection(words)) <= OTHER_MAX_PLAN_WORDS


def document_kind(text: str) -> str:
    """PLAN, PERSONAL or OTHER for a shared file's text (module docstring)."""
    sample = text[:SAMPLE_CHARS]
    head = sample[:TITLE_CHARS]
    personal_title = _PERSONAL_TITLE.search(head) is not None
    denial = _DENIAL.search(head) is not None
    plan_title = _PLAN_TITLE.search(head) is not None
    details = _detail_kinds(sample)
    if personal_title and (details or not plan_title):
        return PERSONAL
    if denial and details and (not plan_title or len(details) >= 2):
        return PERSONAL
    if not plan_title and len(details) >= 2:
        return PERSONAL
    if not plan_title and not details and _has_no_plan_words(sample):
        return OTHER
    return PLAN
