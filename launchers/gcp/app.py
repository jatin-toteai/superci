# The launcher's HTTP side: GET /health is open; everything else needs a token the control plane signed.
# Run with `uvicorn app:api`; the configuration is read from the environment when this module is imported.
import time

from fastapi import Depends, FastAPI, HTTPException, Request

from launcher import Config, verify

cfg = Config.from_env()
api = FastAPI()


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
async def launch(body: dict = Depends(checked)):
    raise HTTPException(status_code=501, detail="launch is not built yet")  # replaced when machine choice and the VM body exist
