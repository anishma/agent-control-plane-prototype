"""Synthetic credential vault used only by the clean-room prototype.

It models a vault-held, per-principal provider credential.  Only the BFF,
authenticated with a separate internal client secret, may resolve it.  The
vault and provider use one non-sensitive synthetic value solely so the local
end-to-end path is executable.
"""

import os

from fastapi import FastAPI, Header, HTTPException, Query

app = FastAPI(title="Prototype Credential Vault")
BFF_CLIENT_TOKEN = os.environ["VAULT_BFF_CLIENT_TOKEN"]
DEMO_PRINCIPAL = os.environ["DEMO_PRINCIPAL"]
SYNTHETIC_PROVIDER_TOKEN = os.environ["SYNTHETIC_PROVIDER_TOKEN"]


@app.get("/v1/credentials/{provider}")
def resolve_credential(
    provider: str,
    principal: str = Query(...),
    x_vault_client: str | None = Header(default=None),
) -> dict:
    """Return a synthetic credential only to the authenticated prototype BFF."""
    if x_vault_client != BFF_CLIENT_TOKEN:
        raise HTTPException(status_code=401, detail="vault client authentication required")
    if provider != "fictional-travel-provider" or principal != DEMO_PRINCIPAL:
        raise HTTPException(status_code=404, detail="no active credential for this principal and provider")
    return {"access_token": SYNTHETIC_PROVIDER_TOKEN, "token_type": "Bearer"}


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
