"""
Regex patterns for detecting tool calls in LLM responses.

These patterns match special tokens/markers that the LLM includes in its
responses to indicate it wants to invoke a tool (e.g., search PubMed,
look up Medicaid info, create/update appeals, etc.)
"""

import re
from typing import Optional

# PubMed query tool - captures query terms
# Matches: [pubmed query: terms], **pubmed_query: terms**, pubmedquery:[terms]
# Mirrors RXNORM_LOOKUP_REGEX: a leading `[`/`*` marker or a word boundary, a
# MANDATORY colon, a non-greedy capture that stops at the closing wrapper /
# newline / end-of-string, and an optional `[...]` around the terms (the
# appeal-generation prompt documents that form).
#
# The colon used to be optional, which made prose like "I can run a pubmed
# query for you if that would help" register as a tool call -- both to the
# handler and to the fan-out's tool-call bonus, so a chatty non-answer could
# outscore a candidate that actually called a tool.
PUBMED_QUERY_REGEX = (
    r"(?:[\[\*]{1,4}|\b)pubmed[ _]?query\s*:\s*\[?"
    r"([^*\[\]\n]+?)\s*\]?(?:[\]\*]{1,4}|$|(?=\n))"
)

# Medicaid info lookup tool - captures JSON parameters
# Matches: medicaid_info {JSON} or **medicaid_info {JSON}**
MEDICAID_INFO_REGEX = r"(?:\*\*)?medicaid_info\s*(\{[^}]*\})\s*(?:\*\*)?"

# Medicaid eligibility tool - captures JSON parameters
# Matches: medicaid_eligibility {JSON} or **medicaid_eligibility {JSON}**
MEDICAID_ELIGIBILITY_REGEX = r"(?:\*\*)?medicaid_eligibility\s*(\{[^}]*\})\s*(?:\*\*)?"

# Create or update appeal tool - captures JSON with appeal data
# Matches: create_or_update_appeal {JSON} with optional ** markers
CREATE_OR_UPDATE_APPEAL_REGEX = (
    r"^\s*\*{0,4}create_or_update_appeal\*{0,4}\s*(\{.*\})\s*$"
)

# Create or update prior auth tool - captures JSON with prior auth data
# Matches: create_or_update_prior_auth {JSON} with optional ** markers
CREATE_OR_UPDATE_PRIOR_AUTH_REGEX = (
    r"^\s*\*{0,4}create_or_update_prior_auth\*{0,4}\s*(\{.*\})\s*$"
)

# Document fetcher tool - captures JSON with URL
# Matches: fetch_doc {JSON} or **fetch_doc {JSON}**
FETCH_DOC_REGEX = r"(?:\*\*)?fetch_doc\s*(\{[^}]*\})\s*(?:\*\*)?"

# USPSTF preventive-services lookup tool - captures JSON parameters
# Matches: uspstf_lookup {JSON} or **uspstf_lookup {JSON}**
USPSTF_LOOKUP_REGEX = r"(?:\*\*)?uspstf_lookup\s*(\{[^}]*\})\s*(?:\*\*)?"

# PA requirement lookup tool - captures JSON with codes/payer/state/LOB.
# Matches: lookup_pa_requirement {JSON} or **lookup_pa_requirement {JSON}**
#
# The regex only matches up to the first ``}``; balanced-brace extraction
# for nested-object payloads (``{"filters": {"lob": ...}}``) happens in
# ``pa_requirement_tool._run_lookup`` via the message-walking helper.
# Keeping the pattern simple avoids the nested-quantifier ReDoS risk that
# a true balanced-brace regex would carry.
LOOKUP_PA_REQUIREMENT_REGEX = (
    r"(?:\*\*)?lookup_pa_requirement\s*(\{[^}]*\})\s*(?:\*\*)?"
)

# RxNorm drug normalization tool - captures the drug name (free-text).
# Matches: [rxnorm_lookup: drug name], **rxnorm_lookup: drug name**, etc.
# The colon is mandatory and we require either a leading `[`/`*` marker
# or a word boundary, so prose like "RxNorm lookup for Lipitor" doesn't
# trip it. The capture is non-greedy and terminates at the closing
# `]`/`**` wrapper, end of line, or end of string, so cleanup doesn't
# leave stray delimiters in the response.
RXNORM_LOOKUP_REGEX = (
    r"(?:[\[\*]{1,4}|\b)rxnorm[ _]?lookup\s*:\s*"
    r"([^*\[\]\n]+?)\s*(?:[\]\*]{1,4}|$|(?=\n))"
)

# ClinicalTrials.gov query tool - captures search terms
# Matches: [clinical_trials_query: terms], **clinical trials query: terms**, etc.
# Useful when an insurer denies a treatment as "experimental/investigational"
# and we need to check the public trial registry.
# Mirrors RXNORM_LOOKUP_REGEX: requires a leading `[`/`*` marker or a word
# boundary, the colon is mandatory, the capture is non-greedy and stops at
# the closing wrapper, newline, or end-of-string. This way an LLM that
# tacks prose after the token (e.g. "...melanoma. Also consider...") can't
# get the trailing prose silently captured and stripped from the reply.
CLINICAL_TRIALS_QUERY_REGEX = (
    r"(?:[\[\*]{1,4}|\b)clinical[ _]?trials?[ _]?query\s*:\s*"
    r"([^*\[\]\n]+?)\s*(?:[\]\*]{1,4}|$|(?=\n))"
)

# Financial assistance directory tool - detects the call prefix only.
# Matches: financial_assistance {JSON} or **financial_assistance {JSON}**
# Looks up pharmacy discount programs (GoodRx, Cost Plus, Crush Cost,
# Amazon Pharmacy), diagnosis-specific copay foundations, manufacturer
# programs, safety-net clinics (340B), and state Medicaid pathways.
#
# The other JSON-payload tools above bound their body with `\{[^}]*\}`, which
# stops at the first `}`. That's fine for their tightly-schemaed payloads,
# but this tool accepts a free-form `denial_text` field whose value can
# contain `}` characters (and the LLM occasionally emits nested objects),
# so a `[^}]*` cap would truncate valid calls and break `json.loads`.
# Instead the pattern only matches the prefix up to the opening `{` (via
# lookahead) and FinancialAssistanceTool uses `json.JSONDecoder().raw_decode`
# to find the real end of the JSON object at runtime. There is no capture
# group; FinancialAssistanceTool reads the payload via _parse_payload(),
# not via match.group(1).
FINANCIAL_ASSISTANCE_REGEX = r"(?:\*\*)?financial_assistance\s*(?=\{)"

# Flags the tool HANDLERS use when detecting a call in a reply (each handler
# sets `detect_flags`). Anything else that screens a reply for tool calls must
# use the same flags: several patterns are `^...$`-anchored, so a flag-less
# re.search only ever matches a tool call at the very start of the reply and
# silently passes one that follows a sentence of prose.
TOOL_DETECT_FLAGS = re.DOTALL | re.MULTILINE | re.IGNORECASE

# Tokens that mean a reply is part of a tool/action flow rather than a plain
# conversational answer (those flows mutate state; only the winner runs them).
# Matches the bare name too -- the handlers tolerate 0-4 asterisks, so
# requiring `**name**` adjacency missed the unadorned form.
ACTION_TOKEN_MENTION_RE = re.compile(
    r"\*{0,4}\b(?:create_or_update_appeal|create_or_update_prior_auth)\b\*{0,4}",
    re.IGNORECASE,
)


# List of all tool patterns for scoring/detection
ALL_TOOL_PATTERNS = [
    PUBMED_QUERY_REGEX,
    MEDICAID_INFO_REGEX,
    MEDICAID_ELIGIBILITY_REGEX,
    CREATE_OR_UPDATE_APPEAL_REGEX,
    CREATE_OR_UPDATE_PRIOR_AUTH_REGEX,
    FETCH_DOC_REGEX,
    USPSTF_LOOKUP_REGEX,
    LOOKUP_PA_REQUIREMENT_REGEX,
    RXNORM_LOOKUP_REGEX,
    CLINICAL_TRIALS_QUERY_REGEX,
    FINANCIAL_ASSISTANCE_REGEX,
]


# Compiled once: contains_tool_call runs per alternate candidate, and relying
# on re's bounded internal cache makes that cost implicit.
_COMPILED_TOOL_PATTERNS = [re.compile(p, TOOL_DETECT_FLAGS) for p in ALL_TOOL_PATTERNS]


def count_tool_invocations(text: Optional[str]) -> int:
    """How many distinct tool patterns ``text`` actually invokes.

    Used by the fan-out scorer for the per-tool-call bonus. Two properties
    matter and neither is free:

    * It must use TOOL_DETECT_FLAGS, like the handlers do. A flag-less
      ``re.search`` over the same patterns silently misses every call that
      isn't at the very start of the reply (CREATE_OR_UPDATE_* are
      ``^...$``-anchored, which needs MULTILINE) and every case variant.
    * It counts PATTERNS, not matches, so a reply repeating one tool call
      five times can't out-bonus a reply that calls two different tools.

    Unlike contains_tool_call this ignores bare token mentions
    (ACTION_TOKEN_MENTION_RE): mentioning ``create_or_update_appeal`` in prose
    is not a tool call and shouldn't earn the bonus.
    """
    if not text:
        return 0
    return sum(1 for p in _COMPILED_TOOL_PATTERNS if p.search(text))


def contains_tool_call(text: str) -> bool:
    """Whether ``text`` contains anything a tool handler would fire on.

    Uses TOOL_DETECT_FLAGS so this agrees with the handlers themselves. Any
    reply shown to a user WITHOUT running the tool pipeline (the side-by-side
    alternate answer) must be screened through here, or raw tool syntax and
    its JSON payload reach the browser.
    """
    if not text:
        return False
    if ACTION_TOKEN_MENTION_RE.search(text):
        return True
    return any(p.search(text) for p in _COMPILED_TOOL_PATTERNS)
