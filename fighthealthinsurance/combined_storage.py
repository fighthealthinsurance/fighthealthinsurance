from django.core.files import File
from django.core.files.storage import Storage
from django.utils.deconstruct import deconstructible
from typing import Any, Optional, IO

from stopit import ThreadingTimeout as Timeout


@deconstructible(path="fighthealthinsurance.combined_storage.CombinedStorage")
class CombinedStorage(Storage):
    """A combined storage backend that uses timeouts."""
    backends: list[Storage]

    def __init__(self, *args: Storage):
        self.backends = list(args)

    def open(self, name: str, mode: str = 'rb') -> File:
        last_error: Optional[BaseException] = None
        for backend in self.backends:
            try:
                with Timeout(2.0) as _timeout_ctx:
                    return backend.open(name, mode=mode)
            except Exception as e:
                print(
                    f"Error opening from {name} {mode} on backend {backend} from {self.backends}: {e}"
                )
                last_error = e
        if last_error is not None:
            print(
                f"Opening failed on all backends -- {self.backends} -- raising {last_error}"
            )
            raise last_error
        else:
            raise Exception("No backends errored but still did not succeed.")

    def delete(self, name: str):
        last_error: Optional[BaseException] = None
        for backend in self.backends:
            try:
                with Timeout(1.0) as _timeout_ctx:
                    return backend.delete(name)
            except Exception as e:
                print(f"Error {e} deleteing {name} from {self}")
                last_error = e
        if last_error is not None:
            raise last_error

    def save(self, name: Optional[str], content: IO[Any], max_length=None):
        for backend in self.backends:
            try:
                with Timeout(4.0) as _timeout_ctx:
                    l = backend.save(
                        name=name,
                        content=content,
                        max_length=max_length)
            except Exception as e:
                print(f"Error saving {e} to {backend}")
        return l

    def url(self, *args, **kwargs):
        last_error = None
        for backend in self.backends:
            try:
                with Timeout(2.0) as _timeout_ctx:
                    return backend.url(*args, **kwargs)
            except Exception as e:
                print(f"Error saving {e} to {backend}")
        if last_error is not None:
            raise last_error
        else:
            return None

    def exists(self, name):
        if name is None:
            return False
        last_error = None
        for backend in self.backends:
            try:
                with Timeout(2.0) as _timeout_ctx:
                    r = backend.exists(name=name)
                    if r:
                        return r
            except Exception as e:
                print(f"Error {e}")
                last_error = e
        if last_error is not None:
            raise last_error
        else:
            return False
