# Fight Health Insurance

A Django application to help people appeal health insurance denials.

This is the web interface for [Fight Health Insurance](https://www.fighthealthinsurance.com/), which helps users generate appeal letters when their health insurance claims are denied.

The [ML model is generated using this repo](https://github.com/fighthealthinsurance/healthinsurance-llm).

## Quick Start

### Prerequisites

- Python 3.10, 3.11, or 3.12
- System dependencies: `tesseract-ocr`, `texlive`, `libcairo2-dev`

On Ubuntu/Debian:
```bash
sudo apt-get install tesseract-ocr texlive libcairo2-dev libpango1.0-dev libgdk-pixbuf2.0-dev
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
python3.11 -m venv .venv
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
tox -e py311-django50-sync -- tests/sync/test_selenium_appeal_generation.py

# Run tests via Django management command
python manage.py run_test

# Run a specific test file
python manage.py run_test --test-file tests/async/test_appeal_file_view.py
```

Test suites:
- `async` - Async tests (run with `-n auto` for parallelization)
- `sync` - Synchronous tests
- `sync-actor` - Ray actor tests

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
