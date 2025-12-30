"""
Chat tools package - contains tool handlers for chat interface.

Each tool handler processes specific "tool calls" that the LLM may include
in its responses (e.g., PubMed searches, Medicaid lookups, etc.)

NOTE: These tool handlers have been extracted from chat_interface.py into
reusable classes but are NOT YET INTEGRATED. The chat_interface.py still
uses inline tool handling code. Integration is tracked as a future task.

To use these handlers, instantiate them with the appropriate callbacks
and call their handle() method with the LLM response text.
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
from .appeal_tool import AppealTool
from .prior_auth_tool import PriorAuthTool

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
    "AppealTool",
    "PriorAuthTool",
]
