#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
from pathlib import Path

import yaml


def load_json(path):
    return json.loads(Path(path).read_text())


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(
        description="Project 11 live RHEL remediation execution gate"
    )
    parser.add_argument("--facts", required=True)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--node", required=True)
    parser.add_argument("--config", default="config/host-prep.yaml")
    parser.add_argument("--confirm-live-host", action="store_true")
    parser.add_argument("--json-out")
    args = parser.parse_args()

    facts = load_json(args.facts)
    plan = load_json(args.plan)
    cfg = yaml.safe_load(Path(args.config).read_text())

    failures = []

    def require(check_id, expected, actual, ok):
        if not ok:
            failures.append({
                "id": check_id,
                "expected": expected,
                "actual": actual,
            })

    require(
        "safety.cli_confirmation",
        True,
        args.confirm_live_host,
        args.confirm_live_host is True,
    )

    env_confirmation = os.environ.get("P11_CONFIRM_LIVE_HOST")
    require(
        "safety.environment_confirmation",
        "yes",
        env_confirmation,
        env_confirmation == "yes",
    )

    nodes = cfg["cluster"]["nodes"]
    require(
        "cluster.known_node",
        True,
        args.node in nodes,
        args.node in nodes,
    )

    if args.node in nodes:
        expected_fqdn = args.node + "." + cfg["cluster"]["domain"]
    else:
        expected_fqdn = None

    platform = cfg["platform"]
    host = cfg["host_requirements"]

    require(
        "platform.os_family",
        platform["os_family"],
        facts.get("os_family"),
        facts.get("os_family") == platform["os_family"],
    )
    require(
        "platform.os_major",
        platform["os_major"],
        facts.get("os_major"),
        facts.get("os_major") == platform["os_major"],
    )
    require(
        "platform.os_minor",
        platform["os_minor"],
        facts.get("os_minor"),
        facts.get("os_minor") == platform["os_minor"],
    )
    require(
        "platform.architecture",
        platform["architecture"],
        facts.get("architecture"),
        facts.get("architecture") == platform["architecture"],
    )

    for key, fact_key in [
        ("minimum_vcpu", "vcpu"),
        ("minimum_memory_mb", "memory_mb"),
        ("minimum_root_gb", "root_gb"),
    ]:
        minimum = host[key]
        actual = facts.get(fact_key)
        require(
            "host." + key,
            ">=" + str(minimum),
            actual,
            actual is not None and actual >= minimum,
        )

    if expected_fqdn is not None:
        require(
            "network.hostname",
            expected_fqdn,
            facts.get("hostname"),
            facts.get("hostname") == expected_fqdn,
        )

    require(
        "network.forward_resolution",
        True,
        facts.get("forward_resolution"),
        facts.get("forward_resolution") is True,
    )
    require(
        "network.reverse_resolution",
        True,
        facts.get("reverse_resolution"),
        facts.get("reverse_resolution") is True,
    )
    require(
        "network.hosts_file_consistent",
        True,
        facts.get("hosts_file_consistent"),
        facts.get("hosts_file_consistent") is True,
    )

    require(
        "plan.mode",
        "dry-run",
        plan.get("mode"),
        plan.get("mode") == "dry-run",
    )
    require(
        "plan.node",
        args.node,
        plan.get("node"),
        plan.get("node") == args.node,
    )

    if expected_fqdn is not None:
        require(
            "plan.fqdn",
            expected_fqdn,
            plan.get("fqdn"),
            plan.get("fqdn") == expected_fqdn,
        )

    require(
        "plan.blocker_count",
        0,
        plan.get("blocker_count"),
        plan.get("blocker_count") == 0,
    )
    require(
        "plan.blockers",
        [],
        plan.get("blockers"),
        plan.get("blockers") == [],
    )

    actions = plan.get("actions")
    require(
        "plan.actions_type",
        "list",
        type(actions).__name__,
        isinstance(actions, list),
    )

    if isinstance(actions, list):
        require(
            "plan.action_count",
            len(actions),
            plan.get("action_count"),
            plan.get("action_count") == len(actions),
        )

    allowed = len(failures) == 0

    result = {
        "mode": "live-execution-gate",
        "decision": "ALLOW" if allowed else "DENY",
        "node": args.node,
        "expected_fqdn": expected_fqdn,
        "plan_sha256": sha256(args.plan),
        "failure_count": len(failures),
        "failures": failures,
        "action_count": plan.get("action_count"),
        "reboot_required": plan.get("reboot_required"),
    }

    print("========================================")
    print(" PROJECT 11 LIVE RHEL EXECUTION GATE")
    print("========================================")
    print("Node:          " + args.node)
    print("Decision:      " + result["decision"])
    print("Failures:      " + str(len(failures)))
    print("Plan actions:  " + str(plan.get("action_count")))
    print("Plan SHA256:   " + result["plan_sha256"])

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
