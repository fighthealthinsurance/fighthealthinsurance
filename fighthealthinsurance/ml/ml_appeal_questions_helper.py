from typing import List, Tuple, Optional
from loguru import logger
import asyncio
from fighthealthinsurance.models import Denial
from fighthealthinsurance.generate_appeal import AppealGenerator


class MLAppealQuestionsHelper:
    @staticmethod
    async def generate_questions_for_denial(denial: Denial) -> List[Tuple[str, str]]:
        """
        Generate appeal questions for a given denial.

        Args:
            denial: The denial object for which to generate questions.

        Returns:
            A list of (question, answer) tuples.
        """
        # Check if candidate questions exist and the diagnosis/procedure has not changed
        if (
            denial.candidate_procedure == denial.procedure
            and denial.candidate_diagnosis == denial.diagnosis
            and denial.candidate_generated_questions
        ):
            logger.debug(f"Using candidate questions for denial {denial.denial_id}")
            stored_questions: List[Tuple[str, str]] = denial.candidate_generated_questions
            return stored_questions

        # Generate questions using the AppealGenerator
        try:
            appeal_generator = AppealGenerator()
            raw_questions = await asyncio.wait_for(
                appeal_generator.get_appeal_questions(
                    denial_text=denial.denial_text,
                    procedure=denial.procedure,
                    diagnosis=denial.diagnosis,
                    patient_context=denial.health_history,
                    plan_context=denial.plan_context,
                ),
                timeout=45,
            )

            # Ensure questions are in the form of tuples
            questions: List[Tuple[str, str]] = [
                (q[0], q[1]) if isinstance(q, (list, tuple)) else (str(q), "")
                for q in raw_questions
            ]

            # Save the generated questions as candidates
            await Denial.objects.filter(denial_id=denial.denial_id).aupdate(
                candidate_generated_questions=questions
            )

            return questions
        except asyncio.TimeoutError:
            logger.warning(
                f"Timeout while generating questions for denial {denial.denial_id}"
            )
            return []
        except Exception as e:
            logger.opt(exception=True).warning(
                f"Failed to generate questions for denial {denial.denial_id}: {e}"
            )
            return []