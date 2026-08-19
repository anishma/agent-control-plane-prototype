"""Run every functional and adversarial scenario, then verify the receipt log.

Exits non-zero if any scenario fails, so the whole evaluation is one command.
"""

import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]

# Functional scenarios, then adversarial ones, then accountability.
SCENARIOS = [
    "allowed",
    "credential-probe",
    "injected",
    "over-limit",
    "revoke",
    "bypass",
    "exfiltration",
    "receipts",
]


def run(command: list[str], label: str) -> bool:
    print(f"\n=== {label} ===", flush=True)
    result = subprocess.run(command, cwd=ROOT, check=False)
    return result.returncode == 0


def main() -> None:
    failures = []

    for scenario in SCENARIOS:
        if not run([sys.executable, str(ROOT / "client" / "demo.py"), scenario], scenario):
            failures.append(scenario)

    # The in-network adversary runs inside the Compose network.
    if not run(
        ["docker", "compose", "--profile", "attack", "run", "--rm", "attacker"],
        "in-network adversary probes",
    ):
        failures.append("attacker")

    if not run(
        [sys.executable, str(ROOT / "tests" / "verify_receipts.py")], "offline receipt verification"
    ):
        failures.append("verify_receipts")

    print(f"\n{'=' * 40}")
    if failures:
        print(f"FAIL: {', '.join(failures)}")
        raise SystemExit(1)
    print(f"PASS: {len(SCENARIOS) + 2} checks")


if __name__ == "__main__":
    main()
