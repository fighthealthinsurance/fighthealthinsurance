# ML backends

The app needs at least one model backend to generate appeals. Every backend is
a class in [`fighthealthinsurance/ml/ml_models.py`](../fighthealthinsurance/ml/ml_models.py),
and the router in [`fighthealthinsurance/ml/ml_router.py`](../fighthealthinsurance/ml/ml_router.py)
registers each one whose configuration is present. A backend with no
configuration is skipped.

Early versions of the model were trained with
[healthinsurance-llm](https://github.com/fighthealthinsurance/healthinsurance-llm).
Current models are trained in a private fork of that repository.

## How the settings are read

The backend classes read their settings with `os.getenv`, so **export them in
the shell that starts the server**. The app does not load `.env` into the
process environment. A value that is only in `.env` is invisible to the
backends. (`.env` is read only for settings fetched through
`get_env_variable` in `fighthealthinsurance/env_utils.py`, via
python-decouple.)

## Which model serves which request

- **Appeals try internal models first.** External (hosted) models are the
  backup tier, and only when the user left the external-models consent on
  (`Denial.use_external`, which defaults to on). See `make_appeals` in
  `fighthealthinsurance/generate_appeal.py`. With no internal backend
  configured, a user who turns that consent off gets no appeal.
- **The chooser always opts into external models**, because its inputs are
  synthetic, with no patient data (`fighthealthinsurance/chooser_tasks.py`).
- **`fhi-legacy` is for appeals and prior auth.** It is an appeal-text
  fine-tune (`supports_general_instructions` returns False), so the router
  keeps it out of chat, extraction and other instruction-following work. The
  router falls back to it there, with a logged warning, only when no
  general-purpose backend is registered (`MLRouter._general_purpose_only`).

## Options

1. **Hosted API (simplest).** Export `ANTHROPIC_API_KEY` (or `DEEPINFRA_API`,
   or one of the Azure pairs below). Keep the routing above in mind: hosted
   models serve appeals only as the backup tier.

2. **Self-hosted model.** Serve the model with an OpenAI-compatible server
   (the team uses vLLM), then point the legacy internal backend at it:

   ```bash
   export HEALTH_BACKEND_HOST=localhost
   export HEALTH_BACKEND_PORT=8001
   # Only if the served model name is not the default
   # (totallylegitco/fighthealthinsurance_model_v0.5):
   export HEALTH_BACKEND_MODEL=your-served-model-name
   ```

   - The app calls `http://HOST:PORT/v1/chat/completions`, so the server must
     expose the OpenAI `/v1` API.
   - `HEALTH_BACKEND_PORT` defaults to `80` when unset.
   - Do not use port 8000: `scripts/run_local.sh` serves the web app there.
   - The default model is a 7B fine-tune. Serving it needs a GPU; the
     project's guidance has been an RTX 3090 (24 GB) or equivalent.
   - The current internal models use two more backends with the same shape:
     `NEW_HEALTH_BACKEND_HOST` / `_PORT` / `_MODEL` and
     `ALPHA_HEALTH_BACKEND_HOST` / `_PORT` / `_MODEL`.
   - On a machine where `kubectl` can see the team's cluster,
     `scripts/run_local.sh` port-forwards the cluster backends and sets
     `HEALTH_BACKEND_*` and `NEW_HEALTH_BACKEND_*` itself, overriding what
     you exported (see [local-development.md](local-development.md)).
   - Local model platforms such as [Ollama](https://ollama.com/) and
     [Lemonade](https://lemonade-server.ai/) serve the OpenAI API under `/v1`
     too, so the same variables can point at them. For example, Lemonade on
     its default port:

     ```bash
     export HEALTH_BACKEND_HOST=localhost
     export HEALTH_BACKEND_PORT=13305   # Ollama's default is 11434
     export HEALTH_BACKEND_MODEL=full_model_name
     ```

     Set `HEALTH_BACKEND_MODEL` to the model's full name, exactly as the
     server lists it at `/v1/models`, tag or quantization suffix included.
     The health sweep marks the backend unhealthy when that name is not in
     the list (`model_is_ok` in `ml_models.py`).
   - A general model served this way still fills the `fhi-legacy` slot, so
     chat and extraction use it only as the fallback described in
     [Which model serves which request](#which-model-serves-which-request).

3. **Hosted generative models (Azure, Anthropic, and others).** Set the
   relevant API keys (see the next section). These models are used when the
   user leaves the external-models consent on, as a fallback after the
   internal models for appeals.

## External and Azure generative models

The router auto-discovers any external backend whose API key (and, for Azure,
endpoint) is configured. Each model is registered under a friendly name that is
recorded for usage tracking in both the regular appeal workflow and the chooser
(for example `azure-openai/gpt-5.5`, `azure-anthropic/claude-opus-4-8`,
`anthropic/claude-opus-4-8`). Configured names are printed in the
`All loaded models` log line when a process first builds the router. The
router is built lazily, on the first model call or by
`python manage.py check_model_backends`, not at import. Context-only models
such as `sonar` are not listed in that line.

| Provider | Class in `ml_models.py` | Required env vars | Friendly name (or prefix) |
| --- | --- | --- | --- |
| Azure OpenAI (GPT) | `RemoteAzureOpenAI` | `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_ENDPOINT` | `azure-openai/` |
| Azure AI Foundry (Claude) | `RemoteAzureClaude` | `AZURE_ANTHROPIC_API_KEY`, `AZURE_ANTHROPIC_ENDPOINT` | `azure-anthropic/` |
| Anthropic (direct) | `RemoteAnthropic` | `ANTHROPIC_API_KEY` | `anthropic/` |
| DeepInfra | `DeepInfra` | `DEEPINFRA_API` | the model id |
| Perplexity (citations only) | `RemotePerplexity` | `PERPLEXITY_API` | `sonar` |

Perplexity is context-only: it finds citations and never generates an appeal
by itself. When DeepInfra or Perplexity has no key, building the router logs a
`Skipping model ... No token found` warning. That is expected, not a fault.

**Set up Azure-hosted models (Claude and OpenAI):**

1. In the [Azure AI Foundry](https://ai.azure.com/) / Azure OpenAI portal,
   create a resource and **deploy** the model(s) you want (for example
   `gpt-5.5`, `claude-opus-4-8`). Note the *deployment name*: that is the
   model id sent on the wire.
2. Copy the resource's **API key** and endpoint. The two providers use
   different API surfaces:
   - **Azure OpenAI (GPT)** is OpenAI-compatible. Use the v1 base URL, for
     example `https://my-resource.openai.azure.com/openai/v1`. A trailing
     `/chat/completions` is tolerated and stripped.
   - **Azure AI Foundry (Claude)** uses the native Anthropic **Messages API**.
     Use the base URL ending in `/anthropic`, for example
     `https://my-resource.services.ai.azure.com/anthropic`. The
     `/v1/messages` path is appended automatically. The bare resource host
     and a URL ending in `/v1/messages` are normalized to the same thing.
3. Export the variables in the environment that starts the server (a `.env`
   entry is not enough, see [How the settings are read](#how-the-settings-are-read)):

   ```bash
   # Azure OpenAI (GPT family)
   export AZURE_OPENAI_API_KEY=...
   export AZURE_OPENAI_ENDPOINT=https://my-resource.openai.azure.com/openai/v1

   # Azure AI Foundry (Claude family)
   export AZURE_ANTHROPIC_API_KEY=...
   export AZURE_ANTHROPIC_ENDPOINT=https://my-resource.services.ai.azure.com/anthropic
   ```

4. By default the deployments provisioned on the project's Azure resource are
   registered. The lists live in `DEFAULT_MODELS` on `RemoteAzureOpenAI` and
   `RemoteAzureClaude`; today they are `gpt-5.5` for Azure OpenAI, and
   `claude-opus-4-8` and `claude-fable-5` for Azure Claude. If your deployment
   names differ, list them explicitly with `AZURE_OPENAI_MODELS` /
   `AZURE_ANTHROPIC_MODELS` (comma-separated).
   - The override **replaces** the default list.
   - Every overridden deployment gets the `custom` routing tier, which ranks
     below `frontier` and `premium`. Routing ranks by tier before cost, so
     re-listing a default model through the override can change which models
     the default fan-out picks (`MLRouter.best_external_models`).

**Restricting which remote models load:** set `ENABLED_REMOTE_MODELS` to a
comma-separated list of names to enable *only* those **remote** generation
models.

- Friendly names and internal names (for example an Azure deployment name)
  both match.
- A name that matches nothing is silently ignored, so check the
  `All loaded models` line.
- **Always enabled** regardless of this setting: local/internal models (for
  example `fhi-legacy` and the other self-hosted backends) and context-only
  models (for example Perplexity `sonar`, used for citations).
- When the variable is unset or blank (the default), every configured model
  is enabled.

```bash
# Of the remote providers, load only Azure Opus and Azure GPT-5.5
# (local models still load):
export ENABLED_REMOTE_MODELS="azure-anthropic/claude-opus-4-8,azure-openai/gpt-5.5"
```

**Testing remote model features with local models:** Azure OpenAI uses the
standard OpenAI API format, so its settings can point at a locally hosted,
OpenAI-compatible server (such as the ones in option 2). The app then calls
that model through its remote-model code, which allows more testing without
token costs.

```bash
export AZURE_OPENAI_ENDPOINT=http://localhost:13305/v1   # your server's /v1 base URL
export AZURE_OPENAI_API_KEY=your-local-key-or-dummy-value
export AZURE_OPENAI_MODELS=full_local_model_name
```

- The key must be set even if your local server does not check one: the
  backend does not register without it (`RemoteAzureOpenLike.__init__`). It
  is sent as a Bearer token.
- The endpoint is used as given, apart from a trailing `/chat/completions`,
  so include `/v1`.
- The model registers as `azure-openai/<name>` in the `custom` tier, and it
  counts as an external model. Chat and appeals call external models only
  while the external-models consent is on: "Allow external AI models (e.g.,
  OpenAI, Google)" on the chat consent page, or "Increase the number of
  possible appeals and use external models" on the first appeal page. Both
  start checked. With it off, they call only the internal backends
  (`HEALTH_BACKEND_*` and the others in option 2).
- In chat, external models are asked alongside the internal ones. For
  appeals they are only the backup tier, so leave the internal backends
  unset if you want appeals to reach this model.

To check that each enabled backend actually answers, see
[model-backend-health.md](model-backend-health.md).
