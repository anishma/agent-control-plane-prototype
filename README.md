# Agent Control Plane - Minimal Reference Prototype

This independent research prototype demonstrates the core request path of a
standards-based control plane for AI-agent MCP tool use. It is a new, minimal
composition of public open-source products and standards; it does not reuse
company code, data, schemas, credentials, tests, or internal documentation.

## What it demonstrates

```text
Keycloak user + OAuth client identity
              |
              v
Agentgateway (mandatory MCP mediation / PEP)
              |
              +-- fresh mandate lookup (in-memory, revocable)
              +-- AuthZEN evaluation (Cedar PDP)
              +-- custody receipt (RS256 JWS, hash-linked)
              |
              v
travel MCP tool (gateway-only path; rejects callers without gateway identity)
              |
              v
travel BFF credential broker <-- per-principal credential -- synthetic vault
              |
              +-- injects credential --> fictional travel API
              |
              v
        execution receipt, linked to its custody receipt by hash
```

- **Mediation:** enforced two ways. The mock MCP server publishes no host port,
  and it rejects with `403` any request that does not carry the gateway's
  authenticated identity, so an adversary who already has a foothold *inside*
  the network still cannot invoke a tool directly.
- **User and agent identity:** Keycloak issues the user subject (`sub`) and the
  OAuth client identity (`azp`). The gateway forwards only these validated
  claims to the policy bridge.
- **Revocable delegation:** the `mandate-registry` initializes its process-local
  state from YAML, then serves a fresh lookup for every `tools/call`. Its admin
  endpoint revokes a mandate without a restart; the following tool call denies.
- **Externalized authorization:** the bridge sends an OpenID AuthZEN-style
  decision request to a Cedar-backed PDP. The delegating user and mandate id
  travel in the agent subject's AuthZEN `properties`, not request context.
- **Credential brokering:** `flight.book` calls a local BFF. The BFF
  authenticates to a separate synthetic credential vault, resolves Elena's
  credential for the fictional travel provider, and injects it into that API
  call. The MCP server holds only a separate internal caller token. The
  test-only `credential.probe` tool reports whether a provider credential is
  present in the MCP-server environment, without revealing any credential
  value.
- **Tamper-evident receipts:** every mediated call mints a pair of RS256 JWS
  receipts at a separate receipt log — a custody receipt after the decision and
  before dispatch, and an execution receipt that references its custody receipt
  by SHA-256 digest. Denied calls are receipted too. Receipts are hash-linked
  into an append-only chain and the verification key is published as a JWKS, so
  any relying party verifies them offline without trusting the log store.

## Prototype-only token mechanics

For a self-contained local demonstration, `client/demo.py` obtains a Keycloak
inbound user access token through the Resource Owner Password Credentials
(`password`) grant, authenticating as the fixed `agent-client` OAuth client.
This is **not a production-grade token-acquisition pattern**: it deliberately
uses fixed demo credentials and maps `sub` to the delegating user and `azp` to
the prototype agent-client identifier. It is neither an OBO token nor a
demonstration of downstream token exchange. A production deployment should use
an appropriate interactive or workload identity flow, protect secrets outside
source code, and select its own authenticated agent-identity mechanism.

## Run

```bash
docker compose up --build -d
python3.12 -m venv .venv
./.venv/bin/pip install -r client/requirements.txt

# Everything at once: all scenarios, the in-network adversary, and verification.
./.venv/bin/python tests/validate.py

# Or individually:
./.venv/bin/python client/demo.py allowed
./.venv/bin/python client/demo.py credential-probe
./.venv/bin/python client/demo.py injected
./.venv/bin/python client/demo.py over-limit
./.venv/bin/python client/demo.py revoke
./.venv/bin/python client/demo.py bypass
./.venv/bin/python client/demo.py exfiltration
./.venv/bin/python client/demo.py receipts
```

`allowed` books a permitted flight through the BFF and the fictional FastAPI
provider. Its result includes `credential_brokered: true`; the BFF retrieves
the synthetic provider credential from the local vault for this call. The
credential is neither returned nor configured in the MCP server.
`credential-probe` is a separately mandated, test-only isolation check and
returns `provider_credential_present_in_mcp_server: false`. `injected`
attempts `hotel.book`, which is not present in the mandate and is denied before
the PDP/tool. `over-limit` shows Cedar rejecting a flight above the policy
threshold. `revoke` proves that an active mandate permits a call, revokes it
through the registry, then shows the next call is denied; it restores the
seeded state afterwards so the suite is repeatable.

## Adversarial scenarios

Three attack families are exercised, and every attempt must fail.

`bypass` tries to reach a tool without the control plane: an unauthenticated
call, a malformed bearer token, and a **forged-signature JWT** carrying the
correct issuer, audience, and subject but signed by a key the gateway's JWKS
does not publish. It then runs a **control**: the same undelegated `hotel.book`
call is invoked through the gateway-only baseline, where it *succeeds*, and
through the control-plane gateway, where it is denied. The control shows the
denials come from the architecture and not from something incidental to the
transport or the tool.

`exfiltration` asserts that the synthetic provider credential never appears in
any response the client can observe, on either the permitted or the probe path.

The `attack` profile runs the same two families from *inside* the Compose
network, which is the more generous threat model — the adversary already has a
foothold, so every service is routable:

```bash
docker compose --profile attack run --rm attacker
```

It attempts a direct `tools/call` on the MCP server with no gateway identity
and with a forged one; direct BFF booking calls with no and with a forged
caller token; direct vault credential reads with no and with a forged client
token; and a direct provider call without the brokered credential. All seven
are refused, and the script fails if any succeeds or if the credential value
appears in any response body.

## Verifying the receipt log

```bash
./.venv/bin/python tests/verify_receipts.py
```

The verifier fetches only the published JWKS and the receipts. It checks that
every signature verifies, that each recorded digest matches the receipt bytes,
that the hash chain is contiguous and correctly ordered, that every execution
receipt pairs with a preceding custody receipt sharing its decision id, and
that no tool executed without a custody record. It then alters a receipt and
confirms verification rejects it, so tamper-evidence is demonstrated rather
than asserted.

## Optional LLM agent demonstration

`agent_demo` is a single-agent LangChain harness running on LangGraph. It
discovers the travel tools from Agentgateway through MCP, and the LLM—not a
hard-coded test client—selects a tool and arguments. It is an optional Compose
profile so the core prototype does not require an LLM-provider key.

Start the core services first, then verify that the agent sees tools only
through Agentgateway (this operation does not call an LLM):

```bash
docker compose up --build -d
docker compose --profile agent-demo build agent-demo
docker compose --profile agent-demo run --rm agent-demo list-tools
docker compose --profile agent-demo run --rm agent-demo validate-tools
```

To run an LLM scenario, provide an API key only in your terminal environment,
not in a source file or Compose file. `AGENT_MODEL` defaults to
`openai:gpt-5.5`; override it with another compatible LangChain model if
needed.

```bash
export OPENAI_API_KEY='your-key'
docker compose --profile agent-demo run --rm agent-demo allowed
docker compose --profile agent-demo run --rm agent-demo injected
```

`allowed` asks the LLM to book a permitted flight. `injected` supplies a
fictional untrusted itinerary note whose embedded instruction asks the model to
book a hotel. The expected observable result is a model-selected hotel tool
call followed by the gateway's delegation denial. This is a controlled
adversarial simulation, not a claim that any specific model will follow
arbitrary injected text in normal use. The runner prints model-selected tool
calls, MCP results, and the final model response.

## Evaluation boundary

Run a repeatable local feasibility microbenchmark with a gateway-only baseline
and the evaluated delegation-plus-PDP path:

```bash
docker compose up --build -d
./.venv/bin/python tests/benchmark.py --runs 50 --warmup 10
```

Pass `--json-out results.json` to retain the raw samples and summary from a
particular run for analysis or inclusion in a revision package.

The benchmark obtains one demo access token before timing, then measures a
fresh MCP session and permitted `flight.book` invocation for every sample. The
baseline and evaluated paths have the same Keycloak authentication, Agentgateway
version, route, gateway-to-tool identity header, MCP tool, BFF, vault lookup,
and fictional provider API; only the evaluated path adds a fresh mandate
lookup, an AuthZEN/Cedar decision, and the two receipts. Three quantities are
reported:

- **Latency** — mean, median, p95, and standard deviation for both paths, plus
  the median difference.
- **Computational overhead** — cumulative CPU microseconds and resident memory
  for the four services the baseline does not run (`gateway-bridge`,
  `mandate-registry`, `authzen-pdp`, `receipt-log`), read from their cgroups
  and divided by the call count to give CPU milliseconds per call.
- **Throughput** — calls per second under a fixed number of concurrent clients
  on both paths, and the percentage retained.

It is a controlled local feasibility measurement on a single machine, not a
production capacity benchmark.

Two implementation details matter for the numbers and are worth preserving in
any reimplementation: the receipt log signs with a retained key object rather
than re-parsing PEM bytes per call, and the bridge uses one pooled HTTP client
for the registry, PDP, and receipt hops. Getting either wrong dominates the
measurement — with a per-call PEM parse and unpooled connections the same code
added 74 ms per call instead of roughly 10 ms.

If you rebuild or recreate `travel-tool` on its own, restart the gateways
afterwards (`docker compose restart gateway gateway-baseline`). Agentgateway
holds the resolved upstream address, so a recreated tool container comes back
on a new address and calls fail with `Connection refused` until the gateway
re-resolves it. A first-time `docker compose up --build -d` is unaffected.

## Prototype limitations

Stated so they are not mistaken for design positions:

- The mandate registry keeps state in process memory and seeds it from YAML, so
  it does not survive a restart, and its admin endpoints are unauthenticated. A
  deployment needs a durable store and an authenticated administrative plane.
  The only effect an in-network caller can reach through those endpoints is
  revocation, which fails safe.
- The gateway's identity to the MCP server, the MCP server's identity to the
  BFF, and the BFF's identity to the vault are pre-shared header values chosen
  for legibility. A deployment would use mTLS or signed workload identity; the
  enforced property is the same.
- The ExtMcp response message carries no request identifier, so the bridge
  pairs a custody receipt with its execution receipt through a per-identity
  FIFO. That is exact for the sequential scenarios here; a deployment would
  correlate on a gateway-issued call id.
- The receipt-signing keypair is generated at startup and held in memory. A
  deployment would hold it in a KMS or HSM and publish a stable, rotated JWKS.
- The credential vault, provider, and BFF share one non-sensitive synthetic
  token value so the local path is executable end to end.

## Components and licenses

- [Agentgateway](https://github.com/agentgateway/agentgateway) (Apache-2.0)
- [Keycloak](https://github.com/keycloak/keycloak) (Apache-2.0)
- [Cedar](https://github.com/cedar-policy/cedar) (Apache-2.0)
- [OpenID AuthZEN Authorization API](https://openid.net/specs/authorization-api-1_0-01.html)
- [Model Context Protocol Python SDK](https://github.com/modelcontextprotocol/python-sdk)

`adapter/ext_mcp.proto` is the public Agentgateway ExtMcp protocol and retains
its upstream Apache-2.0 notice. All other prototype code is MIT licensed.
