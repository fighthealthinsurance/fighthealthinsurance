from typing import Optional, List

from django.core.files.base import File
from django.core.files.storage import Storage
from django.utils.deconstruct import deconstructible

from stopit import ThreadingTimeout as Timeout
from typing import Any, Optional, IO
from loguru import logger


@deconstructible(path="fighthealthinsurance.combined_storage.CombinedStorage")
class CombinedStorage(Storage):
    """A combined storage backend that uses timeouts."""

    backends: List[Storage]

    def __init__(self, *args: Optional[Storage]):
        self.backends = [x for x in args if x is not None]

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
        last_error: Optional[BaseException] = None
        for backend in self.backends:
            try:
                with Timeout(2.0) as _timeout_ctx:
                    return backend.delete(name)
            except Exception as e:
                logger.error(f"Error {e} deleting {name} from {self}")
                last_error = e
        if last_error is not None:
            raise last_error

    def save(
        self, name: Optional[str], content: IO[Any], max_length: Optional[int] = None
    ) -> str:
        l: Optional[str] = None
        last_error: Optional[BaseException] = None
        error_list: List[BaseException] = []
        for backend in self.backends:
            try:
                with Timeout(6.0) as _timeout_ctx:
                    # Only overwrite l if we have not yet saved but always try and save.
                    if l is None:
                        l = backend.save(
                            name=name, content=content, max_length=max_length
                        )
                    else:
                        backend.save(name=name, content=content, max_length=max_length)
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
