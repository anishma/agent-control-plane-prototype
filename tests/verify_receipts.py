"""Verify the receipt log offline, without trusting the log store.

A relying party fetches only the published JWKS and the receipts themselves.
It then checks four independent properties:

  1. Signature   -- every receipt verifies under the published key (RFC 7515).
  2. Integrity   -- each receipt's recorded digest matches its bytes.
  3. Chain       -- receipt n carries the digest of receipt n-1, so deletion
                    or reordering of the append-only log is detectable.
  4. Pairing     -- every execution receipt names a custody receipt that
                    exists, shares its decision id, and precedes it.

It finally re-runs the checks against a deliberately altered receipt to show
that tamper-evidence is enforced rather than asserted.
"""

import argparse
import copy
import hashlib
import json
import sys

import httpx
import jwt
from jwt import PyJWKClient

RECEIPT_LOG_URL = "http://localhost:8092"


def verify_signatures(receipts: list[dict], jwks_url: str) -> list[dict]:
    """Verify each JWS against the published JWKS and return the payloads."""
    client = PyJWKClient(jwks_url)
    payloads = []
    for entry in receipts:
        key = client.get_signing_key_from_jwt(entry["jws"]).key
        payloads.append(
            jwt.decode(entry["jws"], key, algorithms=["RS256"], options={"verify_aud": False})
        )
    return payloads


def check(condition: bool, label: str, failures: list[str]) -> None:
    print(f"  [{'ok' if condition else 'FAIL'}] {label}")
    if not condition:
        failures.append(label)


def verify(log: dict, jwks_url: str) -> list[str]:
    failures: list[str] = []
    receipts = log["receipts"]
    print(f"verifying {len(receipts)} receipts from {log['issuer']}")

    try:
        payloads = verify_signatures(receipts, jwks_url)
        check(True, f"all {len(receipts)} signatures verify under the published JWKS", failures)
    except Exception as exc:  # noqa: BLE001 - any failure is a verification failure
        check(False, f"signature verification: {type(exc).__name__}: {exc}", failures)
        return failures

    digests_ok = all(
        hashlib.sha256(entry["jws"].encode()).hexdigest() == entry["sha256"]
        for entry in receipts
    )
    check(digests_ok, "recorded digests match the receipt bytes", failures)

    chain_ok = True
    for index, payload in enumerate(payloads):
        expected = receipts[index - 1]["sha256"] if index else None
        if payload.get("prev_sha256") != expected or payload.get("seq") != index:
            chain_ok = False
    check(chain_ok, "hash chain is contiguous and correctly ordered", failures)

    by_digest = {
        entry["sha256"]: payload
        for entry, payload in zip(receipts, payloads)
        if payload["receipt_type"] == "custody"
    }
    executions = [p for p in payloads if p["receipt_type"] == "execution"]
    pairing_ok = all(
        p.get("custody_sha256") in by_digest
        and by_digest[p["custody_sha256"]]["decision_id"] == p["decision_id"]
        and by_digest[p["custody_sha256"]]["seq"] < p["seq"]
        for p in executions
    )
    check(
        pairing_ok,
        f"all {len(executions)} execution receipts pair with a preceding custody receipt",
        failures,
    )

    permits = [p for p in payloads if p.get("decision") == "permit"]
    denials = [p for p in payloads if p.get("decision") == "deny"]
    check(
        len(executions) <= len(permits),
        f"no tool executed without a custody record ({len(executions)} executions, "
        f"{len(permits)} permits, {len(denials)} receipted denials)",
        failures,
    )
    return failures


def verify_tampering_is_detected(log: dict, jwks_url: str) -> bool:
    """Alter one receipt's payload and confirm verification rejects it."""
    if not log["receipts"]:
        return False
    tampered = copy.deepcopy(log)
    entry = tampered["receipts"][0]
    header, payload, signature = entry["jws"].split(".")
    decoded = json.loads(
        __import__("base64").urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
    )
    decoded["tool"] = "hotel.book"  # claim a different tool was authorized
    forged = (
        __import__("base64")
        .urlsafe_b64encode(json.dumps(decoded, separators=(",", ":")).encode())
        .decode()
        .rstrip("=")
    )
    entry["jws"] = f"{header}.{forged}.{signature}"
    try:
        verify_signatures(tampered["receipts"], jwks_url)
    except Exception:  # noqa: BLE001 - rejection is the expected outcome
        return True
    return False


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=RECEIPT_LOG_URL, help="receipt log base URL")
    args = parser.parse_args()

    jwks_url = f"{args.url}/.well-known/jwks.json"
    log = httpx.get(f"{args.url}/v1/receipts", timeout=10.0).json()
    if not log["receipts"]:
        raise SystemExit("no receipts to verify; run a scenario first")

    failures = verify(log, jwks_url)

    print("tamper check")
    detected = verify_tampering_is_detected(log, jwks_url)
    check(detected, "an altered receipt is rejected by signature verification", failures)

    if failures:
        print(f"FAIL: {len(failures)} check(s) failed")
        sys.exit(1)
    print("PASS: receipt log verified offline against the published JWKS")


if __name__ == "__main__":
    main()
