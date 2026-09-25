# Local development

The long form of "Run it locally" in the [README](../README.md). Commands run
from the repository root unless a block says otherwise.

## Prerequisites

- **Python 3.12 or 3.13.** CI tests 3.13 (`.github/workflows/ci.yml`).
  Production and the Ray worker images run 3.12 (`k8s/Dockerfile`,
  `scripts/build_ray.sh`), so use 3.12 if you connect to the Ray cluster.
  `scripts/install.sh` and `scripts/run_local.sh` only check for 3.10, but
  3.10 cannot import the settings (they use `datetime.UTC`, new in 3.11).
- **Node.js 20 or newer, with npm** (`engines` in
  `fighthealthinsurance/static/js/package.json`). The static build runs
  `npm run build`, and `run_local.sh` stops if it fails.
- **tesseract-ocr**, for OCR of uploaded denial letters.
- **texlive and pandoc**, for PDF generation. The `pandoc` package in
  `requirements.txt` is a Python wrapper and does not include the binary.
- **mkcert**, for the dev server's HTTPS certificate.
- **git**, for the `git+https` entries in `requirements.txt`, and optionally
  **git-lfs** for a few logo images (`.gitattributes`). Without git-lfs the
  static build warns and carries on.

### Debian and Ubuntu

```bash
sudo apt-get install -y tesseract-ocr texlive pandoc mkcert git git-lfs
```

Install Node.js 20+ separately: the production image uses NodeSource's
`setup_20.x` (`k8s/Dockerfile`), and nvm also works.

- CI also installs `libgirepository1.0-dev pkg-config python3-dev`. Add them if
  pip fails to build a package.
- `libpango1.0-dev` and `libgdk-pixbuf2.0-dev`, listed here before, are not
  needed by the app. They only matter for weasyprint, one of the optional PDF
  engines pandoc may try (`_try_pandoc_engines` in
  `fighthealthinsurance/utils.py`).

### macOS

`scripts/install.sh` uses Homebrew (`brew install`) when `apt-get` is not
present. `scripts/good_to_go.sh` needs Bash 4 or newer (`brew install bash`).

### Other Linux distributions and WSL

`scripts/install.sh` only knows `apt-get` and Homebrew. On other systems,
install the prerequisites above with your own package manager.

#### Fedora and other DNF or RPM systems

These notes come from limited testing on Fedora, so they are less proven than
the Debian and Ubuntu steps. They cover only what differs; otherwise follow
the rest of this page.

```bash
sudo dnf install -y python3.12 tesseract tesseract-langpack-eng \
  texlive-scheme-basic pandoc git git-lfs nodejs npm mkcert
```

- **Name the Python version.** On Fedora 43 and 44, `python3` is 3.14, newer
  than any version CI or tox runs. Create the virtualenv with
  `python3.12 -m venv .venv` ([Option A](#option-a-virtualenv)); in testing on
  Fedora, a virtualenv worked more reliably than micromamba. `python3.13` is
  packaged too, and it is the version CI tests.
- On Fedora 43 and 44, `dnf install nodejs` installs Node.js 22, which meets
  the 20+ requirement.
- If pip fails to build a package, add a compiler and headers:
  `sudo dnf install -y gcc gcc-c++ python3.12-devel libffi-devel openssl-devel`.
  The requirements installed without them on Fedora 44 (x86-64), but a
  platform without prebuilt wheels needs them.
- `pango-devel` and `gdk-pixbuf2-devel` are not needed, for the same reason
  as their Debian counterparts above.
- **Make the certificate before the first run**, from the repository root:

  ```bash
  mkcert -install
  mkcert -cert-file cert.pem -key-file key.pem localhost 127.0.0.1
  ```

  `run_local.sh` runs `scripts/install.sh` only when `cert.pem` is missing,
  and `install.sh` can only install packages through `apt-get` or `brew`.
  With the pair in place, it is skipped. The second line is the one
  `install.sh` would run. `mkcert -install` makes browsers trust the
  certificate; for Firefox and Chrome, mkcert also needs `certutil`, from the
  `nss-tools` package.
- **Tests:** the `py313-` tox envs need Python 3.13, and tox skips an env
  whose interpreter is missing instead of failing it
  (`skip_missing_interpreters` in `tox.ini`). If you installed only
  `python3.12`, install `python3.13` as well, or use the `py312-` names (for
  example `tox -e py312-django52-sync`). CI runs only 3.13. tox on Fedora has
  had less testing than the rest of these notes.

#### WSL

- Inside WSL, install the prerequisites for the distribution you run: the
  Debian and Ubuntu steps for Ubuntu, the notes above for Fedora.
- **Clone the repository inside the WSL file system** (for example under
  `~`), not under `/mnt/c`. Microsoft
  [recommends](https://learn.microsoft.com/en-us/windows/wsl/filesystems)
  keeping files you work on with Linux tools in the WSL file system for
  speed, and in testing the repository's bash scripts ran into problems from
  `/mnt/c`.
- **VS Code:** run `code .` from the repository inside WSL. Or, in VS Code on
  Windows, install the WSL extension and run **WSL: Connect to WSL** from the
  command palette (Ctrl+Shift+P). The lower-left corner shows the WSL
  connection. Extensions installed on the Windows side may need installing
  again for WSL.
- **Certificate:** `mkcert -install` inside WSL adds mkcert's CA to the
  Linux trust stores in WSL. A browser running on Windows does not read those,
  so it still warns about the certificate for https://localhost:8000. Accept
  the warning, or see mkcert's documentation for installing its CA on another
  system.

## Python environment

### Option A: virtualenv

```bash
python3.12 -m venv .venv     # or python3.13
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
./scripts/run_local.sh
```

If no virtualenv is active, `run_local.sh` activates `./build_venv`, or else
`./.venv`. It also installs the requirements when they are missing, and again
whenever `requirements.txt` or `requirements-dev.txt` change (it keeps a
checksum in `.requirements_checksum`), so the `pip install` step above is
optional. If you do install them yourself, write the checksum it compares
against, so its requirements check skips `pip install`. (`scripts/install.sh`,
which runs when `cert.pem` is missing, pip-installs them regardless.)

```bash
md5sum requirements.txt requirements-dev.txt | sort | md5sum | cut -d ' ' -f 1 > .requirements_checksum
```

### Option B: conda, mamba or micromamba

`environment.yml` creates an environment named `fhi` with Python, tesseract,
texlive-core and a few libraries, then pip-installs both requirements files.
It does not include pandoc, Node.js, mkcert or git-lfs, so install those
yourself. CI does not use `environment.yml`.

```bash
micromamba env create -f environment.yml   # or conda / mamba
micromamba activate fhi
./scripts/run_local.sh
```

Install micromamba (or conda) by following its own documentation for your
platform.

Watch the first run: when `cert.pem` is missing, `run_local.sh` calls
`scripts/install.sh`, which always creates `.venv` and pip-installs into it.
Conda activation leaves `VIRTUAL_ENV` empty, so `run_local.sh` then activates
`.venv` and the server runs from there, not from the `fhi` environment. To
stay in the conda environment, create `cert.pem` and `key.pem` yourself before
the first run (the `mkcert` line is in `scripts/install.sh`) and do not create
`.venv`.

## What `run_local.sh` does

Run it from the repository root: it looks for `cert.pem`, `manage.py` and
`conf/uvlog_config.yaml` relative to the current directory. `make run-local`
runs the same script.

1. **First run only** (no `cert.pem`): runs `scripts/install.sh`, which checks
   the Python version, creates `.venv`, pip-installs the requirements into it,
   installs tesseract-ocr and mkcert with `apt-get` or `brew` (retrying with
   `sudo`), and creates `cert.pem`/`key.pem` with mkcert for `localhost` and
   `127.0.0.1`. It does not run `mkcert -install`, so browsers warn about the
   certificate until you install mkcert's local CA.
2. Activates `build_venv` or `.venv` when no virtualenv is active, and
   reinstalls requirements when they changed.
3. On Linux, if the inotify watch limit is below 524288 and passwordless
   `sudo` is available, raises it and writes
   `/etc/sysctl.d/99-fighthealthinsurance-watches.conf`. Otherwise it prints
   the commands to do so.
4. In parallel:
   - builds the JavaScript and static files (`scripts/build_static.sh`: `npm i`
     when dependencies are missing, `npm run build`, then `collectstatic` and
     `compress` into the gitignored `static/` at the root; each step is
     skipped when its inputs are unchanged);
   - downloads and verifies the GeoIP database ([geoip.md](geoip.md));
   - runs `python manage.py setup_local_db`: migrations into SQLite
     (`db.sqlite3` at the repository root, or the path in `DEV_DB_LOC`),
     the fixtures, a superuser `admin`, and two test users (a provider and a
     patient). The logins are in
     [`setup_local_db.py`](../fighthealthinsurance/management/commands/setup_local_db.py);
   - looks for the team's cluster model backends. If `kubectl` can see
     `vllm-health-svc` or `vllm-health-svc-slipstream` in the `totallylegitco`
     namespace, it port-forwards them to ports 4280 and 4281 and sets
     `HEALTH_BACKEND_*` and `NEW_HEALTH_BACKEND_*`, overriding any values you
     exported. It also pings an internal host for `ALPHA_HEALTH_BACKEND_HOST`.
     Without cluster access these print a message and are skipped.
5. Serves **https://localhost:8000** with uvicorn
   (`fighthealthinsurance.asgi:application`, auto-reload, bound to `0.0.0.0`)
   with `RECAPTCHA_TESTING=true` and `OAUTHLIB_RELAX_TOKEN_SCOPE=1`. Extra
   arguments are passed to uvicorn.

To skip the static build and the database setup on later runs:

```bash
FAST=FAST ./scripts/run_local.sh
```

The app needs a model backend to generate appeals; see
[ml-backends.md](ml-backends.md). The Ray background actors are not started
locally; see [ray-actors.md](ray-actors.md).

### `--devserver` and WebSockets

`./scripts/run_local.sh --devserver` runs django-extensions' `runserver_plus`
instead of uvicorn. It must be the first argument; the remaining arguments go
to `runserver_plus`. It serves WSGI only, and the WebSocket routes (chat,
streaming appeal generation) exist only in the ASGI app
(`fighthealthinsurance/asgi.py`), so they do not work under it. The same is
true of plain `python manage.py runserver`. For anything that uses WebSockets,
use the default uvicorn server.

## Troubleshooting

- **`django.core.exceptions.AppRegistryNotReady: The translation infrastructure
  cannot be initialized before the apps registry is ready. Check that you
  don't make non-lazy gettext calls at import time.`** Earlier docs
  pointed at the Python version, so first make sure you are on 3.12 or 3.13.
  If you are, look for a non-lazy `gettext` call in a module imported at
  startup, as the message says.
- **Sync tests fail with `dist/formPersistence.bundle.js is missing`.** The
  `sync` tox env does not build the JavaScript. Run `npm run build` in
  `fighthealthinsurance/static/js/` (or `./scripts/build_static.sh` from the
  root) first. The same applies after any TypeScript change: otherwise the
  sync tests run against the old bundle.
- **The browser rejects the certificate.** Run `mkcert -install` once to trust
  mkcert's local CA, or accept the warning.
