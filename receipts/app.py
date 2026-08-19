"""Minimal tamper-evident receipt log for the E5 demonstration.

Each mediated invocation produces a pair of signed receipts: a custody receipt
minted after the decision and before dispatch, and an execution receipt minted
after the tool returns that references its custody receipt by hash.  Every
receipt is an RS256 JSON Web Signature (RFC 7515) carrying a key identifier,
and the verification key is published at a JWKS endpoint (RFC 7517), so a
relying party verifies a receipt offline without trusting this log.

Receipts are additionally hash-linked into an append-only chain: receipt n
carries the SHA-256 digest of receipt n-1.  The chain and the signatures are
independent checks -- the signature defeats forgery of a single receipt, the
chain defeats silent deletion or reordering of the log.

The keypair is generated at startup because this is an ephemeral research
prototype.  A deployment would hold the signing key in a KMS or HSM and
publish a stable, rotated JWKS.
"""

import base64
import hashlib
import os
import threading
from datetime import UTC, datetime

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

app = FastAPI(title="Prototype Receipt Log")

RECEIPT_TYPE = "application/acp-receipt+jwt"
ISSUER = os.environ.get("RECEIPT_ISSUER", "https://acp.prototype.invalid/receipts")

# The key object is retained and handed to the signer directly.  Passing PEM
# bytes instead would make every signature re-parse the key, which dominates
# the cost of minting a receipt.
_private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_public_numbers = _private_key.public_key().public_numbers()

_lock = threading.Lock()
_log: list[dict] = []


def _b64url_uint(value: int) -> str:
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


_JWK = {
    "kty": "RSA",
    "use": "sig",
    "alg": "RS256",
    "n": _b64url_uint(_public_numbers.n),
    "e": _b64url_uint(_public_numbers.e),
}
# RFC 7638 JWK thumbprint, used as the key identifier.
_KID = hashlib.sha256(
    f'{{"e":"{_JWK["e"]}","kty":"RSA","n":"{_JWK["n"]}"}}'.encode()
).hexdigest()[:32]
_JWK["kid"] = _KID


class ReceiptRequest(BaseModel):
    """One receipt to mint.  `claims` is the auditor-facing payload."""

    receipt_type: str = Field(pattern="^(custody|execution)$")
    claims: dict


@app.post("/v1/receipts")
def mint(request: ReceiptRequest) -> dict:
    """Sign, hash-link, and append one receipt; return its digest."""
    with _lock:
        sequence = len(_log)
        previous = _log[-1]["sha256"] if _log else None
        payload = {
            **request.claims,
            "iss": ISSUER,
            "iat": int(datetime.now(UTC).timestamp()),
            "receipt_type": request.receipt_type,
            "seq": sequence,
            "prev_sha256": previous,
        }
        token = jwt.encode(
            payload,
            _private_key,
            algorithm="RS256",
            headers={"kid": _KID, "typ": RECEIPT_TYPE},
        )
        digest = hashlib.sha256(token.encode()).hexdigest()
        entry = {"seq": sequence, "sha256": digest, "jws": token}
        _log.append(entry)
    return entry


@app.get("/v1/receipts")
def read_log() -> dict:
    """Return the append-only log for offline verification."""
    with _lock:
        return {"issuer": ISSUER, "count": len(_log), "receipts": list(_log)}


@app.delete("/v1/receipts")
def reset_log() -> dict:
    """Test-only: clear the log so a scenario can assert on its own receipts."""
    with _lock:
        _log.clear()
    return {"count": 0}


@app.get("/.well-known/jwks.json")
def jwks() -> dict:
    return {"keys": [_JWK]}


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "kid": _KID, "receipts": len(_log)}


@app.get("/v1/receipts/{sequence}")
def read_one(sequence: int) -> dict:
    with _lock:
        if sequence < 0 or sequence >= len(_log):
            raise HTTPException(status_code=404, detail="unknown receipt sequence")
        return _log[sequence]
