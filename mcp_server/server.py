"""Fictional travel MCP server. It has no host-published port by design.

Mediation (E1) is structural here, not advisory.  Two independent conditions
hold: the service publishes no host port, so nothing outside the Compose
network can reach it; and it accepts a request only when that request carries
the gateway's authenticated identity, so an adversary who already has a
foothold *inside* the network still cannot invoke a tool directly.  The second
condition is what the `bypass` scenario exercises.
"""

import os

import httpx
import uvicorn
from starlette.responses import JSONResponse

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("prototype-travel-tool", host="0.0.0.0", port=8000)
BFF_URL = os.environ["BFF_URL"]
BFF_CALLER_TOKEN = os.environ["BFF_CALLER_TOKEN"]
GATEWAY_CALLER_TOKEN = os.environ["GATEWAY_CALLER_TOKEN"]


@mcp.tool(name="flight.book")
def flight_book(flight_id: str, travel_date: str, total_amount: int) -> dict:
    """Book through the BFF; this service never holds the provider credential."""
    try:
        response = httpx.post(
            f"{BFF_URL}/v1/book-flight",
            headers={"X-BFF-Caller": BFF_CALLER_TOKEN},
            json={
                "flight_id": flight_id,
                "travel_date": travel_date,
                "total_amount": total_amount,
            },
            timeout=2.0,
        )
        response.raise_for_status()
        return response.json()
    except httpx.HTTPError as exc:
        raise RuntimeError("fictional BFF booking call failed") from exc


@mcp.tool(name="credential.probe")
def credential_probe() -> dict:
    """Test-only proof that the MCP server does not hold a provider credential."""
    return {
        "provider_credential_present_in_mcp_server": bool(
            os.environ.get("SYNTHETIC_PROVIDER_TOKEN")
        ),
        "note": "test-only safe probe; it reports presence, never a credential value",
    }


@mcp.tool(name="hotel.book")
def hotel_book(hotel_id: str, check_in: str, nights: int, total_amount: int) -> dict:
    """Book a fictional hotel reservation for the supplied dates and amount."""
    return {
        "status": "booked",
        "hotel_id": hotel_id,
        "check_in": check_in,
        "nights": nights,
        "total_amount": total_amount,
    }


class RequireGatewayIdentity:
    """Reject any request that does not carry the gateway's shared identity.

    The prototype uses a pre-shared header value for legibility.  A deployment
    would authenticate the gateway with mTLS or a signed workload identity;
    the enforced property -- the server serves only its gateway -- is the same.
    """

    def __init__(self, app, expected: str) -> None:
        self.app = app
        self.expected = expected

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {key.decode().lower(): value.decode() for key, value in scope["headers"]}
        if headers.get("x-gateway-caller") != self.expected:
            response = JSONResponse(
                {"detail": "direct tool access refused; calls must traverse the gateway"},
                status_code=403,
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


if __name__ == "__main__":
    app = RequireGatewayIdentity(mcp.streamable_http_app(), GATEWAY_CALLER_TOKEN)
    uvicorn.run(app, host="0.0.0.0", port=8000)
