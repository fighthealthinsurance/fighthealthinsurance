"""
Keep Your Care (https://keepyourcare.org/) links.

Keep Your Care is a nonprofit that helps people keep their Medicaid under the
new federal work/community-engagement requirements: what the rules are, when
their state starts, and navigators who can help. It only runs state pages for
the states it is active in, so a deep link is built only for those and every
other state gets the site's home page (its FAQ and sign-up are national).

This is the one place that knows the site's URLs, so the chat tools, the
system prompt, the state help pages and the MCP server can't drift apart.
"""

from typing import Optional

KEEP_YOUR_CARE_NAME = "Keep Your Care"
KEEP_YOUR_CARE_URL = "https://keepyourcare.org/"

# States with their own page at /states/<code>/, as linked from
# https://keepyourcare.org/faq/ (checked November 2026). The site says it is
# "only active in select states"; add a state here when it gets a page.
KEEP_YOUR_CARE_STATES: frozenset[str] = frozenset(
    {"CA", "GA", "IL", "MI", "NC", "NY", "VA"}
)

KEEP_YOUR_CARE_DESCRIPTION = (
    "Free help keeping your Medicaid under the new work requirements: "
    "your state's deadlines, who is exempt, and navigators who can help."
)


def keep_your_care_state_url(abbreviation: str) -> str:
    """The state's own Keep Your Care page. Caller must check it has one."""
    return f"{KEEP_YOUR_CARE_URL}states/{abbreviation.lower()}/"


def _to_abbreviation(state: Optional[str]) -> Optional[str]:
    """A two-letter code for a code or full state name, or None.

    Resolved against state_help.json rather than medicaid_api's normalizer,
    which pulls in pandas; the chat tools hand us whatever the model wrote,
    which is a full name ("California") or a code ("CA") in practice.
    """
    if not state or not isinstance(state, str):
        return None
    cleaned = state.strip()
    if len(cleaned) == 2:
        return cleaned.upper()
    # Lazy: state_help imports this module.
    from fighthealthinsurance.state_help import load_state_help

    wanted = cleaned.lower()
    for known in load_state_help().values():
        if known.name.lower() == wanted or known.slug == wanted.replace(" ", "-"):
            return known.abbreviation
    return None


def keep_your_care_url(state: Optional[str] = None) -> Optional[str]:
    """The state's Keep Your Care page when it has one, else None."""
    abbreviation = _to_abbreviation(state)
    if abbreviation and abbreviation in KEEP_YOUR_CARE_STATES:
        return keep_your_care_state_url(abbreviation)
    return None


def keep_your_care_url_or_home(state: Optional[str] = None) -> str:
    """The state's Keep Your Care page, falling back to the home page."""
    return keep_your_care_url(state) or KEEP_YOUR_CARE_URL


def keep_your_care_chat_note(state: Optional[str] = None) -> str:
    """Guidance for the chat model on pointing someone at Keep Your Care.

    Hands the model the finished URL so it never has to build one: a guessed
    /states/<code>/ for a state the site doesn't cover is a dead link.
    """
    state_url = keep_your_care_url(state)
    if state_url:
        return (
            f"If they are on Medicaid (or applying) and the work requirements "
            f"could apply to them, mention {KEEP_YOUR_CARE_NAME}, a nonprofit "
            f"with free help keeping Medicaid under the new rules, and link "
            f"their state's page exactly as written: {state_url}"
        )
    return (
        f"If they are on Medicaid (or applying) and the work requirements "
        f"could apply to them, mention {KEEP_YOUR_CARE_NAME}, a nonprofit "
        f"with free help keeping Medicaid under the new rules, at "
        f"{KEEP_YOUR_CARE_URL} -- link that exact address and do NOT build a "
        f"state page URL for it, since the site only has pages for some states."
    )


def keep_your_care_prompt_links() -> str:
    """The state pages, one per line, for the system prompt."""
    return "\n".join(
        f"- {code}: {keep_your_care_state_url(code)}"
        for code in sorted(KEEP_YOUR_CARE_STATES)
    )
