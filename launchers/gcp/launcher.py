# The GCP launcher: starts a job's machine in a Google Cloud project when the SuperCI control plane asks.
# It trusts only tokens that control plane signed (ES256, its public keys given in TRUSTED_KEYS), made for this
# launcher's own URL and for the plane's subject, the way the Modal and Cloudflare launchers do.
import base64
import json
import os
from dataclasses import dataclass

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


# The machine families a job can run on, in the order they are tried.
FAMILIES = ("e2", "n2d", "c4d")
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
