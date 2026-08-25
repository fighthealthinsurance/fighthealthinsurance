"""State-by-state names for Medicaid, shared by the prompt and the filters.

Medicaid is called something different in most states, and the system prompt
tells the model to use whichever name the user does. Two other places have to
know the same list:

* ``ml_models`` builds the "medicaid can go by many names" reminder from it.
* ``chat.safety_filters.detect_eligibility_verdict`` has to recognize a
  verdict about a state program ("you qualify for Medi-Cal") as the same
  claim as one about "Medicaid" -- otherwise the invented-verdict penalty
  misses exactly the phrasing the prompt encourages.

Stdlib-only and dependency-free on purpose: safety_filters is imported from
inside the chat package, which ml_models must not import back.
"""

# Ordered longest-name-first is NOT required here (the consumers build their
# own alternations), but the list is kept alphabetical so additions are easy
# to spot in review.
MEDICAID_PROGRAM_ALIASES: tuple[str, ...] = (
    "Apple Health",
    "Cardinal Care",
    "DenaliCare",
    "Diamond State Health Plan",
    "Equality Care",
    "Forward Health",
    "Green Mountain Care",
    "Health First Colorado",
    "HealthChoice Illinois",
    "Healthy Connections",
    "Hoosier Healthwise",
    "Husky Health",
    "Iowa Medicaid",
    "Kansas Medical Assistance Program",
    "MaineCare",
    "MassHealth",
    "Med-QUEST",
    "Medi-Cal",
    "Medical Assistance",
    "Medical Assistance Program",
    "MO HealthNet",
    "New York State Medicaid",
    "NJ FamilyCare",
    "SoonerCare",
    "STAR",
    "STAR+PLUS",
    "TennCare",
    "Turquoise Care",
)
