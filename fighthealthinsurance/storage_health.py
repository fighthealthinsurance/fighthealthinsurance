"""Storage health for the staff Storage Health page.

Every uploaded document goes through one CombinedStorage, which writes it to
each of its backends (the LOCALISH_STORAGE, EXTERNAL_STORAGE,
EXTERNAL_STORAGE_B and EXTERNAL_STORAGE_C settings, whichever are set) and
reads it back from the first that has it. A backend can fail without anyone
noticing: CombinedStorage logs a failed write and carries on with the
others, and an object store that cannot be reached when the process starts
is left out of the write path until the next restart.

This module answers, from the process serving the page: which backends are in
the write path, whether each answers, how full each disk is, whether each
copy lives somewhere that outlasts the pod, whether a test write round-trips
(only when asked), and whether recent documents have their copies.

A stored file's name is often a person's name, so it is never shown: a
sampled file is named by its row and the keyed file_tag that log lines use.
"""

import functools
import os
import shutil
import threading
import time
import uuid
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Optional,
    Tuple,
    TypeVar,
    cast,
)

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage, Storage
from django.db import models
from django.db.models import Q
from django.urls import NoReverseMatch, reverse
from django.utils import timezone
from django_encrypted_filefield.fields import EncryptedFileField

from loguru import logger
from stopit import ThreadingTimeout as Timeout

from fighthealthinsurance.combined_storage import CombinedStorage
from fighthealthinsurance.log_tags import error_text, file_tag
from fighthealthinsurance.models import STORED_FILE_MODELS, _file_field_names

T = TypeVar("T")

#: The settings CombinedStorage is built from, in the order it tries them.
SLOT_NAMES = (
    "LOCALISH_STORAGE",
    "EXTERNAL_STORAGE",
    "EXTERNAL_STORAGE_B",
    "EXTERNAL_STORAGE_C",
)

REACH_TIMEOUT_S = 3.0
EXISTS_TIMEOUT_S = 2.0
WRITE_STEP_TIMEOUT_S = 5.0
DISK_WARN_PERCENT = 85.0
DISK_FAIL_PERCENT = 95.0
#: Recent documents checked per file field, by default and at most.
DEFAULT_SAMPLE = 3
MAX_SAMPLE = 25
#: The recent-documents check stops here and reports the rest unchecked, so
#: a slow object store cannot hang the page.
SAMPLE_DEADLINE_S = 10.0
#: Names the write test stores under. No document is ever named like this.
PROBE_PREFIX = "fhi-storage-health-probe-"

OK, WARN, FAIL, OFF = "ok", "warn", "fail", "off"
_RANK = {OFF: 0, OK: 1, WARN: 2, FAIL: 3}

# Where a backend keeps its copies.
OBJECT_STORE = "object store"
OWN_MOUNT = "own mount"
APP_DISK = "app's disk"

# What a sampled document's copies add up to.
SAFE = "safe"  # a copy off the app's disk
LOCAL_ONLY = "local only"  # copies only on the app's disk
MISSING = "missing"  # no backend this process reaches has it
UNKNOWN = "unknown"  # no copy found, but a check errored
NOT_CHECKED = "not checked"  # the deadline came first
SAMPLE_STATES = (SAFE, LOCAL_ONLY, MISSING, UNKNOWN, NOT_CHECKED)


def worst(levels: Iterable[str]) -> str:
    """The most serious of the levels, OK when there are none."""
    return max(levels, key=lambda level: _RANK[level], default=OK)


def _timed(seconds: float, fn: Callable[[], T]) -> Tuple[T, int]:
    """Run fn under a timeout; its result and how many milliseconds it took.

    stopit swallows its own timeout, so the context is checked instead.
    It only interrupts between Python statements, so a backend blocked in
    one system call is not cut off by it.
    """
    start = time.monotonic()
    box: List[T] = []
    with Timeout(seconds) as ctx:
        box.append(fn())
    if not ctx or not box:
        raise TimeoutError(f"no answer within {seconds:g}s")
    return box[0], int((time.monotonic() - start) * 1000)


#: Probes of one backend that may sit stuck in a call (from this page load or
#: an earlier one) before the page stops starting new ones against it.
STUCK_PROBES_PER_BACKEND = 2
# Keyed by id(): a stuck probe holds its backend, so an id is only reused
# once every probe of the old backend has returned and freed its slot.
_probe_slots: Dict[int, threading.BoundedSemaphore] = {}
_probe_slots_lock = threading.Lock()


def _probe(backend: Storage, seconds: float, fn: Callable[[], T]) -> Tuple[T, int]:
    """Run a read-only check of backend on a worker thread, waiting at most seconds.

    A call stuck inside one system call (a hung network mount, an object
    store that took the connection and went quiet; the MinIO client's own
    timeouts run to minutes) cannot be interrupted, so the page stops
    waiting and leaves the thread to finish by itself. Such a thread keeps
    its backend's slot until its call really returns, so a dead backend
    holds at most STUCK_PROBES_PER_BACKEND threads however often the page
    is loaded.

    Only for calls that write nothing and touch no database. The write test
    keeps _timed: a save abandoned here could land after its cleanup ran.
    """
    with _probe_slots_lock:
        slots = _probe_slots.setdefault(
            id(backend), threading.BoundedSemaphore(STUCK_PROBES_PER_BACKEND)
        )
    if not slots.acquire(blocking=False):
        raise TimeoutError("still waiting on earlier checks of this backend")
    outcome: Dict[str, Any] = {}
    finished = threading.Event()

    def run() -> None:
        try:
            outcome["value"] = fn()
        except BaseException as e:  # handed back to the caller below
            outcome["error"] = e
        finally:
            slots.release()
            finished.set()

    start = time.monotonic()
    threading.Thread(target=run, name="storage-health-probe", daemon=True).start()
    if not finished.wait(seconds):
        raise TimeoutError(f"no answer within {seconds:g}s")
    ms = int((time.monotonic() - start) * 1000)
    if "error" in outcome:
        raise outcome["error"]
    return cast(T, outcome["value"]), ms


def stored_file_storage() -> Optional[CombinedStorage]:
    """The CombinedStorage the document file fields write through."""
    for model in STORED_FILE_MODELS:
        for name in _file_field_names(model):
            storage = getattr(model._meta.get_field(name), "storage", None)
            if isinstance(storage, CombinedStorage):
                return storage
    return None


def _slot_label(backend: Storage, index: int) -> str:
    for slot in SLOT_NAMES:
        if getattr(settings, slot, None) is backend:
            return slot
    return f"backend {index + 1}"


def _kind(backend: Storage) -> str:
    if isinstance(backend, FileSystemStorage):
        return "filesystem"
    return type(backend).__name__


def _location(backend: Storage) -> Optional[str]:
    """Where the backend points: a path, or a bucket and its endpoint. Never a key."""
    if isinstance(backend, FileSystemStorage):
        return str(backend.location)
    bucket = getattr(backend, "bucket_name", None)
    if bucket:
        endpoint = getattr(settings, "MINIO_STORAGE_ENDPOINT", None)
        return f"bucket {bucket}" + (f" at {endpoint}" if endpoint else "")
    return None


def placement(backend: Storage) -> Optional[str]:
    """Whether the backend's copies leave the app's own disk.

    A filesystem backend on the same device as the app's code is on the
    app's disk: in a container that is the pod's own layer, lost when the
    pod goes and never seen by another pod. On a device of its own it is a
    mounted volume, which usually (not always: an emptyDir is a mount too)
    outlasts the pod. Anything that is not a filesystem is an object store.
    None when the location cannot be looked at.
    """
    if not isinstance(backend, FileSystemStorage):
        return OBJECT_STORE
    app_dir = str(getattr(settings, "BASE_DIR", os.getcwd()))

    def devices() -> Tuple[int, int]:
        return os.stat(backend.location).st_dev, os.stat(app_dir).st_dev

    try:
        (here, app), _ = _probe(backend, REACH_TIMEOUT_S, devices)
    except OSError:  # TimeoutError included
        return None
    return APP_DISK if here == app else OWN_MOUNT


def _object_store(backend: Storage) -> Optional[Tuple[Any, str]]:
    """The S3 client and bucket under an object-store backend (MinioStorage).

    Its exists() answers False for every S3 error (access denied, a bad
    signature, a missing bucket) and logs and swallows a dropped connection
    too, so it cannot tell a missing file from a store that is down. The
    checks here ask the client instead, which raises for all of those.
    """
    client = getattr(backend, "client", None)
    bucket = getattr(backend, "bucket_name", None)
    if bucket and hasattr(client, "stat_object") and hasattr(client, "list_objects"):
        return client, str(bucket)
    return None


def _ping(backend: Storage) -> None:
    store = _object_store(backend)
    if isinstance(backend, FileSystemStorage):
        # The first entry is enough, however many files the root holds; a
        # missing root raises FileNotFoundError naming it.
        with os.scandir(backend.location) as entries:
            next(entries, None)
    elif store is not None:
        client, bucket = store
        # A listing, like the one settings.py makes before it lets the store
        # into the write path, so it needs the list permission the app
        # already relies on (a bucket HEAD can be denied to a key that lists
        # fine). A prefix no document uses keeps it to one empty page rather
        # than the bucket's first thousand keys; it raises on a missing
        # bucket, refused access or a dropped connection.
        next(iter(client.list_objects(bucket, prefix=PROBE_PREFIX)), None)
    else:
        # One round trip: a name that is never stored, so the answer is "no".
        backend.exists(f"{PROBE_PREFIX}ping")


def _has_copy(backend: Storage, name: str) -> bool:
    """Whether the backend holds name; raises when it cannot tell."""
    store = _object_store(backend)
    if store is None:
        return bool(backend.exists(name))
    client, bucket = store
    sanitize = getattr(backend, "_sanitize_path", None)
    key = sanitize(name) if callable(sanitize) else name
    try:
        client.stat_object(bucket_name=bucket, object_name=key)
    except Exception as e:
        # The only answer that means "no such file"; the bucket itself was
        # listed by the reachability probe before any file is checked, so
        # the key has the list permission S3 needs to say 404, not 403.
        if getattr(e, "code", None) == "NoSuchKey":
            return False
        raise
    return True


def check_reachable(
    backend: Optional[Storage], timeout: float = REACH_TIMEOUT_S
) -> Dict[str, Any]:
    """Whether the backend answers, and how fast. Never raises."""
    out: Dict[str, Any] = {"ok": False, "error": None, "ms": None}
    if backend is None:
        out["error"] = "not configured"
        return out
    try:
        _, out["ms"] = _probe(backend, timeout, functools.partial(_ping, backend))
        out["ok"] = True
    except FileNotFoundError as e:
        # The root itself is missing: in prod an unmounted volume, locally a
        # directory never created. The path says it all; no traceback.
        logger.warning(f"Storage location missing: {e}")
        out["error"] = f"storage location does not exist: {e.filename}"
    except TimeoutError as e:
        out["error"] = str(e)
    except Exception as e:
        logger.opt(exception=True).warning("Storage health check failed")
        out["error"] = str(e) or type(e).__name__
    return out


def _disk(backend: Storage) -> Optional[Dict[str, Any]]:
    """How full a filesystem backend's disk is; None for anything else.

    A disk that answered the reachability probe but cannot be measured is
    a warning with the reason, not a silent OK.
    """
    if not isinstance(backend, FileSystemStorage):
        return None
    try:
        usage, _ = _probe(
            backend,
            REACH_TIMEOUT_S,
            functools.partial(shutil.disk_usage, backend.location),
        )
    except OSError as e:  # TimeoutError included
        return {"error": e.strerror or str(e), "level": WARN}
    percent = round(100.0 * usage.used / usage.total, 1) if usage.total else 0.0
    if percent >= DISK_FAIL_PERCENT:
        level = FAIL
    elif percent >= DISK_WARN_PERCENT:
        level = WARN
    else:
        level = OK
    return {
        "total": usage.total,
        "used": usage.used,
        "free": usage.free,
        "percent": percent,
        "level": level,
    }


def backend_rows(storage: Optional[CombinedStorage]) -> List[Dict[str, Any]]:
    """One row per backend in the write path, checked live."""
    rows: List[Dict[str, Any]] = []
    for index, backend in enumerate(storage.backends if storage else []):
        reach = check_reachable(backend)
        disk = _disk(backend) if reach["ok"] else None
        where = placement(backend)
        rows.append(
            {
                "label": _slot_label(backend, index),
                "backend": backend,
                "kind": _kind(backend),
                "location": _location(backend),
                "placement": where,
                "off_disk": where in (OBJECT_STORE, OWN_MOUNT),
                "reach": reach,
                "disk": disk,
                "level": FAIL if not reach["ok"] else (disk or {}).get("level", OK),
            }
        )
    return rows


def unconfigured_slots() -> List[Dict[str, Any]]:
    """The slots left out of the write path, and whether that is a problem.

    An object store whose endpoint is set but which is left out could not
    be reached when this process started, and stays out until it restarts.
    """
    out: List[Dict[str, Any]] = []
    endpoint = getattr(settings, "MINIO_STORAGE_ENDPOINT", None)
    for slot in SLOT_NAMES:
        if getattr(settings, slot, None) is not None:
            continue
        if slot == "EXTERNAL_STORAGE_B" and endpoint:
            out.append(
                {
                    "label": slot,
                    "level": FAIL,
                    "note": (
                        f"Configured for the object store at {endpoint}, which did "
                        "not answer when this process started, so it is not in the "
                        "write path: documents uploaded here get no object-store "
                        "copy until this process restarts."
                    ),
                }
            )
        else:
            out.append({"label": slot, "level": OFF, "note": "Not configured."})
    return out


def _newest_first(model: type[models.Model]) -> List[str]:
    """An ordering that puts the newest rows first, as near as the model allows."""
    for field in model._meta.concrete_fields:
        if getattr(field, "auto_now_add", False):
            return [f"-{field.name}", "-pk"]
    return ["-pk"]


def _admin_url(model: type[models.Model], pk: Any) -> Optional[str]:
    meta = model._meta
    try:
        return reverse(f"admin:{meta.app_label}_{meta.model_name}_change", args=[pk])
    except NoReverseMatch:
        return None


def _copy(backend: Storage, name: str) -> str:
    try:
        found, _ = _probe(
            backend, EXISTS_TIMEOUT_S, functools.partial(_has_copy, backend, name)
        )
    except Exception as e:
        # error_text: an OS error's message can hold the path, i.e. the name.
        logger.warning(f"Checking {file_tag(name)} on {backend}: {error_text(e)}")
        return "error"
    return "yes" if found else "no"


def _state(copies: Dict[str, str], rows: List[Dict[str, Any]]) -> str:
    present = [r for r in rows if copies.get(r["label"]) == "yes"]
    if any(r["off_disk"] for r in present):
        return SAFE
    if present:
        return LOCAL_ONLY
    if {"error", "skipped", "late"} & set(copies.values()):
        return UNKNOWN
    return MISSING


def sample_recent_files(
    rows: List[Dict[str, Any]],
    per_field: int = DEFAULT_SAMPLE,
    deadline_s: float = SAMPLE_DEADLINE_S,
) -> Dict[str, Any]:
    """Check the newest documents in each file field for a copy on each backend.

    Backends that did not answer are skipped rather than waited on. Rows
    are named by model, primary key and file tag, never by file name.
    """
    deadline = time.monotonic() + deadline_s
    reachable = {r["label"] for r in rows if r["reach"]["ok"]}
    per_backend = {r["label"]: {"yes": 0, "no": 0, "error": 0, "late": 0} for r in rows}
    states = {state: 0 for state in SAMPLE_STATES}
    fields: List[Dict[str, Any]] = []
    flagged: List[Dict[str, Any]] = []
    for model in STORED_FILE_MODELS:
        for field_name in _file_field_names(model):
            stored = model._default_manager.exclude(
                Q(**{f"{field_name}__isnull": True}) | Q(**{field_name: ""})
            )
            entry: Dict[str, Any] = {
                "label": f"{model.__name__}.{field_name}",
                "encrypted": isinstance(
                    model._meta.get_field(field_name), EncryptedFileField
                ),
                "files": stored.count(),
                "sampled": 0,
                "present": {r["label"]: 0 for r in rows},
            }
            fields.append(entry)
            if per_field <= 0:
                continue
            newest = stored.order_by(*_newest_first(model)).values_list(
                "pk", field_name
            )[:per_field]
            for pk, name in newest:
                doc: Dict[str, Any] = {
                    "field": entry["label"],
                    "pk": pk,
                    "admin_url": _admin_url(model, pk),
                    "tag": file_tag(name),
                    "copies": {},
                }
                if time.monotonic() > deadline:
                    doc["state"] = NOT_CHECKED
                else:
                    entry["sampled"] += 1
                    for r in rows:
                        # A backend that did not answer is skipped, and one
                        # that answers but is reached after the deadline is
                        # late; neither is waited on.
                        if r["label"] not in reachable:
                            found = "skipped"
                        elif time.monotonic() > deadline:
                            found = "late"
                        else:
                            found = _copy(r["backend"], name)
                        doc["copies"][r["label"]] = found
                        if found in per_backend[r["label"]]:
                            per_backend[r["label"]][found] += 1
                        if found == "yes":
                            entry["present"][r["label"]] += 1
                    doc["state"] = _state(doc["copies"], rows)
                states[doc["state"]] += 1
                if doc["state"] != SAFE:
                    flagged.append(doc)
    checked = sum(states.values()) - states[NOT_CHECKED]
    return {
        "per_field": per_field,
        "fields": fields,
        "states": states,
        "checked": checked,
        "per_backend": per_backend,
        "flagged": flagged,
    }


def problems(
    rows: List[Dict[str, Any]],
    unconfigured: List[Dict[str, Any]],
    sample: Optional[Dict[str, Any]],
    write_results: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, str]]:
    """What is wrong, most serious first, each as a level and a sentence."""
    found: List[Dict[str, str]] = []

    def add(level: str, text: str) -> None:
        found.append({"level": level, "text": text})

    reachable = [r for r in rows if r["reach"]["ok"]]
    if not rows:
        add(FAIL, "No storage backend is in the write path: uploads cannot be saved.")
    elif not reachable:
        add(FAIL, "No backend answers: every upload will fail.")
    for r in rows:
        if not r["reach"]["ok"]:
            add(
                WARN,
                f"{r['label']} does not answer ({r['reach']['error']}): "
                "uploads get no copy there.",
            )
        disk = r["disk"]
        if disk and disk.get("error"):
            add(WARN, f"{r['label']}'s disk usage could not be read: {disk['error']}.")
        elif disk and disk["level"] != OK:
            add(disk["level"], f"{r['label']}'s disk is {disk['percent']}% full.")
    for slot in unconfigured:
        if slot["level"] == FAIL:
            add(FAIL, f"{slot['label']}: {slot['note']}")
    if reachable and not any(r["off_disk"] for r in reachable):
        add(
            WARN,
            "Every backend that answers keeps its copies on the app's own disk. "
            "In a container that is the pod's own layer: documents uploaded "
            "here are lost when the pod goes and other pods cannot read them.",
        )
    if sample:
        states = sample["states"]
        if states[MISSING] or states[UNKNOWN]:
            add(
                WARN,
                f"{states[MISSING] + states[UNKNOWN]} of {sample['checked']} recent "
                "documents checked have no copy this process can find.",
            )
        # Copies on the app's disk come and go with pods, so only an
        # off-disk backend that answers is expected to hold every document.
        # A document safe on another off-disk backend still counts here:
        # it has lost a copy it should have.
        # A check that errored or ran out of time on an off-disk backend
        # confirms no copy either, so it is reported too: otherwise a
        # document safe nowhere but the app's disk could read as healthy.
        late = 0
        for r in reachable:
            counts = sample["per_backend"][r["label"]]
            late += counts["late"]
            if not r["off_disk"]:
                continue
            if counts["no"]:
                add(
                    WARN,
                    f"{r['label']} answers but has no copy of {counts['no']} of "
                    "the recent documents checked: its write failed or was "
                    "skipped.",
                )
            if counts["error"]:
                add(
                    WARN,
                    f"{r['label']} answers but could not be checked for "
                    f"{counts['error']} of the recent documents: their off-disk "
                    "copy is unconfirmed.",
                )
        if states[NOT_CHECKED] or late:
            cut = []
            if states[NOT_CHECKED]:
                cut.append(f"{states[NOT_CHECKED]} recent documents were not checked")
            if late:
                cut.append(f"{late} backend checks were cut off")
            add(
                WARN,
                f"{' and '.join(cut).capitalize()}: the {SAMPLE_DEADLINE_S:g}s "
                "limit came first, so a backend is slow to answer.",
            )
    if write_results:
        failed = [w for w in write_results if not w["ok"]]
        steps = {w.get("step") for w in failed}
        if failed and len(failed) == len(write_results):
            if steps == {"write"}:
                add(FAIL, "No backend accepted the test write: every upload will fail.")
            elif steps <= {"write", "read"}:
                add(
                    FAIL,
                    "No backend read back the test file: stored documents may "
                    "not open.",
                )
        for w in failed:
            step = str(w.get("step") or "write")
            add(
                WARN,
                f"{w['label']} failed the write test at the {step} step "
                f"({_STEP_MEANING.get(step, 'see the error')}): {w['error']}",
            )
    found.sort(key=lambda p: -_RANK[p["level"]])
    return found


def report(
    per_field: int = DEFAULT_SAMPLE, run_write_test: bool = False
) -> Dict[str, Any]:
    """Everything the page shows. The write test writes, so it runs only
    when asked (the page's POST)."""
    storage = stored_file_storage()
    rows = backend_rows(storage)
    unconfigured = unconfigured_slots()
    sample = sample_recent_files(rows, per_field) if per_field > 0 else None
    written = write_test(rows) if run_write_test else None
    found = problems(rows, unconfigured, sample, written)
    return {
        "backends": rows,
        "unconfigured": unconfigured,
        "sample": sample,
        "write_results": written,
        "problems": found,
        "level": worst(p["level"] for p in found),
    }


def _read(backend: Storage, name: str) -> bytes:
    with backend.open(name, "rb") as f:
        data = f.read()
    return data if isinstance(data, bytes) else data.encode()


def _remove_probe(backend: Storage, name: str) -> None:
    """Best effort: a probe a failed test left behind is removed."""
    try:
        _timed(WRITE_STEP_TIMEOUT_S, functools.partial(backend.delete, name))
    except Exception as e:
        logger.warning(f"Could not remove storage probe {name} from {backend}: {e}")


#: What a write test that stops at each step says about real documents.
_STEP_MEANING = {
    "write": "uploads get no copy there",
    "read": "its copies may not open",
    "delete": "deleting documents there fails, so data removal leaves copies",
}


def write_test(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Write a small file to each backend, read it back, and delete it.

    Each step has its own timeout. The probe is not a document and holds
    nothing personal, so its name and errors are shown as they are.
    """
    results: List[Dict[str, Any]] = []
    for r in rows:
        backend: Storage = r["backend"]
        name = f"{PROBE_PREFIX}{uuid.uuid4().hex}.txt"
        payload = f"fhi storage health probe {timezone.now().isoformat()}".encode()
        result: Dict[str, Any] = {
            "label": r["label"],
            "ok": False,
            "write_ms": None,
            "read_ms": None,
            "delete_ms": None,
            "error": None,
            # The step under way, so a failure names it: write, read or delete.
            "step": "write",
        }
        stored: Optional[str] = None
        try:
            saved, result["write_ms"] = _timed(
                WRITE_STEP_TIMEOUT_S,
                functools.partial(backend.save, name, ContentFile(payload)),
            )
            stored = saved
            result["step"] = "read"
            data, result["read_ms"] = _timed(
                WRITE_STEP_TIMEOUT_S, functools.partial(_read, backend, saved)
            )
            if data != payload:
                raise ValueError("read back different bytes than were written")
            result["step"] = "delete"
            _, result["delete_ms"] = _timed(
                WRITE_STEP_TIMEOUT_S, functools.partial(backend.delete, saved)
            )
            result["ok"] = True
            result["step"] = None
        except Exception as e:
            logger.warning(f"Storage write test failed on {r['label']}: {e}")
            result["error"] = f"{type(e).__name__}: {e}"[:300]
            # A save cut off part way may have left a file under the name.
            _remove_probe(backend, stored or name)
        results.append(result)
    return results
