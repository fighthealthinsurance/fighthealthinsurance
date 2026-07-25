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

We also pre-warm the denial summary into the separate
``candidate_denial_text_summary`` field so the over-long context path has it
ready (the live flow promotes it into ``denial_text_summary`` on first use).

``generate_for_denial`` is async and is the real implementation: the actor
awaits it directly, and every lookup/insert uses the native async ORM. Only the
genuinely blocking part is bridged -- ``AppealGenerator.make_appeals`` runs the
models synchronously AND hands back a lazy iterator whose ``next()`` blocks on
model futures, so the call and the draining go into one worker thread together.
``generate_for_denial_sync`` is a thin shim over it for the daemon-thread
fallback, which is dispatched from the sync denial-creation path.
"""

from typing import Any, List, Optional

from asgiref.sync import async_to_sync
from channels.db import aclose_old_connections, database_sync_to_async
from django.conf import settings
from loguru import logger

from fighthealthinsurance.base_actor_ref import ray_cluster_available
from fighthealthinsurance.context_utils import CONTEXT_LEVEL_SPECULATIVE
from fighthealthinsurance.utils import is_real_appeal


class SpeculativeAppealsHelper:
    """Internal-only, no-research-context candidate-appeal precompute."""

    # Cap so a slow/looping backend can't fill the table with speculative rows.
    MAX_SPECULATIVE_APPEALS = 3

    @classmethod
    def generate_for_denial_sync(cls, denial_id: Any, force: bool = False) -> int:
        """Blocking entry point for the in-process daemon-thread fallback.

        ``generate_for_denial`` is the real implementation; this exists only
        because ``dispatch_speculative_appeals`` is reached from the SYNC denial
        creation path, where there is no running loop to await on.

        Inherits the never-raises contract for everything the async body does,
        but is NOT itself unconditionally safe: ``async_to_sync`` raises
        RuntimeError when the calling thread already has a running event loop.
        Call it only from a genuinely sync context -- today that is the bare
        daemon thread from ``run_in_registered_daemon_thread``, whose own
        ``try/except`` would catch it anyway. From async code, await
        ``generate_for_denial`` instead.
        """
        return async_to_sync(cls.generate_for_denial)(denial_id, force=force)

    @classmethod
    async def generate_for_denial(cls, denial_id: Any, force: bool = False) -> int:
        """Generate + persist speculative candidate appeals for ``denial_id``.

        A no-op if the denial ALREADY HAS ANY proposed appeal -- reserve or
        live. This is both the idempotency guard (don't build a second reserve)
        and the backlog shed: the actor bounds itself to MAX_CONCURRENCY
        in-flight generations, so under a burst this check runs when the task is
        finally dequeued, by which point the live flow may already have produced
        appeals -- and a reserve that arrives after them is work nobody will
        ever serve. Dropping those keeps the queue moving toward the denials
        speculation can still help.

        Returns the number of speculative appeals persisted (0 on skip/failure).
        Exception-safe: never raises, so a background caller can't crash on it.

        ``force=True`` skips that guard. Required by the denial-text-replacement
        path: after a replacement the denial still carries drafts about the OLD
        letter (invalidation deletes only the held-back reserve, keeping served
        and ordinary rows), so the guard would match and a replaced letter could
        never get a reserve of its own.
        """
        try:
            # Lazy imports: this module is imported from the denial-creation
            # path, and common_view_logic (appealGenerator) / the ORM models pull
            # in a large graph -- keep it out of import time and any cycle.
            # INSIDE the try so the "never raises" contract above actually holds:
            # an ImportError/ImproperlyConfigured from that graph would otherwise
            # escape into whichever thread or actor dispatched this.
            from fighthealthinsurance.common_view_logic import appealGenerator
            from fighthealthinsurance.generate_appeal import (
                AppealTemplateGenerator,
                GeneratedAppeal,
            )
            from fighthealthinsurance.ml.ml_appeal_context_helper import (
                MLAppealContextHelper,
            )
            from fighthealthinsurance.models import Denial, ProposedAppeal

            # Sweep connections killed while this actor sat idle, BEFORE any ORM
            # call. The actor is detached and long-lived with no request cycle,
            # so nothing else ever calls close_old_connections for it -- and the
            # native async ORM below runs thread_sensitive, i.e. on asgiref's
            # process-wide executor thread, which the _generate_drafts bridge
            # cannot clean because django.db.connections is thread-local.
            # Without this, the first connection stays cached with
            # health_check_done=True forever: once the server's
            # idle_session_timeout drops the session, CONN_HEALTH_CHECKS never
            # re-checks it, and every later precompute in this actor fails with
            # OperationalError, swallowed and logged as a 0-appeal reserve until
            # the process restarts. Same hazard, same fix, as the long-lived
            # WebSocket consumers (see websockets._aclose_if_socket_was_idle).
            await aclose_old_connections()

            denial = await (
                Denial.objects.filter(denial_id=denial_id)
                .select_related(
                    "patient_user",
                    "patient_user__user",
                    "domain",
                    "primary_professional",
                    "primary_professional__user",
                )
                .afirst()
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
            # ANY existing proposed appeal means this run has nothing to add.
            #
            # Deliberately broader than "do we already have a reserve": it also
            # matches LIVE drafts, which is what makes a backlog self-shedding.
            # Evaluated here, when the actor dequeues the task rather than when
            # it was submitted, so a precompute that waited behind the actor's
            # concurrency limit sees the world as it is now -- and if the user's
            # real generation already delivered, a reserve built afterwards is
            # work nobody will ever serve (the end-of-flow reconciliation only
            # reaches for it while under ENOUGH_APPEALS). Skipping frees the slot
            # for a denial speculation can still get ahead of.
            #
            # Subsumes the old speculative-only idempotency check: reserve rows
            # (held-back or promoted) are ProposedAppeals too, so a second
            # dispatch still can't duplicate a reserve.
            #
            # force=True bypasses it for a replaced denial letter -- see the
            # docstring: those old drafts are about a letter that no longer
            # exists, so they must not veto a reserve for the new one.
            if (
                not force
                and await ProposedAppeal.objects.filter(for_denial=denial).aexists()
            ):
                logger.debug(
                    f"speculative appeals: denial {denial_id} already has "
                    f"proposed appeal(s); skipping (speculation would land too "
                    f"late to be served)"
                )
                return 0

            # The letter this run is about. Generation takes tens of seconds to
            # minutes, and the user can replace the text meanwhile (correcting a
            # bad OCR pass is a normal flow) -- so this is re-checked against the
            # DB before anything is persisted. Without that, a run started on the
            # old letter finishes AFTER the replacement's invalidation swept the
            # reserve, inserts rows arguing about a denial that no longer exists,
            # and the reconciliation's oldest-first promotion actively PREFERS
            # them -- handing the user an appeal letter about the wrong denial.
            generated_from_text = denial.denial_text

            # Force internal-only end-to-end: the primary calls are already
            # internal, but make_appeals' backup_calls honor denial.use_external.
            # Override it on the in-memory instance ONLY (never saved) so a
            # denial that opted into external models still gets an internal-only
            # speculative precompute.
            denial.use_external = False

            # Pre-warm the denial summary FIRST, into the SEPARATE candidate
            # field (kept out of denial_text_summary so it never clobbers a
            # summary the live flow may compute for itself; the live flow
            # promotes the candidate on first use). Returns the summary for an
            # oversized denial, else None. We feed it to make_appeals below as
            # denial_text_override so that a very long denial -- whose full text
            # alone overflows the model window, and which the shed ladder never
            # truncates -- still produces real speculative appeals instead of an
            # empty reserve. That long-denial case is exactly where this safety
            # net matters most (it's the most likely to fail the live run too).
            denial_text_override: Optional[str] = None
            try:
                denial_text_override = (
                    await MLAppealContextHelper.prewarm_candidate_denial_text_summary(
                        denial
                    )
                )
            except Exception as e:
                logger.opt(exception=True).warning(
                    f"speculative appeals: denial-summary warm failed for "
                    f"denial {denial_id}: {e}"
                )

            diagnostics: dict = {}

            def _generate_drafts() -> List[GeneratedAppeal]:
                """Blocking: run the models, then pull real drafts off the wire.

                Both halves have to be off the event loop. make_appeals runs the
                model calls synchronously, and what it hands back is a LAZY
                iterator (as_available_nested over concurrent futures) whose
                next() blocks on the next model to finish -- so draining it here
                rather than in the async caller is what actually keeps the loop
                free. Doing it here also preserves the early break: we stop
                pulling the moment we have enough, instead of waiting out every
                remaining model future for drafts we would discard.

                Bare run: empty template generator, and NO research/enrichment
                context (none has been gathered at create time). make_appeals
                does still auto-build a DB-only payer_policy_context, which
                contacts no external model, so the internal-only guarantee holds.
                """
                drafts: List[GeneratedAppeal] = []
                for item in appealGenerator.make_appeals(
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
                    denial_text_override=denial_text_override,
                    diagnostics_sink=diagnostics,
                ):
                    if not is_real_appeal(item.text):
                        continue
                    drafts.append(item)
                    if len(drafts) >= cls.MAX_SPECULATIVE_APPEALS:
                        break
                return drafts

            # thread_sensitive=False: a burst of denial creations dispatches
            # overlapping precomputes, and the default (True) funnels every one
            # of them onto a single shared executor thread. Since one generation
            # can hold that thread for minutes, later precomputes would not be
            # ready by the time their live flows need them -- defeating the point
            # of precomputing. A pool thread per call restores the concurrency;
            # DatabaseSyncToAsync still wraps each call in close_old_connections()
            # either way, so per-call connection isolation is unchanged.
            drafts = await database_sync_to_async(
                _generate_drafts, thread_sensitive=False
            )()

            # Generation is done; make sure it is still about the CURRENT letter
            # before persisting anything (see generated_from_text above). Read
            # straight from the DB -- `denial` is a snapshot from before the
            # generation and cannot see a replacement.
            current_text = await (
                Denial.objects.filter(denial_id=denial_id)
                .values_list("denial_text", flat=True)
                .afirst()
            )
            if current_text != generated_from_text:
                logger.info(
                    f"speculative appeals: denial {denial_id} text was replaced "
                    f"while precomputing; discarding this reserve so a stale "
                    f"letter can't be served (a fresh precompute is dispatched "
                    f"by the text-change path)"
                )
                return 0

            saved = 0
            created_pks: List[int] = []
            # Already filtered to real appeals and capped in _generate_drafts.
            for item in drafts:
                try:
                    row = await ProposedAppeal.objects.acreate(
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
                    created_pks.append(row.pk)
                except Exception as e:
                    logger.opt(exception=True).warning(
                        f"speculative appeals: failed to persist a draft for "
                        f"denial {denial_id}: {e}"
                    )

            # Re-check AFTER writing, which is what actually closes the race.
            # The pre-write check above leaves a window: a replacement landing
            # between it and these inserts runs its invalidation sweep while we
            # have no rows to sweep, and our stale drafts then appear behind it
            # -- permanently, since nothing sweeps again. Checking once more here
            # closes it completely, because the only replacement this can now
            # miss is one that lands after this read, and that one's own
            # invalidation WILL find our speculative=True rows and delete them.
            if saved:
                current_text = await (
                    Denial.objects.filter(denial_id=denial_id)
                    .values_list("denial_text", flat=True)
                    .afirst()
                )
                if current_text != generated_from_text:
                    deleted, _ = await ProposedAppeal.objects.filter(
                        pk__in=created_pks
                    ).adelete()
                    logger.info(
                        f"speculative appeals: denial {denial_id} text was "
                        f"replaced while precomputing; removed {deleted} reserve "
                        f"row(s) written about the old letter"
                    )
                    return 0

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
        finally:
            # Don't leave a connection checked out on the shared executor thread
            # for however long it is until the next denial arrives. Mirrors what
            # run_in_registered_daemon_thread does in its own finally for the
            # in-process fallback, so both dispatch paths behave the same.
            try:
                await aclose_old_connections()
            except Exception:
                logger.opt(exception=True).debug(
                    f"speculative appeals: connection sweep failed after denial "
                    f"{denial_id}"
                )


def dispatch_speculative_appeals(denial_id: Any, force: bool = False) -> None:
    """Fire-and-forget the speculative precompute for a freshly-created denial.

    Primary path (production): a detached Ray actor so the work survives the
    request and worker restarts with no deadline. Fallback (no Ray cluster to
    attach to, e.g. dev): a registered daemon thread so denial creation still
    isn't blocked by a full generation. Never raises.

    Disabled entirely by ``settings.SPECULATIVE_APPEALS_PRECOMPUTE=False`` (the
    Test* configs), where a background generation's DB writes would race
    TestCase transaction teardown -- same reasoning as the site-banner refresh
    thread.

    ``force`` is passed through to the helper's idempotency check; the
    denial-text-replacement path needs it (see generate_for_denial).
    """
    if not getattr(settings, "SPECULATIVE_APPEALS_PRECOMPUTE", True):
        logger.debug(
            f"speculative appeals: precompute disabled by settings; "
            f"skipping denial {denial_id}"
        )
        return

    if ray_cluster_available():
        try:
            from fighthealthinsurance.speculative_appeals_actor_ref import (
                speculative_appeals_actor_ref,
            )

            actor = speculative_appeals_actor_ref.get
            actor.prefetch_for_denial.remote(denial_id, force=force)
            return
        except Exception:
            logger.opt(exception=True).warning(
                "speculative appeals: actor dispatch unavailable; using thread "
                "fallback"
            )

    try:
        from fighthealthinsurance.utils import run_in_registered_daemon_thread

        run_in_registered_daemon_thread(
            SpeculativeAppealsHelper.generate_for_denial_sync, denial_id, force=force
        )
    except Exception:
        logger.opt(exception=True).error(
            f"speculative appeals: thread-fallback dispatch failed for denial "
            f"{denial_id}"
        )
