from typing import List, Tuple, Optional
from loguru import logger
import asyncio
from fighthealthinsurance.models import Denial
from fighthealthinsurance.generate_appeal import AppealGenerator


class MLAppealQuestionsHelper:
    @staticmethod
    async def generate_questions_for_denial(
        denial: Denial, speculative: bool
    ) -> List[Tuple[str, str]]:
        """
        Generate appeal questions for a given denial.

        Args:
            denial: The denial object for which to generate questions.

        Returns:
            A list of (question, answer) tuples.
        """
        questions: List[Tuple[str, str]] = []
        # Check if candidate questions exist and the diagnosis/procedure has not changed
        if (
            denial.candidate_procedure == denial.procedure
            and denial.candidate_diagnosis == denial.diagnosis
            and denial.candidate_generated_questions
            and len(denial.candidate_generated_questions) > 0
        ):
            logger.debug(f"Using candidate questions for denial {denial.denial_id}")
            questions = denial.candidate_generated_questions
        # Generate questions using the AppealGenerator
        else:
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
                    timeout=90,
                )

                # Ensure questions are in the form of tuples
                questions = [
                    (q[0], q[1]) if isinstance(q, (list, tuple)) else (str(q), "")
                    for q in raw_questions
                ]
            except asyncio.TimeoutError:
                logger.warning(
                    f"Timeout while generating questions for denial {denial.denial_id}"
                )
            except Exception as e:
                logger.opt(exception=True).warning(
                    f"Failed to generate questions for denial {denial.denial_id}: {e}"
                )
        # Update the denial with the result
        if questions and len(questions) > 0:
            logger.debug(
                f"Generated {len(questions)} questions for denial {denial.denial_id}"
            )
            qs = Denial.objects.filter(denial_id=denial.denial_id)
            if speculative:
                await qs.aupdate(candidate_generated_questions=questions)
            else:
                await qs.aupdate(generated_questions=questions)
            return questions
        else:
            return []
