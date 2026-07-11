# Fight Health Insurance

A Django application to help people appeal health insurance denials.

This is the web interface for [Fight Health Insurance](https://www.fighthealthinsurance.com/), which helps users generate appeal letters when their health insurance claims are denied.

The [ML model is generated using this repo](https://github.com/fighthealthinsurance/healthinsurance-llm).

## Quick Start

### Prerequisites

- Python 3.12 (3.13 is partially supported)
- System dependencies: `tesseract-ocr`, `texlive`

On Ubuntu/Debian:
```bash
sudo apt-get install tesseract-ocr texlive libpango1.0-dev libgdk-pixbuf2.0-dev
```

### Option A: Using Conda/Micromamba (Recommended)

This handles both Python and system dependencies automatically:

```bash
# Install micromamba if you don't have conda/mamba
curl -Ls https://micro.mamba.pm/api/micromamba/linux-64/latest | tar -xvj bin/micromamba
mkdir -p ~/.local/bin && mv bin/micromamba ~/.local/bin/

# Create and activate the environment
micromamba env create -f environment.yml
micromamba activate fhi

# Run the local server
./scripts/run_local.sh
```

### Option B: Using virtualenv/venv

```bash
# Create a virtual environment (use .venv or build_venv - these are auto-detected)
python3.13 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt
pip install -r requirements-dev.txt

# Run the local server
./scripts/run_local.sh

# Note: For WebSocket support (chat interface, real-time updates),
# do NOT use the --devserver flag. The default uvicorn server
# properly supports WebSockets/ASGI, but runserver_plus does not.
```

### Connecting to an ML Backend

The app needs an ML backend to generate appeals. Options:

1. **External API (easiest)**: Set `OCTOAI_TOKEN` environment variable with an [OctoAI](https://octoai.cloud/) API key.

2. **Local model**: Use [healthinsurance-llm](https://github.com/fighthealthinsurance/healthinsurance-llm) and set:
   ```bash
   export HEALTH_BACKEND_PORT=8000
   export HEALTH_BACKEND_HOST=localhost
   ```
   Note: Requires a GPU (3090 or equivalent).

3. **Hosted generative models (Azure, Anthropic, Groq, …)**: Set the relevant
   API keys (see below). These external models are used when `use_external=True`
   (e.g. premium/chooser workflows).

### External & Azure Generative Models

The router auto-discovers any external backend whose API key (and, for Azure,
endpoint) is configured. Each model is registered under a friendly name that is
recorded for usage tracking in both the regular appeal workflow and the chooser
(e.g. `azure-openai/gpt-5`, `azure-anthropic/claude-opus-4-8`,
`anthropic/claude-opus-4-8`). Configured names are printed in the
`All loaded models` line at startup.

| Provider | Required env vars | Friendly name prefix |
| --- | --- | --- |
| Azure OpenAI (GPT) | `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_ENDPOINT` | `azure-openai/` |
| Azure AI Foundry (Claude) | `AZURE_ANTHROPIC_API_KEY`, `AZURE_ANTHROPIC_ENDPOINT` | `azure-anthropic/` |
| Anthropic (direct) | `ANTHROPIC_API_KEY` | `anthropic/` |
| Groq | `GROQ_API_KEY` | `groq/` |
| DeepInfra | `DEEPINFRA_API` | (model id) |
| Perplexity (citations) | `PERPLEXITY_API` | `sonar` |

**Set up Azure-hosted models (Claude & OpenAI):**

1. In the [Azure AI Foundry](https://ai.azure.com/) / Azure OpenAI portal, create
   a resource and **deploy** the model(s) you want (e.g. `gpt-5`,
   `claude-opus-4-8`). Note the *deployment name* — that is the model id sent on
   the wire.
2. Copy the resource's **API key** and endpoint. The two providers use different
   API surfaces:
   - **Azure OpenAI (GPT)** is OpenAI-compatible — use the v1 base URL *without*
     a trailing `/chat/completions`, e.g.
     `https://my-resource.openai.azure.com/openai/v1`.
   - **Azure AI Foundry (Claude)** uses the native Anthropic **Messages API** —
     use the base URL ending in `/anthropic`, e.g.
     `https://my-resource.services.ai.azure.com/anthropic` (the `/v1/messages`
     path is appended automatically).
3. Export the variables (or add them to `.env`):
   ```bash
   # Azure OpenAI (GPT family)
   export AZURE_OPENAI_API_KEY=...
   export AZURE_OPENAI_ENDPOINT=https://my-resource.openai.azure.com/openai/v1

   # Azure AI Foundry (Claude family)
   export AZURE_ANTHROPIC_API_KEY=...
   export AZURE_ANTHROPIC_ENDPOINT=https://my-resource.services.ai.azure.com/anthropic
   ```
4. By default the latest models are exposed (Azure OpenAI: `gpt-4.1-mini`,
   `gpt-5-mini`, `gpt-5`; Azure Claude: `claude-haiku-4-5`, `claude-sonnet-4-6`,
   `claude-opus-4-8`). If your Azure *deployment names* differ from these model
   ids, list them explicitly with `AZURE_OPENAI_MODELS` /
   `AZURE_ANTHROPIC_MODELS` (comma-separated).

**Restricting which remote models load:** Set `ENABLED_REMOTE_MODELS` to a
comma-separated list of friendly names to enable *only* those **remote**
generation models. **Always enabled** regardless of this setting: local/internal
models (e.g. `fhi-legacy`, the self-hosted backends) and context-only models
(e.g. Perplexity `sonar` used for citations). When the variable is unset (the
default), every configured model is enabled.
```bash
# Of the remote providers, load only Azure Opus and Azure GPT-5
# (local models still load):
export ENABLED_REMOTE_MODELS="azure-anthropic/claude-opus-4-8,azure-openai/gpt-5"
```

### Model backend health checks

Every deployment runs an end-to-end health check of all **enabled** model
backends (`fighthealthinsurance/ml/model_health_check.py`): each backend gets
one tiny "Reply with exactly: OK" inference through the exact same client,
credentials, endpoint, and router registration real requests use. Results are
categorized (`PASS`, `PASS_UNREGISTERED`, `NOT_CONFIGURED`, `DISABLED`,
`FAIL_MISSING_CREDENTIALS`, `FAIL_CLIENT_INIT`, `FAIL_AUTH`,
`FAIL_MODEL_NOT_FOUND`, `FAIL_RATE_LIMITED`, `FAIL_TIMEOUT`, `FAIL_NETWORK`,
`FAIL_MALFORMED_RESPONSE`, `FAIL_OTHER`), persisted for the staff status page,
and logged as a greppable summary block.

**How the deployment hook is invoked:** `scripts/start-server.sh` runs
`python manage.py check_model_backends --deploy-hook` in the
`web-actor-launch` job (the `POLLING_ACTORS=1` container in
`k8s/deploy.yaml`), right after the polling actors launch — i.e. once the
migrations job has done its work and the app image is live.

**Leader election / duplicate-run prevention:** `--deploy-hook` first claims a
row in the shared database (`ModelHealthAlertState.try_claim`, a single
atomic conditional UPDATE) keyed on the deployment identifier
(`FHI_DEPLOYMENT_ID` > `FHI_RELEASE` — baked into the image from the build
`RELEASE` arg > `FHI_VERSION`, with an hourly timestamp fallback). Exactly one
process per deployment wins; every other pod/worker/job-retry sees the claim
taken and skips, so the check runs — and the alert email is sent — at most
once per deployment. Claims expire after 6 hours, so re-deploying the same
version later still re-checks.

**Running it manually** (any pod or local shell with the env configured):
```bash
# All enabled backends; exits non-zero if any check fails
python manage.py check_model_backends

# One model (friendly registry name or wire/internal name), custom timeout
python manage.py check_model_backends --model anthropic/claude-sonnet-4-6 --timeout 15

# Skip writing rows to the results table
python manage.py check_model_backends --no-persist
```

**Alerting:** when any enabled backend fails, the deploy hook sends **one**
consolidated email to `support42@fighthealthinsurance.com` listing every
failing backend (provider, model, internal key, failure category, sanitized
error, registry state) plus the deployment id, environment, timestamp, and
where to look next. Controlled by `FHI_MODEL_HEALTH_ALERT_EMAIL`: unset =
enabled in production, disabled under `DEBUG`/tests; `1` forces on anywhere;
`0` forces off. Only the leader can send it, and only for `--deploy-hook`
runs. Error text is sanitized — API keys, Authorization/x-api-key headers,
and secret-looking environment values are redacted before logging, storing,
or emailing.

**Strict mode:** by default a failing backend does NOT fail the deployment
(healthy backends keep serving; the failure is logged and emailed). Set
`FHI_MODEL_HEALTH_STRICT=1` in environments where a dead required backend
should fail the `web-actor-launch` job instead.

**Where to inspect results:**
- Deployment logs: search for `MODEL_BACKEND_HEALTH_SUMMARY` in the
  `web-actor-launch` job output — one line per backend with category,
  latency, and sanitized detail.
- Staff dashboard: `/timbit/help/model_backends` shows, per configured model,
  enabled/disabled state, provider, registry name, internal key,
  selection-UI/reporting registration, the latest check result + timestamp,
  and the last stored generation. `/timbit/help/model_usage` shows which
  models users actually pick.
- Database: `ModelBackendHealthCheckResult` keeps one row per backend per run.

**Backfilling historical metadata:** `python manage.py
backfill_model_metadata` normalizes legacy `model_name` values on
`ProposedAppeal`/`ChooserCandidate` rows (old object reprs,
`ClassName(model)` descriptors, unambiguous bare wire ids) to the canonical
registry names the dashboard aggregates. Dry-run by default; `--apply` to
write. Ambiguous or unrecognized values are reported and left untouched, and
NULL rows stay in the dashboard's "(unknown)" bucket.

### Troubleshooting

If you see `django.core.exceptions.AppRegistryNotReady`, make sure you're using Python 3.10+.

## Tests

Tests are run through `tox`. Install it with `pip install tox` if needed.

```bash
# Run all tests
tox

# Run specific test suite
tox -e py313-django52-sync -- tests/sync/test_selenium_appeal_generation.py

# Run tests via Django management command
python manage.py run_test

# Run a specific test file
python manage.py run_test --test-file tests/async/test_appeal_file_view.py
```

Test suites:
- `async` - Async tests (run with `-n auto` for parallelization)
- `sync` - Tests which must be run synchronously (must be run synch)
- `selenium` - Selenium tests (must be run synch)
- `sync-actor` - Ray actor tests (must be run synch)

## Actor Management

The application uses Ray actors for background tasks like email polling, fax polling, and chooser refill. These actors run continuously in the background.

### Monitoring Actor Health

Check the health status of all polling actors via the REST API:

```bash
curl http://localhost:8010/ziggy/rest/actor_health_status
```

Response format:
```json
{
  "alive_actors": 3,
  "total_actors": 3,
  "details": [
    {"name": "email_polling_actor", "alive": true, "error": null},
    {"name": "fax_polling_actor", "alive": true, "error": null},
    {"name": "chooser_refill_actor", "alive": true, "error": null}
  ]
}
```

### Relaunching Actors

If actors have crashed or need to be restarted:

```bash
# Force relaunch all actors (kills existing ones first)
python manage.py launch_polling_actors --force

# Just load actors (uses existing if available)
python manage.py launch_polling_actors
```

The `--force` flag is useful when actors are in a bad state and need a clean restart.

## Code Quality

### Style (Black)

```bash
# Check formatting
black --check fighthealthinsurance fhi_users

# Auto-fix formatting
black fighthealthinsurance fhi_users
```

### Type Checking (mypy)

```bash
mypy --config-file mypy.ini -p fighthealthinsurance -p fhi_users
```

## Project Structure

- `fighthealthinsurance/` - Main Django application
  - `views.py` - Primary appeal flow views
  - `rest_views.py` - REST API endpoints
  - `common_view_logic.py` - Shared business logic
  - `chat_interface.py` - Chat-based appeal assistance
  - `models.py` - Database models
  - `forms/` - Django forms
  - `ml/` - Machine learning integration
- `fhi_users/` - User management app
- `templates/` - Django templates
- `static/` - Frontend assets (CSS, JS)
- `tests/` - Test suites (async, sync, sync-actor)

## Contributing

1. Create a feature branch from `main`
2. Make your changes with small, focused commits
3. Ensure tests pass (`tox`)
4. Ensure type checking passes (`mypy`)
5. Ensure code is formatted (`black`)
6. Submit a pull request
