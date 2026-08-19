"""Run scenarios against the local prototype.

The password grant and fixed local client/user credentials below exist only to
make this self-contained demonstration easy to run. They are not a
production-grade token-acquisition pattern.
"""

import argparse
import asyncio
import logging
import time

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

KEYCLOAK_TOKEN_URL = "http://localhost:8081/realms/acp/protocol/openid-connect/token"
GATEWAY_URL = "http://localhost:3001/mcp"
BASELINE_GATEWAY_URL = "http://localhost:3002/mcp"
REGISTRY_URL = "http://localhost:8091"
RECEIPT_LOG_URL = "http://localhost:8092"
# The same non-sensitive synthetic value the vault and provider are seeded with,
# used only so a scenario can assert that it never appears in a response.
SYNTHETIC_PROVIDER_TOKEN = "synthetic-provider-token-elena-only"
logging.getLogger("mcp").setLevel(logging.ERROR)
# The client library may attempt to deliver a late SSE event after the session
# closes on a deliberate denial. The denial is already returned by invoke().
logging.getLogger("mcp.client.streamable_http").setLevel(logging.CRITICAL)


def error_detail(error: BaseException) -> str:
    """Return the most specific message nested inside async exception groups."""
    if isinstance(error, BaseExceptionGroup):
        details = [error_detail(child) for child in error.exceptions]
        details = [detail for detail in details if detail]
        if details:
            return "; ".join(dict.fromkeys(details))

    message = str(error)
    if message and message != "unhandled errors in a TaskGroup":
        return message

    if error.__cause__ is not None:
        return error_detail(error.__cause__)
    if error.__context__ is not None:
        return error_detail(error.__context__)
    return "gateway rejected the tool invocation"


def access_token() -> str:
    # Prototype-only: obtain an inbound user token through a confidential OAuth
    # client using the Resource Owner Password Credentials grant.  Production
    # deployments should use an appropriate interactive or workload identity
    # flow and must not embed user passwords or client secrets in source code.
    response = httpx.post(
        KEYCLOAK_TOKEN_URL,
        data={
            "grant_type": "password",
            "client_id": "agent-client",
            "client_secret": "agent-client-secret",
            "username": "elena",
            "password": "elena-password",
        },
        timeout=10.0,
    )
    response.raise_for_status()
    return response.json()["access_token"]


async def invoke(tool: str, arguments: dict, url: str = GATEWAY_URL) -> tuple[bool, float, str]:
    started = time.perf_counter()
    try:
        token = access_token()
        headers = {"Authorization": f"Bearer {token}"}
        async with streamablehttp_client(url, headers=headers) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(tool, arguments)
        text = " ".join(str(block) for block in result.content)
        return (not result.isError, (time.perf_counter() - started) * 1000, text)
    except Exception as exc:
        return (False, (time.perf_counter() - started) * 1000, error_detail(exc))


def forged_token() -> str:
    """A structurally valid JWT with the right issuer, audience, and subject,
    signed by a key the gateway's JWKS does not publish."""
    import jwt
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    now = int(time.time())
    return jwt.encode(
        {
            "iss": "http://keycloak:8080/realms/acp",
            "aud": "agent-control-plane",
            "sub": "00000000-0000-0000-0000-000000000001",
            "azp": "agent-client",
            "iat": now,
            "exp": now + 300,
        },
        pem,
        algorithm="RS256",
        headers={"kid": "attacker-key"},
    )


def raw_gateway_post(headers: dict) -> tuple[int | None, str]:
    """Post a tools/call directly to the gateway, bypassing the MCP client."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "flight.book",
            "arguments": {
                "flight_id": "FL-204",
                "travel_date": "2026-09-15",
                "total_amount": 480,
            },
        },
    }
    base = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    try:
        response = httpx.post(GATEWAY_URL, json=payload, headers={**base, **headers}, timeout=10.0)
        return response.status_code, response.text[:160].replace("\n", " ")
    except httpx.HTTPError as exc:
        return None, f"transport refused: {type(exc).__name__}"


async def scenario(name: str) -> int:
    if name == "allowed":
        allowed, elapsed, detail = await invoke(
            "flight.book",
            {"flight_id": "FL-204", "travel_date": "2026-09-15", "total_amount": 480},
        )
        print(f"flight.book: {'ALLOW' if allowed else 'DENY'} ({elapsed:.1f} ms) {detail}")
        return 0 if allowed else 1
    if name == "injected":
        allowed, elapsed, detail = await invoke(
            "hotel.book",
            {
                "hotel_id": "HTL-ATTACK-77",
                "check_in": "2026-09-15",
                "nights": 2,
                "total_amount": 950,
            },
        )
        print(f"hotel.book: {'ALLOW' if allowed else 'DENY'} ({elapsed:.1f} ms) {detail}")
        return 1 if allowed else 0
    if name == "over-limit":
        allowed, elapsed, detail = await invoke(
            "flight.book",
            {"flight_id": "FL-991", "travel_date": "2026-09-15", "total_amount": 5000},
        )
        print(f"over-limit flight.book: {'ALLOW' if allowed else 'DENY'} ({elapsed:.1f} ms) {detail}")
        return 1 if allowed else 0
    if name == "credential-probe":
        allowed, elapsed, detail = await invoke("credential.probe", {})
        isolated = "provider_credential_present_in_mcp_server" in detail and "false" in detail.lower()
        print(
            f"credential.probe: {'ALLOW' if allowed else 'DENY'} ({elapsed:.1f} ms) "
            f"{detail}"
        )
        return 0 if allowed and isolated else 1

    if name == "bypass":
        # Every route to a tool that skips the control plane must fail.
        failures = 0
        for label, headers in [
            ("no bearer token", {}),
            ("malformed bearer token", {"Authorization": "Bearer not-a-token"}),
            ("forged-signature token", {"Authorization": f"Bearer {forged_token()}"}),
        ]:
            status, detail = raw_gateway_post(headers)
            blocked = status == 401
            failures += 0 if blocked else 1
            print(f"gateway {label}: {'BLOCKED' if blocked else 'SUCCEEDED'} ({status}) {detail}")

        # Control: the same undelegated call on a gateway with no control plane.
        # This shows the denials above come from the architecture, not from
        # something incidental to the transport or the tool.
        control, _, detail = await invoke(
            "hotel.book",
            {"hotel_id": "HTL-CTL-1", "check_in": "2026-09-15", "nights": 2, "total_amount": 950},
            url=BASELINE_GATEWAY_URL,
        )
        print(
            f"control: undelegated hotel.book on gateway-only baseline: "
            f"{'INVOKED' if control else 'DENIED'} {detail[:90]}"
        )
        governed, _, _ = await invoke(
            "hotel.book",
            {"hotel_id": "HTL-CTL-1", "check_in": "2026-09-15", "nights": 2, "total_amount": 950},
        )
        print(f"same call on the control-plane gateway: {'INVOKED' if governed else 'DENIED'}")
        # The control must succeed and the governed call must not.
        return 0 if failures == 0 and control and not governed else 1

    if name == "exfiltration":
        # The agent-side client must never observe the provider credential on
        # any path, permitted or not.
        observed = []
        for tool, arguments in [
            ("flight.book", {"flight_id": "FL-204", "travel_date": "2026-09-15", "total_amount": 480}),
            ("credential.probe", {}),
        ]:
            _, _, detail = await invoke(tool, arguments)
            leaked = SYNTHETIC_PROVIDER_TOKEN in detail
            observed.append(leaked)
            print(f"{tool}: credential in response = {leaked}")
        probe_leaked, _, probe_detail = await invoke("credential.probe", {})
        isolated = "provider_credential_present_in_mcp_server" in probe_detail and (
            "false" in probe_detail.lower()
        )
        print(f"MCP server holds a provider credential: {not isolated}")
        print(
            "NOTE: in-network exfiltration attempts against the BFF, vault, and "
            "provider are run by the attacker profile."
        )
        return 0 if not any(observed) and isolated else 1

    if name == "receipts":
        # A permitted call and a denied call, then offline verification.
        httpx.delete(f"{RECEIPT_LOG_URL}/v1/receipts", timeout=5.0)
        await invoke(
            "flight.book", {"flight_id": "FL-204", "travel_date": "2026-09-15", "total_amount": 480}
        )
        await invoke(
            "hotel.book",
            {"hotel_id": "HTL-ATTACK-77", "check_in": "2026-09-15", "nights": 2, "total_amount": 950},
        )
        log = httpx.get(f"{RECEIPT_LOG_URL}/v1/receipts", timeout=5.0).json()
        print(f"receipts minted: {log['count']} (expected 3: custody+execution, then a denial)")
        return 0 if log["count"] == 3 else 1

    allowed_flight = {"flight_id": "FL-204", "travel_date": "2026-09-15", "total_amount": 480}
    before, before_ms, _ = await invoke("flight.book", allowed_flight)
    revoke = httpx.post(
        f"{REGISTRY_URL}/v1/admin/revoke", json={"mandate_id": "travel-elena-v1"}, timeout=5.0
    )
    revoke.raise_for_status()
    after, after_ms, detail = await invoke("flight.book", allowed_flight)
    print(f"flight.book before revoke: {'ALLOW' if before else 'DENY'} ({before_ms:.1f} ms)")
    print(
        f"flight.book after revoke:  {'ALLOW' if after else 'DENY'} "
        f"({after_ms:.1f} ms) {detail}"
    )
    # Restore the seeded state so the scenario suite can be re-run.
    httpx.post(
        f"{REGISTRY_URL}/v1/admin/reinstate", json={"mandate_id": "travel-elena-v1"}, timeout=5.0
    ).raise_for_status()
    return 0 if before and not after else 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "scenario",
        choices=[
            "allowed",
            "credential-probe",
            "injected",
            "over-limit",
            "revoke",
            "bypass",
            "exfiltration",
            "receipts",
        ],
    )
    args = parser.parse_args()
    raise SystemExit(asyncio.run(scenario(args.scenario)))


if __name__ == "__main__":
    main()
