# The GCP launcher: starts a job's machine in a Google Cloud project when the SuperCI control plane asks.
# It trusts only tokens that control plane signed (ES256, its public keys given in TRUSTED_KEYS), made for this
# launcher's own URL and for the plane's subject, the way the Modal and Cloudflare launchers do.
import base64
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from prices import ON_DEMAND, SPOT


@dataclass
class Config:
    project: str
    region: str
    zones: tuple[str, ...]
    network: str
    subnet: str
    image_family: str
    runner_version: str
    on_demand_fallback: bool
    public_url: str  # the URL registered with the plane; a token's audience must be exactly this
    trust: dict  # {"issuer", "subject", "keys": the plane's JWKS}
    plane_id: str
    sweep_secret: str
    behind_gateway: bool = False  # path B: API Gateway checks the token and the launcher reads its claims
    external_address: bool = True  # path A gives job VMs an external address; path B has Cloud NAT instead
    budget_s: float = 8.0  # a launch call is bounded to this many seconds in all
    insert_wait_s: float = 5.0  # and each insert waits at most this long

    @classmethod
    def from_env(cls) -> "Config":
        """The launcher's configuration from its environment; a missing required variable raises KeyError."""
        env = os.environ
        region = env["REGION"]
        zones = env.get("ZONES") or ",".join(f"{region}-{z}" for z in "abc")
        subject = env["TRUSTED_SUBJECT"]
        return cls(
            project=env["PROJECT"],
            region=region,
            zones=tuple(z.strip() for z in zones.split(",") if z.strip()),
            network=env["NETWORK"],
            subnet=env["SUBNET"],
            image_family=env["IMAGE_FAMILY"],
            runner_version=env["RUNNER_VERSION"],
            on_demand_fallback=env.get("ON_DEMAND_FALLBACK") == "1",
            public_url=env["PUBLIC_URL"],
            trust={"issuer": env["TRUSTED_ISSUER"], "subject": subject, "keys": json.loads(env["TRUSTED_KEYS"])},
            plane_id=subject.removeprefix("plane:"),
            sweep_secret=env["SWEEP_SECRET"],
            behind_gateway=env.get("BEHIND_GATEWAY") == "1",
            external_address=env.get("EXTERNAL_ADDRESS") != "0",
        )


def b64url(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def verify(authorization: str, audience: str, trust: dict, now: float) -> dict:
    """The claims of the plane's token in an Authorization header; PermissionError says why it is not accepted."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

    if not authorization.startswith("Bearer "):
        raise PermissionError("no token")
    try:
        header, payload, signature = authorization[7:].split(".")
        raw = b64url(signature)
        der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    except ValueError:  # not three parts, or not base64
        raise PermissionError("bad token") from None
    for jwk in trust["keys"]["keys"]:
        key = ec.EllipticCurvePublicNumbers(int.from_bytes(b64url(jwk["x"]), "big"), int.from_bytes(b64url(jwk["y"]), "big"), ec.SECP256R1()).public_key()
        try:
            key.verify(der, f"{header}.{payload}".encode(), ec.ECDSA(hashes.SHA256()))
        except InvalidSignature:
            continue
        try:
            claims = json.loads(b64url(payload))
        except ValueError:  # signed, but not JSON
            raise PermissionError("bad token") from None
        if not isinstance(claims, dict):
            raise PermissionError("bad token")
        if claims.get("exp", 0) < now:
            raise PermissionError("expired")
        if claims.get("iss") != trust["issuer"] or claims.get("sub") != trust["subject"] or claims.get("aud") != audience:
            raise PermissionError("token for another agent or control plane")
        return claims
    raise PermissionError("bad token")


# The machine families a job can run on, in the order they are tried: by spot price in us-west4, e2 first and c4d
# before n2d (c4d's spot price is below n2d's at every size they share; on demand it is the other way round).
FAMILIES = ("e2", "c4d", "n2d")
SIZES = {"e2": (2, 4, 8, 16, 32), "n2d": (2, 4, 8, 16, 32, 64), "c4d": (2, 4, 8, 16, 32, 64)}  # vCPUs; e2 stops at 32


def choose(cpu: int, ram_gb: int, families: tuple[str, ...] = FAMILIES) -> list[str]:
    """For each family in order, the smallest standard machine type with `cpu` vCPUs and `ram_gb` GB of memory.

    A standard type has 4 GB a vCPU, which is what the fit is tested against. A c4d standard type has 3.75 GB a
    vCPU, so a job there can get up to 6 percent less memory than it asked for; that is accepted. A family with
    no size that fits is left out, and the result is empty when none fits.
    """
    types = []
    for family in families:
        fits = [n for n in SIZES[family] if n >= cpu and n * 4 >= ram_gb]
        if fits:
            types.append(f"{family}-standard-{min(fits)}")
    return types


def price(machine_type: str, spot: bool) -> float:
    """The hourly price in US dollars of a machine type in the price table; KeyError when it is not in it."""
    return (SPOT if spot else ON_DEMAND)[machine_type]


def instance_name(runner: str) -> str:
    """A GCE instance name for a runner name: lowercase letters, digits and `-`, starting with a letter, at most 63
    characters, not ending in `-`. The same runner name always gives the same instance name."""
    name = re.sub(r"[^a-z0-9-]", "-", runner.lower())
    if not name[:1].isalpha():
        name = "r-" + name
    return name[:63].rstrip("-")


# What the job VM runs at boot: GitHub's runner for one job, then power off. `startup.sh.tmpl` is the script; these
# are the two optional parts that `render_startup` puts in it.

# GCE's metadata server answers `preempted?wait_for_change=true` when the value changes, to TRUE as the VM is taken
# back (30 seconds before it is stopped). The plane is told once, in the background, from before the runner starts.
_NOTICE_WATCHER = """# Told to the control plane when GCE takes this VM back.
( until [ "$(curl -s -H 'Metadata-Flavor: Google' 'http://metadata.google.internal/computeMetadata/v1/instance/preempted?wait_for_change=true')" = TRUE ]; do sleep 1; done
  curl -s -X POST __NOTICE__ ) &"""

# A failing runner's job-started hook says why (base64, as the control plane wrote it) and fails the job before any
# step, as `runner_command` in the Modal launcher does. `RUNNER_ENV` is the setting the runner's command line gets (`sudo` would drop an exported one).
_FAIL_HOOK = """# This runner only says why its job fails.
printf '#!/bin/bash\\necho __WHY__ | base64 -d\\nexit 1\\n' > /tmp/superci-fail.sh && chmod 755 /tmp/superci-fail.sh
RUNNER_ENV=ACTIONS_RUNNER_HOOK_JOB_STARTED=/tmp/superci-fail.sh"""

_BASE64 = re.compile(r"[A-Za-z0-9+/=]+")


def _quoted(text: str) -> str:
    """`text` as one word for a shell, in single quotes."""
    return "'" + text.replace("'", "'\\''") + "'"


def render_startup(jit: str, fail: str | None, max_minutes: int, notice: str | None, runner_version: str) -> str:
    """The startup script of a job VM.

    `jit` is the runner's JIT configuration and `fail` (when the runner is only to fail its job) the reason, both
    base64 and ValueError otherwise, as they are put on a command line. The VM powers off after `max_minutes`
    whatever happens. `notice`, a link, is posted to once when GCE preempts the VM; none for an on-demand VM.
    `runner_version` is what the image's runner is expected to be: logged with the installed one.
    """
    if not _BASE64.fullmatch(jit):
        raise ValueError("the runner configuration is not base64")
    if fail is not None and not _BASE64.fullmatch(fail):
        raise ValueError("the failing runner's reason is not base64")
    if max_minutes < 1:
        raise ValueError("the time bound is under a minute")
    watcher = _NOTICE_WATCHER.replace("__NOTICE__", _quoted(notice)) if notice is not None else ""
    hook = _FAIL_HOOK.replace("__WHY__", fail) if fail is not None else ""
    template = Path(__file__).with_name("startup.sh.tmpl").read_text()
    # (A part left out leaves no blank line.) One pass over the template, so no inserted text is searched for placeholders.
    parts = {
        "__MAX_MINUTES__": str(max_minutes),
        "__RUNNER_VERSION__": _quoted(runner_version),
        "__JIT__": jit,
        "__NOTICE_WATCHER__\n": watcher + "\n" if watcher else "",
        "__FAIL_HOOK__\n": hook + "\n" if hook else "",
    }
    return re.sub("|".join(map(re.escape, parts)), lambda found: parts[found.group()], template)
