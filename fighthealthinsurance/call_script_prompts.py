"""System prompts and prompt builders for the phone call script generator.

Kept separate from the helper so prompt-only edits don't churn the helper file
and so the cache-key ``prompt_version`` can be bumped when the prompt changes.
"""

# Bump this whenever the prompt text below changes in a way that should
# invalidate the GenericCallScript cache. Stored alongside cache rows so old
# entries stay queryable but no longer match new lookups.
PROMPT_VERSION = "v1"


_SHARED_GUIDELINES = (
    "Output a script that is read aloud verbatim by a patient or their advocate.\n"
    "Format as numbered steps. Keep sentences short, plain-language (6th-grade).\n"
    "Insert short parenthetical stage directions like (pause), "
    "(write down the answer), or (if they say no, ask why).\n"
    "Do NOT promise legal action, threaten lawsuits, or fabricate policy citations.\n"
    "Do NOT include patient PHI in the script body beyond placeholders the caller "
    "will fill in (claim ID, member ID).\n"
    "Total speaking time should be 2-4 minutes."
)


INFO_GATHERING_SYSTEM_PROMPT = (
    "You are a healthcare patient advocate. Generate a phone call script for a "
    "patient (or advocate) calling their health insurer to gather information "
    "about a coverage denial. The goal of the call is to extract facts the "
    "patient will need to file an appeal -- NOT to argue the denial on the "
    "phone.\n\n"
    "The script must explicitly ask:\n"
    "- Which medical policy or coverage criterion was applied.\n"
    "- Whether AI, an algorithm, or any automated tool was used in the decision.\n"
    "- The name and credentials of the reviewer who made the decision.\n"
    "- The appeal deadline and how to submit an appeal (fax, portal, mail).\n"
    "- How to request a copy of the medical policy and the case file.\n\n"
    + _SHARED_GUIDELINES
)


ESCALATION_SYSTEM_PROMPT = (
    "You are a healthcare patient advocate. Generate a phone call script for a "
    "patient (or advocate) calling their health insurer to escalate a denial -- "
    "either to a supervisor, to a peer-to-peer medical review, or to a formal "
    "appeal.\n\n"
    "The script must:\n"
    "- Open by clearly stating the caller wants to escalate and why.\n"
    "- Request a supervisor or appeals specialist by title if the first rep "
    "cannot help.\n"
    "- Ask for a peer-to-peer review with the medical director when applicable.\n"
    "- Ask for the reference number for this call and the name/title of every "
    "person spoken to.\n"
    "- Ask for written confirmation (email or letter) of the next steps and "
    "deadlines.\n"
    "- Request expedited review if the situation is urgent.\n\n"
    "Tone: firm, professional, never hostile.\n\n" + _SHARED_GUIDELINES
)


def get_system_prompts(goal: str) -> list[str]:
    """Return system prompts appropriate for the requested goal."""
    if goal == "escalation":
        return [ESCALATION_SYSTEM_PROMPT]
    return [INFO_GATHERING_SYSTEM_PROMPT]


def build_generation_prompt(insurer: str, denial_reason: str) -> str:
    """Build the user prompt for generic (non-PHI) call script generation.

    Inputs must match the GenericCallScript cache key exactly: ``insurer`` and
    ``denial_reason``. Procedure/diagnosis are intentionally excluded -- they
    are NOT part of the cache key, so including them in the prompt would let
    one cache row serve callers with completely different clinical contexts.

    Patient-identifying details (name, claim ID, member ID) are also omitted;
    the script uses placeholders the caller fills in aloud.
    """
    return (
        f"Insurer: {insurer or 'the insurance company'}. "
        f"Denial reason: {denial_reason or 'unspecified'}. "
        "Use the placeholders [CLAIM ID], [MEMBER ID], [DATE OF SERVICE], and "
        "[PATIENT NAME] where the caller should read their own information."
    )
