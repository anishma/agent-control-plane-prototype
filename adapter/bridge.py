"""Agentgateway ExtMcp adapter for mandate resolution, AuthZEN evaluation, and receipts.

The gateway invokes CheckRequest before a tool is dispatched and CheckResponse
after it returns.  The request phase resolves the delegation mandate (E3),
obtains an AuthZEN decision (E2), and mints a custody receipt (E5); the
response phase mints the paired execution receipt.
"""

import collections
import hashlib
import json
import os
import threading
import uuid
from concurrent import futures

import grpc
import httpx

import ext_mcp_pb2
import ext_mcp_pb2_grpc

MANDATE_REGISTRY_URL = os.environ["MANDATE_REGISTRY_URL"]
PDP_URL = os.environ["PDP_URL"]
RECEIPT_LOG_URL = os.environ.get("RECEIPT_LOG_URL")
PASS_RESPONSE = ext_mcp_pb2.McpResponseResult(**{"pass": ext_mcp_pb2.Pass()})

# One pooled client for every control-plane hop.  Opening a fresh connection
# per call would charge each tool invocation a TCP handshake to the registry,
# the PDP, and the receipt log.
_http = httpx.Client(
    timeout=2.0, limits=httpx.Limits(max_keepalive_connections=32, max_connections=64)
)

# Correlates a custody receipt with the execution receipt for the same call.
# The ExtMcp response message carries no request identifier, so the pending
# custody digest is handed to the response phase through the request result's
# metadata when the gateway propagates it, and otherwise through this
# per-identity FIFO.  The fallback is exact for the sequential prototype
# scenarios; a deployment would correlate on a gateway-issued call id.
_pending: dict[tuple[str, str], collections.deque] = collections.defaultdict(
    collections.deque
)
_pending_lock = threading.Lock()


def deny(reason: str) -> ext_mcp_pb2.McpRequestResult:
    return ext_mcp_pb2.McpRequestResult(
        error=ext_mcp_pb2.AuthorizationError(
            code=ext_mcp_pb2.AuthorizationError.PERMISSION_DENIED, reason=reason
        )
    )


def validated_claim(request, name: str) -> str:
    field = request.metadata_context.fields.get(name)
    return field.string_value if field else ""


def digest(value) -> str:
    """Stable SHA-256 over a JSON-serializable value."""
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def mint_receipt(receipt_type: str, claims: dict) -> dict | None:
    """Sign one receipt at the receipt log.  Returns None if signing failed."""
    if not RECEIPT_LOG_URL:
        return None
    try:
        response = _http.post(
            f"{RECEIPT_LOG_URL}/v1/receipts",
            json={"receipt_type": receipt_type, "claims": claims},
        )
        response.raise_for_status()
        return response.json()
    except httpx.HTTPError:
        return None


class PolicyBridge(ext_mcp_pb2_grpc.ExtMcpServicer):
    def CheckRequest(self, request, grpc_context):
        if request.method != "tools/call":
            return ext_mcp_pb2.McpRequestResult(**{"pass": ext_mcp_pb2.Pass()})

        principal = validated_claim(request, "principal")
        agent_client = validated_claim(request, "agent_client")
        if not principal or not agent_client:
            return deny("validated user and agent identities are required")

        try:
            params = json.loads(request.mcp_request) if request.mcp_request else {}
            operation = params["name"]
            arguments = params.get("arguments") or {}
        except (KeyError, json.JSONDecodeError):
            return deny("malformed MCP tool invocation")

        decision_id = str(uuid.uuid4())
        base_claims = {
            "decision_id": decision_id,
            "agent": agent_client,
            "on_behalf_of": principal,
            "tool": operation,
            "arguments_sha256": digest(arguments),
        }

        def refuse(reason: str, enforced_at: str) -> ext_mcp_pb2.McpRequestResult:
            """Receipt the denial, then refuse.  Refusals are as accountable as permits."""
            mint_receipt(
                "custody",
                {**base_claims, "decision": "deny", "enforced_at": enforced_at, "reason": reason},
            )
            return deny(reason)

        try:
            mandate_response = _http.get(
                f"{MANDATE_REGISTRY_URL}/v1/mandates/resolve",
                params={
                    "principal": principal,
                    "agent_client": agent_client,
                    "operation": operation,
                },
            )
            mandate_response.raise_for_status()
            mandate = mandate_response.json()
        except httpx.HTTPError:
            return refuse("mandate registry unavailable; failing closed", "delegation")

        if not mandate.get("allowed"):
            return refuse("no active mandate permits this agent operation", "delegation")

        mandate_id = mandate["mandate"]["mandate_id"]
        base_claims["mandate_id"] = mandate_id

        authzen_request = {
            "subject": {
                "type": "agent",
                "id": agent_client,
                "properties": {"on_behalf_of": principal, "mandate_id": mandate_id},
            },
            "action": {"name": "tools/call"},
            "resource": {
                "type": "mcp-tool",
                "id": operation,
                "properties": {"arguments": arguments},
            },
        }
        try:
            decision_response = _http.post(
                f"{PDP_URL}/access/v1/evaluation", json=authzen_request
            )
            decision_response.raise_for_status()
            decision = decision_response.json().get("decision", False)
        except httpx.HTTPError:
            return refuse("policy decision point unavailable; failing closed", "pdp")

        if not decision:
            return refuse("AuthZEN PDP denied this invocation", "pdp")

        custody = mint_receipt("custody", {**base_claims, "decision": "permit"})
        if RECEIPT_LOG_URL and custody is None:
            # No tool runs without a custody record.
            return deny("custody receipt could not be signed; failing closed")

        result = ext_mcp_pb2.McpRequestResult(**{"pass": ext_mcp_pb2.Pass()})
        if custody is not None:
            with _pending_lock:
                _pending[(principal, agent_client)].append({**base_claims, "custody": custody})
            result.metadata.update(
                {"custody_sha256": custody["sha256"], "decision_id": decision_id}
            )
        return result

    def CheckResponse(self, request, grpc_context):
        if request.method != "tools/call" or not RECEIPT_LOG_URL:
            return PASS_RESPONSE

        principal = validated_claim(request, "principal")
        agent_client = validated_claim(request, "agent_client")
        with _pending_lock:
            queue = _pending.get((principal, agent_client))
            pending = queue.popleft() if queue else None
        if pending is None:
            return PASS_RESPONSE

        try:
            body = json.loads(request.mcp_response) if request.mcp_response else {}
        except json.JSONDecodeError:
            body = {"unparsed": True}

        mint_receipt(
            "execution",
            {
                "decision_id": pending["decision_id"],
                "agent": pending["agent"],
                "on_behalf_of": pending["on_behalf_of"],
                "mandate_id": pending.get("mandate_id"),
                "tool": pending["tool"],
                "custody_sha256": pending["custody"]["sha256"],
                "custody_seq": pending["custody"]["seq"],
                "outcome": "error" if body.get("isError") else "success",
                "result_sha256": digest(body),
            },
        )
        return PASS_RESPONSE


def main() -> None:
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=8))
    ext_mcp_pb2_grpc.add_ExtMcpServicer_to_server(PolicyBridge(), server)
    server.add_insecure_port("0.0.0.0:50051")
    server.start()
    server.wait_for_termination()


if __name__ == "__main__":
    main()
