# The launcher's HTTP side: GET /health is open; everything else needs a token the control plane signed.
# Run with `uvicorn app:api`; the configuration is read from the environment when this module is imported.
import time

from fastapi import Depends, FastAPI, HTTPException, Request

from launcher import Compute, Config, GoogleCompute, NoRoom, launch as start_vm, verify

cfg = Config.from_env()
api = FastAPI()
gce = GoogleCompute(cfg)  # made once, here; it makes its Google clients (which look for credentials) when first used


def get_compute() -> Compute:
    """The Compute that routes use. A test replaces it: `api.dependency_overrides[get_compute] = lambda: fake`."""
    return gce


async def checked(request: Request) -> dict:
    """The JSON body of a request that carries a token the plane signed for this launcher's registered URL.

    The audience is the registered URL, not the address the request came to: a token for the machine's own
    address is a token for something else.
    """
    try:
        verify(request.headers.get("authorization", ""), cfg.public_url, cfg.trust, time.time())
    except PermissionError as e:
        raise HTTPException(status_code=401, detail=f"not allowed: {e}")
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="the body is not JSON")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="the body is not a JSON object")
    return body


@api.get("/health")
async def health():
    return {"agent": "gcp"}


@api.post("/launch")
def launch(body: dict = Depends(checked), compute: Compute = Depends(get_compute)):
    """Starts a job's VM (see `launcher.launch`). 429 means no room now and the plane is to wait, 503 that it failed and may be
    tried again, 400 that the request is refused. A plain `def`: Google's client blocks, so the route runs in a thread, not in the
    event loop that answers /health."""
    try:
        return start_vm(body, compute, cfg, time.monotonic)
    except NoRoom as e:
        raise HTTPException(status_code=429, detail=f"no {'spot ' if e.spot else ''}room in {cfg.region} ({e})")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
