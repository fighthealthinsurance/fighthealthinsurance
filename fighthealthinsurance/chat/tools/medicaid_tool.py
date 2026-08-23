"""
Medicaid tool handlers for the chat interface.

Handles medicaid_info and medicaid_eligibility tool calls from the LLM
to look up Medicaid information and check eligibility.
"""

import json
import re
from typing import Any, Awaitable, Callable, List, Optional, Tuple

# Plain asgiref sync_to_async (NOT database_sync_to_async): used below for a
# non-ORM blocking call, per the repo convention for subprocess/network/file
# work.
from asgiref.sync import sync_to_async

from loguru import logger

from .base_tool import BaseTool
from .patterns import MEDICAID_ELIGIBILITY_REGEX, MEDICAID_INFO_REGEX


class MedicaidInfoTool(BaseTool):
    """
    Tool handler for Medicaid information lookups.

    When the LLM includes a medicaid_info call in its response, this tool:
    1. Extracts the JSON parameters (state, topic, limit)
    2. Calls the Medicaid API
    3. Returns context for the LLM to incorporate
    """

    pattern = MEDICAID_INFO_REGEX
    detect_flags = re.DOTALL | re.IGNORECASE
    name = "Medicaid Info"

    def __init__(
        self,
        send_status_message: Callable[[str], Awaitable[None]],
        call_llm_callback: Optional[
            Callable[..., Awaitable[Tuple[Optional[str], Optional[str]]]]
        ] = None,
    ):
        """
        Initialize the Medicaid info tool.

        Args:
            send_status_message: Async function to send status updates
            call_llm_callback: Callback to call LLM with additional context
        """
        super().__init__(send_status_message)
        self.call_llm_callback = call_llm_callback

    async def execute(
        self,
        match: re.Match[str],
        response_text: str,
        context: str,
        model_backends: Any = None,
        current_message_for_llm: str = "",
        history_for_llm: Optional[List[dict]] = None,
        depth: int = 0,
        is_logged_in: bool = False,
        is_professional: bool = False,
        **kwargs,
    ) -> Tuple[str, str]:
        """
        Execute Medicaid info lookup and incorporate results.

        Args:
            match: Regex match containing JSON parameters
            response_text: Current LLM response
            context: Current context string
            model_backends: LLM backends for follow-up calls
            current_message_for_llm: The user's current message
            history_for_llm: Chat history (will be modified)
            depth: Current recursion depth
            is_logged_in: Whether user is logged in
            is_professional: Whether user is a professional

        Returns:
            Tuple of (updated_response, updated_context)
        """
        # Find all matches and clean all of them
        all_matches = self.detect_all(response_text)
        cleaned_response = self.clean_all_matches(response_text, all_matches)

        if len(all_matches) > 1:
            logger.warning(
                f"Found {len(all_matches)} Medicaid tool calls, processing only the first one"
            )

        logger.debug(f"Medicaid tool call detected: {match.group(0)}")

        json_data = match.group(1).strip()
        logger.debug(f"Extracted JSON data: {json_data}")

        try:
            medicaid_info_data = json.loads(json_data)
            logger.debug(f"Parsed JSON data: {medicaid_info_data}")

            await self.send_status_message("Processing Medicaid info lookup data...")

            # Import here to avoid circular imports
            from fighthealthinsurance.medicaid_api import get_medicaid_info

            # get_medicaid_info does blocking pandas/IO work (no ORM, so
            # plain sync_to_async is right; database_sync_to_async is
            # reserved for ORM-touching callables). Running it inline blocked
            # the event loop -- freezing every OTHER chat on this worker.
            medicaid_info = await sync_to_async(get_medicaid_info)(medicaid_info_data)
            logger.debug(
                f"Got Medicaid info response: {medicaid_info[:200] if medicaid_info else 'None'}..."
            )

            if medicaid_info:
                await self.send_status_message(
                    "Medicaid info lookup completed successfully."
                )

                state = medicaid_info_data.get("state")
                state_name = state or "the state"
                medicaid_info_text = (
                    f"Here's the official Medicaid information for {state_name}:\n\n"
                    f"{medicaid_info}\n\n"
                    f" -- use it to answer the question {current_message_for_llm}"
                    f"\n\n{self._build_eligibility_handoff(state)}"
                )

                # Call LLM with the Medicaid info context
                if self.call_llm_callback and model_backends:
                    # Update history with current exchange
                    if history_for_llm is not None:
                        history_for_llm.append(
                            {"role": "user", "content": current_message_for_llm}
                        )
                        history_for_llm.append(
                            {"role": "agent", "content": response_text}
                        )

                    (
                        additional_response,
                        additional_context,
                    ) = await self.call_llm_callback(
                        model_backends,
                        medicaid_info_text,
                        "",  # Empty previous context summary
                        history_for_llm,
                        depth=depth + 1,
                        is_logged_in=is_logged_in,
                        is_professional=is_professional,
                        fallback_backends=kwargs.get("fallback_backends"),
                        full_history=kwargs.get("full_history"),
                        # Raw user message, so repeat detection/exemption in the
                        # recursive pass keys off what the USER said rather than this
                        # tool payload (which routinely contains words like "repeat").
                        user_message_for_scoring=kwargs.get("user_message_for_scoring"),
                    )

                    logger.debug(
                        f"Medicaid with intro/conclusion: {medicaid_info[:200]}..."
                    )

                    if cleaned_response and additional_response:
                        cleaned_response += additional_response
                    elif additional_response:
                        cleaned_response = additional_response

                    if context and additional_context:
                        context = context + additional_context
                    elif additional_context:
                        context = additional_context

                return cleaned_response, context

            else:
                # get_medicaid_info returns None when it can't tell which
                # state was meant -- ask, rather than guessing or reporting a
                # successful lookup.
                await self.send_status_message(
                    "Need the user's state before looking up Medicaid info."
                )
                # BaseTool.handle propagates this as the assistant's reply,
                # so it has to read as something we'd say to the user, not as
                # an instruction addressed to the model.
                return (
                    "Which state are you in? I need it to look up your "
                    "state's Medicaid contact information.",
                    context,
                )

        except json.JSONDecodeError:
            logger.warning(f"Invalid JSON data in medicaid_info token: {json_data}")
            await self.send_status_message(
                "Error processing Medicaid info data: Invalid JSON format."
            )
            raise

        except Exception as e:
            logger.opt(exception=True).warning(
                f"Error processing Medicaid info data: {e}"
            )
            await self.send_status_message(
                f"Error processing Medicaid info data: {str(e)}"
            )
            raise

    @staticmethod
    def _build_eligibility_handoff(state: Optional[str]) -> str:
        """Guidance that leaves the door open to the eligibility check.

        A general "how does Medicaid work in my state?" question used to end
        with contact details and nothing else, so someone who came to us
        wondering whether they qualify left without ever hearing that we can
        estimate it. The info lookup is the natural jumping-off point, so
        every successful one now hands the model the offer.

        It is an OFFER, not a redirect: the wording comes from
        MEDICAID_ELIGIBILITY_OFFER_RULE, which the system prompt uses too, so
        the two can't drift into telling the model different things. No tool
        call is spelled out here -- the model already has the tool's format,
        and building one by string-concatenating an unescaped state name
        produced invalid JSON whenever the payload had no "state" key.
        """
        # Imported lazily: ml_models is a heavy module and this file
        # otherwise stays free of it (same reason medicaid_api is imported
        # inside execute()).
        from fighthealthinsurance.ml.ml_models import (
            MEDICAID_ELIGIBILITY_OFFER_RULE,
        )

        handoff = "Then, after you have answered their question, " + (
            MEDICAID_ELIGIBILITY_OFFER_RULE
        )
        if state:
            handoff += (
                f" You already know their state is {state}, so send it in the "
                "first call along with anything else they have told you."
            )
        return handoff


class MedicaidEligibilityTool(BaseTool):
    """
    Tool handler for Medicaid eligibility checks.

    When the LLM includes a medicaid_eligibility call in its response, this tool:
    1. Extracts the JSON parameters (income, household size, state, etc.)
    2. Calls the eligibility checker
    3. Returns context for the LLM to incorporate
    """

    pattern = MEDICAID_ELIGIBILITY_REGEX
    detect_flags = re.DOTALL | re.IGNORECASE
    name = "Medicaid Eligibility"

    def __init__(
        self,
        send_status_message: Callable[[str], Awaitable[None]],
        call_llm_callback: Optional[
            Callable[..., Awaitable[Tuple[Optional[str], Optional[str]]]]
        ] = None,
        eligibility_computed: Optional[List[bool]] = None,
    ):
        """
        Initialize the Medicaid eligibility tool.

        Args:
            send_status_message: Async function to send status updates
            call_llm_callback: Callback to call LLM with additional context
            eligibility_computed: Optional one-element list owned by the
                ChatInterface, flipped to True once this tool produces a
                determination. The tool is rebuilt every turn, so the session
                needs somewhere outside it to remember that a verdict is now
                legitimate to state (see score_llm_response's invented-verdict
                penalty).
        """
        super().__init__(send_status_message)
        self.call_llm_callback = call_llm_callback
        self.eligibility_computed = eligibility_computed

    async def execute(
        self,
        match: re.Match[str],
        response_text: str,
        context: str,
        model_backends: Any = None,
        current_message_for_llm: str = "",
        history_for_llm: Optional[List[dict]] = None,
        depth: int = 0,
        is_logged_in: bool = False,
        is_professional: bool = False,
        **kwargs,
    ) -> Tuple[str, str]:
        """
        Execute Medicaid eligibility check and incorporate results.

        Args:
            match: Regex match containing JSON parameters
            response_text: Current LLM response
            context: Current context string
            model_backends: LLM backends for follow-up calls
            current_message_for_llm: The user's current message
            history_for_llm: Chat history (will be modified)
            depth: Current recursion depth
            is_logged_in: Whether user is logged in
            is_professional: Whether user is a professional

        Returns:
            Tuple of (updated_response, updated_context)
        """
        logger.debug(f"Medicaid eligibility tool call detected: {match.group(0)}")

        # Find all matches for cleaning purposes
        all_matches = self.detect_all(response_text)
        cleaned_response = self.clean_all_matches(response_text, all_matches)

        if len(all_matches) > 1:
            logger.warning(
                f"Found {len(all_matches)} Medicaid eligibility tool calls, "
                "processing only the first one"
            )

        # Parse JSON from the provided match (not re-detecting)
        json_data = match.group(1).strip()
        logger.debug(f"Extracted JSON data: {json_data}")

        try:
            loaded = json.loads(json_data)
            logger.debug(f"Parsed JSON data: {loaded}")
        except json.JSONDecodeError as e:
            logger.warning(
                f"Invalid JSON in medicaid_eligibility token: {json_data} - {e}"
            )
            await self.send_status_message(
                "Error processing Medicaid eligibility data: Invalid JSON format."
            )
            return (
                "I couldn't process the eligibility check due to a formatting error. "
                "Please try again with your eligibility question.",
                context,
            )

        if len(cleaned_response) > 1:
            await self.send_status_message(
                f"Looking up medicaid eligibility, please wait. "
                f"Remaining information: {cleaned_response}"
            )

        if not isinstance(loaded, dict):
            error_msg = f"Expected dict, got {type(loaded).__name__} while loading tool call params."
            logger.warning(error_msg)
            await self.send_status_message(f"Error: {error_msg}")
            raise TypeError(error_msg)

        try:
            # Import here to avoid circular imports
            from fighthealthinsurance.medicaid_api import (
                BASE_ELIGIBILITY_YEAR,
                WORK_REQUIREMENT_FIRST_YEAR,
                is_eligible,
                resolve_target_year,
                summarize_eligibility_inputs,
                target_year_requested,
            )

            await self.send_status_message("Processing Medicaid eligibility data")

            # Which year the second verdict covers. Resolved with the same
            # helper the checker uses, so the label we hand the LLM can't
            # claim a different year than the one that was scored.
            target_year = resolve_target_year(loaded.get("target_year"))

            # The checker only projects income limits forward for a year the
            # user actually named; on the default path both verdicts use the
            # published table. Recomputed with the same helpers the checker
            # uses so the caveat we print and the arithmetic it did agree.
            thresholds_estimated = (
                target_year_requested(loaded.get("target_year"))
                and target_year > BASE_ELIGIBILITY_YEAR
            )
            work_req_applies = target_year >= WORK_REQUIREMENT_FIRST_YEAR

            # What we actually understood from the payload. The LLM parses
            # the user's free text and carries the running answers in its
            # own context, so it needs this echo to stay in sync with us --
            # especially for values we normalized and keys we ignored.
            parsed_summary = self._build_parsed_summary(
                summarize_eligibility_inputs(loaded)
            )

            (
                eligible_base,
                eligible_target,
                medicare,
                alternatives,
                missing,
                determination_made,
            ) = is_eligible(**loaded)

            # From here on the checker's own output is in play, so the
            # write-up pass (and the rest of the session) may state a verdict.
            if self.eligibility_computed is not None:
                self.eligibility_computed[0] = True

            info_text = self._build_eligibility_info(
                eligible_base,
                eligible_target,
                medicare,
                alternatives,
                missing,
                determination_made,
                target_year=target_year,
                thresholds_estimated=thresholds_estimated,
            )
            if parsed_summary:
                info_text += "\n\n" + parsed_summary

            # The work-requirement coaching only makes sense for years the
            # overlay actually applies to. Offering it for a base-year check
            # told the user to chase 80 hours a month for a rule that isn't
            # in force in the year they asked about.
            work_req_advice = (
                (
                    f"If the {target_year} work "
                    "requirement is the barrier, suggest ways to reach 80 "
                    "qualifying hours a month (work, school, volunteering, or "
                    "caregiving) and remind them to keep good records -- with "
                    "empathy, since this is stressful and unfair. "
                )
                if work_req_applies
                else ""
            )
            action_text = (
                "\n\nUse this info to either ask the user the next follow-up "
                "questions (no more than two or three per message, in the "
                "order listed, rephrased naturally) or deliver the news of "
                "our determination along with the alternatives. Don't re-ask "
                "anything the user already answered. Never state an "
                "eligibility answer this check did not produce -- the "
                "verdicts above are the only ones you may give. Always make "
                "it very clear that this eligibility check is an "
                "EXPERIMENTAL feature and only an approximation -- it can be "
                "wrong or out of date -- and they must contact the state to "
                "know for sure (you can use the "
                "medicaid_info tool call to get state-specific contact info "
                f"to provide to the user). {work_req_advice}This check "
                f"covered {target_year}; if the user asks about a different "
                'year, call the tool again with "target_year" set to it '
                "(along with everything else you already have). Remember to "
                "use the panda emoji and context."
            )

            await self.send_status_message("Formatting response...")

            # Call LLM with eligibility info
            if self.call_llm_callback and model_backends:
                if history_for_llm is not None:
                    history_for_llm.append(
                        {"role": "user", "content": current_message_for_llm}
                    )
                    history_for_llm.append({"role": "agent", "content": response_text})

                additional_response, additional_context = await self.call_llm_callback(
                    model_backends,
                    info_text + action_text,
                    "Medicaid eligibility investigation",
                    history_for_llm,
                    depth=depth + 1,
                    # This pass is writing up what the checker computed, so a
                    # verdict in its reply is earned, not invented.
                    eligibility_verified=True,
                    is_logged_in=is_logged_in,
                    is_professional=is_professional,
                    fallback_backends=kwargs.get("fallback_backends"),
                    full_history=kwargs.get("full_history"),
                    # Raw user message, so repeat detection/exemption in the
                    # recursive pass keys off what the USER said rather than this
                    # tool payload (which routinely contains words like "repeat").
                    user_message_for_scoring=kwargs.get("user_message_for_scoring"),
                )

                if additional_response and len(additional_response) > 1:
                    response_text = additional_response

                if context:
                    if additional_context:
                        context += additional_context
                elif additional_context:
                    context = additional_context

                return response_text, context

            return cleaned_response, context

        except Exception as e:
            logger.opt(exception=True).debug(
                f"Error parsing params for medicaid eligibility tool: {e}"
            )
            return (
                "Something went wrong trying to figure out eligibility. "
                "Please contact your state for more info.",
                context,
            )

    def _build_parsed_summary(self, summary: dict) -> str:
        """Tell the LLM what we recorded, ignored, and couldn't read.

        Without this the model gets no feedback on its own parsing: an
        unrecognized key (``income`` instead of ``monthly_income``) is
        dropped silently, which is indistinguishable from being accepted, so
        the model believes it answered and the same question comes back.
        """
        parts: List[str] = []

        recorded = summary.get("recorded") or {}
        if recorded:
            lines = "\n".join(f"- {k}: {v}" for k, v in sorted(recorded.items()))
            parts.append(
                "This is what we have recorded so far — keep these in your "
                "context and send them all back on the next call:\n" + lines
            )

        unrecognized = summary.get("unrecognized") or []
        if unrecognized:
            parts.append(
                "We do NOT have a parameter named "
                + ", ".join(sorted(unrecognized))
                + " so those values were ignored. Re-send them under the "
                "documented parameter names if they still apply."
            )

        unreadable = summary.get("unreadable") or []
        if unreadable:
            parts.append(
                "We couldn't read the value given for "
                + ", ".join(sorted(unreadable))
                + ". Send a plain number, a JSON true/false, or a US state "
                'name — or "unknown" if the user can\'t answer.'
            )

        declined = summary.get("declined") or []
        if declined:
            # The declined marker lives only in the payload, so it has to come
            # back on every subsequent call. Omitting it from the re-send list
            # made the decline last exactly one turn: the next call arrived
            # without it, the field read as unanswered again, and the
            # suppressed question came straight back -- the stall this channel
            # exists to prevent.
            names = ", ".join(sorted(declined))
            parts.append(
                f"The user couldn't answer {names} — don't ask about those "
                f'again, and keep sending {names} back as "unknown" on '
                "every following call so they stay marked as declined."
            )

        return "\n\n".join(parts)

    def _build_eligibility_info(
        self,
        eligible_base: bool,
        eligible_target: bool,
        medicare: bool,
        alternatives: List[str],
        missing: List[str],
        determination_made: bool = True,
        target_year: Optional[int] = None,
        thresholds_estimated: bool = False,
    ) -> str:
        """
        Build the eligibility information text passed back to the LLM.

        Args:
            eligible_base: Whether eligible under today's (base-year) rules
            eligible_target: Whether eligible in ``target_year`` (which adds
                the federal work/community-engagement requirement from
                ``WORK_REQUIREMENT_FIRST_YEAR`` on)
            medicare: Whether eligible for Medicare
            alternatives: List of alternative suggestions
            missing: List of missing-information questions still to ask
            determination_made: False when the checker could not score this
                person at all (a US territory, or a required answer they
                declined). A False here must never be rendered as an
                ineligibility verdict -- that is the whole point of the flag.
            target_year: The year the second verdict covers -- the user's own
                year when they asked about one. Must be the year
                ``resolve_target_year`` returned for this payload, or the
                label would name a year we didn't score. Defaults to the
                checker's own default year.
            thresholds_estimated: True only when the checker projected income
                limits forward because the USER asked about a future year. On
                the default path it doesn't, so telling the user the limits
                were estimated would be describing arithmetic we never did.

        Returns:
            Formatted information text
        """
        # Imported lazily like the rest of medicaid_api here: the module
        # pulls in pandas, and the chat tools are imported on every chat.
        from fighthealthinsurance.medicaid_api import (
            BASE_ELIGIBILITY_YEAR,
            DEFAULT_TARGET_YEAR,
            WORK_REQUIREMENT_FIRST_YEAR,
        )

        if target_year is None:
            target_year = DEFAULT_TARGET_YEAR

        # One shared description of the work-requirement rules so the
        # eligible and not-eligible wordings can't drift apart. Only years
        # the requirement can apply to get it.
        work_req_note = ""
        if target_year >= WORK_REQUIREMENT_FIRST_YEAR:
            work_req_note = (
                " (once the federal 80-hours-per-month work/community-engagement "
                "requirement applies to them — states must implement it by "
                "January 1, 2027, a few earlier)"
            )
        base_label = f"current ({BASE_ELIGIBILITY_YEAR})"
        # A user who asked about this year gets one verdict, not the same
        # verdict printed twice under two labels.
        target_is_separate_year = target_year > BASE_ELIGIBILITY_YEAR

        parts: List[str] = [
            "We're helping figure out if someone is likely eligible for "
            "Medicaid using our EXPERIMENTAL eligibility checker. Be very "
            "clear with the user that this is an experimental feature that "
            "can be wrong: it only gives an approximation, and they'll need "
            "to confirm with the state to be sure."
        ]

        if len(missing) > 0:
            question_lines = "\n".join(f"- {q}" for q in missing)
            parts.append(
                "We don't have enough information yet, so we have the "
                "following questions to ask (ask only two or three at a "
                "time, in this order, rephrased naturally):\n"
                f"{question_lines}"
            )
            # Report findings that are already settled. Withholding them
            # until every question is answered meant a firm "yes, you look
            # eligible" sat unsaid behind an unrelated follow-up. Only
            # positives are reported here: a False at this stage means "not
            # established yet", not "no".
            if eligible_base:
                parts.append(
                    "Based on what we have so far they already look eligible "
                    f"for medicaid under the {base_label} rules -- you can "
                    "share that, while noting the questions above are still "
                    "needed to be sure."
                )
            if target_is_separate_year and eligible_target:
                # The target year clears too. Held back with the base-year
                # positive above, this was the good half of the answer for
                # someone who asked "will I still qualify in 2028?" and it
                # sat unsaid behind whatever question was still outstanding.
                parts.append(
                    f"They also already look eligible in {target_year}"
                    f"{work_req_note} -- same caveat, the questions above are "
                    "still needed to be sure."
                )
            if medicare:
                parts.append(
                    "Our data already suggests they may be eligible for medicare."
                )
            if not determination_made and len(alternatives) > 0:
                # Indeterminate with questions still outstanding -- a
                # territory resident whose Medicare answer is still coming.
                # The reason we can't estimate Medicaid is actionable right
                # now, so don't sit on it until the interview finishes.
                alternative_lines = "\n".join(f"- {a}" for a in alternatives)
                parts.append(
                    "Separately, we canNOT produce a Medicaid estimate for "
                    "this person — do not say they may be ineligible. Share "
                    "these next steps now:\n" + alternative_lines
                )
        elif not determination_made:
            # We could not score this person at all (a US territory, or a
            # required answer they declined). Saying "may not be eligible"
            # here would be a confident denial we never actually computed --
            # and it would contradict the explanation in the alternatives.
            parts.append(
                "We could NOT produce a Medicaid estimate for this person — "
                "do not tell them they may be ineligible. Explain that their "
                "situation isn't something our checker can estimate and point "
                "them at the next step below."
            )
            if medicare:
                parts.append(
                    "We were able to check Medicare, and our data suggests "
                    "they may be eligible for it."
                )
            if len(alternatives) > 0:
                alternative_lines = "\n".join(f"- {a}" for a in alternatives)
                parts.append("Next steps to share:\n" + alternative_lines)
        else:
            verdicts = [(base_label, eligible_base, "")]
            if target_is_separate_year:
                verdicts.append((str(target_year), eligible_target, work_req_note))
            for label, is_eligible_for_year, note in verdicts:
                verdict = "could be" if is_eligible_for_year else "may not be"
                parts.append(
                    f"Our data so far suggests they {verdict} eligible for "
                    f"medicaid under the {label} rules{note}."
                )

            if thresholds_estimated:
                # The requested year's income limits are the published
                # base-year guidelines rolled forward by an inflation guess,
                # so a borderline verdict there is softer than it sounds.
                parts.append(
                    f"Note: {target_year} income limits aren't published yet, "
                    f"so we estimated them from the {BASE_ELIGIBILITY_YEAR} "
                    "guidelines. Say so if the answer is close to the line."
                )

            if medicare:
                parts.append("Our data suggests they may be eligible for medicare.")

            # Alternatives are next-steps for a finished determination (they
            # include appeal-after-denial advice), so they'd be confusing
            # mid-interview for someone who currently looks eligible.
            if len(alternatives) > 0:
                alternative_lines = "\n".join(f"- {a}" for a in alternatives)
                parts.append(
                    "Alternative programs and next steps worth mentioning:\n"
                    f"{alternative_lines}"
                )

        return "\n\n".join(parts)
