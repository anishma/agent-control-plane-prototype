"""Minimal BFF credential broker for the clean-room E4 demonstration.

The BFF authenticates to the separate synthetic credential vault, resolves the
demo user's provider credential for one provider, and injects it only into the
fictional provider API call. It never returns that credential to its caller.
"""

import os

import httpx
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

app = FastAPI(title="Prototype Travel BFF Credential Broker")
CALLER_TOKEN = os.environ["BFF_CALLER_TOKEN"]
PROVIDER_URL = os.environ["PROVIDER_URL"]
DEMO_PRINCIPAL = os.environ["DEMO_PRINCIPAL"]
VAULT_URL = os.environ["VAULT_URL"]
VAULT_CLIENT_TOKEN = os.environ["VAULT_CLIENT_TOKEN"]


class FlightBooking(BaseModel):
    flight_id: str
    travel_date: str
    total_amount: int


@app.post("/v1/book-flight")
def book_flight(
    booking: FlightBooking, x_bff_caller: str | None = Header(default=None)
) -> dict:
    if x_bff_caller != CALLER_TOKEN:
        raise HTTPException(status_code=401, detail="trusted MCP server authentication required")

    try:
        vault_response = httpx.get(
            f"{VAULT_URL}/v1/credentials/fictional-travel-provider",
            headers={"X-Vault-Client": VAULT_CLIENT_TOKEN},
            params={"principal": DEMO_PRINCIPAL},
            timeout=2.0,
        )
        vault_response.raise_for_status()
        credential = vault_response.json()
        provider_response = httpx.post(
            f"{PROVIDER_URL}/v1/flights/book",
            headers={"Authorization": f"{credential['token_type']} {credential['access_token']}"},
            json={"principal": DEMO_PRINCIPAL, **booking.model_dump()},
            timeout=2.0,
        )
        provider_response.raise_for_status()
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="fictional provider unavailable") from exc

    provider_result = provider_response.json()
    return {
        **provider_result,
        "credential_brokered": True,
        "note": "fictional provider call; synthetic credential was resolved from the vault by the BFF",
    }


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
