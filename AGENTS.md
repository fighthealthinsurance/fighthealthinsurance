# Agent instructions

Repo-wide instructions for AI coding agents (Codex, Copilot, Cursor, Claude,
and anything else that reads this file). The full guidance — build commands,
architecture map, async/ORM bridge rules, test organization, code style —
lives in [CLAUDE.md](CLAUDE.md). Read that first and follow all of it; the
rules below are the ones agents most often get wrong, repeated here so they
are never missed.

## Non-negotiables

- **Run tests only via tox** (`tox -e py313-django52-async-unit`, etc.).
  Never invoke pytest directly or through `.tox/` virtualenv paths.
- **Tests must be hermetic to credentials and live backends.** Never read
  real API keys, endpoints, or backend hosts from the surrounding
  environment, and never hardcode one into a test. A test needing a
  credential uses `patch.dict(os.environ, ...)` scoped to that test — never
  a module-level `os.environ[...]` write or `setdefault`, which leaks into
  every later test in the process. The async-unit conftest scrubs the ML
  provider env vars at collection start, and
  `test_ml_router.py::TestRouterHermeticity` fails if a backend registers
  from the environment anyway; when adding a new env-configured backend to
  `ml_models.py`, add its env vars to `_AMBIENT_BACKEND_ENV_VARS` in
  `tests/async-unit/conftest.py`. A unit test that makes a real network
  call to a provider is a bug, even when it passes.
- **No secrets anywhere in the repo** — not in code, tests, fixtures,
  migrations, or docs, and not as "temporary" values written process-wide
  via `os.environ`.
- **Format and type-check via tox**: `tox -e py313-black` to check
  formatting (fix with `black fighthealthinsurance fhi_users`),
  `tox -e mypy` for types.
- **Match the file you are editing** — naming, import style, comment
  density, and the async/ORM bridge conventions described in CLAUDE.md.
