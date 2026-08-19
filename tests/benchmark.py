"""Measure local end-to-end latency and computational overhead for both paths.

This is a feasibility microbenchmark, not a production capacity test.  Both
paths use the same Keycloak-authenticated Agentgateway, gateway-to-tool
identity header, fictional MCP tool, BFF, vault lookup, and provider API.  The
evaluated path additionally performs a fresh mandate resolution, an
AuthZEN/Cedar decision, and receipt minting for every tool call.

Three quantities are reported:

  latency     -- end-to-end wall clock per call, mean/p50/p95, both paths.
  cpu         -- CPU time consumed by the control-plane services that the
                 baseline does not have, read from their cgroups as cumulative
                 microseconds and divided by the call count.
  throughput  -- calls per second under a fixed number of concurrent clients.
"""

import argparse
import asyncio
import json
import pathlib
import statistics
import subprocess
import sys
import time

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from client.demo import access_token, error_detail

BASELINE_URL = "http://localhost:3002/mcp"
CONTROL_PLANE_URL = "http://localhost:3001/mcp"
FLIGHT_ARGUMENTS = {"flight_id": "FL-204", "travel_date": "2026-09-15", "total_amount": 480}

# The services the evaluated path adds; the baseline runs without them.
CONTROL_PLANE_SERVICES = ["gateway-bridge", "mandate-registry", "authzen-pdp", "receipt-log"]


def cgroup_sample(service: str) -> dict[str, int] | None:
    """Cumulative CPU microseconds and current memory bytes for one service."""
    try:
        cpu = subprocess.run(
            ["docker", "compose", "exec", "-T", service, "cat", "/sys/fs/cgroup/cpu.stat"],
            cwd=ROOT, capture_output=True, text=True, timeout=20, check=True,
        ).stdout
        memory = subprocess.run(
            ["docker", "compose", "exec", "-T", service, "cat", "/sys/fs/cgroup/memory.current"],
            cwd=ROOT, capture_output=True, text=True, timeout=20, check=True,
        ).stdout
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return None
    usage = next(
        (int(line.split()[1]) for line in cpu.splitlines() if line.startswith("usage_usec")), None
    )
    if usage is None:
        return None
    return {"cpu_usec": usage, "memory_bytes": int(memory.strip())}


def sample_all() -> dict[str, dict[str, int]]:
    samples = {}
    for service in CONTROL_PLANE_SERVICES:
        sample = cgroup_sample(service)
        if sample is not None:
            samples[service] = sample
    return samples


async def invoke(url: str, token: str) -> float:
    started = time.perf_counter()
    headers = {"Authorization": f"Bearer {token}"}
    async with streamablehttp_client(url, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("flight.book", FLIGHT_ARGUMENTS)
    if result.isError:
        detail = " ".join(str(block) for block in result.content)
        raise RuntimeError(f"MCP tool returned an error: {detail}")
    return (time.perf_counter() - started) * 1000


async def samples(label: str, url: str, token: str, warmup: int, runs: int) -> list[float]:
    try:
        for _ in range(warmup):
            await invoke(url, token)
        values = [await invoke(url, token) for _ in range(runs)]
    except Exception as exc:
        raise RuntimeError(f"{label} path failed: {error_detail(exc)}") from exc
    return values


async def throughput(url: str, token: str, concurrency: int, per_client: int) -> float:
    """Calls per second with `concurrency` clients each issuing `per_client` calls."""

    async def worker() -> None:
        for _ in range(per_client):
            await invoke(url, token)

    started = time.perf_counter()
    await asyncio.gather(*(worker() for _ in range(concurrency)))
    elapsed = time.perf_counter() - started
    return (concurrency * per_client) / elapsed


def percentile(values: list[float], percent: float) -> float:
    ordered = sorted(values)
    rank = (len(ordered) - 1) * percent / 100
    lower, upper = int(rank), min(int(rank) + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (rank - lower)


def report(label: str, values: list[float]) -> dict[str, float]:
    metrics = {
        "mean": statistics.fmean(values),
        "p50": statistics.median(values),
        "p95": percentile(values, 95),
        "stdev": statistics.stdev(values) if len(values) > 1 else 0.0,
    }
    print(
        f"{label:24} {metrics['mean']:8.1f} {metrics['p50']:8.1f} "
        f"{metrics['p95']:8.1f} {metrics['stdev']:8.1f}"
    )
    return metrics


def cpu_delta(before: dict, after: dict, calls: int) -> dict:
    """CPU milliseconds per call and memory for each control-plane service."""
    result = {}
    for service in before:
        if service not in after:
            continue
        used_usec = after[service]["cpu_usec"] - before[service]["cpu_usec"]
        result[service] = {
            "cpu_ms_per_call": used_usec / 1000 / calls,
            "memory_mib": after[service]["memory_bytes"] / (1024 * 1024),
        }
    result["total_cpu_ms_per_call"] = sum(
        entry["cpu_ms_per_call"] for entry in result.values() if isinstance(entry, dict)
    )
    return result


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=30, help="measured calls per path (default: 30)")
    parser.add_argument("--warmup", type=int, default=5, help="unreported calls per path (default: 5)")
    parser.add_argument("--concurrency", type=int, default=8, help="concurrent clients for throughput")
    parser.add_argument("--per-client", type=int, default=5, help="calls per concurrent client")
    parser.add_argument("--skip-throughput", action="store_true")
    parser.add_argument("--json-out", type=pathlib.Path, help="write raw samples and summary to this file")
    args = parser.parse_args()
    if args.runs < 2 or args.warmup < 0:
        raise SystemExit("--runs must be at least 2 and --warmup must be non-negative")

    token = access_token()  # Token acquisition is deliberately outside timing.

    print(f"{'path':24} {'mean':>8} {'p50':>8} {'p95':>8} {'sd':>8}   (ms)")
    baseline = await samples("gateway-only baseline", BASELINE_URL, token, args.warmup, args.runs)
    baseline_cpu_before = sample_all()
    control = await samples("control-plane path", CONTROL_PLANE_URL, token, args.warmup, args.runs)
    baseline_cpu_after = sample_all()

    baseline_metrics = report("gateway-only baseline", baseline)
    control_metrics = report("control-plane path", control)
    added_median = control_metrics["p50"] - baseline_metrics["p50"]
    print(f"added median latency: {added_median:.1f} ms")

    overhead = {}
    if baseline_cpu_before and baseline_cpu_after:
        # CPU is attributed to the measured calls plus the warm-ups that preceded them.
        overhead = cpu_delta(
            baseline_cpu_before, baseline_cpu_after, args.runs + args.warmup
        )
        print("\ncontrol-plane computational overhead (services the baseline does not run)")
        for service in CONTROL_PLANE_SERVICES:
            entry = overhead.get(service)
            if entry:
                print(
                    f"  {service:18} {entry['cpu_ms_per_call']:6.2f} ms CPU/call "
                    f"{entry['memory_mib']:7.1f} MiB resident"
                )
        print(f"  {'total':18} {overhead['total_cpu_ms_per_call']:6.2f} ms CPU/call")

    rates = {}
    if not args.skip_throughput:
        print(f"\nthroughput ({args.concurrency} concurrent clients x {args.per_client} calls)")
        rates["baseline"] = await throughput(
            BASELINE_URL, token, args.concurrency, args.per_client
        )
        rates["control_plane"] = await throughput(
            CONTROL_PLANE_URL, token, args.concurrency, args.per_client
        )
        print(f"  gateway-only baseline  {rates['baseline']:6.1f} calls/s")
        print(f"  control-plane path     {rates['control_plane']:6.1f} calls/s")
        print(
            f"  retained throughput    "
            f"{100 * rates['control_plane'] / rates['baseline']:6.1f}%"
        )

    if args.json_out:
        args.json_out.write_text(
            json.dumps(
                {
                    "runs": args.runs,
                    "warmup": args.warmup,
                    "units": "milliseconds",
                    "gateway_only_baseline": {"samples": baseline, "summary": baseline_metrics},
                    "control_plane_path": {"samples": control, "summary": control_metrics},
                    "added_median_latency": added_median,
                    "control_plane_cpu": overhead,
                    "throughput_calls_per_second": rates,
                },
                indent=2,
            )
            + "\n"
        )


if __name__ == "__main__":
    asyncio.run(main())
