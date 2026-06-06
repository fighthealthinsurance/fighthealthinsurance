"""
Regex patterns for detecting tool calls in LLM responses.

These patterns match special tokens/markers that the LLM includes in its
responses to indicate it wants to invoke a tool (e.g., search PubMed,
look up Medicaid info, create/update appeals, etc.)
"""

# PubMed query tool - captures query terms
# Matches: [pubmed_query: terms], **pubmed query: terms**, etc.
PUBMED_QUERY_REGEX = r"[\[\*]{0,4}pubmed[ _]?query:?\s*([^*\[\]]+)"

# Medicaid info lookup tool - captures JSON parameters
# Matches: medicaid_info {JSON} or **medicaid_info {JSON}**
MEDICAID_INFO_REGEX = r"(?:\*\*)?medicaid_info\s*(\{[^}]*\})\s*(?:\*\*)?"

# Medicaid eligibility tool - captures JSON parameters
# Matches: medicaid_eligibility {JSON} or **medicaid_eligibility {JSON}**
MEDICAID_ELIGIBILITY_REGEX = (
    r".*?(?:\*\*)?medicaid_eligibility\s*(\{[^}]*\})\s*(?:\*\*)?"
)

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
