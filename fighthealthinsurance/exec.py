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
bridge_executor = ThreadPoolExecutor(
    max_workers=_pool_size("FHI_BRIDGE_EXECUTOR_WORKERS", 32),
    thread_name_prefix="fhi-bridge",
)
