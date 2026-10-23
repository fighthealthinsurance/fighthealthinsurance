"""Temporal activities for ``SharedPlanDocumentsWorkflow``.

Asyncio activities, like the appeal journey's, on the plan documents queue.
The work is in ``plan_documents_processing``; these wrappers add the
Temporal conventions:

* Ids in, ids and statuses out. The text read from a file is written to the
  row inside the activity and never returned, so no text, file name, email,
  insurer or employer reaches workflow history.
* A row that is gone (remove-my-data) is reported as "gone" and nothing is
  written.
* Errors are reported as the exception's class name and the row's id only,
  in history and in the logs, since a message could quote a file. Schema
  and bad-input errors are not retried.
"""

import uuid

from loguru import logger
from temporalio import activity
from temporalio.exceptions import ApplicationError

from fighthealthinsurance import plan_documents_processing as processing
from fighthealthinsurance.activities.appeal_journey import (
    _NON_RETRYABLE_ERRORS,
    _aclose_old_connections,
)


def _checked_uuid(value: str, what: str) -> str:
    """``value`` as a canonical uuid string; a bad one is never retried."""
    try:
        return str(uuid.UUID(str(value)))
    except ValueError:
        raise ApplicationError(f"{what} is not a uuid", non_retryable=True) from None


def _failure(e: Exception, what: str, retryable: bool = True) -> ApplicationError:
    name = type(e).__name__
    logger.warning(f"Shared plan documents: {name} for {what}")
    return ApplicationError(f"{name} for {what}", non_retryable=not retryable)


@activity.defn
async def list_shared_plan_documents(share_batch: str) -> list[str]:
    """The ids of the batch's rows still waiting to be read."""
    batch = _checked_uuid(share_batch, "share batch")
    await _aclose_old_connections()
    try:
        return await processing.abatch_document_ids(batch)
    except _NON_RETRYABLE_ERRORS as e:
        raise _failure(e, f"share batch {batch}", retryable=False) from None
    except Exception as e:
        raise _failure(e, f"share batch {batch}") from None


@activity.defn
async def process_shared_plan_document(document_id: str) -> str:
    """Read, clean and store one row's text; returns its status or "gone"."""
    doc_id = _checked_uuid(document_id, "document id")
    await _aclose_old_connections()
    try:
        return await processing.aprocess_document(doc_id)
    except _NON_RETRYABLE_ERRORS as e:
        raise _failure(e, f"shared plan document {doc_id}", retryable=False) from None
    except Exception as e:
        raise _failure(e, f"shared plan document {doc_id}") from None


@activity.defn
async def mark_shared_plan_document_unreadable(document_id: str) -> str:
    """For a row whose reading failed every attempt: unreadable, no text."""
    doc_id = _checked_uuid(document_id, "document id")
    await _aclose_old_connections()
    try:
        return await processing.amark_unreadable(doc_id)
    except _NON_RETRYABLE_ERRORS as e:
        raise _failure(e, f"shared plan document {doc_id}", retryable=False) from None
    except Exception as e:
        raise _failure(e, f"shared plan document {doc_id}") from None
