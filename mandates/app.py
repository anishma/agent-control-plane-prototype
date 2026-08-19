"""Ephemeral, independently modeled mandate registry for the paper prototype."""

import copy
import os
from datetime import UTC, datetime
from pathlib import Path

import yaml
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="Prototype mandate registry")
_mandates: dict[str, dict] = {}


def _load_seed() -> None:
    raw = yaml.safe_load(Path(os.environ["MANDATE_SEED"]).read_text())
    _mandates.update({entry["mandate_id"]: entry for entry in raw["mandates"]})


@app.on_event("startup")
def seed_registry() -> None:
    _load_seed()


def _not_expired(mandate: dict) -> bool:
    expiry = datetime.fromisoformat(mandate["expires_at"].replace("Z", "+00:00"))
    return expiry > datetime.now(UTC)


@app.get("/v1/mandates/resolve")
def resolve(principal: str, agent_client: str, operation: str) -> dict:
    """Read the authoritative in-memory state for exactly one invocation."""
    for mandate in _mandates.values():
        if (
            mandate["principal"] == principal
            and mandate["agent_client"] == agent_client
            and operation in mandate["permitted_operations"]
        ):
            active = mandate["state"] == "active" and _not_expired(mandate)
            return {"allowed": active, "mandate": copy.deepcopy(mandate) if active else None}
    return {"allowed": False, "mandate": None}


class Revocation(BaseModel):
    mandate_id: str


@app.post("/v1/admin/revoke")
def revoke(request: Revocation) -> dict:
    mandate = _mandates.get(request.mandate_id)
    if mandate is None:
        raise HTTPException(status_code=404, detail="unknown mandate")
    mandate["state"] = "revoked"
    return {"mandate_id": request.mandate_id, "state": mandate["state"]}


@app.post("/v1/admin/reinstate")
def reinstate(request: Revocation) -> dict:
    """Test-only: restore a revoked mandate so the scenario suite is repeatable."""
    mandate = _mandates.get(request.mandate_id)
    if mandate is None:
        raise HTTPException(status_code=404, detail="unknown mandate")
    mandate["state"] = "active"
    return {"mandate_id": request.mandate_id, "state": mandate["state"]}


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "mandates": len(_mandates)}

