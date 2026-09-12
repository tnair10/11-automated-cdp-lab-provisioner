#!/usr/bin/env python3
import argparse
import json
from pathlib import Path

import yaml


def load_json(path):
    return json.loads(Path(path).read_text())


def add_blocker(items, check_id, expected, actual):
    items.append({
        "id": check_id,
        "expected": expected,
        "actual": actual,
    })


def add_action(items, action_id, expected, actual, description, reboot=False):
    if actual != expected:
        items.append({
            "id": action_id,
            "expected": expected,
            "actual": actual,
            "description": description,
            "reboot_required": reboot,
        })


def main():
    parser = argparse.ArgumentParser(
        description="Project 11 dry-run RHEL remediation planner"
    )
    parser.add_argument("--facts", required=True)
    parser.add_argument("--node", required=True)
    parser.add_argument("--config", default="config/host-prep.yaml")
    parser.add_argument("--json-out")
    args = parser.parse_args()

    facts = load_json(args.facts)
    cfg = yaml.safe_load(Path(args.config).read_text())

    if args.node not in cfg["cluster"]["nodes"]:
        parser.error("Unknown node: " + args.node)

    platform = cfg["platform"]
    host = cfg["host_requirements"]
    network = cfg["network"]
    services = cfg["services"]
    kernel = cfg["kernel"]
    java = cfg["java"]
    cgroups = cfg["cgroups"]

    blockers = []
    actions = []

    platform_checks = [
        ("platform.os_family", platform["os_family"], facts.get("os_family")),
        ("platform.os_major", platform["os_major"], facts.get("os_major")),
        ("platform.os_minor", platform["os_minor"], facts.get("os_minor")),
        ("platform.architecture", platform["architecture"], facts.get("architecture")),
    ]

    for check_id, expected, actual in platform_checks:
        if actual != expected:
            add_blocker(blockers, check_id, expected, actual)

    capacity_checks = [
        ("host.minimum_vcpu", host["minimum_vcpu"], facts.get("vcpu")),
        ("host.minimum_memory_mb", host["minimum_memory_mb"], facts.get("memory_mb")),
        ("host.minimum_root_gb", host["minimum_root_gb"], facts.get("root_gb")),
    ]

    for check_id, minimum, actual in capacity_checks:
        if actual is None or actual < minimum:
            add_blocker(blockers, check_id, ">=" + str(minimum), actual)

    if network.get("require_forward_resolution") and facts.get("forward_resolution") is not True:
        add_blocker(
            blockers,
            "network.forward_resolution",
            True,
            facts.get("forward_resolution"),
        )

    if network.get("require_reverse_resolution") and facts.get("reverse_resolution") is not True:
        add_blocker(
            blockers,
            "network.reverse_resolution",
            True,
            facts.get("reverse_resolution"),
        )

    if network.get("require_consistent_hosts_file") and facts.get("hosts_file_consistent") is not True:
        add_blocker(
            blockers,
            "network.hosts_file_consistent",
            True,
            facts.get("hosts_file_consistent"),
        )

    fqdn = args.node + "." + cfg["cluster"]["domain"]

    if facts.get("hostname") != fqdn:
        add_action(
            actions,
            "network.hostname",
            fqdn,
            facts.get("hostname"),
            "Set the persistent hostname to " + fqdn,
        )

    if network.get("require_ipv6_disabled"):
        add_action(
            actions,
            "network.ipv6_disabled",
            True,
            facts.get("ipv6_disabled"),
            "Disable IPv6 persistently and at runtime",
        )

    add_action(
        actions,
        "services.chronyd.enabled",
        services["chronyd"]["enabled"],
        facts.get("chronyd_enabled"),
        "Enable chronyd",
    )

    add_action(
        actions,
        "services.chronyd.active",
        True,
        facts.get("chronyd_active"),
        "Start chronyd",
    )

    add_action(
        actions,
        "services.time_synced",
        True,
        facts.get("time_synced"),
        "Wait for and verify time synchronization",
    )

    add_action(
        actions,
        "services.firewalld.enabled",
        services["firewalld"]["enabled"],
        facts.get("firewalld_enabled"),
        "Disable firewalld for the isolated lab network",
    )

    add_action(
        actions,
        "services.fapolicyd.enabled",
        services["fapolicyd"]["enabled"],
        facts.get("fapolicyd_enabled"),
        "Disable fapolicyd before CDP installation",
    )

    add_action(
        actions,
        "services.selinux.mode",
        services["selinux"]["mode"],
        facts.get("selinux"),
        "Set SELinux mode to " + services["selinux"]["mode"],
    )

    add_action(
        actions,
        "kernel.net.core.somaxconn",
        kernel["net.core.somaxconn"],
        facts.get("somaxconn"),
        "Persist and apply net.core.somaxconn",
    )

    add_action(
        actions,
        "kernel.vm.swappiness",
        kernel["vm.swappiness"],
        facts.get("swappiness"),
        "Persist and apply vm.swappiness",
    )

    add_action(
        actions,
        "kernel.transparent_hugepages",
        kernel["transparent_hugepages"],
        facts.get("thp"),
        "Disable transparent huge pages",
    )

    add_action(
        actions,
        "java.major_version",
        java["major_version"],
        facts.get("java_major_version"),
        "Install and select OpenJDK " + str(java["major_version"]),
    )

    add_action(
        actions,
        "cgroups.mode",
        cgroups["required_mode"],
        facts.get("cgroup_mode"),
        "Configure the required cgroup mode",
        reboot=True,
    )

    reboot_required = any(item["reboot_required"] for item in actions)

    result = {
        "mode": "dry-run",
        "node": args.node,
        "fqdn": fqdn,
        "blocker_count": len(blockers),
        "action_count": len(actions),
        "reboot_required": reboot_required,
        "ready_without_changes": not blockers and not actions,
        "blockers": blockers,
        "actions": actions,
    }

    print("========================================")
    print(" PROJECT 11 RHEL REMEDIATION PLAN")
    print("========================================")
    print("Node:            " + args.node)
    print("FQDN:            " + fqdn)
    print("Blockers:        " + str(len(blockers)))
    print("Planned actions: " + str(len(actions)))
    print("Reboot required: " + str(reboot_required))

    if blockers:
        print("")
        print("BLOCKERS")
        for item in blockers:
            print(
                "BLOCK "
                + item["id"]
                + " expected="
                + repr(item["expected"])
                + " actual="
                + repr(item["actual"])
            )

    if actions:
        print("")
        print("PLANNED ACTIONS")
        for item in actions:
            suffix = " [REBOOT]" if item["reboot_required"] else ""
            print(
                "PLAN  "
                + item["id"]
                + suffix
                + " expected="
                + repr(item["expected"])
                + " actual="
                + repr(item["actual"])
            )

    print("")
    print("No host changes were made.")

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(result, indent=2) + "\n"
        )

    return 2 if blockers else 0


if __name__ == "__main__":
    raise SystemExit(main())
