# GeoIP (chat state guessing and ASN tracking)

The chat can seed the model with a *best-effort, unconfirmed* guess of the
user's US state from their connection IP, so Medicaid questions can start with
"it looks like you might be in California, is that right?" instead of a cold
"which state are you in?". Request tracking can also record network-level ASN
info (the ASN name only: geoip2fast 1.2.x has no numeric ASN field). Both need
a [geoip2fast](https://github.com/rabuchaim/geoip2fast) city-level database,
switched on by **one key**:

```bash
export FHI_GEOIP_CITY_DB=/path/to/geoip2fast-city-asn-ipv6.dat.gz
```

The code is in [`fhi_users/audit.py`](../fhi_users/audit.py)
(`_get_geo_reader`, `guess_us_state`, `get_asn_info`).

## Getting the database

The `geoip2fast` package itself is installed from `requirements.txt` (pinned to
1.2.2), but it only bundles country/ASN databases. State (subdivision) data
needs the city-level `-city-asn-ipv6` file, which also carries the ASN data.
[`scripts/fetch_geoip_db.sh`](../scripts/fetch_geoip_db.sh) is the one place
that knows how to get it, so dev boxes and images install identical,
identically verified bytes.

```bash
# from the repository root
scripts/fetch_geoip_db.sh                 # installs into geoip_data/ (gitignored)
scripts/fetch_geoip_db.sh --print-sha     # downloads and prints the digest, installs nothing
scripts/fetch_geoip_db.sh --help          # full usage
```

| Flag or variable | Effect |
| --- | --- |
| `--dest PATH` | Install somewhere other than `geoip_data/`. |
| `--require` | Exit non-zero if the database was not installed. |
| `--allow-unverified`, or `GEOIP_ALLOW_UNVERIFIED=1` | Install even with no pinned digest. Dev boxes only, never an image. |
| `--print-sha` | Download to a temp file, print its sha256, install nothing. |
| `GEOIP2FAST_DB_URL` | Download from somewhere else (for example an internal mirror). |
| `GEOIP2FAST_DB_SHA256` | Expected digest; wins over the pinned file. |

The script downloads the file and checks it against the digest pinned in
[`scripts/geoip2fast-city-asn-ipv6.sha256`](../scripts/geoip2fast-city-asn-ipv6.sha256)
(or `GEOIP2FAST_DB_SHA256` when set). On a mismatch it installs nothing at
all, and a previously installed copy that no longer matches is removed too.
Re-running is a no-op once the file is in place (it still hashes the file).

- **Local dev:** `scripts/run_local.sh` runs the fetch (in parallel with the
  other startup work) and exports `FHI_GEOIP_CITY_DB` when the file is there.
  Nothing to do by hand.
- **Deployments:** `k8s/Dockerfile` runs the same script at build time into
  `/opt/geoip/`, root-owned and read-only to the app, and bakes
  `ENV FHI_GEOIP_CITY_DB` into the image. No runtime download, nothing to
  mount, and the bytes are fixed at build time. Kubernetes needs no extra
  config; a pod picks it up from the image. Only the web image carries it; the
  Ray images under `k8s/ray/` do not.

## Why the digest is pinned

> **Treat this file as trusted code, not data.** geoip2fast loads the database
> with `pickle.load()`, so anything that can replace the file (a swapped
> release asset, a MITM on a build box, write access to the deployment volume)
> can execute code inside the web process, which holds DB credentials and the
> PHI field-encryption keys. That is what the pinned digest protects. It is
> why bytes that fail verification are never installed (and a stale copy that
> stops matching is removed) even though a missing database is otherwise
> tolerated, and why the file is baked into the image instead of downloaded at
> pod start.

Upstream publishes these databases only under the moving `LATEST` tag, so the
pinned digest, not a version tag, is what makes the fetch reproducible: a
republished asset fails verification loudly instead of swapping itself in.
When you deliberately move to a newer release, record the new digest with
`--print-sha` **on a machine and network you trust** and commit it to
`scripts/geoip2fast-city-asn-ipv6.sha256`. A build with no digest pinned, an
unreachable upstream, or a republished (mismatching) asset still succeeds; it
just ships without the database and the app warns at startup. Pass `--require`
where shipping without it should fail instead.

## Soft fail

When `FHI_GEOIP_CITY_DB` is unset (or the file is missing or unreadable, or
the package isn't installed) nothing breaks: the state guess and ASN lookups
simply return nothing, and a single warning is logged at startup so the
misconfiguration is visible. The warning names `FHI_GEOIP_CITY_DB` except when
the package itself is missing. A corrupt but non-empty file passes the startup
check and is reported instead by the background warm-up load.

The guess is transient by design: it is fed to the model as unconfirmed
context each turn and the app never persists it, for any user. The prompt also
tells the model not to copy it into the stored context summary.
