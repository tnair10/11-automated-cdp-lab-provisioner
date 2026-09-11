#!/usr/bin/env python3

from pathlib import Path
import sys

import yaml


ROOT = Path(__file__).resolve().parents[1]


def load_yaml(path):
    return yaml.safe_load(path.read_text())


topology = load_yaml(
    ROOT / "config/topology.yaml"
)

guard = load_yaml(
    ROOT / "config/cost-guard.yaml"
)

nodes = topology["nodes"]

checks = []


def check(name, passed, detail):
    checks.append((name, passed, detail))


check(
    "instance_count",
    len(nodes) <= guard["max_instances"],
    f"{len(nodes)}/{guard['max_instances']}",
)

invalid_types = [
    node["instance_type"]
    for node in nodes
    if node["instance_type"]
    not in guard["allowed_instance_types"]
]

check(
    "instance_types",
    not invalid_types,
    (
        "approved"
        if not invalid_types
        else f"invalid={invalid_types}"
    ),
)

oversized_volumes = [
    f"{node['name']}={node['root_volume_gb']}GB"
    for node in nodes
    if node["root_volume_gb"]
    > guard["max_root_volume_gb"]
]

check(
    "root_volume_limits",
    not oversized_volumes,
    (
        "approved"
        if not oversized_volumes
        else str(oversized_volumes)
    ),
)

check(
    "auto_destroy_required",
    guard["require_auto_destroy"] is True,
    str(guard["require_auto_destroy"]),
)

check(
    "explicit_aws_confirmation",
    guard[
        "require_explicit_aws_confirmation"
    ] is True,
    str(
        guard[
            "require_explicit_aws_confirmation"
        ]
    ),
)


print(
    "========================================"
)
print(
    " PROJECT 11 COST GUARD"
)
print(
    "========================================"
)

for name, passed, detail in checks:
    state = "PASS" if passed else "FAIL"

    print(
        f"{name:<28} {state:<5} {detail}"
    )

overall = all(
    passed
    for _, passed, _ in checks
)

print()

print(
    "Cost guard: "
    + ("PASS" if overall else "FAIL")
)

if not overall:
    sys.exit(1)
