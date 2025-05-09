from typing import Dict, Any, Optional
from loguru import logger
from fighthealthinsurance.models import PriorAuthRequest, ProposedPriorAuth, UserDomain
import re

from datetime import date


class PriorAuthTextSubstituter:
    """
    Utility class for substituting professional and patient information into prior authorization proposals.

    This class provides functionality to replace placeholders in ML-generated prior authorization
    text with actual patient and provider information from the database, using the string.Template
    mechanism for safe substitutions.

    Example placeholders that can be used in templates:
    - $patient_name, $patient_dob - Patient information
    - $provider_name, $provider_npi - Provider information
    - $practice_name, $practice_address - Practice/domain information
    - $today - Current date
    """

    @staticmethod
    def substitute_patient_and_provider_info(
        prior_auth: PriorAuthRequest, proposal_text: str
    ) -> str:
        """
        Replace placeholders in the proposal text with actual patient and provider information.

        Args:
            prior_auth: The PriorAuthRequest object containing patient and provider info
            proposal_text: The original proposal text with placeholders

        Returns:
            The proposal text with patient and provider information substituted
        """
        # This function can be called with an empty string
        if not proposal_text or len(proposal_text) < 5:
            return proposal_text

        # Build context dictionary with all available information
        try:
            context = PriorAuthTextSubstituter._build_context_dict(prior_auth)
        except Exception as e:
            logger.error(f"Error building context dictionary: {e}")
            # Return the original if there was an error
            return proposal_text

        # Use string.Template to substitute values
        try:
            for k, v in context.items():
                if v is None:
                    continue
                pattern = rf"(?i)(\${k}|\[{k}\])"
                # See https://docs.python.org/3/library/re.html#re.escape
                escaped_value = str(v).replace("\\", r"\\")
                proposal_text = re.sub(pattern, escaped_value, proposal_text)
            return proposal_text
        except Exception as e:
            logger.error(f"Error substituting values in prior auth text: {e}")
            # Return the original if there was an error
            return proposal_text

    @staticmethod
    def _build_context_dict(prior_auth: PriorAuthRequest) -> Dict[str, str]:
        """
        Build a dictionary of context values from the prior auth request.

        Args:
            prior_auth: The PriorAuthRequest object

        Returns:
            A dictionary with context values for substitution
        """
        context: Dict[str, str] = {}

        try:
            # Patient information
            patient_name = prior_auth.patient_name or "[PATIENT NAME]"
            context["patient_name"] = patient_name
            context["patient name"] = patient_name

            plan_id = prior_auth.plan_id or "[PLAN ID]"
            context["plan_id"] = plan_id
            context["plan id"] = plan_id

            member_id = prior_auth.member_id or "[MEMBER ID]"
            context["member_id"] = member_id
            context["member id"] = member_id
            context["enter member id"] = member_id

            patient_dob = "[DATE OF BIRTH]"
            if prior_auth.patient_dob:
                patient_dob = str(prior_auth.patient_dob)
            context["patient_dob"] = patient_dob
            context["Patient DOB"] = patient_dob
            context["Enter DOB"] = patient_dob

            # Medical information
            diagnosis = prior_auth.diagnosis or "[DIAGNOSIS]"
            context["diagnosis"] = diagnosis
            treatment = prior_auth.treatment or "[TREATMENT]"
            context["treatment"] = treatment
            insurance_company = prior_auth.insurance_company or "[INSURANCE COMPANY]"
            context["insurance_company"] = insurance_company
            context["insurance company"] = insurance_company
            context["health plan name"] = insurance_company
            context["Esteemed Members of the Health Plan Review Board"] = (
                insurance_company
            )

            # Insurance information
            insurance_information = f"Plan ID: {plan_id}, Member ID: {member_id}, Insurance Company: {insurance_company}"
            context["insurance information"] = insurance_information
            context["insurance_information"] = insurance_information

            # Add urgent flag if applicable
            context["urgent"] = "URGENT" if prior_auth.urgent else ""

            # Professional information
            professional = (
                prior_auth.created_for_professional_user
                or prior_auth.creator_professional_user
            )
            if professional:
                context["provider_name"] = professional.get_display_name()
                context["provider name"] = professional.get_display_name()
                context["your name"] = professional.get_display_name()
                context["your_name"] = professional.get_display_name()
                context["provider_npi"] = professional.npi_number or "[NPI NUMBER]"
                context["provider_type"] = (
                    professional.provider_type or "[PROVIDER TYPE]"
                )
                credentials = professional.credentials or "[CREDENTIALS]"
                context["CREDENTIALS"] = credentials
                context["provider_credentials"] = credentials
                context["Your Title/Credentials"] = credentials
                context["Your Credentials"] = credentials

                # Get professional contact information
                fax = professional.get_fax_number() or "[PROVIDER FAX]"
                context["provider_fax"] = fax
                context["provider fax"] = fax
                context["Your Contact Information"] = fax
            else:
                # Default placeholders if no professional is associated
                context["provider_name"] = "[PROVIDER NAME]"
                context["provider_npi"] = "[NPI NUMBER]"
                context["provider_type"] = "[PROVIDER TYPE]"
                context["provider_credentials"] = "[CREDENTIALS]"
                context["provider_fax"] = "[PROVIDER FAX]"

            # Domain/Practice information
            if prior_auth.domain:
                domain: UserDomain = prior_auth.domain
                context["practice_name"] = (
                    domain.business_name or domain.display_name or "[PRACTICE NAME]"
                )
                context["practice_phone"] = (
                    domain.visible_phone_number or "[PRACTICE PHONE]"
                )

                if domain.office_fax:
                    context["practice_fax"] = domain.office_fax
                else:
                    context["practice_fax"] = context.get(
                        "provider_fax", "[PRACTICE FAX]"
                    )

                # Address information
                address_parts = []
                if domain.address1:
                    address_parts.append(domain.address1)
                if domain.address2:
                    address_parts.append(domain.address2)

                city_state_zip = ""
                if domain.city:
                    city_state_zip += domain.city
                if domain.state:
                    city_state_zip += f", {domain.state}"
                if domain.zipcode:
                    city_state_zip += f" {domain.zipcode}"

                if city_state_zip:
                    address_parts.append(city_state_zip)

                if address_parts:
                    context["practice_address"] = ", ".join(address_parts)
                else:
                    context["practice_address"] = "[PRACTICE ADDRESS]"
            else:
                context["practice_name"] = "[PRACTICE NAME]"
                context["practice_phone"] = "[PRACTICE PHONE]"
                context["practice_fax"] = context.get("provider_fax", "[PRACTICE FAX]")
                context["practice_address"] = "[PRACTICE ADDRESS]"

            # Add the date
            context["today"] = date.today().strftime("%B %d, %Y")

        except Exception as e:
            # Log the error but return at least a basic context to avoid cascading failures
            logger.error(
                f"Error building context dictionary for prior auth substitution: {e}"
            )
            if not context:
                # Ensure we have at least the basic medical information
                context = {
                    "diagnosis": getattr(prior_auth, "diagnosis", "[DIAGNOSIS]"),
                    "treatment": getattr(prior_auth, "treatment", "[TREATMENT]"),
                    "insurance_company": getattr(
                        prior_auth, "insurance_company", "[INSURANCE COMPANY]"
                    ),
                    "today": date.today().strftime("%B %d, %Y"),
                }

        return context
