from typing import IO, Any, List, Optional

from django.core.files.base import File
from django.core.files.storage import Storage

from loguru import logger
from stopit import ThreadingTimeout as Timeout

from fighthealthinsurance.log_tags import error_text, file_tag

#: How long one backend may take to store a file: this long for any file,
#: and this much longer for each megabyte, so a large upload on a slow volume
#: is not cut off mid-write. stopit only interrupts between Python
#: statements, so a backend blocked in one system call is not stopped by it.
SAVE_TIMEOUT_SECONDS = 6.0
SAVE_SECONDS_PER_MEGABYTE = 1.0


def save_timeout_seconds(content: Any) -> float:
    """The time one backend gets to store ``content``, by its size when it has one."""
    try:
        size = content.size
    except Exception:
        size = None
    if not isinstance(size, (int, float)) or size <= 0:
        return SAVE_TIMEOUT_SECONDS
    return SAVE_TIMEOUT_SECONDS + SAVE_SECONDS_PER_MEGABYTE * size / (1024 * 1024)


class CombinedStorage(Storage):
    """A combined storage backend that uses timeouts."""

    backends: List[Storage]

    def __init__(self, *args: Optional[Storage]):
        self.backends = [x for x in args if x is not None]

    def deconstruct(self):
        """Serialize into migrations without the wrapped backends.

        Written by hand rather than via @deconstructible, whose generated
        deconstruct() replays the constructor args -- that recorded each
        backend's root in every migration that touches a FileField
        (``FileSystemStorage(location="/external_data")``). Storage roots are
        deployment configuration, not schema, so pointing dev at a directory
        that exists there made makemigrations believe all twelve file fields
        had changed. The backends come from settings at runtime; migration
        state only needs to know the field uses this storage.
        """
        return ("fighthealthinsurance.combined_storage.CombinedStorage", [], {})

    def open(self, name: str, mode: str = "rb") -> File:
        last_error: Optional[BaseException] = None
        error_list: List[BaseException] = []
        for backend in self.backends:
            try:
                with Timeout(2.0) as _timeout_ctx:
                    result = backend.open(name, mode=mode)
                    assert isinstance(
                        result, File
                    ), "Opened object is not a File instance."
                    return result
            except Exception as e:
                error_list.append(e)
                logger.warning(
                    f"Error opening {file_tag(name)} {mode} on backend {backend} from {self.backends}: {error_text(e)}"
                )
                last_error = e
        if last_error is not None:
            logger.error(
                f"Opening {file_tag(name)} failed on all backends -- {self.backends} w/ errors {[error_text(e) for e in error_list]} -- raising {error_text(last_error)}"
            )
            raise last_error
        else:
            raise Exception("No backends errored but still did not succeed.")

    def delete(self, name: str) -> None:
        """Delete the file from every backend, since save() writes to all of them.

        A backend without a copy is fine. Each failure is logged, and the
        last one is raised only when no backend deleted the file.
        """
        deleted = False
        last_error: Optional[BaseException] = None
        for backend in self.backends:
            try:
                if self._delete_from(backend, name):
                    deleted = True
            except FileNotFoundError:
                continue
            except Exception as e:
                logger.error(
                    f"Error {error_text(e)} deleting {file_tag(name)} from {backend}"
                )
                last_error = e
        if last_error is not None and not deleted:
            raise last_error

    def _delete_from(self, backend: Storage, name: str) -> bool:
        """Delete the backend's copy; False when it had none."""
        had = False
        with Timeout(2.0) as timeout_ctx:
            # FileSystemStorage.delete returns quietly for a missing file.
            had = backend.exists(name)
            if had:
                backend.delete(name)
        # stopit swallows the timeout, so check the context instead.
        if not timeout_ctx:
            raise TimeoutError(f"Timed out deleting {file_tag(name)} from {backend}")
        return had

    def save(
        self, name: Optional[str], content: IO[Any], max_length: Optional[int] = None
    ) -> str:
        l: Optional[str] = None
        last_error: Optional[BaseException] = None
        error_list: List[BaseException] = []
        seconds = save_timeout_seconds(content)
        for backend in self.backends:
            stored: Optional[str] = None
            target_name = name if l is None else l
            # Whether this name is already taken on the backend BEFORE we
            # write. A timed-out save never returns the name it chose, and
            # FileSystemStorage picks an alternative when the name is taken,
            # so this is the only way to tell a partial file we just created
            # (name was free -> the write targets exactly this name) from a
            # pre-existing document (name taken -> the write went to some
            # other name we can't see, and this one must not be touched).
            created_here = False
            try:
                created_here = not self._name_in_use(backend, target_name)
            except Exception:
                created_here = False  # Unsure: never delete.
            try:
                with Timeout(seconds) as timeout_ctx:
                    # Only overwrite l if we have not yet saved but always try and save.
                    if l is None:
                        l = backend.save(
                            name=name, content=content, max_length=max_length
                        )
                    else:
                        # One name everywhere, so open() and delete() find every copy.
                        stored = backend.save(
                            name=l, content=content, max_length=max_length
                        )
            except Exception as e:
                logger.warning(f"Error saving {error_text(e)} to {backend}")
                error_list.append(e)
                last_error = e
            # stopit swallows the timeout, so check the context instead. A
            # write cut off part way leaves a partial file under the name the
            # others will use, and open() returns the first copy it finds, so
            # that partial copy is removed rather than kept -- but ONLY when
            # the name was free before this write, since otherwise the bytes
            # under it are a different, pre-existing document.
            if not timeout_ctx:
                timed_out = TimeoutError(
                    f"Timed out after {seconds:g}s saving {file_tag(name or '')} to {backend}"
                )
                logger.warning(f"Error saving {error_text(timed_out)} to {backend}")
                error_list.append(timed_out)
                last_error = timed_out
                if target_name and created_here:
                    self._remove_partial_copy(backend, target_name)
                else:
                    logger.warning(
                        f"{backend} may hold a partial copy from a timed-out save of "
                        f"{file_tag(target_name or '')} under a name we cannot see; "
                        "left in place so a pre-existing document is not deleted"
                    )
                continue
            if l is not None and stored is not None and stored != l:
                self._remove_renamed_copy(backend, l, stored)
        if l is None:
            errors = [error_text(e) for e in error_list]
            logger.error(
                f"Saving to all backends {self.backends} failed with {errors}!"
            )
            raise Exception(f"Failed to save to any backend w/ errors {errors}")
        else:
            return l

    def _name_in_use(self, backend: Storage, name: Optional[str]) -> bool:
        """Whether ``name`` already names a file on ``backend``, under a short
        timeout. A name we can't check is reported in use, so a save that
        times out over it is never followed by a delete."""
        if not name:
            return True
        with Timeout(2.0) as timeout_ctx:
            in_use = backend.exists(name)
        if not timeout_ctx:
            raise TimeoutError(f"Timed out checking {file_tag(name)} on {backend}")
        return bool(in_use)

    def _remove_partial_copy(self, backend: Storage, name: Optional[str]) -> None:
        """Best effort: a file the timed-out write left behind is removed, so a
        later open() cannot hand out part of a document as the whole."""
        if not name:
            return
        try:
            self._delete_from(backend, name)
        except Exception as e:
            logger.warning(
                f"{backend} may hold a partial copy of {file_tag(name)} that could not be removed: {error_text(e)}"
            )

    def _remove_renamed_copy(self, backend: Storage, name: str, stored: str) -> None:
        """No row records the other name, so nothing would ever delete that copy.

        The backend's existing file under the shared name is left alone.
        """
        try:
            self._delete_from(backend, stored)
        except Exception as e:
            logger.warning(
                f"{backend} stored {file_tag(name)} as {file_tag(stored)} and could not remove it: {error_text(e)}"
            )
            return
        logger.warning(
            f"{backend} stored {file_tag(name)} as {file_tag(stored)}; removed {file_tag(stored)}"
        )

    def url(self, name: Optional[str]) -> str:
        last_error: Optional[BaseException] = None
        for backend in self.backends:
            try:
                with Timeout(2.0) as _timeout_ctx:
                    return backend.url(name)
            except Exception as e:
                logger.error(f"Error getting URL {error_text(e)} from {backend}")
                last_error = e
        if last_error is not None:
            raise last_error
        else:
            raise Exception("No backends?")

    def exists(self, name: str) -> bool:
        last_error: Optional[BaseException] = None
        for backend in self.backends:
            try:
                with Timeout(2.0) as _timeout_ctx:
                    r = backend.exists(name=name)
                    if r:
                        return r
            except Exception as e:
                logger.error(
                    f"Error {error_text(e)} checking {file_tag(name)} on {backend}"
                )
                last_error = e
        if last_error is not None:
            raise last_error
        else:
            return False
