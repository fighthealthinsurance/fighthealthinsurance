"""Background precompute of bare, internal-model-only candidate appeals.

The instant a denial's text arrives (denial creation), we kick off -- off the
request path, with no deadline -- a first-pass appeal generation from the RAW
denial text alone, using ONLY internal models and NO research/enrichment
context (none of it has been gathered yet at create time; procedure/diagnosis
aren't even extracted). The resulting drafts are stored as ``speculative=True``
ProposedAppeal rows (``context_level="speculative"``) and held in reserve --
excluded from the normal serving/synthesis/attribution queries -- until the
live generation run underdelivers or gathered no extra data, at which point
they're promoted as a fallback (see AppealsBackendHelper.generate_appeals).

We also warm the denial summary (``denial_text_summary``) so the over-long
context path has it ready.

The heavy lifting is synchronous (``AppealGenerator.make_appeals`` is a plain
sync iterator), so this helper is sync and is called either from the
SpeculativeAppealsActor (wrapped in a thread) or, when Ray is unavailable, from
a daemon thread spun up by ``dispatch_speculative_appeals``.
"""

from typing import Any, Optional

from asgiref.sync import async_to_sync
from loguru import logger

from fighthealthinsurance.context_utils import CONTEXT_LEVEL_SPECULATIVE
from fighthealthinsurance.utils import is_real_appeal


class SpeculativeAppealsHelper:
    """Internal-only, no-research-context candidate-appeal precompute."""

    # Cap so a slow/looping backend can't fill the table with speculative rows.
    MAX_SPECULATIVE_APPEALS = 3

    @classmethod
    def generate_for_denial_sync(cls, denial_id: Any) -> int:
        """Generate + persist speculative candidate appeals for ``denial_id``.

        Idempotent: a no-op if speculative rows already exist for the denial.
        Returns the number of speculative appeals persisted (0 on skip/failure).
        Exception-safe: never raises, so a background caller can't crash on it.
        """
        # Lazy imports: this module is imported from the denial-creation path,
        # and common_view_logic (appealGenerator) / the ORM models pull in a
        # large graph -- keep it out of import time and any cycle.
        from fighthealthinsurance.common_view_logic import appealGenerator
        from fighthealthinsurance.generate_appeal import AppealTemplateGenerator
        from fighthealthinsurance.ml.ml_appeal_context_helper import (
            MLAppealContextHelper,
        )
        from fighthealthinsurance.models import Denial, ProposedAppeal

        try:
            denial = (
                Denial.objects.filter(denial_id=denial_id)
                .select_related(
                    "patient_user",
                    "patient_user__user",
                    "domain",
                    "primary_professional",
                    "primary_professional__user",
                )
                .first()
            )
            if denial is None:
                logger.warning(
                    f"speculative appeals: denial {denial_id} not found; skipping"
                )
                return 0
            if not denial.denial_text:
                logger.debug(
                    f"speculative appeals: denial {denial_id} has no text; skipping"
                )
                return 0
            # Idempotency: don't regenerate if we already have speculative rows.
            if ProposedAppeal.objects.filter(
                for_denial=denial, speculative=True
            ).exists():
                logger.debug(
                    f"speculative appeals: denial {denial_id} already has "
                    f"speculative rows; skipping"
                )
                return 0

            # Force internal-only end-to-end: the primary calls are already
            # internal, but make_appeals' backup_calls honor denial.use_external.
            # Override it on the in-memory instance ONLY (never saved) so a
            # denial that opted into external models still gets an internal-only
            # speculative precompute.
            denial.use_external = False

            diagnostics: dict = {}
            # Bare run: empty template generator, and NO research/enrichment
            # context (none has been gathered at create time).
            appeals = appealGenerator.make_appeals(
                denial,
                AppealTemplateGenerator([], [], []),
                medical_reasons=None,
                non_ai_appeals=None,
                pubmed_context=None,
                ml_citations_context=None,
                plan_context=None,
                rag_context=None,
                nice_context=None,
                specialized_templates=None,
                pa_context=None,
                uspstf_context=None,
                clinical_trials_context=None,
                diagnostics_sink=diagnostics,
            )

            saved = 0
            for item in appeals:
                if saved >= cls.MAX_SPECULATIVE_APPEALS:
                    break
                if not is_real_appeal(item.text):
                    continue
                try:
                    ProposedAppeal.objects.create(
                        appeal_text=item.text,
                        for_denial=denial,
                        model_name=item.model_name,
                        synthesized=item.synthesized,
                        # These are the speculative precompute regardless of the
                        # internal tier make_appeals used to produce them.
                        speculative=True,
                        context_level=CONTEXT_LEVEL_SPECULATIVE,
                    )
                    saved += 1
                except Exception as e:
                    logger.opt(exception=True).warning(
                        f"speculative appeals: failed to persist a draft for "
                        f"denial {denial_id}: {e}"
                    )

            # Warm the denial summary (a no-op unless the letter is very long).
            try:
                async_to_sync(MLAppealContextHelper.maybe_summarize_denial_text)(denial)
            except Exception as e:
                logger.opt(exception=True).warning(
                    f"speculative appeals: denial-summary warm failed for "
                    f"denial {denial_id}: {e}"
                )

            logger.info(
                f"speculative appeals: persisted {saved} internal-only draft(s) "
                f"for denial {denial_id} "
                f"(models_tried={diagnostics.get('models_tried')})"
            )
            return saved
        except Exception as e:
            logger.opt(exception=True).error(
                f"speculative appeals: generation failed for denial "
                f"{denial_id}: {e}"
            )
            return 0


def dispatch_speculative_appeals(denial_id: Any) -> None:
    """Fire-and-forget the speculative precompute for a freshly-created denial.

    Primary path (production): a detached Ray actor so the work survives the
    request and worker restarts with no deadline. Fallback (Ray absent, e.g.
    dev/tests): a registered daemon thread so denial creation still isn't
    blocked by a full generation. Never raises.
    """
    try:
        from fighthealthinsurance.speculative_appeals_actor_ref import (
            speculative_appeals_actor_ref,
        )

        actor = speculative_appeals_actor_ref.get
        actor.prefetch_for_denial.remote(denial_id)
        return
    except Exception:
        logger.opt(exception=True).warning(
            "speculative appeals: actor dispatch unavailable; using thread " "fallback"
        )

    try:
        from fighthealthinsurance.utils import run_in_registered_daemon_thread

        run_in_registered_daemon_thread(
            SpeculativeAppealsHelper.generate_for_denial_sync, denial_id
        )
    except Exception:
        logger.opt(exception=True).error(
            f"speculative appeals: thread-fallback dispatch failed for denial "
            f"{denial_id}"
        )
