# fighthealthinsurance

This is the django interface for [Fight Health Insurance a health insurance appeal bot](https://www.fighthealthinsurance.com/)(FHI).

Currently this has both the frontend (HTML & javascript) along with a bunch of "middle-ware" type logic (forms, database interactions, etc.)

In the future this may be seperated out into different projects.

The [ML model is generated using this repo](https://github.com/totallylegitco/healthinsurance-llm).

## Running Locally

FHI requires Python 3.10 or 3.11. You can check the version of Python on your system by typing `python`. If you have a different version of Python two easy ways to get a different version of Python on your system are Anaconda or UV. Once you have a supported version of Python installed, you'll want to install all of the requirements for the project with `pip install -r requirements.txt`. You can do this inside a virtualenv or conda env.

Make sure your virtual env is one of the ones expected by `run_local` (e.g. `.venv` or `build_venv`.)

If you get `django.core.exceptions.AppRegistryNotReady: The translation infrastructure cannot be initialized before the apps registry is ready. Check that you don't make non-lazy gettext calls at import time.` make sure you are running Python 3.10 or higher.

The `./scripts/run_local.sh` can be used to launch django to run locally.

To really test changes you'll likely want access to a model, one [option is using this repo](https://github.com/totallylegitco/healthinsurance-llm) and setting `HEALTH_BACKEND_PORT` to `8000` and `HEALTH_BACKEND_HOST` to `localhost`. Deploying locally requires ~ GPU equivalent to a 3090.

If you don't have a GPU handy the other option is to use an external model. The current one setup by default is [octoai](https://octoai.cloud/) & you can get a free API key with enough credits to run locally. You'll want to set the enviornment variable `OCTOAI_TOKEN` to the value of your API key.

## Tests

Tests are run through `tox`. If you dont have tox you can `pip` or `uv` install it. The tests are broken up into sync and async. You can run all tests by running `tox`.

An example of running just one test suite is `tox -e py311-django50-sync -- tests/sync/test_selenium_appeal_generation.py`


Run test:

```bash
python manage.py run_test
```

Run test specific file:
```bash
python manage.py run_test --test-file [file path]
```
ex: python manage.py run_test --test-file tests/async/test_appeal_file_view.py


## Style

We use black for style checking, and it can auto apply many style fixes so if you get a style error just run `black fighthealthinsurance`.

## Types

We use mypy for type checking.
