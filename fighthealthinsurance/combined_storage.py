import hashlib
from typing import IO, Any, List, Optional

from django.core.files.base import File
from django.core.files.storage import Storage

from loguru import logger
from stopit import ThreadingTimeout as Timeout


def _name_tag(name: Optional[str]) -> str:
    """A short hash to tell stored names apart in logs without writing them."""
    return hashlib.sha256((name or "").encode()).hexdigest()[:8]


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
                    f"Error opening from {name} {mode} on backend {backend} from {self.backends}: {e}"
                )
                last_error = e
        if last_error is not None:
            logger.error(
                f"Opening failed on all backends -- {self.backends} w/ errors {error_list}-- raising {last_error}"
            )
            raise last_error
        else:
            raise Exception("No backends errored but still did not succeed.")

    def delete(self, name: str) -> None:
        """Delete the file from every backend, since save() writes to all of them.

        A backend without a copy is fine. Any other failure is raised once
        every backend has been tried, so callers learn a copy may remain.
        """
        last_error: Optional[BaseException] = None
        for backend in self.backends:
            try:
                with Timeout(2.0) as timeout_ctx:
                    backend.delete(name)
                # stopit swallows the timeout, so check the context instead.
                if not timeout_ctx:
                    raise TimeoutError(
                        f"Timed out deleting {_name_tag(name)} from {backend}"
                    )
            except FileNotFoundError:
                continue
            except Exception as e:
                logger.error(f"Error {e} deleting {name} from {backend}")
                last_error = e
        if last_error is not None:
            raise last_error

    def save(
        self, name: Optional[str], content: IO[Any], max_length: Optional[int] = None
    ) -> str:
        l: Optional[str] = None
        last_error: Optional[BaseException] = None
        error_list: List[BaseException] = []
        for index, backend in enumerate(self.backends):
            try:
                with Timeout(6.0) as _timeout_ctx:
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
                        if stored != l:
                            logger.warning(
                                f"Backend {index} ({type(backend).__name__}) stored "
                                f"name {_name_tag(l)} as {_name_tag(stored)}"
                            )
            except Exception as e:
                logger.warning(f"Error saving {e} to {backend}")
                error_list.append(e)
                last_error = e
        if l is None:
            logger.error(
                f"Saving to all backends {self.backends} failed with {error_list}!"
            )
            raise Exception(
                f"Failed to save to any backend w/ errors {error_list} -- last error {last_error}"
            )
        else:
            return l

    def url(self, name: Optional[str]) -> str:
        last_error: Optional[BaseException] = None
        for backend in self.backends:
            try:
                with Timeout(2.0) as _timeout_ctx:
                    return backend.url(name)
            except Exception as e:
                logger.error(f"Error getting URL {e} from {backend}")
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
                logger.error(f"Error {e}")
                last_error = e
        if last_error is not None:
            raise last_error
        else:
            return False
