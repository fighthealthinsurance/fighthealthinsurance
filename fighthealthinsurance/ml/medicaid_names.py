"""State-by-state names for Medicaid, shared by the prompt and the filters.

Medicaid is called something different in most states, and the system prompt
tells the model to use whichever name the user does. Four other places have
to know the same list:

* ``ml_models`` builds the "medicaid can go by many names" reminder from it,
  and attaches the Medicaid tool blocks to the prompt when one shows up in
  the chat (``RemoteModelLike.MEDICAID_KEYWORDS``).
* ``chat.safety_filters.detect_eligibility_verdict`` has to recognize a
  verdict about a state program ("you qualify for Medi-Cal") as the same
  claim as one about "Medicaid" -- otherwise the invented-verdict penalty
  misses exactly the phrasing the prompt encourages.
* ``chat.eligibility_intent`` reads the USER's side of the same sentences
  ("do I qualify for Medi-Cal?") and, for the names that belong to one
  state, learns the state from them.
* ``medicaid_api._normalize_state`` resolves a program name the model passed
  straight through as the "state".

Stdlib-only and dependency-free on purpose: safety_filters is imported from
inside the chat package, which ml_models must not import back, and
medicaid_api pulls in pandas.
"""

# Program name -> lowercase postal code, for the names that belong to ONE
# state. Kept alphabetical (case-insensitively) so additions are easy to spot
# in review. Spelling variants the model emits ("medquest", "forwardhealth",
# "star plus") live in medicaid_api next to its other aliases, so this list
# stays one display name per program for the prompt's reminder.
#
# Deliberately absent: a bare "Medical" (Pennsylvania, Minnesota and Maryland
# all call their programs "Medical Assistance", so users there say "Medical"
# and it must not resolve to California), and "Healthy Connections", which
# names both South Carolina's and Idaho's programs -- on opposite sides of
# the expansion line.
MEDICAID_PROGRAM_STATES: dict[str, str] = {
    "AHCCCS": "az",
    "Apple Health": "wa",
    "BadgerCare": "wi",
    "BadgerCare Plus": "wi",
    "Cardinal Care": "va",
    "DenaliCare": "ak",
    "Diamond State Health Plan": "de",
    "Equality Care": "wy",
    "Forward Health": "wi",
    "Green Mountain Care": "vt",
    "Health First Colorado": "co",
    "HealthChoice Illinois": "il",
    "Healthy Michigan Plan": "mi",
    "Hoosier Healthwise": "in",
    "Husky Health": "ct",
    "Iowa Medicaid": "ia",
    "KanCare": "ks",
    "Kansas Medical Assistance Program": "ks",
    "MaineCare": "me",
    "MassHealth": "ma",
    "Med-QUEST": "hi",
    "Medi-Cal": "ca",
    "MO HealthNet": "mo",
    "New York State Medicaid": "ny",
    "NJ FamilyCare": "nj",
    "Oregon Health Plan": "or",
    "SoonerCare": "ok",
    "STAR+PLUS": "tx",
    "TennCare": "tn",
    "Turquoise Care": "nm",
}

# Names that are a Medicaid program somewhere, but not ONE state's: shared by
# several states, or an ordinary phrase before it is a program name. They are
# in the prompt's reminder and in the verdict/intent sentence shapes, but
# never resolve to a state.
MEDICAID_PROGRAM_NAMES_WITHOUT_A_STATE: tuple[str, ...] = (
    "Healthy Connections",  # South Carolina and Idaho
    "Medical Assistance",  # Pennsylvania, Minnesota, Maryland, ...
    "Medical Assistance Program",
    "STAR",  # Texas -- but "star" is a word first
)

# Aliases that are ordinary English before they are program names. They stay
# in MEDICAID_PROGRAM_ALIASES -- the prompt wants them, because a user saying
# "STAR" tells the model they are in Texas -- but they are NOT evidence that a
# sentence is about Medicaid eligibility: "you qualify for medical assistance
# benefits" and "do I qualify for STAR" were both read as Medicaid verdicts /
# requests, and a false positive there costs a candidate its whole model
# prior (safety_filters) or starts an interview nobody asked for
# (eligibility_intent). Both detectors leave these out and err toward
# missing a generically-named program.
AMBIGUOUS_PROGRAM_ALIASES: frozenset[str] = frozenset(
    {"STAR", "Medical Assistance", "Medical Assistance Program"}
)

# Every name, for the prompt's reminder and the sentence-shape detectors.
# Derived, so a program added above cannot be missing from a consumer.
MEDICAID_PROGRAM_ALIASES: tuple[str, ...] = tuple(
    sorted(
        set(MEDICAID_PROGRAM_STATES) | set(MEDICAID_PROGRAM_NAMES_WITHOUT_A_STATE),
        key=str.lower,
    )
)
