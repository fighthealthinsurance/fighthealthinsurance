from concurrent.futures import Future
from typing import Any, List, Dict, AsyncIterator
from loguru import logger
import asyncio
import uuid
import datetime

from fighthealthinsurance.ml.ml_models import RemoteModelLike
from fighthealthinsurance.ml.ml_router import ml_router
from fighthealthinsurance.models import PriorAuthRequest, ProposedPriorAuth
from fighthealthinsurance.utils import as_available
from asgiref.sync import sync_to_async, async_to_sync
import random
from fighthealthinsurance.exec import executor
from fighthealthinsurance.prior_auth_utils import PriorAuthTextSubstituter


class PriorAuthGenerator:
    """
    Generator for prior authorization proposals using ML models.
    """

    def __init__(self):
        """Initialize the prior auth generator."""
        pass

    async def generate_prior_auth_proposals(
        self, prior_auth: PriorAuthRequest
    ) -> AsyncIterator[Dict[str, str]]:
        """
        Generate prior auth proposals using ML models and stream results as they're available.

        Args:
            prior_auth: The PriorAuthRequest object with diagnosis, treatment, etc.

        Yields:
            Dictionary with proposed_id and text for each generated proposal
        """
        # Extract relevant information
        diagnosis = prior_auth.diagnosis
        treatment = prior_auth.treatment
        insurance_company = prior_auth.insurance_company
        # Convert the provider info, can result in a query through domain.
        provider_info = await sync_to_async(str)(
            (
                prior_auth.created_for_professional_user
                or prior_auth.creator_professional_user
            )
        )
        patient_health_history = prior_auth.patient_health_history
        answers = prior_auth.answers
        patient_info = {}
        if prior_auth.patient_name and len(prior_auth.patient_name) > 0:
            patient_info["name"] = prior_auth.patient_name
        if prior_auth.patient_dob:
            patient_info["dob"] = str(prior_auth.patient_dob)
        if prior_auth.plan_id:
            patient_info["plan_id"] = prior_auth.plan_id

        proposal_type = "letter"
        if prior_auth.proposal_type and prior_auth.proposal_type != "letter":
            proposal_type = prior_auth.proposal_type
        logger.debug(f"Creating proposal type {proposal_type} from {prior_auth}")

        # Prepare prompt context
        context = {
            "diagnosis": diagnosis,
            "treatment": treatment,
            "insurance_company": insurance_company,
            "patient_health_history": patient_health_history,
            "provider_info": provider_info,
            "qa_pairs": answers,
            "urgent": prior_auth.urgent,
            "patient_info": patient_info,
            "proposal_type": proposal_type,
        }  # type: Dict[str, Any]

        # Get available models
        models = ml_router.get_prior_auth_backends()
        if not models:
            yield {"error": "No language models are currently available."}
            return

        # Maximum number of models to use (2-3 is typically good)
        num_models = max(min(len(models), 3), 2)
        selected_models = models[0:num_models]
        if len(selected_models) < 4:
            selected_models = models + [models[0]]

        # Create tasks for concurrent generation
        futures: List[Future[Dict[str, Any]]] = []
        for i, model in enumerate(selected_models):
            future = executor.submit(
                self._sync_generate_single_proposal, prior_auth, model, context, i
            )
            futures.append(future)

        # Stream results as they become available using as_available
        for result in as_available(futures):
            if result and "proposed_id" in result and "text" in result:
                yield result  # type: ignore
            else:
                logger.error(f"Error generating proposal: {result}")

    def _sync_generate_single_proposal(
        self,
        prior_auth: PriorAuthRequest,
        model: RemoteModelLike,
        context: Dict[str, Any],
        index: int,
    ) -> Dict[str, Any]:
        """
        Synchronous wrapper for generating a single prior authorization proposal.
        """
        return async_to_sync(self._generate_single_proposal)(
            prior_auth, model, context, index
        )

    async def _generate_single_proposal(
        self,
        prior_auth: PriorAuthRequest,
        model: RemoteModelLike,
        context: Dict[str, Any],
        index: int,
    ) -> Dict[str, Any]:
        """
        Generate a single prior authorization proposal using the specified model.

        Args:
            prior_auth: The PriorAuthRequest object
            model: The ML model to use for generation
            context: The context with diagnosis, treatment, etc.
            index: The index/ID of this proposal

        Returns:
            Dictionary with proposed_id and text
        """
        try:
            # Generate the proposal text
            letter = True
            if prior_auth.proposal_type and prior_auth.proposal_type != "letter":
                letter = False
            prompt = self._create_prompt(context, letter=letter)

            proposal_text = None
            try:
                proposal_text = await model.generate_prior_auth_response(prompt)
            except Exception as gen_error:
                logger.opt(exception=True).debug(f"Error generating text: {gen_error}")
                return {
                    "error": f"Failed to generate with model {index+1}: {str(gen_error)}"
                }

            if not proposal_text:
                return {
                    "error": f"Failed to generate proposal text with model {index+1}"
                }

            # Substitute in patient and provider information
            substituted_text = await sync_to_async(
                PriorAuthTextSubstituter.substitute_patient_and_provider_info
            )(prior_auth, proposal_text)

            # Create a unique ID for this proposal
            proposed_id = uuid.uuid4()

            # Create and save the proposal in the database, with sqlite this can result in db locked errors.
            try:
                await self._create_proposal(prior_auth, proposed_id, substituted_text)
            except:
                pass

            # Return the result to be streamed to the client
            return {
                "proposed_id": str(proposed_id),
                "text": substituted_text,
                "model_index": index,
            }

        except Exception as e:
            logger.opt(exception=True).debug(
                f"Error generating proposal with model {index+1}: {e}"
            )
            return {
                "error": f"Error generating proposal with model {index+1}: {str(e)}"
            }

    def _create_prompt(self, context: Dict[str, Any], letter: bool) -> str:
        """
        Create a prompt for the language model based on the context.

        Args:
            context: Dictionary with diagnosis, treatment, etc.
            letter: Boolean indicating whether to generate a letter format

        Returns:
            Formatted prompt string
        """
        # Extract data from context
        diagnosis = context.get("diagnosis", "")
        treatment = context.get("treatment", "")
        insurance_company = context.get("insurance_company", "")
        patient_health_history = context.get("patient_health_history", "")
        qa_pairs = context.get("qa_pairs", [])
        provider_info = context.get("provider_info", "")
        patient_info = context.get("patient_info", "")
        proposal_type = context.get("proposal_type", "letter")

        what_to_gen = (
            "prior authorization request letter"
            if proposal_type == "letter"
            else "medical note to include in a prior authorization request"
        )

        # Build the prompt
        prompt = f"""
        Generate a {what_to_gen} for {treatment} to treat {diagnosis}.
        Insurance Company: {insurance_company}
        """

        # Add Q&A information if available
        if qa_pairs:
            prompt += (
                f"\n\nUse the following information from the patient's answers: "
                f"{qa_pairs}"
            )
        # Add patient history if available
        if patient_health_history:
            prompt += f"\n\nAdditional Patient History:\n{patient_health_history}"

        if provider_info:
            prompt += f"\n\nProvider Information:\n{provider_info}"

        if patient_info:
            prompt += f"\n\nPatient Information:\n{patient_info}"

        prompt += f"\n\n Today's date is {str(datetime.date.today())}.\n\n"

        # Add formatting instructions
        if proposal_type == "letter":
            prompt += """
        Format the prior authorization request as a formal {{what_to_gen}} with:
        1. Date and header
        2. Patient and provider information (use placeholders if unknown)
        3. Clear statement of the requested treatment/procedure
        4. Medical necessity justification
        5. Supporting evidence and clinical rationale
        6. Relevant billing codes if available
        7. Closing with provider details

        Use $placeholders for information that will be filled in later, such as:
        - $patient_name, $patient_dob, $plan_id, $member_id
        - $provider_name, $provider_npi, $provider_type, $provider_credentials
        - $practice_name, $practice_phone, $practice_fax, $practice_address

        But if the information is available, use it directly.

        Make it persuasive, evidence-based, and compliant with insurance requirements.
        """
        else:
            prompt += """Format the medical note as a concise summary written by the provider about the patient."""

        return prompt

    async def _create_proposal(
        self, prior_auth: PriorAuthRequest, proposed_id: uuid.UUID, text: str
    ) -> ProposedPriorAuth:
        """
        Create a new proposal record in the database.

        Args:
            prior_auth: The PriorAuthRequest object
            proposed_id: UUID for the new proposal
            text: Generated text for the proposal

        Returns:
            The created ProposedPriorAuth object
        """
        proposal = await ProposedPriorAuth.objects.acreate(
            proposed_id=proposed_id, prior_auth_request=prior_auth, text=text
        )
        return proposal

    def substitute_values_in_proposal(
        self, prior_auth: PriorAuthRequest, proposal_text: str
    ) -> str:
        """
        Substitute patient and provider values into a prior auth proposal text.
        Can be called on saved proposals to refresh the placeholders.

        Args:
            prior_auth: The PriorAuthRequest object
            proposal_text: The proposal text with placeholders

        Returns:
            The proposal text with patient and provider information substituted
        """
        return PriorAuthTextSubstituter.substitute_patient_and_provider_info(
            prior_auth, proposal_text
        )


# Create a singleton instance for import
prior_auth_generator = PriorAuthGenerator()
