# SuperCI launcher for GCP

An HTTP agent that starts a job's machine in a Google Cloud project when the SuperCI control plane asks.
It answers `GET /health` without a token. Every other route needs a token the control plane signed: ES256,
`iss` the plane's URL, `sub` `plane:<plane id>`, `aud` this launcher's own registered URL (`PUBLIC_URL`, not the
address a request happened to arrive at), and an `exp` in the future. A token that fails any of these is answered
with 401 and `not allowed: <why>`.

`POST /launch` starts a job's VM. It picks the smallest standard machine type that fits (`e2`, then `c4d`, then `n2d`) and
walks them, and in each the zones, inserting the VM until one has room, all within about eight seconds (each insert
waits at most five). It answers `{"id", "kind", "usd_per_hour", "spot"}`; the id is the instance name, and asking
again for the same runner name answers the VM already made. `429 {"detail"}` means no room (the plane waits and asks again),
`503` that GCE failed another way (the plane retries), `400` that the request is refused (for instance a GitLab job, which
has no `jit`). With `ON_DEMAND_FALLBACK=1`, when no spot room is found it starts one on-demand VM of the first machine type in
the first zone instead.

`POST /stop` with `{"id": "<instance name>"}` deletes that VM in every zone that has it and answers `{"stopped": true}`, also
when it is gone already. `POST /sweep` deletes this plane's job VMs (label `superci-plane=<plane id>`) in every zone that are
`TERMINATED` or older than their `superci-max-minutes` label plus 10 minutes, and answers `{"swept": n}`; a VM whose label or
creation time cannot be read is left alone. It is for something that calls it on a timer, so it takes no plane token: the header
`x-superci-sweep` must equal `SWEEP_SECRET`, or the answer is 404. `/launch` and `/stop` also run that sweep first when more than
600 seconds have passed since the last one (the first request after the start does not); a failure in it is logged and the
request is answered.

## Configuration

Read from the environment when the service starts; a missing required variable stops the start.

| Variable | Meaning |
|---|---|
| `PROJECT` | GCP project that holds the job VMs (required) |
| `REGION` | region of the job VMs, for example `us-west4` (required) |
| `ZONES` | comma-separated zones to try, in order; default `<REGION>-a,<REGION>-b,<REGION>-c` |
| `NETWORK`, `SUBNET` | VPC network and subnet of the job VMs (required). `SUBNET` is a bare name (the subnet of that name in `PROJECT` and `REGION`) or a path GCE takes, such as `projects/<project>/regions/<region>/subnetworks/<name>`; the VM's network is the subnet's |
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
