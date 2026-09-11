#!/usr/bin/env python3
"""Collect non-mutating RHEL host facts for Project 11 host-prep validation."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import re
import shutil
import socket
import subprocess
import sys
from typing import Optional


def run_command(args: list[str]) -> tuple[int, str]:
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        return result.returncode, result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return 127, ""


def read_text(path: str) -> str:
    try:
        return Path(path).read_text().strip()
    except (FileNotFoundError, PermissionError, OSError):
        return ""


def parse_os_release() -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in read_text("/etc/os-release").splitlines():
        line = raw_line.strip()
        if not line or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value.strip().strip('"').strip("'")
    return values


def parse_rhel_version(version_id: str) -> tuple[Optional[int], Optional[int]]:
    match = re.match(r"^(\d+)(?:\.(\d+))?", version_id or "")
    if not match:
        return None, None
    return int(match.group(1)), int(match.group(2) or 0)


def read_int(path: str) -> Optional[int]:
    value = read_text(path)
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def memory_mb() -> Optional[int]:
    text = read_text("/proc/meminfo")
    match = re.search(r"^MemTotal:\s+(\d+)\s+kB$", text, re.MULTILINE)
    return int(match.group(1)) // 1024 if match else None


def root_gb() -> Optional[int]:
    try:
        return int(round(shutil.disk_usage("/").total / (1024 ** 3)))
    except OSError:
        return None


def systemd_enabled(unit: str) -> bool:
    rc, output = run_command(["systemctl", "is-enabled", unit])
    return rc == 0 and output.lower() == "enabled"


def systemd_active(unit: str) -> bool:
    rc, output = run_command(["systemctl", "is-active", unit])
    return rc == 0 and output.lower() == "active"


def time_synced() -> bool:
    rc, output = run_command(
        ["timedatectl", "show", "-p", "NTPSynchronized", "--value"]
    )
    return rc == 0 and output.lower() == "yes"


def selinux_mode() -> str:
    rc, output = run_command(["getenforce"])
    return output.lower() if rc == 0 and output else "unknown"


def thp_mode() -> str:
    text = read_text("/sys/kernel/mm/transparent_hugepage/enabled")
    match = re.search(r"\[([^\]]+)\]", text)
    return match.group(1) if match else (text or "unknown")


def java_major() -> Optional[int]:
    try:
        result = subprocess.run(
            ["java", "-version"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None

    text = (result.stderr or "") + "\n" + (result.stdout or "")
    match = re.search(r'version\s+"([^"]+)"', text)
    if not match:
        return None

    version = match.group(1)
    if version.startswith("1."):
        parts = version.split(".")
        return int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None

    major_match = re.match(r"^(\d+)", version)
    return int(major_match.group(1)) if major_match else None


def cgroup_mode() -> str:
    if Path("/sys/fs/cgroup/cgroup.controllers").exists():
        return "v2"
    if Path("/sys/fs/cgroup").exists():
        return "v1"
    return "unknown"


def forward_resolution(name: str, expected_ip: Optional[str]) -> bool:
    if not name:
        return False
    try:
        answers = socket.getaddrinfo(name, None, socket.AF_INET)
    except socket.gaierror:
        return False

    addresses = {answer[4][0] for answer in answers}
    return expected_ip in addresses if expected_ip else bool(addresses)


def reverse_resolution(ip: str, expected_fqdn: Optional[str]) -> bool:
    if not ip:
        return False
    try:
        hostname, aliases, _ = socket.gethostbyaddr(ip)
    except (socket.herror, socket.gaierror):
        return False

    if not expected_fqdn:
        return True

    expected = expected_fqdn.rstrip(".").lower()
    names = {hostname.rstrip(".").lower()}
    names.update(alias.rstrip(".").lower() for alias in aliases)
    return expected in names


def discover_primary_ip() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("198.51.100.1", 9))
        return sock.getsockname()[0]
    except OSError:
        return ""
    finally:
        sock.close()


def hosts_file_consistent(expected_fqdn: str, expected_ip: str) -> bool:
    if not expected_fqdn or not expected_ip:
        return False

    short_name = expected_fqdn.split(".", 1)[0]
    for raw_line in read_text("/etc/hosts").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        fields = line.split()
        if len(fields) < 2:
            continue
        ip, names = fields[0], fields[1:]
        if ip == expected_ip and expected_fqdn in names and short_name in names:
            return True
    return False


def collect(expected_fqdn: Optional[str], expected_ip: Optional[str]) -> dict:
    os_release = parse_os_release()
    major, minor = parse_rhel_version(os_release.get("VERSION_ID", ""))

    hostname = socket.gethostname()
    current_fqdn = socket.getfqdn()
    check_fqdn = expected_fqdn or current_fqdn
    primary_ip = expected_ip or discover_primary_ip()

    return {
        "os_family": os_release.get("ID", "").lower(),
        "os_major": major,
        "os_minor": minor,
        "architecture": platform.machine(),
        "cpu_count": os.cpu_count(),
        "memory_mb": memory_mb(),
        "root_gb": root_gb(),
        "hostname_lowercase": (
            hostname == hostname.lower()
            and current_fqdn == current_fqdn.lower()
        ),
        "forward_resolution": forward_resolution(check_fqdn, expected_ip),
        "reverse_resolution": reverse_resolution(primary_ip, expected_fqdn),
        "hosts_file_consistent": hosts_file_consistent(check_fqdn, primary_ip),
        "chronyd_active": systemd_active("chronyd"),
        "chronyd_enabled": systemd_enabled("chronyd"),
        "time_synced": time_synced(),
        "firewalld_enabled": systemd_enabled("firewalld"),
        "fapolicyd_enabled": systemd_enabled("fapolicyd"),
        "selinux": selinux_mode(),
        "somaxconn": read_int("/proc/sys/net/core/somaxconn"),
        "swappiness": read_int("/proc/sys/vm/swappiness"),
        "thp": thp_mode(),
        "java_major": java_major(),
        "cgroup_mode": cgroup_mode(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Collect live host facts for Project 11 RHEL validation."
    )
    parser.add_argument(
        "--expected-fqdn",
        help="Expected FQDN used for forward/reverse resolution and hosts checks.",
    )
    parser.add_argument(
        "--expected-ip",
        help="Expected node IP used for forward/reverse resolution and hosts checks.",
    )
    parser.add_argument(
        "--json-out",
        help="Write collected facts to this path instead of stdout.",
    )
    args = parser.parse_args()

    facts = collect(args.expected_fqdn, args.expected_ip)
    payload = json.dumps(facts, indent=2, sort_keys=True) + "\n"

    if args.json_out:
        output_path = Path(args.json_out)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(payload)
        print(f"RHEL facts written: {output_path}")
    else:
        sys.stdout.write(payload)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
