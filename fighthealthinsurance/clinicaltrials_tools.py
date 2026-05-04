"""
ClinicalTrials.gov API v2 client.

The ClinicalTrials.gov registry helps distinguish, for "experimental or
investigational" denials, between treatments that are:

* FDA-approved standard-of-care
* off-label but guideline-supported
* actively studied (in registered trials) but not yet established
* truly speculative (no registered trials)

A trial existing does not by itself prove insurance coverage, but it is one
data point that supports the framing: "the relevant question is not whether
the therapy exists, but whether it is medically appropriate for this patient."
"""

import asyncio
import hashlib
import json
import sys
from datetime import timedelta
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import aiohttp
from django.utils import timezone
from loguru import logger

from fighthealthinsurance.models import (
    ClinicalTrial,
    ClinicalTrialQueryData,
    Denial,
)

if sys.version_info >= (3, 11):
    from asyncio import timeout as async_timeout
else:
    from async_timeout import timeout as async_timeout


CLINICAL_TRIALS_API_BASE = "https://clinicaltrials.gov/api/v2"

_FETCH_HEADERS = {
    "User-Agent": "FightHealthInsurance/1.0 (mailto:support@fighthealthinsurance.com)",
    "Accept": "application/json",
}

# Cap how many trials we look at for any one query, to keep prompts compact.
DEFAULT_PAGE_SIZE = 10
DEFAULT_MAX_TRIALS = 5


def _join_nonempty(values: Optional[List[Any]], sep: str = "\n") -> str:
    if not values:
        return ""
    cleaned = [str(v).strip() for v in values if v is not None and str(v).strip()]
    return sep.join(cleaned)


def _intervention_strings(interventions: Optional[List[Dict[str, Any]]]) -> List[str]:
    if not interventions:
        return []
    out: List[str] = []
    for it in interventions:
        if not isinstance(it, dict):
            continue
        name = (it.get("name") or "").strip()
        itype = (it.get("type") or "").strip()
        if name and itype:
            out.append(f"{itype}: {name}")
        elif name:
            out.append(name)
    return out


def _parse_study(study: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not isinstance(study, dict):
        return None
    proto = study.get("protocolSection") or {}
    ident = proto.get("identificationModule") or {}
    nct_id = (ident.get("nctId") or "").strip()
    if not nct_id:
        return None
    status = proto.get("statusModule") or {}
    desc = proto.get("descriptionModule") or {}
    cond_mod = proto.get("conditionsModule") or {}
    arms = proto.get("armsInterventionsModule") or {}
    design = proto.get("designModule") or {}

    def _date(field: str) -> str:
        return ((status.get(field) or {}).get("date") or "").strip()

    return {
        "nct_id": nct_id,
        "brief_title": (ident.get("briefTitle") or "").strip(),
        "official_title": (ident.get("officialTitle") or "").strip(),
        "overall_status": (status.get("overallStatus") or "").strip(),
        "phases": ",".join(design.get("phases") or []),
        "study_type": (design.get("studyType") or "").strip(),
        "conditions": _join_nonempty(cond_mod.get("conditions")),
        "interventions": _join_nonempty(
            _intervention_strings(arms.get("interventions"))
        ),
        "brief_summary": (desc.get("briefSummary") or "").strip(),
        "start_date": _date("startDateStruct"),
        "completion_date": _date("completionDateStruct"),
        "last_update_date": _date("lastUpdatePostDateStruct"),
        "has_results": bool(study.get("hasResults")),
    }


class ClinicalTrialsTools(object):
    """Async client + cache for ClinicalTrials.gov API v2."""

    def __init__(self, api_base: str = CLINICAL_TRIALS_API_BASE) -> None:
        self.api_base = api_base.rstrip("/")

    async def _fetch_json(
        self,
        session: aiohttp.ClientSession,
        url: str,
        timeout_secs: float,
        label: str = "",
    ) -> Optional[Dict[str, Any]]:
        """Fetch JSON from url with timeout. Returns None on any failure.

        Logs are redacted: the label/url contain diagnosis/procedure terms
        derived from a denial, so we hash them before emitting any log line.
        """
        # Stable short hash so support can correlate without reading PHI.
        redacted = "req_" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:10]
        try:
            async with async_timeout(timeout_secs):
                async with session.get(url) as resp:
                    if resp.status == 200:
                        data: Dict[str, Any] = await resp.json()
                        return data
                    # 429/5xx is worth surfacing -- the registry rate-limits and
                    # silent retries on the next query would compound the problem.
                    log = logger.warning if resp.status >= 429 else logger.debug
                    log(f"[clinicaltrials:{redacted}] HTTP {resp.status}")
        except Exception as e:
            logger.debug(
                f"[clinicaltrials:{redacted}] fetch failed: {type(e).__name__}"
            )
        return None

    def _build_studies_url(
        self,
        query: Optional[str],
        condition: Optional[str],
        intervention: Optional[str],
        page_size: int,
    ) -> str:
        params: Dict[str, Any] = {
            "format": "json",
            "pageSize": max(1, min(page_size, 50)),
            # Trim the response so we only pay for what we render.
            "fields": ",".join(
                [
                    "protocolSection.identificationModule",
                    "protocolSection.statusModule",
                    "protocolSection.descriptionModule.briefSummary",
                    "protocolSection.conditionsModule",
                    "protocolSection.armsInterventionsModule.interventions",
                    "protocolSection.designModule",
                    "hasResults",
                ]
            ),
        }
        if query:
            params["query.term"] = query
        if condition:
            params["query.cond"] = condition
        if intervention:
            params["query.intr"] = intervention
        return f"{self.api_base}/studies?{urlencode(params)}"

    async def find_trials_for_query(
        self,
        query: str,
        condition: Optional[str] = None,
        intervention: Optional[str] = None,
        page_size: int = DEFAULT_PAGE_SIZE,
        timeout: float = 30.0,
    ) -> List[str]:
        """
        Return NCT IDs matching the supplied query, using cached results
        from the last 30 days when available.

        The cache is global (denial_id IS NULL); per-denial audit rows belong
        in a separate write path so they don't shadow the cache lookup.
        """
        nct_ids: List[str] = []
        normalized_query = (query or "").strip()
        norm_condition = (condition or "").strip() or None
        norm_intervention = (intervention or "").strip() or None
        if not (normalized_query or norm_condition or norm_intervention):
            return []

        try:
            async with async_timeout(timeout):
                month_ago = timezone.now() - timedelta(days=30)
                cached_qs = ClinicalTrialQueryData.objects.filter(
                    query=normalized_query,
                    condition=norm_condition,
                    intervention=norm_intervention,
                    created__gte=month_ago,
                    denial_id__isnull=True,
                ).order_by("-created")

                # A row with nct_ids == "[]" is a positive "we already checked,
                # no matches" answer -- return it instead of refetching.
                async for cached in cached_qs:
                    if cached.nct_ids is None:
                        continue
                    try:
                        ids: List[str] = json.loads(cached.nct_ids.replace("\x00", ""))
                    except json.JSONDecodeError:
                        logger.error("Error parsing cached NCT IDs (corrupt row)")
                        continue
                    return ids

                url = self._build_studies_url(
                    normalized_query, norm_condition, norm_intervention, page_size
                )
                async with aiohttp.ClientSession(headers=_FETCH_HEADERS) as session:
                    data = await self._fetch_json(
                        session, url, timeout_secs=timeout, label=normalized_query
                    )
                if not data:
                    return []
                studies = data.get("studies") or []
                for study in studies:
                    parsed = _parse_study(study)
                    if not parsed:
                        continue
                    nct_ids.append(parsed["nct_id"])
                    # Upsert cached metadata so get_trials() stays fast.
                    await ClinicalTrial.objects.aupdate_or_create(
                        nct_id=parsed["nct_id"],
                        defaults={k: v for k, v in parsed.items() if k != "nct_id"},
                    )
                # Always cache, including empty results, so we don't keep hammering
                # the registry for queries that legitimately have zero matches.
                nct_ids_json = json.dumps(nct_ids).replace("\x00", "")
                await ClinicalTrialQueryData.objects.acreate(
                    query=normalized_query,
                    condition=norm_condition,
                    intervention=norm_intervention,
                    nct_ids=nct_ids_json,
                )
        except asyncio.TimeoutError:
            logger.debug("Timeout in find_trials_for_query")
        except asyncio.exceptions.CancelledError:
            logger.debug("Cancelled in find_trials_for_query")
        except Exception as e:
            # Don't include {e} -- some HTTP/DB libraries embed the URL or
            # query string in the exception message, which would re-leak
            # diagnosis/procedure terms via the logs.
            logger.opt(exception=True).debug(
                f"Unexpected error in find_trials_for_query: {type(e).__name__}"
            )
        return nct_ids

    async def _fetch_trial(
        self,
        nct_id: str,
        session: aiohttp.ClientSession,
        timeout: float = 20.0,
    ) -> Optional[ClinicalTrial]:
        """Fetch a single trial from the API and upsert it into the cache."""
        url = f"{self.api_base}/studies/{nct_id}?format=json"
        try:
            data = await self._fetch_json(
                session, url, timeout_secs=timeout, label=nct_id
            )
        except Exception as e:
            logger.debug(f"Error fetching trial {nct_id}: {e}")
            return None
        if not data:
            return None
        parsed = _parse_study(data)
        if not parsed:
            return None
        trial, _ = await ClinicalTrial.objects.aupdate_or_create(
            nct_id=parsed["nct_id"],
            defaults={k: v for k, v in parsed.items() if k != "nct_id"},
        )
        return trial

    async def get_trial(
        self, nct_id: str, timeout: float = 20.0
    ) -> Optional[ClinicalTrial]:
        """Fetch a single trial by NCT ID, using DB cache when present."""
        nct_id = (nct_id or "").strip().upper()
        if not nct_id:
            return None
        cached = await ClinicalTrial.objects.filter(nct_id=nct_id).afirst()
        if cached:
            return cached
        async with aiohttp.ClientSession(headers=_FETCH_HEADERS) as session:
            return await self._fetch_trial(nct_id, session, timeout=timeout)

    async def get_trials(self, nct_ids: List[str]) -> List[ClinicalTrial]:
        """Bulk fetch trials, preserving input order and skipping unknown IDs."""
        seen: set[str] = set()
        ordered: List[str] = []
        for nct_id in nct_ids:
            if not nct_id:
                continue
            normalized = nct_id.strip().upper()
            if normalized and normalized not in seen:
                seen.add(normalized)
                ordered.append(normalized)
        if not ordered:
            return []

        by_id: Dict[str, ClinicalTrial] = {
            t.nct_id: t async for t in ClinicalTrial.objects.filter(nct_id__in=ordered)
        }
        missing = [n for n in ordered if n not in by_id]
        if missing:
            async with aiohttp.ClientSession(headers=_FETCH_HEADERS) as session:
                fetched = await asyncio.gather(
                    *[self._fetch_trial(n, session) for n in missing],
                    return_exceptions=True,
                )
            for nct_id, result in zip(missing, fetched):
                if isinstance(result, ClinicalTrial):
                    by_id[nct_id] = result

        return [by_id[n] for n in ordered if n in by_id]

    async def find_trials_for_denial(
        self,
        denial: Denial,
        max_trials: int = DEFAULT_MAX_TRIALS,
        timeout: float = 40.0,
    ) -> List[ClinicalTrial]:
        """Search the registry using denial.procedure as intervention and
        denial.diagnosis as condition; return at most max_trials trials."""
        denial = await Denial.objects.aget(pk=denial.denial_id)
        procedure = (denial.procedure or "").strip()
        diagnosis = (denial.diagnosis or "").strip()
        if not (procedure or diagnosis):
            return []

        query = " ".join(p for p in (procedure, diagnosis) if p)
        nct_ids = await self.find_trials_for_query(
            query=query,
            condition=diagnosis or None,
            intervention=procedure or None,
            page_size=max(max_trials, DEFAULT_PAGE_SIZE),
            timeout=timeout,
        )
        if not nct_ids:
            return []
        # Audit row tying this denial to the NCT IDs we surfaced. Stored
        # alongside (not in place of) the global cache row so denial-scoped
        # writes don't shadow future global cache hits.
        await ClinicalTrialQueryData.objects.acreate(
            denial_id=denial,
            query=query,
            condition=diagnosis or None,
            intervention=procedure or None,
            nct_ids=json.dumps(nct_ids).replace("\x00", ""),
        )
        trials = await self.get_trials(nct_ids[:max_trials])
        return trials

    @staticmethod
    def format_trial_short(trial: ClinicalTrial) -> str:
        """Compact one-line-per-field rendering for LLM context."""
        parts: List[str] = []
        if trial.nct_id:
            parts.append(f"NCT: {trial.nct_id}")
        if trial.brief_title:
            parts.append(f"Title: {trial.brief_title}")
        if trial.overall_status:
            parts.append(f"Status: {trial.overall_status}")
        if trial.phases:
            parts.append(f"Phase: {trial.phases}")
        if trial.study_type:
            parts.append(f"Type: {trial.study_type}")
        if trial.conditions:
            parts.append("Conditions: " + trial.conditions.replace("\n", "; ")[:200])
        if trial.interventions:
            parts.append(
                "Interventions: " + trial.interventions.replace("\n", "; ")[:200]
            )
        if trial.has_results:
            parts.append("HasResults: yes")
        if trial.start_date:
            parts.append(f"Start: {trial.start_date}")
        if trial.completion_date:
            parts.append(f"Completion: {trial.completion_date}")
        if trial.brief_summary:
            summary = trial.brief_summary.replace("\n", " ")
            parts.append(f"Summary: {summary[:400]}")
        if trial.study_url:
            parts.append(f"URL: {trial.study_url}")
        return "; ".join(parts)
