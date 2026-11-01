import asyncio
import json
import math
import re
from typing import Any, Awaitable, Optional, Sequence

from django.conf import settings
from loguru import logger

from fighthealthinsurance.context_utils import truncate_at_boundary
from fighthealthinsurance.ml import chat_gate, letter_quality
from fighthealthinsurance.ml.ml_router import ml_router
from fighthealthinsurance.models import ChooserCandidate, ChooserTask

LOCAL_RUBRIC = "chooser-rubric-1"
_DRAFT_CHAR_CAP = 6000
_LOCAL_TIMEOUT_SECONDS = 60


def _model_name(model: Any) -> str:
    return str(getattr(model, "name", None) or model)


def _pick(
    candidates: Sequence[ChooserCandidate],
    scores: Optional[Sequence[float]],
    scorer: str,
) -> Optional[dict[str, Any]]:
    if not scores or len(scores) != len(candidates) or not scorer:
        return None
    if any(not math.isfinite(score) for score in scores):
        return None
    best = max(range(len(scores)), key=lambda index: (scores[index], -index))
    candidate = candidates[best]
    return {
        "candidate_id": candidate.id,
        "candidate_index": candidate.candidate_index,
        "model_name": candidate.model_name,
        "score": round(float(scores[best]), 4),
        "scorer": scorer,
    }


def _appeal_context(task: ChooserTask) -> str:
    return json.dumps(task.context_json or {}, ensure_ascii=False)


def _chat_message(task: ChooserTask) -> str:
    context = task.context_json or {}
    history = [
        f"{turn.get('role', 'user')}: {turn.get('content', '')}"
        for turn in context.get("history") or []
        if isinstance(turn, dict)
    ]
    history.append(f"user: {context.get('prompt', '')}")
    return "\n".join(history)


async def _jev_appeal_scores(
    task: ChooserTask, candidates: Sequence[ChooserCandidate]
) -> Optional[dict[str, Any]]:
    context = _appeal_context(task)
    scored = await asyncio.gather(
        *(
            letter_quality.score_letter(context, candidate.content)
            for candidate in candidates
        )
    )
    if any(score is None for score in scored):
        return None
    qualities = [score.quality for score in scored if score is not None]
    scorer = next(score.scorer for score in scored if score is not None)
    return _pick(candidates, qualities, scorer)


def _balanced_reply_batches(replies: Sequence[str]) -> list[Sequence[str]]:
    count = len(replies)
    batch_count = math.ceil(count / chat_gate.MAX_RANKED)
    batch_size = math.ceil(count / batch_count)
    return [
        replies[start : start + batch_size] for start in range(0, count, batch_size)
    ]


async def _jev_chat_scores(
    task: ChooserTask, candidates: Sequence[ChooserCandidate]
) -> Optional[dict[str, Any]]:
    if not chat_gate.enabled():
        return None
    message = _chat_message(task)
    scorer = ""
    qualities: list[float] = []
    for batch in _balanced_reply_batches([c.content for c in candidates]):
        result = await chat_gate.rank_replies(
            message, batch, timeout=chat_gate.rank_timeout_seconds()
        )
        if result.outcome != chat_gate.RANK_PICKED or len(result.scores) != len(batch):
            return None
        if scorer and result.scorer != scorer:
            return None
        scorer = result.scorer
        qualities.extend(chat_gate.quality(score) for score in result.scores)
    return _pick(candidates, qualities, scorer)


async def _jev_scores(
    task: ChooserTask, candidates: Sequence[ChooserCandidate]
) -> Optional[dict[str, Any]]:
    if task.task_type == "appeal":
        return await _jev_appeal_scores(task, candidates)
    return await _jev_chat_scores(task, candidates)


def _local_scores(raw: Optional[str], expected: int) -> list[float]:
    match = re.search(r"\{.*\}", raw or "", re.DOTALL)
    if not match:
        raise ValueError("local scorer returned no JSON object")
    scores = json.loads(match.group(0)).get("scores")
    if not isinstance(scores, list) or len(scores) != expected:
        raise ValueError("local scorer returned the wrong number of scores")
    out = [float(score) for score in scores]
    if any(not math.isfinite(score) or not 0.0 <= score <= 1.0 for score in out):
        raise ValueError("local scorer returned an out-of-range score")
    return out


def _local_prompt(task: ChooserTask, candidates: Sequence[ChooserCandidate]) -> str:
    parts = [
        "Evaluate these synthetic responses independently on a 0 to 1 scale. "
        "Judge truthfulness and grounding first, then responsiveness, usefulness, "
        "and clarity. Do not reward length, confidence, or candidate order. "
        'Return only JSON in this exact form: {"scores": [0.0, 0.5, 1.0]} '
        f"with exactly {len(candidates)} scores in candidate order.\n\n",
        f"THE CASE:\n{json.dumps(task.context_json or {}, ensure_ascii=False)}\n",
    ]
    for index, candidate in enumerate(candidates, start=1):
        content = truncate_at_boundary(candidate.content, _DRAFT_CHAR_CAP)
        parts.append(f"\nCANDIDATE {index}:\n{content}\n")
    return "".join(parts)


async def _local_scores_for_task(
    task: ChooserTask, candidates: Sequence[ChooserCandidate]
) -> Optional[dict[str, Any]]:
    judge = (
        ml_router.best_internal_model()
        if ml_router._healthy_general_internal()
        else None
    )
    if judge is None:
        return None
    raw = await asyncio.wait_for(
        judge._infer_no_context(
            system_prompts=[
                "You are a careful, impartial evaluator of synthetic health "
                "insurance appeals and assistance replies. Follow the requested "
                "JSON output format exactly."
            ],
            prompt=_local_prompt(task, candidates),
        ),
        timeout=_LOCAL_TIMEOUT_SECONDS,
    )
    scores = _local_scores(str(raw) if raw else None, len(candidates))
    return _pick(candidates, scores, f"{_model_name(judge)}/{LOCAL_RUBRIC}")


async def score_task_candidates(task: ChooserTask) -> None:
    """Store Jev's and fhi-local's top candidates, never voter-facing data."""
    if not getattr(settings, "CHOOSER_SCORE_CANDIDATES", True):
        return
    candidates = [
        candidate
        async for candidate in ChooserCandidate.objects.filter(
            task=task, is_active=True
        ).order_by("candidate_index")
    ]
    if len(candidates) < 2:
        return

    async def attempt(
        key: str, work: Awaitable[Optional[dict[str, Any]]]
    ) -> tuple[str, Optional[dict[str, Any]]]:
        try:
            return key, await work
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(
                f"ChooserTask {task.id}: {key} scoring unavailable: "
                f"{type(e).__name__}"
            )
            return key, None

    results = await asyncio.gather(
        attempt("jev", _jev_scores(task, candidates)),
        attempt("fhi_local", _local_scores_for_task(task, candidates)),
    )
    task.scoring_json = {key: value for key, value in results if value is not None}
    await task.asave(update_fields=["scoring_json", "updated_at"])
