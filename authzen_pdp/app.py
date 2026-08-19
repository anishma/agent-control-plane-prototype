"""Minimal OpenID AuthZEN evaluation endpoint backed by the Cedar engine."""

import json
from pathlib import Path

import cedarpy
from fastapi import FastAPI
from pydantic import BaseModel, Field

POLICY_FILE = Path("/policies/policies.cedar")
ENTITIES_FILE = Path("/policies/entities.json")
app = FastAPI(title="Cedar-backed AuthZEN PDP")


class Entity(BaseModel):
    type: str
    id: str
    properties: dict = Field(default_factory=dict)


class Action(BaseModel):
    name: str


class EvaluationRequest(BaseModel):
    subject: Entity
    action: Action
    resource: Entity
    context: dict = Field(default_factory=dict)


@app.post("/access/v1/evaluation")
def evaluate(request: EvaluationRequest) -> dict:
    delegator = request.subject.properties.get("on_behalf_of")
    if not isinstance(delegator, str) or not delegator:
        return {"decision": False}

    cedar_request = {
        "principal": f'Agent::"{request.subject.id}"',
        "action": f'Action::"{request.action.name}"',
        "resource": f'Tool::"{request.resource.id}"',
        "context": {
            "delegator": delegator,
            "arguments": request.resource.properties.get("arguments", {}),
        },
    }
    try:
        result = cedarpy.is_authorized(
            cedar_request, POLICY_FILE.read_text(), json.loads(ENTITIES_FILE.read_text())
        )
        return {"decision": result.decision == cedarpy.Decision.Allow}
    except Exception:
        return {"decision": False}


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
