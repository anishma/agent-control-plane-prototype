"""In-network adversary probes for the bypass and exfiltration scenarios.

The threat model here is deliberately generous to the attacker: it already has
code execution *inside* the Compose network, so every service is routable.  It
still must not be able to (a) invoke a tool without traversing the gateway, or
(b) obtain the user's provider credential.

Every probe records the observed status and asserts that the synthetic
credential value never appears in any response body.  The script exits
non-zero if any attack succeeds.
"""

import json
import os
import sys

import httpx

MCP_SERVER_URL = os.environ["MCP_SERVER_URL"]
BFF_URL = os.environ["BFF_URL"]
VAULT_URL = os.environ["VAULT_URL"]
PROVIDER_URL = os.environ["PROVIDER_URL"]
CREDENTIAL = os.environ["SYNTHETIC_PROVIDER_TOKEN"]

MCP_CALL = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/call",
    "params": {
        "name": "flight.book",
        "arguments": {"flight_id": "FL-204", "travel_date": "2026-09-15", "total_amount": 480},
    },
}
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def attempt(name: str, family: str, blocked_when, call) -> dict:
    """Run one probe and classify it as blocked or successful."""
    try:
        response = call()
        status = response.status_code
        body = response.text
    except httpx.HTTPError as exc:
        status = None
        body = f"transport refused: {type(exc).__name__}"

    leaked = CREDENTIAL in body
    blocked = blocked_when(status, body)
    return {
        "probe": name,
        "family": family,
        "status": status,
        "blocked": blocked,
        "credential_leaked": leaked,
        "detail": body[:160].replace("\n", " "),
    }


def main() -> None:
    probes = [
        attempt(
            "direct tools/call on the MCP server, no gateway identity",
            "policy bypass",
            lambda status, body: status == 403,
            lambda: httpx.post(MCP_SERVER_URL, json=MCP_CALL, headers=MCP_HEADERS, timeout=5.0),
        ),
        attempt(
            "direct tools/call on the MCP server, forged gateway identity",
            "policy bypass",
            lambda status, body: status == 403,
            lambda: httpx.post(
                MCP_SERVER_URL,
                json=MCP_CALL,
                headers={**MCP_HEADERS, "X-Gateway-Caller": "guessed-gateway-token"},
                timeout=5.0,
            ),
        ),
        attempt(
            "direct BFF booking call, no caller token",
            "credential exfiltration",
            lambda status, body: status == 401,
            lambda: httpx.post(
                f"{BFF_URL}/v1/book-flight",
                json={"flight_id": "FL-204", "travel_date": "2026-09-15", "total_amount": 480},
                timeout=5.0,
            ),
        ),
        attempt(
            "direct BFF booking call, forged caller token",
            "credential exfiltration",
            lambda status, body: status == 401,
            lambda: httpx.post(
                f"{BFF_URL}/v1/book-flight",
                headers={"X-BFF-Caller": "guessed-caller-token"},
                json={"flight_id": "FL-204", "travel_date": "2026-09-15", "total_amount": 480},
                timeout=5.0,
            ),
        ),
        attempt(
            "direct vault credential read, no client token",
            "credential exfiltration",
            lambda status, body: status == 401,
            lambda: httpx.get(
                f"{VAULT_URL}/v1/credentials/fictional-travel-provider",
                params={"principal": "00000000-0000-0000-0000-000000000001"},
                timeout=5.0,
            ),
        ),
        attempt(
            "direct vault credential read, forged client token",
            "credential exfiltration",
            lambda status, body: status == 401,
            lambda: httpx.get(
                f"{VAULT_URL}/v1/credentials/fictional-travel-provider",
                headers={"X-Vault-Client": "guessed-vault-token"},
                params={"principal": "00000000-0000-0000-0000-000000000001"},
                timeout=5.0,
            ),
        ),
        attempt(
            "direct provider call without the brokered credential",
            "credential exfiltration",
            lambda status, body: status == 401,
            lambda: httpx.post(
                f"{PROVIDER_URL}/v1/flights/book",
                json={
                    "principal": "00000000-0000-0000-0000-000000000001",
                    "flight_id": "FL-204",
                    "travel_date": "2026-09-15",
                    "total_amount": 480,
                },
                timeout=5.0,
            ),
        ),
    ]

    for probe in probes:
        verdict = "BLOCKED" if probe["blocked"] else "SUCCEEDED"
        print(f"[{verdict}] {probe['family']}: {probe['probe']} -> {probe['status']}")

    print(json.dumps({"probes": probes}, indent=2))

    succeeded = [p for p in probes if not p["blocked"]]
    leaked = [p for p in probes if p["credential_leaked"]]
    if succeeded or leaked:
        print(f"FAIL: {len(succeeded)} attack(s) succeeded, {len(leaked)} credential leak(s)")
        sys.exit(1)
    print(f"PASS: {len(probes)}/{len(probes)} in-network attacks blocked; no credential observed")


if __name__ == "__main__":
    main()
