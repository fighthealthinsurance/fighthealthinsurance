# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Fight Health Insurance is a Django application that helps patients appeal health insurance denials. It combines a web interface with ML model integration to generate persuasive appeal letters. The system supports both synchronous and asynchronous workflows, with WebSocket streaming for real-time appeal generation.

## Build & Development Commands

```bash
# Install dependencies (Option A: Conda - recommended)
micromamba env create -f environment.yml
micromamba activate fhi

# Install dependencies (Option B: venv)
python3.13 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt

# Run development server
./scripts/run_local.sh

# Run all tests via tox
tox

# Run specific test suites
tox -e py313-django60-sync       # Synchronous tests
tox -e py313-django60-async      # Async tests (parallelized)
tox -e py313-django60-sync-actor # Ray actor tests

# Run single test file
python manage.py run_test --test-file tests/async/test_appeal_file_view.py

# Code formatting
black --check fighthealthinsurance fhi_users  # Check
black fighthealthinsurance fhi_users          # Fix

# Type checking
mypy --config-file mypy.ini -p fighthealthinsurance -p fhi_users

# Database migrations
python manage.py makemigrations
python manage.py migrate
python manage.py loaddata initial followup plan_source
```

## Architecture

### Core Data Flow
1. User submits denial information via web form or chat interface
2. `common_view_logic.py` orchestrates the appeal workflow
3. `generate_appeal.py` calls ML backend (OctoAI or local) to create appeal letter
4. WebSocket consumers in `websockets.py` stream results in real-time
5. Appeals can be sent via email, fax, or downloaded as PDF

### Key Modules
- **views.py** - Primary web view handlers (~1700 lines)
- **rest_views.py** - REST API endpoints (DRF with drf-spectacular)
- **common_view_logic.py** - Main business logic for processing denials (~2100 lines)
- **chat_interface.py** - AI-powered chat assistance (~1500 lines)
- **websockets.py** - Django Channels WebSocket consumers for streaming
- **models.py** - Database models (~30+ models with encrypted PII fields)
- **ml/** - ML integration layer with routing, citations, and model helpers

### User System
- `fhi_users/models.py` defines PatientUser and ProfessionalUser
- UserDomain provides multi-tenant support for healthcare professionals
- 2FA/MFA support via django-mfa2 and django-two-factor-auth

### Async Processing
- Ray actors (`*_actor.py`) for distributed background tasks
- WebSocket streaming for long-running ML operations
- async/await patterns with sync-to-async bridges via asgiref

### Frontend
- React 19 + TypeScript components in `fighthealthinsurance/static/js/`
- Mantine UI component library
- Webpack bundler; to rebuild, run `npm run build` from the `fighthealthinsurance/static/js/` directory

## Important Patterns

### Helper Classes
Business logic is organized into large helper classes:
- `AppealsBackendHelper` - Appeal generation logic
- `DenialCreatorHelper` - Denial creation workflow
- `FaxHelperResults` - Fax sending with results

### Encrypted Fields
Sensitive data uses `django-encrypted-model-fields`. Look for `encrypted_` prefixed fields in models.

### Environment Configuration
Django uses `django-configurations` with classes: Dev, TestSync, Test, TestActor, Prod. Set via `DJANGO_CONFIGURATION` env var.

### API Documentation
REST API uses drf-spectacular for OpenAPI docs. API endpoints are under `/ziggy/rest/` prefix.

## Environment Variables

Required for ML:
- `OCTOAI_TOKEN` - OctoAI API key (for cloud ML)
- `HEALTH_BACKEND_HOST` / `HEALTH_BACKEND_PORT` - Local ML backend

Development:
- `RECAPTCHA_TESTING=true` - Disable reCAPTCHA locally
- `OAUTHLIB_RELAX_TOKEN_SCOPE=1` - OAuth scope relaxation

## Test Organization

Tests are in `tests/` with subdirectories:
- `async/` - Async tests (use pytest-asyncio)
- `sync/` - Synchronous tests
- `sync-actor/` - Ray actor tests
- `async-unit/` - Unit tests
- `selenium/` - Sync selenium tests

Fixtures live in `fighthealthinsurance/fixtures/` (initial, followup, plan_source).
