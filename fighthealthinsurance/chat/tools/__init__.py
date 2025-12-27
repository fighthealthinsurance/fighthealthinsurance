"""
Chat tools package - contains tool handlers for chat interface.

Each tool handler processes specific "tool calls" that the LLM may include
in its responses (e.g., PubMed searches, Medicaid lookups, etc.)
"""
from .patterns import (
    PUBMED_QUERY_REGEX,
    MEDICAID_INFO_REGEX,
    MEDICAID_ELIGIBILITY_REGEX,
    CREATE_OR_UPDATE_APPEAL_REGEX,
    CREATE_OR_UPDATE_PRIOR_AUTH_REGEX,
    ALL_TOOL_PATTERNS,
)
from .base_tool import BaseTool
from .pubmed_tool import PubMedTool
from .medicaid_tool import MedicaidInfoTool, MedicaidEligibilityTool

__all__ = [
    # Patterns
    "PUBMED_QUERY_REGEX",
    "MEDICAID_INFO_REGEX",
    "MEDICAID_ELIGIBILITY_REGEX",
    "CREATE_OR_UPDATE_APPEAL_REGEX",
    "CREATE_OR_UPDATE_PRIOR_AUTH_REGEX",
    "ALL_TOOL_PATTERNS",
    # Tool handlers
    "BaseTool",
    "PubMedTool",
    "MedicaidInfoTool",
    "MedicaidEligibilityTool",
]
