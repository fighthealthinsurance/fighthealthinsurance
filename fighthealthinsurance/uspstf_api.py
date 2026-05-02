"""
USPSTF (US Preventive Services Task Force) Prevention TaskForce API client.

The USPSTF maintains a public API of evidence-based recommendations for
preventive services (screenings, counseling, preventive medications). Under
the Affordable Care Act, services that receive a USPSTF "A" or "B" grade
generally must be covered without cost-sharing by non-grandfathered private
plans, the ACA marketplace, and Medicaid expansion populations.

This module:
1. Fetches recommendation data from the USPSTF Prevention TaskForce API.
2. Caches recommendations locally in the USPSTFRecommendation Django model.
3. Exposes search and formatting helpers used by the chat tool and the
   denial-classification expert system.

If the live API is unreachable (network blocked, transient outage, etc.),
the module falls back to a small bundled set of high-impact A/B
recommendations so appeal generation can still cite preventive guidance.
"""

import asyncio
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

import aiohttp
from asgiref.sync import sync_to_async
from django.db import transaction
from django.utils import timezone
from loguru import logger

from fighthealthinsurance.env_utils import get_env_variable

DEFAULT_API_URL = "https://data.uspreventiveservicestaskforce.org/api/json"
USER_AGENT = (
    "FightHealthInsurance/1.0 (+https://www.fighthealthinsurance.com; "
    "mailto:support@fighthealthinsurance.com)"
)
FETCH_TIMEOUT = 30.0
VALID_GRADES = {"A", "B", "C", "D", "I"}


# A small, hand-curated fallback used when the API is unreachable. These are
# all current A/B graded recommendations and cover common denial categories.
# The structure mirrors what the live API returns so callers can treat both
# sources uniformly.
FALLBACK_RECOMMENDATIONS: List[Dict[str, Any]] = [
    {
        "id": "colorectal-cancer-screening",
        "title": "Colorectal Cancer: Screening",
        "grade": "A",
        "status": "current",
        "topic": "Colorectal Cancer",
        "population": "Adults aged 45 to 75 years",
        "shortDescription": (
            "The USPSTF recommends screening for colorectal cancer in all adults "
            "aged 45 to 75 years."
        ),
        "rationale": (
            "Screening reduces colorectal cancer mortality. Multiple modalities "
            "(colonoscopy, sigmoidoscopy, FIT, FIT-DNA, CT colonography) are "
            "acceptable."
        ),
        "clinicalConsiderations": (
            "Polyp removal during a screening colonoscopy does not change the "
            "preventive classification under ACA guidance."
        ),
        "url": (
            "https://www.uspreventiveservicestaskforce.org/uspstf/recommendation/"
            "colorectal-cancer-screening"
        ),
        "dateIssued": "2021-05-18",
    },
    {
        "id": "breast-cancer-screening",
        "title": "Breast Cancer: Screening",
        "grade": "B",
        "status": "current",
        "topic": "Breast Cancer",
        "population": "Women aged 40 to 74 years",
        "shortDescription": (
            "The USPSTF recommends biennial screening mammography for women aged "
            "40 to 74 years."
        ),
        "rationale": (
            "Screening mammography reduces breast cancer mortality, with the "
            "greatest absolute benefit for women aged 50 to 74."
        ),
        "clinicalConsiderations": (
            "Screening every 2 years offers the best balance of benefit and harm "
            "for most women in this age range."
        ),
        "url": (
            "https://www.uspreventiveservicestaskforce.org/uspstf/recommendation/"
            "breast-cancer-screening"
        ),
        "dateIssued": "2024-04-30",
    },
    {
        "id": "cervical-cancer-screening",
        "title": "Cervical Cancer: Screening",
        "grade": "A",
        "status": "current",
        "topic": "Cervical Cancer",
        "population": "Women aged 21 to 65 years",
        "shortDescription": (
            "The USPSTF recommends screening for cervical cancer with cytology "
            "every 3 years (ages 21-29) or with cytology every 3 years, hrHPV "
            "testing every 5 years, or co-testing every 5 years (ages 30-65)."
        ),
        "rationale": (
            "Screening substantially reduces cervical cancer incidence and "
            "mortality."
        ),
        "clinicalConsiderations": "",
        "url": (
            "https://www.uspreventiveservicestaskforce.org/uspstf/recommendation/"
            "cervical-cancer-screening"
        ),
        "dateIssued": "2018-08-21",
    },
    {
        "id": "lung-cancer-screening",
        "title": "Lung Cancer: Screening",
        "grade": "B",
        "status": "current",
        "topic": "Lung Cancer",
        "population": (
            "Adults aged 50 to 80 years with a 20 pack-year smoking history who "
            "currently smoke or have quit within the past 15 years"
        ),
        "shortDescription": (
            "The USPSTF recommends annual screening for lung cancer with low-dose "
            "computed tomography (LDCT) in eligible adults."
        ),
        "rationale": (
            "Annual LDCT screening reduces lung cancer mortality in high-risk "
            "adults."
        ),
        "clinicalConsiderations": "",
        "url": (
            "https://www.uspreventiveservicestaskforce.org/uspstf/recommendation/"
            "lung-cancer-screening"
        ),
        "dateIssued": "2021-03-09",
    },
    {
        "id": "prep-for-hiv-prevention",
        "title": "Prevention of Acquisition of HIV: Preexposure Prophylaxis",
        "grade": "A",
        "status": "current",
        "topic": "HIV",
        "population": "Adolescents and adults at increased risk of HIV acquisition",
        "shortDescription": (
            "The USPSTF recommends that clinicians prescribe preexposure "
            "prophylaxis (PrEP) using effective antiretroviral therapy to persons "
            "at increased risk of HIV acquisition to decrease the risk of "
            "acquiring HIV."
        ),
        "rationale": (
            "PrEP is highly effective at reducing HIV acquisition when taken as "
            "prescribed."
        ),
        "clinicalConsiderations": (
            "Counseling, baseline labs, and ongoing monitoring are part of the "
            "preventive service and should be covered without cost-sharing under "
            "the ACA."
        ),
        "url": (
            "https://www.uspreventiveservicestaskforce.org/uspstf/recommendation/"
            "prevention-of-human-immunodeficiency-virus-hiv-infection-pre-exposure-prophylaxis"
        ),
        "dateIssued": "2023-08-22",
    },
    {
        "id": "hiv-screening",
        "title": "Human Immunodeficiency Virus (HIV) Infection: Screening",
        "grade": "A",
        "status": "current",
        "topic": "HIV",
        "population": (
            "Adolescents and adults aged 15 to 65 years; younger and older "
            "persons at increased risk; pregnant persons"
        ),
        "shortDescription": (
            "The USPSTF recommends that clinicians screen for HIV infection in "
            "adolescents and adults aged 15 to 65 years and all pregnant persons."
        ),
        "rationale": "Early identification enables timely treatment and reduces transmission.",
        "clinicalConsiderations": "",
        "url": (
            "https://www.uspreventiveservicestaskforce.org/uspstf/recommendation/"
            "human-immunodeficiency-virus-hiv-infection-screening"
        ),
        "dateIssued": "2019-06-11",
    },
    {
        "id": "statin-use-prevention-cvd",
        "title": (
            "Statin Use for the Primary Prevention of Cardiovascular Disease in "
            "Adults: Preventive Medication"
        ),
        "grade": "B",
        "status": "current",
        "topic": "Cardiovascular Disease",
        "population": (
            "Adults aged 40 to 75 years with one or more CVD risk factors and a "
            "10-year CVD event risk of 10% or greater"
        ),
        "shortDescription": (
            "The USPSTF recommends initiating statin therapy for the primary "
            "prevention of cardiovascular disease in eligible adults."
        ),
        "rationale": "Statin therapy reduces the risk of CVD events in higher-risk adults.",
        "clinicalConsiderations": "",
        "url": (
            "https://www.uspreventiveservicestaskforce.org/uspstf/recommendation/"
            "statin-use-in-adults-preventive-medication"
        ),
        "dateIssued": "2022-08-23",
    },
    {
        "id": "diabetes-screening",
        "title": "Prediabetes and Type 2 Diabetes: Screening",
        "grade": "B",
        "status": "current",
        "topic": "Diabetes",
        "population": "Adults aged 35 to 70 years who are overweight or obese",
        "shortDescription": (
            "The USPSTF recommends screening for prediabetes and type 2 diabetes "
            "in adults aged 35 to 70 years who have overweight or obesity."
        ),
        "rationale": "Screening enables early intervention to prevent or delay diabetes complications.",
        "clinicalConsiderations": "",
        "url": (
            "https://www.uspreventiveservicestaskforce.org/uspstf/recommendation/"
            "screening-for-prediabetes-and-type-2-diabetes"
        ),
        "dateIssued": "2021-08-24",
    },
    {
        "id": "perinatal-depression-prevention",
        "title": "Perinatal Depression: Preventive Interventions",
        "grade": "B",
        "status": "current",
        "topic": "Pregnancy",
        "population": "Pregnant and postpartum persons at increased risk of perinatal depression",
        "shortDescription": (
            "The USPSTF recommends that clinicians provide or refer pregnant and "
            "postpartum persons at increased risk to counseling interventions."
        ),
        "rationale": "Counseling reduces the incidence of perinatal depression.",
        "clinicalConsiderations": "",
        "url": (
            "https://www.uspreventiveservicestaskforce.org/uspstf/recommendation/"
            "perinatal-depression-preventive-interventions"
        ),
        "dateIssued": "2019-02-12",
    },
    {
        "id": "obesity-intensive-behavioral-interventions",
        "title": "Weight Loss to Prevent Obesity-Related Morbidity and Mortality in Adults: Behavioral Interventions",
        "grade": "B",
        "status": "current",
        "topic": "Obesity",
        "population": "Adults with a body mass index of 30 or higher",
        "shortDescription": (
            "The USPSTF recommends that clinicians offer or refer adults with a "
            "BMI of 30 or higher to intensive, multicomponent behavioral "
            "interventions."
        ),
        "rationale": "Intensive behavioral interventions improve weight and cardiometabolic outcomes.",
        "clinicalConsiderations": "",
        "url": (
            "https://www.uspreventiveservicestaskforce.org/uspstf/recommendation/"
            "obesity-in-adults-interventions"
        ),
        "dateIssued": "2018-09-18",
    },
]


def _get_api_url() -> str:
    """Resolve the API URL, allowing override via the USPSTF_API_URL env var."""
    return get_env_variable("USPSTF_API_URL", DEFAULT_API_URL)


def _normalize_grade(grade: Optional[str]) -> str:
    """Coerce a raw grade value (e.g. "A", "Grade A", "I (Insufficient)") to a
    single uppercase letter, or empty string when nothing recognizable is found.
    """
    if not grade:
        return ""
    g = str(grade).strip().upper()
    match = re.search(r"\b([A-DI])\b", g)
    return match.group(1) if match else ""


def _coerce_record(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize a raw API record into a stable snake_case dict.

    The USPSTF API has used several field-name conventions over time; we accept
    common variants on input and always emit the canonical snake_case schema
    that matches the database columns.
    """

    def pick(*keys: str) -> str:
        for key in keys:
            value = raw.get(key)
            if value:
                return str(value).strip()
        return ""

    record_id = pick("id", "uspstfId", "topicId", "recommendationId")
    if not record_id:
        title = pick("title", "topic")
        record_id = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or "uspstf"

    return {
        "id": record_id,
        "title": pick("title", "name", "topic"),
        "grade": _normalize_grade(pick("grade", "recommendationGrade", "currentGrade")),
        "status": pick("status", "recommendationStatus") or "current",
        "topic": pick("topic", "category", "specialty"),
        "population": pick("population", "populationDescription", "subgroup"),
        "short_description": pick(
            "shortDescription",
            "summary",
            "recommendationSummary",
            "description",
            "shortDesc",
        ),
        "rationale": pick("rationale", "rationaleStatement", "evidence"),
        "clinical_considerations": pick(
            "clinicalConsiderations", "clinicalConsiderationsHtml", "considerations"
        ),
        "url": pick("url", "permalink", "topicUrl", "link"),
        "date_issued": pick(
            "dateIssued", "datePublished", "publicationDate", "currentDate"
        ),
        "raw": raw,
    }


def _record_to_defaults(record: Dict[str, Any]) -> Dict[str, Any]:
    """Build the ``defaults`` dict for a USPSTFRecommendation upsert."""
    return {
        "title": record.get("title", "") or "",
        "grade": record.get("grade", "") or "",
        "status": record.get("status", "current") or "current",
        "topic": record.get("topic", "") or "",
        "population": record.get("population", "") or "",
        "short_description": record.get("short_description", "") or "",
        "rationale": record.get("rationale", "") or "",
        "clinical_considerations": record.get("clinical_considerations", "") or "",
        "url": record.get("url", "") or "",
        "date_issued": record.get("date_issued", "") or "",
        "raw_data": record.get("raw"),
        "last_synced": timezone.now(),
    }


_KNOWN_LIST_KEYS = ("specifications", "recommendations", "data", "results", "items")


def _find_record_list(payload: Any) -> List[Any]:
    """Locate the list of recommendations inside an arbitrary API payload."""
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in _KNOWN_LIST_KEYS:
        value = payload.get(key)
        if isinstance(value, list):
            return value
    # Fallback: any value that looks like a list of dicts.
    for value in payload.values():
        if isinstance(value, list) and value and isinstance(value[0], dict):
            return value
    return []


def _extract_records(payload: Any) -> List[Dict[str, Any]]:
    """Pull a list of normalized recommendation dicts out of an API payload."""
    return [
        _coerce_record(item)
        for item in _find_record_list(payload)
        if isinstance(item, dict)
    ]


class USPSTFClient:
    """Async client for the USPSTF Prevention TaskForce API."""

    def __init__(
        self,
        api_url: Optional[str] = None,
        timeout: float = FETCH_TIMEOUT,
    ):
        self.api_url = api_url or _get_api_url()
        self.timeout = timeout

    async def fetch_all_recommendations(self) -> List[Dict[str, Any]]:
        """Fetch and normalize every recommendation exposed by the API.

        Returns an empty list on network or parsing errors so callers can
        gracefully fall back to the cache or the bundled dataset.
        """
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        try:
            async with aiohttp.ClientSession(
                timeout=timeout, headers=headers
            ) as session:
                async with session.get(self.api_url) as resp:
                    if resp.status != 200:
                        logger.warning(
                            f"USPSTF API returned status {resp.status} for {self.api_url}"
                        )
                        return []
                    # content_type=None: tolerate misconfigured servers that
                    # return JSON with a non-application/json header.
                    payload = await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
            logger.warning(f"USPSTF API fetch failed: {e}")
            return []
        except Exception as e:
            logger.opt(exception=True).warning(f"Unexpected USPSTF fetch error: {e}")
            return []

        return _extract_records(payload)

    async def sync_recommendations(self) -> int:
        """Refresh the local cache from the API. Returns number of records stored.

        On failure (including an empty response from the API), the bundled
        fallback dataset is used so the cache always has *something* to serve.
        """
        records = await self.fetch_all_recommendations()
        if not records:
            logger.info("Using bundled USPSTF fallback dataset (API empty/unreachable)")
            records = [_coerce_record(item) for item in FALLBACK_RECOMMENDATIONS]
        return await _store_records(records)


@sync_to_async
def _store_records(records: Iterable[Dict[str, Any]]) -> int:
    """Persist normalized records into the USPSTFRecommendation table.

    Imports the model lazily so the API client can be imported in contexts
    where Django isn't fully configured (e.g. unit-test bootstrapping).
    """
    from fighthealthinsurance.models import USPSTFRecommendation

    count = 0
    for record in records:
        if not record.get("id"):
            continue
        USPSTFRecommendation.objects.update_or_create(
            uspstf_id=record["id"], defaults=_record_to_defaults(record)
        )
        count += 1
    return count


def _row_to_dict(row: Any) -> Dict[str, Any]:
    """Render a USPSTFRecommendation row as a plain dict."""
    return {
        "id": row.uspstf_id,
        "title": row.title,
        "grade": row.grade,
        "status": row.status,
        "topic": row.topic,
        "population": row.population,
        "short_description": row.short_description,
        "rationale": row.rationale,
        "clinical_considerations": row.clinical_considerations,
        "url": row.url,
        "date_issued": row.date_issued,
    }


_SEARCHABLE_FIELDS = (
    "title",
    "topic",
    "population",
    "short_description",
    "rationale",
    "clinical_considerations",
)


def _matches_query(record: Dict[str, Any], terms: List[str]) -> bool:
    """Case-insensitive AND substring match across the searchable fields of a dict."""
    if not terms:
        return True
    haystack = " ".join(
        str(record.get(f, "")) for f in _SEARCHABLE_FIELDS if record.get(f)
    ).lower()
    return all(term in haystack for term in terms)


def _ensure_cache_loaded() -> None:
    """If the cache is empty, seed it from the bundled fallback dataset.

    A full API sync is async; doing it synchronously here would block the
    request thread. The fallback gives us a usable result immediately while
    the periodic sync (or an explicit management command) refreshes the real
    data in the background. The seed is wrapped in a transaction so two
    concurrent callers can't both insert duplicates.
    """
    from fighthealthinsurance.models import USPSTFRecommendation

    if USPSTFRecommendation.objects.exists():
        return
    logger.info("Seeding USPSTFRecommendation cache from bundled fallback dataset")
    with transaction.atomic():
        if USPSTFRecommendation.objects.exists():
            return
        for item in FALLBACK_RECOMMENDATIONS:
            record = _coerce_record(item)
            USPSTFRecommendation.objects.update_or_create(
                uspstf_id=record["id"], defaults=_record_to_defaults(record)
            )


def search_recommendations(
    query: str = "",
    grade: str = "",
    topic: str = "",
    limit: int = 5,
) -> List[Dict[str, Any]]:
    """Search the cached USPSTF recommendations.

    Args:
        query: free-text search applied to title/topic/population/description.
        grade: filter to a specific letter grade (A/B/C/D/I).
        topic: substring match against the topic field.
        limit: maximum results to return (1-25).

    Returns:
        A list of recommendation dicts ordered by grade (A first) then title.
    """
    from django.db.models import Case, IntegerField, Q, Value, When

    from fighthealthinsurance.models import USPSTFRecommendation

    _ensure_cache_loaded()

    qs = USPSTFRecommendation.objects.all()
    grade = _normalize_grade(grade)
    if grade:
        qs = qs.filter(grade=grade)
    if topic:
        qs = qs.filter(topic__icontains=topic)

    # Push free-text term filtering down to the database: each term must appear
    # in at least one searchable field (AND across terms, OR across fields).
    for term in (t.strip() for t in (query or "").split() if t.strip()):
        field_match = Q()
        for field in _SEARCHABLE_FIELDS:
            field_match |= Q(**{f"{field}__icontains": term})
        qs = qs.filter(field_match)

    # Order by grade priority (A,B,C,D,I,unknown) then by title.
    grade_priority = Case(
        When(grade="A", then=Value(0)),
        When(grade="B", then=Value(1)),
        When(grade="C", then=Value(2)),
        When(grade="D", then=Value(3)),
        When(grade="I", then=Value(4)),
        default=Value(5),
        output_field=IntegerField(),
    )
    qs = qs.annotate(_grade_priority=grade_priority).order_by(
        "_grade_priority", "title"
    )

    try:
        limit_int = max(1, min(int(limit), 25))
    except (TypeError, ValueError):
        limit_int = 5
    return [_row_to_dict(r) for r in qs[:limit_int]]


# Conservative mapping from preventive ICD-10 / CPT codes (matched as prefixes)
# to USPSTF topic keywords. Prefixes are written in their canonical dotted form
# for readability; both the prefix and the incoming code are normalized
# (uppercased, dots/whitespace stripped) by ``_strip_code_punct`` before
# comparison, so denials that reference either "Z12.11" or "Z1211" both match.
_CODE_TOPIC_KEYWORDS: List[Tuple[str, List[str]]] = [
    ("Z12.11", ["colorectal"]),  # Colon screening
    ("Z12.31", ["breast"]),  # Routine mammogram
    ("Z11.4", ["hiv"]),
    ("Z12.4", ["cervical"]),
    ("Z13.1", ["diabetes"]),
    ("Z13.6", ["cardiovascular"]),
    ("Z12.2", ["lung"]),  # Respiratory-organ screening (lung), not colorectal
    ("Z11.3", ["hiv"]),
    ("Z32.2", ["pregnancy", "perinatal"]),
    ("99401", ["counseling"]),
    ("99403", ["counseling"]),
    ("G0297", ["lung"]),  # Lung CT screening
    ("77067", ["breast"]),  # Screening mammography
    ("45378", ["colorectal"]),  # Diagnostic colonoscopy
    ("45380", ["colorectal"]),
    ("82270", ["colorectal"]),  # FOBT
]


def _strip_code_punct(code: str) -> str:
    """Normalize a CPT/ICD-10 code for prefix matching: uppercase, no dots/spaces."""
    return re.sub(r"[.\s]", "", (code or "").strip().upper())


def find_recommendations_for_codes(
    codes: Iterable[str],
    limit: int = 5,
) -> List[Dict[str, Any]]:
    """Match preventive ICD-10 / CPT codes to USPSTF recommendations.

    The mapping is deliberately conservative: it keys off well-known topic
    keywords associated with the most common preventive codes. Returns up to
    ``limit`` matched recommendations; an empty list when nothing matches.

    Codes are normalized (uppercased, dots stripped) before comparison so the
    same mapping covers both "Z12.11" and "Z1211"-style inputs.
    """
    keywords: List[str] = []
    seen_keywords: set = set()
    normalized_prefixes = [
        (_strip_code_punct(prefix), kws) for prefix, kws in _CODE_TOPIC_KEYWORDS
    ]
    for code in codes:
        normalized = _strip_code_punct(code)
        if not normalized:
            continue
        for prefix, kws in normalized_prefixes:
            if normalized.startswith(prefix):
                for kw in kws:
                    if kw not in seen_keywords:
                        seen_keywords.add(kw)
                        keywords.append(kw)
                break

    if not keywords:
        return []

    # Load all recommendations once instead of running a search per keyword.
    all_recs = search_recommendations(limit=25)
    seen_ids: set = set()
    matches: List[Dict[str, Any]] = []
    for keyword in keywords:
        for rec in all_recs:
            if rec["id"] in seen_ids:
                continue
            if not _matches_query(rec, [keyword.lower()]):
                continue
            seen_ids.add(rec["id"])
            matches.append(rec)
            if len(matches) >= limit:
                return matches
    return matches


def format_recommendation(rec: Dict[str, Any]) -> str:
    """Format a single recommendation for inclusion in an LLM prompt or appeal."""
    parts: List[str] = []
    title = rec.get("title")
    grade = rec.get("grade")
    if title and grade:
        parts.append(f"{title} (Grade {grade})")
    elif title:
        parts.append(str(title))
    if rec.get("population"):
        parts.append(f"Population: {rec['population']}")
    if rec.get("short_description"):
        parts.append(f"Recommendation: {rec['short_description']}")
    if rec.get("rationale"):
        parts.append(f"Rationale: {rec['rationale']}")
    if rec.get("clinical_considerations"):
        parts.append(f"Clinical considerations: {rec['clinical_considerations']}")
    if rec.get("date_issued"):
        parts.append(f"Date issued: {rec['date_issued']}")
    if rec.get("url"):
        parts.append(f"URL: {rec['url']}")
    return "\n".join(parts)


def get_uspstf_info(query: Dict[str, Any]) -> str:
    """Return a human/LLM-friendly summary of matching USPSTF recommendations.

    query example: ``{"query": "colon cancer", "grade": "A", "limit": 3}``.
    ``query`` is the free-text search; ``topic`` is a separate substring
    filter on the topic field; ``grade`` filters to a single letter.

    The output mirrors the style of :func:`get_medicaid_info` so the chat
    surface stays consistent.
    """
    text_query = (query.get("query") or "").strip()
    grade = (query.get("grade") or "").strip()
    topic = (query.get("topic") or "").strip()
    limit = query.get("limit") or 5

    results = search_recommendations(
        query=text_query, grade=grade, topic=topic, limit=limit
    )
    if not results:
        descriptor = text_query or topic or grade or "your query"
        return f"No USPSTF recommendations found matching {descriptor}."

    intro = (
        "USPSTF recommendations are evidence-based preventive service ratings. "
        "Under the ACA, services with an A or B grade generally must be covered "
        "without cost-sharing by non-grandfathered private plans, the marketplace, "
        "and Medicaid expansion populations.\n"
    )
    blocks = [format_recommendation(r) for r in results]
    return intro + "\n\n".join(blocks)


__all__ = [
    "DEFAULT_API_URL",
    "FALLBACK_RECOMMENDATIONS",
    "USPSTFClient",
    "find_recommendations_for_codes",
    "format_recommendation",
    "get_uspstf_info",
    "search_recommendations",
]
