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
  - `patient_views.py` - Patient dashboard (call logs, evidence, appeals tracking)
  - `patient_export_views.py` - CSV export functionality
  - `rest_views.py` - REST API endpoints
  - `common_view_logic.py` - Shared business logic
  - `chat_interface.py` - Chat-based appeal assistance
  - `followup_digest.py` - Weekly digest email sender
  - `calendar_emails.py` - Calendar reminder email sender
  - `models.py` - Database models
  - `forms/` - Django forms
  - `ml/` - Machine learning integration
  - `management/commands/` - CLI commands (send_followup_digest, send_calendar_reminders, etc.)
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
