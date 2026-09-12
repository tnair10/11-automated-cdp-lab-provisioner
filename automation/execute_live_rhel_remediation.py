#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
import platform
from pathlib import Path


ALLOWED_ACTIONS = {
    "network.hostname",
    "network.ipv6_disabled",
    "services.chronyd.enabled",
    "services.chronyd.active",
    "services.time_synced",
    "services.firewalld.enabled",
    "services.fapolicyd.enabled",
    "services.selinux.mode",
    "kernel.net.core.somaxconn",
    "kernel.vm.swappiness",
    "kernel.transparent_hugepages",
    "java.major_version",
    "cgroups.mode",
}


def load_json(path):
    return json.loads(Path(path).read_text())


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def os_release():
    data = {}
    path = Path("/etc/os-release")
    if not path.exists():
        return data

    for line in path.read_text().splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key] = value.strip().strip(chr(34))
    return data


def deny(reason, expected, actual):
    return {
        "id": reason,
        "expected": expected,
        "actual": actual,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Project 11 guarded live RHEL remediation executor"
    )
    parser.add_argument("--plan", required=True)
    parser.add_argument("--gate", required=True)
    parser.add_argument("--node", required=True)
    parser.add_argument("--confirm-live-execution", action="store_true")
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--json-out")
    args = parser.parse_args()

    plan = load_json(args.plan)
    gate = load_json(args.gate)

    failures = []

    if os.geteuid() != 0:
        failures.append(
            deny("runtime.root", 0, os.geteuid())
        )

    release = os_release()
    os_id = release.get("ID")
    version_id = release.get("VERSION_ID", "")
    machine = platform.machine()

    if os_id not in {"rhel", "redhat"}:
        failures.append(
            deny("runtime.os_family", "rhel", os_id)
        )

    if not version_id.startswith("8.10"):
        failures.append(
            deny("runtime.os_version", "8.10", version_id)
        )

    if machine != "x86_64":
        failures.append(
            deny("runtime.architecture", "x86_64", machine)
        )

    if not args.confirm_live_execution:
        failures.append(
            deny("safety.cli_confirmation", True, False)
        )

    env_confirmation = os.environ.get("P11_CONFIRM_LIVE_EXECUTION")
    if env_confirmation != "yes":
        failures.append(
            deny("safety.environment_confirmation", "yes", env_confirmation)
        )

    if not args.preflight_only:
        failures.append(
            deny(
                "safety.execution_stage",
                "preflight-only",
                "mutation-not-enabled",
            )
        )

    if gate.get("decision") != "ALLOW":
        failures.append(
            deny("gate.decision", "ALLOW", gate.get("decision"))
        )

    if gate.get("failure_count") != 0:
        failures.append(
            deny("gate.failure_count", 0, gate.get("failure_count"))
        )

    if gate.get("node") != args.node:
        failures.append(
            deny("gate.node", args.node, gate.get("node"))
        )

    if plan.get("node") != args.node:
        failures.append(
            deny("plan.node", args.node, plan.get("node"))
        )

    if plan.get("blocker_count") != 0:
        failures.append(
            deny("plan.blocker_count", 0, plan.get("blocker_count"))
        )

    actual_hash = sha256(args.plan)
    expected_hash = gate.get("plan_sha256")

    if actual_hash != expected_hash:
        failures.append(
            deny("plan.sha256", expected_hash, actual_hash)
        )

    actions = plan.get("actions")

    if not isinstance(actions, list):
        failures.append(
            deny("plan.actions", "list", type(actions).__name__)
        )
        actions = []

    unknown_actions = sorted({
        action.get("id")
        for action in actions
        if action.get("id") not in ALLOWED_ACTIONS
    })

    if unknown_actions:
        failures.append(
            deny("plan.allowed_actions", [], unknown_actions)
        )

    if plan.get("action_count") != len(actions):
        failures.append(
            deny(
                "plan.action_count",
                len(actions),
                plan.get("action_count"),
            )
        )

    allowed = len(failures) == 0

    result = {
        "mode": "live-execution-preflight",
        "decision": "ALLOW" if allowed else "DENY",
        "node": args.node,
        "plan_sha256": actual_hash,
        "failure_count": len(failures),
        "failures": failures,
        "action_count": len(actions),
        "reboot_required": plan.get("reboot_required"),
        "mutations_performed": False,
    }

    print("========================================")
    print(" PROJECT 11 LIVE RHEL EXECUTOR")
    print("========================================")
    print("Mode:              PREFLIGHT ONLY")
    print("Node:              " + args.node)
    print("Decision:          " + result["decision"])
    print("Failures:          " + str(len(failures)))
    print("Actions validated: " + str(len(actions)))
    print("Mutations:         NONE")

    if failures:
        print("")
        print("DENY REASONS")
        for item in failures:
            print(
                "DENY  "
                + item["id"]
                + " expected="
                + repr(item["expected"])
                + " actual="
                + repr(item["actual"])
            )

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(result, indent=2) + "\n"
        )

    return 0 if allowed else 2


if __name__ == "__main__":
    raise SystemExit(main())
