#!/usr/bin/env python3
import argparse
import json
import sys
from pathlib import Path

import yaml


def result(name, actual, expected, ok):
    return {
        "name": name,
        "actual": actual,
        "expected": expected,
        "status": "PASS" if ok else "FAIL",
    }


def evaluate(config, facts):
    p = config["platform"]
    h = config["host_requirements"]
    n = config["network"]
    s = config["services"]
    k = config["kernel"]
    j = config["java"]
    c = config["cgroups"]

    checks = [
        result("platform.os_family", facts.get("os_family"), p["os_family"],
               facts.get("os_family") == p["os_family"]),
        result("platform.os_major", facts.get("os_major"), p["os_major"],
               facts.get("os_major") == p["os_major"]),
        result("platform.os_minor", facts.get("os_minor"), p["os_minor"],
               facts.get("os_minor") == p["os_minor"]),
        result("platform.architecture", facts.get("architecture"), p["architecture"],
               facts.get("architecture") == p["architecture"]),
        result("host.minimum_vcpu", facts.get("cpu_count"), h["minimum_vcpu"],
               (facts.get("cpu_count") or 0) >= h["minimum_vcpu"]),
        result("host.minimum_memory_mb", facts.get("memory_mb"), h["minimum_memory_mb"],
               (facts.get("memory_mb") or 0) >= h["minimum_memory_mb"]),
        result("host.minimum_root_gb", facts.get("root_gb"), h["minimum_root_gb"],
               (facts.get("root_gb") or 0) >= h["minimum_root_gb"]),
    ]

    if n.get("require_lowercase_hostname"):
        checks.append(result("network.hostname_lowercase",
                             facts.get("hostname_lowercase"), True,
                             facts.get("hostname_lowercase") is True))
    if n.get("require_forward_resolution"):
        checks.append(result("network.forward_resolution",
                             facts.get("forward_resolution"), True,
                             facts.get("forward_resolution") is True))
    if n.get("require_reverse_resolution"):
        checks.append(result("network.reverse_resolution",
                             facts.get("reverse_resolution"), True,
                             facts.get("reverse_resolution") is True))
    if n.get("require_consistent_hosts_file"):
        checks.append(result("network.hosts_file_consistent",
                             facts.get("hosts_file_consistent"), True,
                             facts.get("hosts_file_consistent") is True))

    checks.extend([
        result("services.chronyd.active", facts.get("chronyd_active"), True,
               facts.get("chronyd_active") is True),
        result("services.chronyd.enabled", facts.get("chronyd_enabled"),
               s["chronyd"]["enabled"],
               facts.get("chronyd_enabled") == s["chronyd"]["enabled"]),
        result("services.time_synced", facts.get("time_synced"), True,
               facts.get("time_synced") is True),
        result("services.firewalld.enabled", facts.get("firewalld_enabled"),
               s["firewalld"]["enabled"],
               facts.get("firewalld_enabled") == s["firewalld"]["enabled"]),
        result("services.fapolicyd.enabled", facts.get("fapolicyd_enabled"),
               s["fapolicyd"]["enabled"],
               facts.get("fapolicyd_enabled") == s["fapolicyd"]["enabled"]),
        result("services.selinux.mode", facts.get("selinux"),
               s["selinux"]["mode"],
               facts.get("selinux") == s["selinux"]["mode"]),
        result("kernel.net.core.somaxconn", facts.get("somaxconn"),
               k["net.core.somaxconn"],
               (facts.get("somaxconn") or 0) >= k["net.core.somaxconn"]),
        result("kernel.vm.swappiness", facts.get("swappiness"),
               k["vm.swappiness"],
               facts.get("swappiness") == k["vm.swappiness"]),
        result("kernel.transparent_hugepages", facts.get("thp"),
               k["transparent_hugepages"],
               facts.get("thp") == k["transparent_hugepages"]),
        result("java.major_version", facts.get("java_major"),
               j["major_version"],
               facts.get("java_major") == j["major_version"]),
        result("cgroups.mode", facts.get("cgroup_mode"),
               c["required_mode"],
               facts.get("cgroup_mode") == c["required_mode"]),
    ])

    return checks


def main():
    ap = argparse.ArgumentParser(description="Project 11 RHEL host-prep checker")
    ap.add_argument("--config", default="config/host-prep.yaml")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--facts", required=True,
                    help="JSON host-facts file; live collection comes in Stage 6A.3")
    ap.add_argument("--json-out")
    args = ap.parse_args()

    if not args.check:
        ap.error("--check is required; mutation is intentionally disabled")

    config = yaml.safe_load(Path(args.config).read_text())
    facts = json.loads(Path(args.facts).read_text())
    checks = evaluate(config, facts)

    passed = sum(x["status"] == "PASS" for x in checks)
    failed = len(checks) - passed
    overall = "PASS" if failed == 0 else "FAIL"

    print("========================================")
    print(" PROJECT 11 RHEL HOST PREP CHECK")
    print("========================================")
    for item in checks:
        print(
            f"{item['status']:<4} {item['name']:<36} "
            f"expected={item['expected']!r} actual={item['actual']!r}"
        )

    print()
    print(f"Checks passed: {passed}")
    print(f"Checks failed: {failed}")
    print(f"Overall:       {overall}")

    payload = {
        "overall": overall,
        "passed": passed,
        "failed": failed,
        "facts": facts,
        "checks": checks,
    }

    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, indent=2) + "\n")
        print(f"JSON evidence: {out}")

    raise SystemExit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
