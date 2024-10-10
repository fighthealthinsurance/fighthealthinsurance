from stopit import ThreadingTimeout as Timeout
from django.core.files.storage import Storage
from django.utils.deconstruct import deconstructible


@deconstructible(path="fighthealthinsurance.combined_storage.CombinedStorage")
class CombinedStorage(Storage):
    """A combined storage backend that uses timeouts."""

    def __init__(self, *args):
        self.backends = args

    def open(self, *args, **kwargs):
        last_error = None
        if name is None:
            return
        for backend in self.backends:
            try:
                with Timeout(2.0) as timeout_ctx:
                    return backend.open(*args, **kwargs)
            except Exception as e:
                print(f"Error {e}")
                last_error = e
        raise last_error

    def delete(self, *args, **kwargs):
        last_error = None
        if name is None:
            return
        for backend in self.backends:
            try:
                with Timeout(2.0) as timeout_ctx:
                    return backend.open(*args, **kwargs)
            except Exception as e:
                print(f"Error {e}")
                last_error = e
        raise last_error

    def save(self, *args, **kwargs):
        for backend in self.backends:
            try:
                print(f"Called with {args} {kwargs}")
                with Timeout(2.0) as timeout_ctx:
                    backend.save(*args, **kwargs)
            except Exception as e:
                print(f"Error saving {e}")

    def exists(self, name):
        if name is None:
            return False
        last_error = None
        for backend in self.backends:
            try:
                with Timeout(2.0) as timeout_ctx:
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
