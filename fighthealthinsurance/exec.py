import os
from concurrent.futures import ThreadPoolExecutor


def _pool_size(env_name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(env_name, default)))
    except ValueError:
        return default


# Interactive work: appeal generation fanout for a user who is actively
# waiting on a stream. Sized so one slow generation (which can hold a thread
# for minutes of serial model calls) can't starve the others -- the old
# shared 10-thread pool deadlocked interactive generations behind background
# precompute + abandoned work.
#
# NOTE: DB connection-pool sizing in settings.py assumes these caps; update
# both together.
executor = ThreadPoolExecutor(
    max_workers=_pool_size("FHI_INTERACTIVE_EXECUTOR_WORKERS", 24),
    thread_name_prefix="fhi-interactive",
)

# Background/speculative work (make_appeals run_kind="speculative" and other
# precompute): deliberately smaller and fully isolated from the interactive
# pool so precompute can never queue ahead of a waiting user.
background_executor = ThreadPoolExecutor(
    max_workers=_pool_size("FHI_BACKGROUND_EXECUTOR_WORKERS", 8),
    thread_name_prefix="fhi-background",
)

pubmed_executor = ThreadPoolExecutor(
    max_workers=_pool_size("FHI_PUBMED_EXECUTOR_WORKERS", 4),
    thread_name_prefix="fhi-pubmed",
)

# Async→sync bridge hops: thread_sensitive=False database_sync_to_async call
# sites and SyncIteratorToAsync's per-item next() hops. Without an explicit
# executor these land on the event loop's DEFAULT executor (min(32, cpus+4)
# threads -- single digits on a small pod), shared with everything else the
# loop offloads; a handful of long-blocking generation drains there starves
# every other bridge hop in the process. Isolated and sized for many
# concurrent short hops plus a few long drains.
#
# DEADLOCK INVARIANT: work submitted to this pool may BLOCK ON model futures
# (make_appeals peeks, iterator drains), so nothing a model call transitively
# depends on may run here -- if it did, saturating this pool with waiting
# generations would starve the very work those generations are waiting for.
# That's why the result-cleaner hop below has its own pool.
bridge_executor = ThreadPoolExecutor(
    max_workers=_pool_size("FHI_BRIDGE_EXECUTOR_WORKERS", 32),
    thread_name_prefix="fhi-bridge",
)

# Result-cleaner hops (_checked_infer's tla_fixer/url_fixer/note_remover
# chain, which network-validates URLs): every MODEL CALL depends on one of
# these to complete, and callers waiting on model calls occupy
# bridge_executor -- putting these there would let saturation deadlock the
# whole generation pipeline until its deadline (see invariant above). Small
# and dedicated; tasks here depend on nothing but the network.
cleaner_executor = ThreadPoolExecutor(
    max_workers=_pool_size("FHI_CLEANER_EXECUTOR_WORKERS", 8),
    thread_name_prefix="fhi-cleaner",
)

# Chat-driven appeal-letter drains (chat/appeal_letter_generator): each one
# can hold a thread for up to its generation deadline (~75s), and a
# degraded-model period -- exactly when the letter fallback fires -- can
# start many at once. A dedicated pool caps that concurrency so the drains
# can't crowd the shared bridge_executor's short hops (websocket consumers'
# DB bridges included), and doubles as backpressure on the ML backends.
# Sized for burst headroom rather than the minimum, because the cap is also
# a QUEUE: the callers' deadlines are absolute and include time spent
# waiting here (see generate_letter_for_denial), so a queued drain that is
# already past its deadline exits at once instead of holding a thread --
# but a too-small pool would still make live requests wait behind
# abandoned ones. Same deadlock posture as bridge_executor: these tasks
# block ON model futures, and nothing a model call depends on runs here.
letter_executor = ThreadPoolExecutor(
    max_workers=_pool_size("FHI_LETTER_EXECUTOR_WORKERS", 8),
    thread_name_prefix="fhi-letter",
)
