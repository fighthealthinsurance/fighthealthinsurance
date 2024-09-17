# fighthealthinsurance

This is the django interface for [Fight Health Insurance a health insurance appeal bot](https://www.fighthealthinsurance.com/).

Currently this has both the frontend (HTML & javascript) along with a bunch of "middle-ware" type logic (forms, database interactions, etc.)

In the future this may be seperated out into different projects.

The [ML model is generated using this repo](https://github.com/totallylegitco/healthinsurance-llm).

## Running Locally

The `run_local.sh` can be used to launch django to run locally.

To really test changes you'll likely want access to a model, one [option is using this repo](https://github.com/totallylegitco/healthinsurance-llm) and setting `HEALTH_BACKEND_PORT` to `8000` and `HEALTH_BACKEND_HOST` to `localhost`. Deploying locally requires ~ GPU equivalent to a 3090.

If you don't have a GPU handy the other option is to use an external model. The current one setup by default is [octoai](https://octoai.cloud/) & you can get a free API key with enough credits to run locally. You'll want to set the enviornment variable `OCTOAI_TOKEN` to the value of your API key.
