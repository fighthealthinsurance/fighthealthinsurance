"""
Chat tools package - contains tool handlers for chat interface.

Each tool handler processes specific "tool calls" that the LLM may include
in its responses (e.g., PubMed searches, Medicaid lookups, etc.)

To use these handlers, instantiate them with the appropriate callbacks
and call their handle() method with the LLM response text.
"""

from .appeal_tool import AppealTool
from .base_tool import BaseTool
from .doc_fetcher_tool import DocFetcherTool
from .json_followup_tool import JsonFollowupTool
from .medicaid_tool import MedicaidEligibilityTool, MedicaidInfoTool
from .pa_requirement_tool import PaRequirementLookupTool
from .patterns import (
    ALL_TOOL_PATTERNS,
    CREATE_OR_UPDATE_APPEAL_REGEX,
    CREATE_OR_UPDATE_PRIOR_AUTH_REGEX,
    FETCH_DOC_REGEX,
    LOOKUP_PA_REQUIREMENT_REGEX,
    MEDICAID_ELIGIBILITY_REGEX,
    MEDICAID_INFO_REGEX,
    PUBMED_QUERY_REGEX,
    USPSTF_LOOKUP_REGEX,
)
from .prior_auth_tool import PriorAuthTool
from .pubmed_tool import PubMedTool
from .uspstf_tool import USPSTFLookupTool

__all__ = [
    # Patterns
    "PUBMED_QUERY_REGEX",
    "MEDICAID_INFO_REGEX",
    "MEDICAID_ELIGIBILITY_REGEX",
    "CREATE_OR_UPDATE_APPEAL_REGEX",
    "CREATE_OR_UPDATE_PRIOR_AUTH_REGEX",
    "FETCH_DOC_REGEX",
    "USPSTF_LOOKUP_REGEX",
    "LOOKUP_PA_REQUIREMENT_REGEX",
    "ALL_TOOL_PATTERNS",
    # Tool handlers
    "BaseTool",
    "JsonFollowupTool",
    "PubMedTool",
    "MedicaidInfoTool",
    "MedicaidEligibilityTool",
    "AppealTool",
    "PriorAuthTool",
    "DocFetcherTool",
    "USPSTFLookupTool",
    "PaRequirementLookupTool",
]
