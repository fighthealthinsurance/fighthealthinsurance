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
MEDICAID_ELIGIBILITY_REGEX = r".*?(?:\*\*)?medicaid_eligibility\s*(\{[^}]*\})\s*(?:\*\*)?"

# Create or update appeal tool - captures JSON with appeal data
# Matches: create_or_update_appeal {JSON} with optional ** markers
CREATE_OR_UPDATE_APPEAL_REGEX = r"^\s*\*{0,4}create_or_update_appeal\*{0,4}\s*(\{.*\})\s*$"

# Create or update prior auth tool - captures JSON with prior auth data
# Matches: create_or_update_prior_auth {JSON} with optional ** markers
CREATE_OR_UPDATE_PRIOR_AUTH_REGEX = (
    r"^\s*\*{0,4}create_or_update_prior_auth\*{0,4}\s*(\{.*\})\s*$"
)

# List of all tool patterns for scoring/detection
ALL_TOOL_PATTERNS = [
    PUBMED_QUERY_REGEX,
    MEDICAID_INFO_REGEX,
    MEDICAID_ELIGIBILITY_REGEX,
    CREATE_OR_UPDATE_APPEAL_REGEX,
    CREATE_OR_UPDATE_PRIOR_AUTH_REGEX,
]
