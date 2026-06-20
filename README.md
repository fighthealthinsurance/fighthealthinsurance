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
`anthropic/claude-opus-4-7`). Configured names are printed in the
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
2. Copy the resource's **API key** and its **OpenAI-compatible v1 endpoint**.
   The endpoint is the base URL *without* a trailing `/chat/completions`, e.g.
   `https://my-resource.openai.azure.com/openai/v1` (Azure OpenAI) or
   `https://my-resource.services.ai.azure.com/openai/v1` (Foundry/Claude).
3. Export the variables (or add them to `.env`):
   ```bash
   # Azure OpenAI (GPT family)
   export AZURE_OPENAI_API_KEY=...
   export AZURE_OPENAI_ENDPOINT=https://my-resource.openai.azure.com/openai/v1

   # Azure AI Foundry (Claude family)
   export AZURE_ANTHROPIC_API_KEY=...
   export AZURE_ANTHROPIC_ENDPOINT=https://my-resource.services.ai.azure.com/openai/v1
   ```
4. By default the latest models are exposed (Azure OpenAI: `gpt-4.1-mini`,
   `gpt-5-mini`, `gpt-5`; Azure Claude: `claude-haiku-4-5`, `claude-sonnet-4-6`,
   `claude-opus-4-8`). If your Azure *deployment names* differ from these model
   ids, list them explicitly with `AZURE_OPENAI_MODELS` /
   `AZURE_ANTHROPIC_MODELS` (comma-separated).

**Restricting which models load:** Set `ENABLED_REMOTE_MODELS` to a
comma-separated list of friendly names to enable *only* those models. When the
variable is unset (the default), every configured model is enabled.
```bash
# Only load Azure Opus and Azure GPT-5, nothing else:
export ENABLED_REMOTE_MODELS="azure-anthropic/claude-opus-4-8,azure-openai/gpt-5"
```

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
