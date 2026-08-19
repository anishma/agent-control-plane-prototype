"""Time each control-plane hop as the enforcement point experiences it.

The end-to-end microbenchmark in tests/benchmark.py reports what a caller sees.
This reports where that added time goes, by issuing the same three requests the
bridge issues -- the mandate resolution, the AuthZEN decision, and the two
receipt mints -- from inside the Compose network, so no host port mapping is
included in the measurement.
"""

import argparse
import json
import os
import statistics
import time

import httpx

REGISTRY_URL = os.environ["MANDATE_REGISTRY_URL"]
PDP_URL = os.environ["PDP_URL"]
RECEIPT_LOG_URL = os.environ["RECEIPT_LOG_URL"]
PRINCIPAL = os.environ["DEMO_PRINCIPAL"]
AGENT = "agent-client"
ARGUMENTS = {"flight_id": "FL-204", "travel_date": "2026-09-15", "total_amount": 480}

AUTHZEN_REQUEST = {
    "subject": {
        "type": "agent",
        "id": AGENT,
        "properties": {"on_behalf_of": PRINCIPAL, "mandate_id": "travel-elena-v1"},
    },
    "action": {"name": "tools/call"},
    "resource": {"type": "mcp-tool", "id": "flight.book", "properties": {"arguments": ARGUMENTS}},
}


def percentile(values, percent):
    ordered = sorted(values)
    rank = (len(ordered) - 1) * percent / 100
    lower, upper = int(rank), min(int(rank) + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (rank - lower)


def timed(client, label, call, runs, warmup):
    for _ in range(warmup):
        call(client)
    samples = []
    for _ in range(runs):
        started = time.perf_counter()
        call(client)
        samples.append((time.perf_counter() - started) * 1000)
    return {
        "component": label,
        "mean": statistics.fmean(samples),
        "p50": statistics.median(samples),
        "p95": percentile(samples, 95),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=200)
    parser.add_argument("--warmup", type=int, default=20)
    parser.add_argument("--json-out")
    args = parser.parse_args()

    # The bridge holds one pooled client for every hop; mirror that here.
    with httpx.Client(timeout=5.0) as client:
        results = [
            timed(client, "Delegation read (E3)", lambda c: c.get(
                f"{REGISTRY_URL}/v1/mandates/resolve",
                params={"principal": PRINCIPAL, "agent_client": AGENT, "operation": "flight.book"},
            ).raise_for_status(), args.runs, args.warmup),
            timed(client, "AuthZEN decision (E2)", lambda c: c.post(
                f"{PDP_URL}/access/v1/evaluation", json=AUTHZEN_REQUEST
            ).raise_for_status(), args.runs, args.warmup),
            timed(client, "Receipt mint, one (E5)", lambda c: c.post(
                f"{RECEIPT_LOG_URL}/v1/receipts",
                json={"receipt_type": "custody", "claims": {"benchmark": True}},
            ).raise_for_status(), args.runs, args.warmup),
        ]

    print(f"{'component':26} {'mean':>7} {'p50':>7} {'p95':>7}   (ms)")
    for row in results:
        print(f"{row['component']:26} {row['mean']:7.2f} {row['p50']:7.2f} {row['p95']:7.2f}")
    receipts = next(r for r in results if r["component"].startswith("Receipt"))
    total = sum(r["p50"] for r in results) + receipts["p50"]  # two receipts per call
    print(f"{'sum (two receipts)':26} {total:7.2f} p50")

    if args.json_out:
        with open(args.json_out, "w") as handle:
            json.dump({"runs": args.runs, "components": results, "sum_p50": total}, handle, indent=2)


if __name__ == "__main__":
    main()
