# State Medicare and Medicaid resources

`extract_info.py` builds a state-by-state index of who helps with Medicare
and Medicaid appeals, and what each of them says about it. Nothing here runs
in the app; the files are data checked into `data/`:

| File | What is in it | How it is made |
|---|---|---|
| `data/state_medicaid_medicare_resources.json` | For each state and DC, one URL per resource category (`SEARCH_CATEGORIES` in the script: SHIP, Senior Medicare Patrol, protection and advocacy, fair hearing, managed care review, preferred drug list) plus the LSC legal aid grantee profiles | DuckDuckGo searches and the LSC grantee page. An existing file is kept; only empty entries are searched for again. |
| `data/extracted_state_resources.json` | For each state, the agencies on those pages: name, category, phone, URL, filing deadline and short notes | Each page is fetched, trimmed to its main text, and read by a model through [Instructor](https://python.useinstructor.com/), which returns `AppealsResource` objects. Written after every state, so a stopped run leaves a partial file. |

## Run it

```sh
pip install -r scripts/state_data/requirements.txt
python scripts/state_data/extract_info.py
```

The model is any OpenAI-compatible server. By default the script talks to a
local one, plain HTTP, and takes the same variables as the app's self-hosted
backend (see [docs/ml-backends.md](../../docs/ml-backends.md)), read from
the environment or from `scripts/.env`:

| Variable | Default | Meaning |
|---|---|---|
| `HEALTH_BACKEND_HOST`, `HEALTH_BACKEND_PORT` | `localhost`, `13305` (Lemonade; Ollama is 11434) | Local server, called at `http://HOST:PORT/v1` |
| `HEALTH_BACKEND_URL` | unset | A full base URL, such as `https://api.deepinfra.com/v1/openai`; when set it wins over host and port |
| `HEALTH_BACKEND_API_KEY` | a placeholder | The key a hosted endpoint wants; a local server ignores it |
| `HEALTH_BACKEND_MODEL` | `totallylegitco/fighthealthinsurance_model_v0.5` | The model name the server expects |

A full run reads about 430 pages, one model call each; the pages are public
and no patient data is involved, so a hosted model is fine for them.

## Scheduled refresh

The `Update State Resources` workflow
([`.github/workflows/update-state-resources.yml`](../../.github/workflows/update-state-resources.yml))
runs the script on the 2nd of every month, and on demand from the Actions
tab, against DeepInfra with the model the app's router uses there. It opens
a pull request with the changed data files for review rather than committing
them; if the run kept fewer than half the previously extracted resources
(a dead endpoint or a rejected key looks like an empty state, not an error)
it fails instead of opening one.

It needs a repository secret with the model key: `STATE_RESOURCES_API_KEY`,
or `DEEPINFRA_API`. The repository variables `STATE_RESOURCES_BACKEND_URL`
and `STATE_RESOURCES_MODEL` move it to another endpoint or model. As with the
browser data refresh, the `BROWSERSLIST_UPDATE_TOKEN` secret, when present,
opens the pull request so that CI runs on it.
