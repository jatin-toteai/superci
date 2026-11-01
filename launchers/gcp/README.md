# SuperCI launcher for GCP

An HTTP agent that starts a job's machine in a Google Cloud project when the SuperCI control plane asks.
It answers `GET /health` without a token. Every other route needs a token the control plane signed: ES256,
`iss` the plane's URL, `sub` `plane:<plane id>`, `aud` this launcher's own registered URL (`PUBLIC_URL`, not the
address a request happened to arrive at), and an `exp` in the future. A token that fails any of these is answered
with 401 and `not allowed: <why>`.

This directory holds the token check and `/health`. Machine choice, the startup script, `/launch`, `/stop` and
`/sweep` are added in later changes; `/launch` answers 501 until then.

## Configuration

Read from the environment when the service starts; a missing required variable stops the start.

| Variable | Meaning |
|---|---|
| `PROJECT` | GCP project that holds the job VMs (required) |
| `REGION` | region of the job VMs, for example `us-west4` (required) |
| `ZONES` | comma-separated zones to try, in order; default `<REGION>-a,<REGION>-b,<REGION>-c` |
| `NETWORK`, `SUBNET` | VPC network and subnet of the job VMs (required) |
| `IMAGE_FAMILY` | image family of the runner image (required) |
| `RUNNER_VERSION` | GitHub runner version the startup script expects (required) |
| `ON_DEMAND_FALLBACK` | `1` starts an on-demand VM when no spot room is found; anything else is off |
| `PUBLIC_URL` | the URL registered with the control plane; the audience of its tokens (required) |
| `TRUSTED_ISSUER` | the plane's URL (required) |
| `TRUSTED_SUBJECT` | `plane:<plane id>` (required) |
| `TRUSTED_KEYS` | the plane's JWKS as JSON, from `<plane>/.well-known/jwks.json` (required) |
| `SWEEP_SECRET` | shared secret for `/sweep` (required) |
| `BEHIND_GATEWAY` | `1` on path B, where API Gateway sits in front |
| `EXTERNAL_ADDRESS` | `0` on path B, where job VMs have no external address |

## Run

Python 3.12 (Ubuntu 24.04's).

```
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app:api --host 127.0.0.1 --port 8080
```

## Test

```
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/pytest -q
```

The tests bind no port; they call the app through FastAPI's `TestClient`.
