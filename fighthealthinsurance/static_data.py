"""
Read a data file that ships in the app's static directory.

The microsites and the state help pages each keep their data as a JSON file
in fighthealthinsurance/static/. In production, and under run_local.sh,
collectstatic has copied it to STATIC_ROOT, where staticfiles_storage finds
it. In the test suites nothing has been collected, so the file has to be
read from the app's own static directory instead. Both loaders go through
here, so neither can load nothing in one of those places while working in
the other: the state help loader only asked staticfiles_storage, and every
state page answered 404 wherever collectstatic had not run.
"""

from pathlib import Path
from typing import Optional

from django.conf import settings
from django.contrib.staticfiles.storage import staticfiles_storage

from loguru import logger


def read_static_text(filename: str) -> Optional[str]:
    """The file's contents, or None when no copy of it can be found.

    Tried in order: staticfiles_storage (collectstatic has run), the app's
    static source directories, then STATIC_ROOT read directly.
    """
    try:
        with staticfiles_storage.open(filename, "r") as f:
            contents = f.read()
            if not isinstance(contents, str):
                contents = contents.decode("utf-8")
            return str(contents)
    except Exception as e:
        logger.debug(f"Could not open {filename} via staticfiles_storage: {e}")

    search_dirs = list(getattr(settings, "STATICFILES_DIRS", []))
    app_static_dir = getattr(settings, "APP_STATIC_DIR", None)
    if app_static_dir:
        search_dirs.append(app_static_dir)
    static_root = getattr(settings, "STATIC_ROOT", None)
    if static_root:
        search_dirs.append(static_root)
    for static_dir in search_dirs:
        path = Path(static_dir) / filename
        if path.exists():
            logger.debug(f"Found {filename} at {path}")
            return path.read_text()
    return None
