"""
USPSTF preventive-services lookup tool for the chat interface.

When the LLM emits a ``uspstf_lookup`` tool call, this handler looks up
recommendations from the cached USPSTF Prevention TaskForce data and feeds
them back into the conversation. USPSTF A/B graded services generally must
be covered without cost-sharing under the ACA, so the tool is most useful
for preventive-care denial appeals.
"""

from typing import Tuple

from asgiref.sync import sync_to_async

from .json_followup_tool import JsonFollowupTool
from .patterns import USPSTF_LOOKUP_REGEX


class USPSTFLookupTool(JsonFollowupTool):
    """
    Tool handler for USPSTF preventive-service lookups.

    Expected tool-call format:
        **uspstf_lookup {"query": "colorectal cancer", "grade": "A", "limit": 3}**

    All JSON keys are optional. The tool returns matching recommendations
    formatted for the LLM to incorporate into appeal language.
    """

    pattern = USPSTF_LOOKUP_REGEX
    name = "USPSTF Lookup"
    # Downstream tools (e.g. appeal generation) can reuse the raw
    # recommendation text via the running context. Only the raw lookup
    # output is appended — the LLM-targeted instruction text lives in
    # ``build_followup_prompt`` so it doesn't bloat downstream prompts.
    append_note_to_context: bool = True

    async def run(
        self, params: dict, *, current_message_for_llm: str = ""
    ) -> Tuple[str, str]:
        from fighthealthinsurance.uspstf_api import get_uspstf_info

        info = await sync_to_async(get_uspstf_info)(params)
        if not info:
            return "", ""

        descriptor = (
            params.get("query")
            or params.get("topic")
            or params.get("grade")
            or "USPSTF"
        )
        return info, f"USPSTF results for {descriptor} ready."

    def build_followup_prompt(self, note: str, current_message_for_llm: str) -> str:
        return (
            "USPSTF preventive-service recommendations relevant to the user's "
            f"question:\n\n{note}\n\n"
            "Use this information to answer their question. When the cited "
            "recommendation is grade A or B, remind the user that under the ACA "
            "non-grandfathered private plans, the marketplace, and Medicaid "
            "expansion populations generally must cover the service without "
            "cost-sharing. Always link to the USPSTF source URL when citing a "
            "specific recommendation."
        )
