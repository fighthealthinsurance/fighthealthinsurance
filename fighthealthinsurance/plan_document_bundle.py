"""The plan-document bundle: what the pipeline hands FHI, and how it lands.

A bundle is what the plan-document pipeline in healthinsurance-llm-internal
exports (keep its exporter in step with this module: the record fields and
their limits are the contract). ``import_plan_documents`` reads one from a
file or stdin; the admin's add form runs one record's worth through the
same code for a document added by hand. Each record becomes, or updates,
one :class:`PublicPlanDocument`, keyed by ``document_key``.

A record carries the document's metadata and its text in one of three ways:
``pages`` (a list of page texts, the pipeline's usual shape), ``text``
(already page-marked, or wrapped as one page), or ``file`` (a PDF, Word or
text file under the importer's ``--files-root``, parsed here with the same
readers the appeal flow uses). The text is stored as "[Page N]" blocks, the
shape MLPlanDocHelper's passage selection reads.

Public documents, no PHI: an error message may name a field, an index and
a document key, never the text.
"""

import datetime
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from loguru import logger

from fighthealthinsurance.ml.ml_document_extraction import (
    MAX_PDF_CHARS,
    extract_text_from_bytes,
)
from fighthealthinsurance.models import (
    InsuranceCompany,
    InsurancePlan,
    LineOfBusiness,
    PlanDocumentMatch,
    PublicPlanDocument,
)
from fighthealthinsurance.pa_requirements import resolve_insurance_company_by_name

BUNDLE_VERSION = 1
#: The largest bundle the import reads, refused before any of it is decoded.
MAX_BUNDLE_BYTES = 256 * 1024 * 1024
#: The largest file a record may point at.
MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_DOCUMENTS = 5_000
MAX_TEXT_CHARS = min(PublicPlanDocument.MAX_TEXT_CHARS, MAX_PDF_CHARS)
MAX_PAGES = 2_000

KEY_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,128}")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_PAGE_MARKER_RE = re.compile(r"^\[Page \d+\]$", re.MULTILINE)
_REGEX_FLAGS = re.IGNORECASE | re.UNICODE | re.M
#: Words every denial letter carries; a negative regex that matches them
#: rules its document out of every match (``medi-?cal`` matched "medical").
NEGATIVE_REGEX_PROBE = "medical necessity medically necessary medical center"
FILE_EXTENSIONS = (".pdf", ".docx", ".txt")

MAX_TITLE = 500
MAX_REGION = 120
MAX_EMPLOYER = 300
MAX_URL = 2000
MAX_REGEX = 800
MAX_VERSION = 64
MIN_PLAN_YEAR, MAX_PLAN_YEAR = 2000, 2100

TEXT_SOURCE_FIELDS = ("pages", "text", "file")
RECORD_FIELDS = frozenset(
    {
        "document_key",
        "insurer",
        "insurance_company_id",
        "plan_name",
        "plan_state",
        "title",
        "document_type",
        "coverage_kind",
        "line_of_business",
        "state",
        "region",
        "employer_name",
        "plan_year",
        "plan_id_prefixes",
        "match_keywords",
        "match_regex",
        "negative_regex",
        "source_url",
        "fetched_at",
        "source_sha256",
        "pages",
        "text",
        "file",
        "is_active",
        "pipeline_version",
        "notes",
    }
)
REQUIRED_FIELDS = ("document_key", "title", "document_type")

_DOCUMENT_TYPES = {value for value, _ in PublicPlanDocument.DOCUMENT_TYPE_CHOICES}
_COVERAGE_KINDS = {value for value, _ in PublicPlanDocument.COVERAGE_KIND_CHOICES}
_LINES_OF_BUSINESS = set(LineOfBusiness.values)


class BundleError(Exception):
    """The bundle as a whole cannot be read."""


class RecordError(Exception):
    """One record is refused; the message names the field, never the text."""


@dataclass
class PreparedDocument:
    """One record, validated and resolved, ready to be stored."""

    document_key: str
    fields: Dict[str, Any]
    warnings: List[str] = field(default_factory=list)
    file_bytes: Optional[bytes] = None
    file_name: str = ""


@dataclass
class ImportResult:
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    deactivated: int = 0
    #: ``(index, document_key, reason)`` for each refused record.
    skipped: List[Tuple[int, str, str]] = field(default_factory=list)
    warnings: List[Tuple[int, str, str]] = field(default_factory=list)


# ---- Reading the bundle -----------------------------------------------------


def no_repeated_fields(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    """json's object hook: a field given twice is refused, not last-wins."""
    seen: Dict[str, Any] = {}
    for name, value in pairs:
        if name in seen:
            raise BundleError(f"a JSON object gives the field {name!r} twice")
        seen[name] = value
    return seen


def parse_bundle(data: Any) -> List[Dict[str, Any]]:
    """The records of a decoded bundle: a JSON array of records, or an
    object ``{"bundle_version": 1, "documents": [...]}``."""
    if isinstance(data, dict):
        version = data.get("bundle_version")
        if version not in (None, BUNDLE_VERSION):
            raise BundleError(f"bundle_version {version!r} is not supported")
        documents = data.get("documents")
        if not isinstance(documents, list):
            raise BundleError("the bundle object needs a 'documents' array")
        data = documents
    if not isinstance(data, list):
        raise BundleError("a bundle is a JSON array of document records")
    if len(data) > MAX_DOCUMENTS:
        raise BundleError(f"a bundle holds at most {MAX_DOCUMENTS} documents")
    for index, record in enumerate(data):
        if not isinstance(record, dict):
            raise BundleError(f"record {index} is not a JSON object")
    return data


def parse_jsonl(lines: Iterable[str]) -> List[Dict[str, Any]]:
    """Records from JSON Lines text, one object per non-blank line."""
    records: List[Dict[str, Any]] = []
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line, object_pairs_hook=no_repeated_fields)
        except json.JSONDecodeError as exc:
            raise BundleError(f"line {number} is not valid JSON: {exc.msg}") from None
        if not isinstance(record, dict):
            raise BundleError(f"line {number} is not a JSON object")
        records.append(record)
        if len(records) > MAX_DOCUMENTS:
            raise BundleError(f"a bundle holds at most {MAX_DOCUMENTS} documents")
    return records


# ---- Text --------------------------------------------------------------------------


def page_blocks(pages: Sequence[Tuple[int, str]]) -> str:
    """``"[Page N]"`` blocks for ``(number, text)`` pages, blank pages left
    out, joined the way MLPlanDocHelper.load_plan_documents joins them."""
    return "\n\n".join(
        f"[Page {number}]\n{text.strip()}"
        for number, text in sorted(pages, key=lambda p: p[0])
        if text and text.strip()
    )


def page_marked_text_from_bytes(data: bytes, filename: str) -> Tuple[str, int]:
    """A file's text as page blocks and its page count, through the readers
    the appeal flow uses (PDF, Word, text). Raises RecordError when no text
    can be read (a scanned PDF, an unknown type)."""
    lower = filename.lower()
    if not lower.endswith(FILE_EXTENSIONS):
        raise RecordError(f"file: only {', '.join(FILE_EXTENSIONS)} files are read")
    if len(data) > MAX_FILE_BYTES:
        raise RecordError(f"file: larger than {MAX_FILE_BYTES} bytes")
    _full_text, page_dict = extract_text_from_bytes(data, filename)
    pages = [
        (number, text) for number, text in page_dict.items() if text and text.strip()
    ]
    if not pages:
        raise RecordError("file: no text could be read from it")
    text = page_blocks(pages)
    if len(text) > MAX_TEXT_CHARS:
        raise RecordError(f"file: more than {MAX_TEXT_CHARS} characters of text")
    return text, len(pages)


def _text_from_pages(pages: Any) -> Tuple[str, int]:
    if not isinstance(pages, list) or not pages:
        raise RecordError("pages: a non-empty list of page texts")
    if len(pages) > MAX_PAGES:
        raise RecordError(f"pages: at most {MAX_PAGES} pages")
    numbered: List[Tuple[int, str]] = []
    for position, page in enumerate(pages, start=1):
        if isinstance(page, str):
            numbered.append((position, page))
        elif isinstance(page, dict) and isinstance(page.get("text"), str):
            number = page.get("page", position)
            if not isinstance(number, int) or isinstance(number, bool) or number < 1:
                raise RecordError(f"pages: page {position} has no valid page number")
            numbered.append((number, page["text"]))
        else:
            raise RecordError(f"pages: page {position} is not text")
    text = page_blocks(numbered)
    if not text:
        raise RecordError("pages: every page is blank")
    return text, sum(1 for _, page_text in numbered if page_text.strip())


def _text_from_text(text: Any) -> Tuple[str, int]:
    if not isinstance(text, str) or not text.strip():
        raise RecordError("text: a non-empty string")
    markers = _PAGE_MARKER_RE.findall(text)
    if markers:
        return text.strip(), len(markers)
    return f"[Page 1]\n{text.strip()}", 1


def _read_file(relative: Any, files_root: Optional[str]) -> Tuple[bytes, str]:
    if not isinstance(relative, str) or not relative.strip():
        raise RecordError("file: a relative path")
    if files_root is None:
        raise RecordError("file: the import needs --files-root to read files")
    if "\x00" in relative or os.path.isabs(relative):
        raise RecordError("file: the path must be relative to the files root")
    try:
        root = os.path.realpath(files_root)
        path = os.path.realpath(os.path.join(root, relative))
        if os.path.commonpath([root, path]) != root:
            raise RecordError("file: the path leaves the files root")
        if not os.path.isfile(path):
            raise RecordError("file: no such file under the files root")
        if os.path.getsize(path) > MAX_FILE_BYTES:
            raise RecordError(f"file: larger than {MAX_FILE_BYTES} bytes")
        with open(path, "rb") as fh:
            return fh.read(), os.path.basename(path)
    except (OSError, ValueError) as exc:
        raise RecordError(f"file: cannot be read ({type(exc).__name__})") from None


# ---- One record -------------------------------------------------------------------


def _string(
    record: Dict[str, Any], name: str, limit: int, required: bool = False
) -> str:
    """A one-line string field: whitespace collapsed, so a scraped title or
    URL cannot carry a line break into the prompt."""
    value = record.get(name, "")
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise RecordError(f"{name}: a string")
    value = " ".join(value.split())
    if required and not value:
        raise RecordError(f"{name}: required")
    if len(value) > limit:
        raise RecordError(f"{name}: longer than {limit} characters")
    return value


def _phrases(record: Dict[str, Any], name: str, *, upper: bool) -> List[str]:
    value = record.get(name) or []
    if not isinstance(value, list):
        raise RecordError(f"{name}: a list of strings")
    out: List[str] = []
    for item in value:
        if not isinstance(item, str):
            raise RecordError(f"{name}: a list of strings")
        cleaned = " ".join(item.split())
        cleaned = cleaned.upper() if upper else cleaned.lower()
        if cleaned and cleaned not in out:
            out.append(cleaned)
    return out


def _regex(record: Dict[str, Any], name: str) -> str:
    pattern = _string(record, name, MAX_REGEX)
    if pattern:
        try:
            re.compile(pattern, _REGEX_FLAGS)
        except re.error as exc:
            raise RecordError(f"{name}: does not compile ({exc.msg})") from None
    return pattern


def _bool(record: Dict[str, Any], name: str, default: bool) -> bool:
    value = record.get(name, default)
    if not isinstance(value, bool):
        raise RecordError(f"{name}: true or false")
    return value


def _choice(
    record: Dict[str, Any], name: str, allowed: set, default: Optional[str]
) -> str:
    value = record.get(name, default)
    if value is None and default is None:
        raise RecordError(f"{name}: required")
    if not isinstance(value, str) or value not in allowed:
        raise RecordError(f"{name}: one of {', '.join(sorted(allowed))}")
    return value


def _resolve_company(record: Dict[str, Any]) -> InsuranceCompany:
    company_id = record.get("insurance_company_id")
    if company_id is not None:
        if isinstance(company_id, bool) or not isinstance(company_id, int):
            raise RecordError("insurance_company_id: an integer")
        company = InsuranceCompany.objects.filter(id=company_id).first()
        if company is None:
            raise RecordError("insurance_company_id: no such insurance company")
        return company
    insurer = _string(record, "insurer", 300)
    if not insurer:
        raise RecordError("insurer: required (or insurance_company_id)")
    company = resolve_insurance_company_by_name(insurer)
    if company is None:
        raise RecordError("insurer: no insurance company by that name")
    return company


def _resolve_plan(
    company: InsuranceCompany, plan_name: str, plan_state: str, state: str
) -> Optional[InsurancePlan]:
    """The carrier's plan of that name: the one for ``plan_state`` when
    given, else the one for the document's state, else the national row
    (blank state), else the only plan of that name."""
    named = InsurancePlan.objects.filter(
        insurance_company=company, plan_name__iexact=plan_name
    )
    if plan_state:
        return named.filter(state=plan_state).first()
    for candidate_state in (state, ""):
        plan = named.filter(state=candidate_state).first()
        if plan is not None:
            return plan
    plans = list(named[:2])
    return plans[0] if len(plans) == 1 else None


def prepare_document_fields(
    record: Dict[str, Any],
    *,
    files_root: Optional[str] = None,
    store_file: bool = False,
    import_batch: str = "",
) -> PreparedDocument:
    """Validate one record and resolve what it names.

    Returns the ``PublicPlanDocument`` field values (``fields``), the
    warnings worth printing, and the file's bytes when ``store_file`` asked
    for them. Raises RecordError, whose message names the field and never
    quotes the text.
    """
    unknown = sorted(set(record) - RECORD_FIELDS)
    if unknown:
        raise RecordError(f"unknown field(s): {', '.join(unknown)}")
    for name in REQUIRED_FIELDS:
        if name not in record:
            raise RecordError(f"{name}: required")
    key = _string(record, "document_key", 128, required=True)
    if not KEY_PATTERN.fullmatch(key):
        raise RecordError("document_key: letters, digits, '.', '_' and '-' only")

    warnings: List[str] = []
    company = _resolve_company(record)
    state = _string(record, "state", 2).upper()
    if state and not (len(state) == 2 and state.isalpha()):
        raise RecordError("state: a two-letter code or blank")

    plan: Optional[InsurancePlan] = None
    plan_name = _string(record, "plan_name", 300)
    if plan_name:
        plan = _resolve_plan(
            company, plan_name, _string(record, "plan_state", 2).upper(), state
        )
        if plan is None:
            warnings.append(
                f"plan_name: no plan named so for {company.name}; left blank"
            )

    plan_year: Optional[int] = record.get("plan_year")
    if plan_year is not None:
        if isinstance(plan_year, bool) or not isinstance(plan_year, int):
            raise RecordError("plan_year: a year")
        if not MIN_PLAN_YEAR <= plan_year <= MAX_PLAN_YEAR:
            raise RecordError(f"plan_year: between {MIN_PLAN_YEAR} and {MAX_PLAN_YEAR}")

    negative_regex = _regex(record, "negative_regex")
    if negative_regex and re.compile(negative_regex, _REGEX_FLAGS).search(
        NEGATIVE_REGEX_PROBE
    ):
        warnings.append(
            "negative_regex: matches ordinary denial-letter words "
            "(medical, medically); it would rule this document out of every letter"
        )

    source_url = _string(record, "source_url", MAX_URL)
    if source_url:
        try:
            URLValidator(schemes=["http", "https"])(source_url)
        except ValidationError:
            raise RecordError("source_url: an http(s) URL") from None
    fetched_at = None
    fetched_raw = record.get("fetched_at")
    if fetched_raw not in (None, ""):
        if not isinstance(fetched_raw, str):
            raise RecordError("fetched_at: an ISO 8601 datetime")
        try:
            fetched_at = parse_datetime(fetched_raw)
        except ValueError:
            fetched_at = None
        if fetched_at is None:
            raise RecordError("fetched_at: an ISO 8601 datetime")
        if timezone.is_naive(fetched_at):
            fetched_at = timezone.make_aware(fetched_at, datetime.timezone.utc)
    source_sha256 = _string(record, "source_sha256", 64).lower()
    if source_sha256 and not _SHA256_PATTERN.fullmatch(source_sha256):
        raise RecordError("source_sha256: 64 hex characters")

    sources = [
        name for name in TEXT_SOURCE_FIELDS if record.get(name) not in (None, "", [])
    ]
    if len(sources) != 1:
        raise RecordError("exactly one of pages, text or file")
    file_bytes: Optional[bytes] = None
    file_name = ""
    if sources[0] == "pages":
        text, page_count = _text_from_pages(record["pages"])
    elif sources[0] == "text":
        text, page_count = _text_from_text(record["text"])
    else:
        data, file_name = _read_file(record["file"], files_root)
        text, page_count = page_marked_text_from_bytes(data, file_name)
        if not source_sha256:
            source_sha256 = hashlib.sha256(data).hexdigest()
        if store_file:
            file_bytes = data
    if len(text) > MAX_TEXT_CHARS:
        raise RecordError(f"text: more than {MAX_TEXT_CHARS} characters")

    fields = {
        "insurance_company": company,
        "insurance_plan": plan,
        "title": _string(record, "title", MAX_TITLE, required=True),
        "document_type": _choice(record, "document_type", _DOCUMENT_TYPES, None),
        "coverage_kind": _choice(
            record,
            "coverage_kind",
            _COVERAGE_KINDS,
            PublicPlanDocument.COVERAGE_KIND_MEDICAL,
        ),
        "line_of_business": _choice(
            record, "line_of_business", _LINES_OF_BUSINESS, LineOfBusiness.ALL
        ),
        "state": state,
        "region": _string(record, "region", MAX_REGION),
        "employer_name": _string(record, "employer_name", MAX_EMPLOYER),
        "plan_year": plan_year,
        "plan_id_prefixes": _phrases(record, "plan_id_prefixes", upper=True),
        "match_keywords": _phrases(record, "match_keywords", upper=False),
        "match_regex": _regex(record, "match_regex"),
        "negative_regex": negative_regex,
        "source_url": source_url,
        "fetched_at": fetched_at,
        "source_sha256": source_sha256,
        "text": text,
        "text_sha256": PublicPlanDocument.text_hash(text),
        "page_count": page_count,
        "char_count": len(text),
        "is_active": _bool(record, "is_active", True),
        "pipeline_version": _string(record, "pipeline_version", MAX_VERSION),
        "notes": record.get("notes") if isinstance(record.get("notes"), str) else "",
        "import_batch": import_batch[:MAX_VERSION],
    }
    return PreparedDocument(
        document_key=key,
        fields=fields,
        warnings=warnings,
        file_bytes=file_bytes,
        file_name=file_name,
    )


# ---- Storing -------------------------------------------------------------------------


def _differs(doc: PublicPlanDocument, name: str, value: Any) -> bool:
    current = getattr(doc, name)
    if name in ("match_regex", "negative_regex"):
        current = getattr(current, "pattern", current) or ""
    if name in ("insurance_company", "insurance_plan"):
        return bool(
            getattr(doc, f"{name}_id") != (value.id if value is not None else None)
        )
    return bool(current != value)


def store_document(
    prepared: PreparedDocument, *, write_files: bool = True
) -> Tuple[str, Optional[str]]:
    """Create or update the row for one prepared record.

    Returns ``(outcome, stored_name)``: "created", "updated" or
    "unchanged", and the name of the file written to storage, if one was
    (``write_files`` off keeps storage untouched, for a dry run). A changed
    text drops the match rows built from the old one."""
    doc = PublicPlanDocument.objects.filter(document_key=prepared.document_key).first()
    if doc is None:
        doc = PublicPlanDocument(document_key=prepared.document_key, **prepared.fields)
        doc.save()
        return "created", (_store_file(doc, prepared) if write_files else None)
    changed = [
        name
        for name, value in prepared.fields.items()
        if name != "import_batch" and _differs(doc, name, value)
    ]
    stored_name = _store_file(doc, prepared) if write_files else None
    if not changed and stored_name is None:
        return "unchanged", None
    for name in changed:
        setattr(doc, name, prepared.fields[name])
    doc.import_batch = prepared.fields["import_batch"]
    doc.save()
    if "text_sha256" in changed:
        PlanDocumentMatch.objects.filter(document=doc).delete()
    return "updated", stored_name


def _store_file(doc: PublicPlanDocument, prepared: PreparedDocument) -> Optional[str]:
    """Keep the original file when the import asked for it; the stored
    name when a file was written, else None. The file already kept is
    recognised by its content hash and extension, not its name: the
    storage adds a token of its own to every name it saves
    (combined_storage.unique_name)."""
    if prepared.file_bytes is None:
        return None
    from django.core.files.base import ContentFile

    ext = os.path.splitext(prepared.file_name)[1].lower()
    same_hash = bool(doc.source_sha256) and (
        doc.source_sha256 == prepared.fields["source_sha256"]
    )
    if (
        doc.stored_file
        and doc.stored_file.name
        and same_hash
        and doc.stored_file.name.lower().endswith(ext)
    ):
        return None
    if doc.stored_file:
        # After the commit, not now: a rollback puts the row back pointing
        # at this file. The helper also leaves a file another row still
        # names.
        from fighthealthinsurance.models import _delete_files_on_commit

        # A detached copy: saving below renames doc.stored_file in place,
        # and the callback reads the name only at commit time.
        old_file = doc.stored_file
        old_copy = old_file.field.attr_class(doc, old_file.field, old_file.name)
        _delete_files_on_commit(PublicPlanDocument, doc.pk, [("stored_file", old_copy)])
    doc.stored_file.save(
        f"{prepared.document_key}{ext}", ContentFile(prepared.file_bytes), save=True
    )
    return str(doc.stored_file.name)


def _remove_files(names: Sequence[str]) -> None:
    """Remove files this run wrote to storage before it was abandoned."""
    storage = PublicPlanDocument._meta.get_field("stored_file").storage
    for name in names:
        try:
            storage.delete(name)
        except Exception:
            logger.opt(exception=True).warning(
                "Could not remove a plan document file written before the "
                "import was abandoned"
            )


def import_bundle(
    records: Sequence[Dict[str, Any]],
    *,
    files_root: Optional[str] = None,
    store_file: bool = False,
    import_batch: str = "",
    deactivate_missing: bool = False,
    dry_run: bool = False,
    strict: bool = False,
) -> ImportResult:
    """Store every record of a bundle in one transaction.

    A refused record is counted and skipped; with ``strict`` the first one
    (or the first warning) raises BundleError and nothing is written.
    ``deactivate_missing`` retires the documents of the carriers the bundle
    names whose keys it does not carry (a key whose record was refused
    still counts as carried), and only theirs. ``dry_run`` does the work
    and rolls it back, writing no files; a run that is abandoned removes
    the files it had written.
    """
    result = ImportResult()
    # Every key the bundle names, valid record or not.
    named_keys: List[str] = []
    seen_companies: set = set()
    # Files written to storage this run, removed if the run is abandoned:
    # the rows roll back with the transaction, the files would not.
    written_files: List[str] = []
    try:
        with transaction.atomic():
            for index, record in enumerate(records):
                raw_key = (
                    record.get("document_key") if isinstance(record, dict) else None
                )
                key = raw_key if isinstance(raw_key, str) else ""
                try:
                    if key and key in named_keys:
                        raise RecordError("document_key: given twice in this bundle")
                    if key:
                        named_keys.append(key)
                    prepared = prepare_document_fields(
                        record,
                        files_root=files_root,
                        store_file=store_file,
                        import_batch=import_batch,
                    )
                    if strict and prepared.warnings:
                        raise RecordError(prepared.warnings[0])
                    outcome, stored_name = store_document(
                        prepared, write_files=not dry_run
                    )
                    if stored_name:
                        written_files.append(stored_name)
                except RecordError as exc:
                    if strict:
                        transaction.set_rollback(True)
                        raise BundleError(
                            f"record {index} ({key or 'no key'}): {exc}"
                        ) from None
                    result.skipped.append((index, key, str(exc)))
                    continue
                except Exception as exc:
                    # A record this code did not foresee (a value of an odd
                    # type deep in a field) is a bad record, not a dead run.
                    logger.opt(exception=True).warning(
                        f"Plan document record {index} could not be read: "
                        f"{type(exc).__name__}"
                    )
                    reason = f"could not be read ({type(exc).__name__})"
                    if strict:
                        transaction.set_rollback(True)
                        raise BundleError(
                            f"record {index} ({key or 'no key'}): {reason}"
                        ) from None
                    result.skipped.append((index, key, reason))
                    continue
                for warning in prepared.warnings:
                    result.warnings.append((index, prepared.document_key, warning))
                seen_companies.add(prepared.fields["insurance_company"].id)
                setattr(result, outcome, getattr(result, outcome) + 1)
            if deactivate_missing and seen_companies:
                result.deactivated = (
                    PublicPlanDocument.objects.filter(
                        insurance_company_id__in=seen_companies
                    )
                    .exclude(document_key__in=named_keys)
                    .filter(is_active=True)
                    .update(is_active=False, updated_at=timezone.now())
                )
            if dry_run:
                transaction.set_rollback(True)
    except BaseException:
        _remove_files(written_files)
        raise
    logger.info(
        f"Plan document bundle: created={result.created} updated={result.updated} "
        f"unchanged={result.unchanged} skipped={len(result.skipped)} "
        f"deactivated={result.deactivated} dry_run={dry_run}"
    )
    return result
