#!/usr/bin/env python3

import argparse
import json
import os
import subprocess
import sys


def required_env(name):
    value = os.environ.get(name)

    if not value:
        print(f"{name}: MISSING")
        sys.exit(1)

    return value


def aws(profile, *args):
    result = subprocess.run(
        [
            "aws",
            *args,
            "--profile",
            profile,
            "--output",
            "json",
        ],
        text=True,
        capture_output=True,
    )

    if result.returncode != 0:
        print(result.stderr.strip())
        sys.exit(result.returncode)

    return result.stdout


def main():
    parser = argparse.ArgumentParser(
        description="Project 11 real-AWS safety gate"
    )

    parser.add_argument(
        "--require-confirmation",
        action="store_true",
        help=(
            "Require CONFIRM_AWS_COST=yes. "
            "Use only immediately before a real apply."
        ),
    )

    args = parser.parse_args()

    profile = required_env(
        "P11_AWS_PROFILE"
    )

    expected_region = required_env(
        "P11_AWS_REGION"
    )

    expected_account = required_env(
        "P11_EXPECTED_AWS_ACCOUNT_ID"
    )

    expected_principal = required_env(
        "P11_EXPECTED_AWS_PRINCIPAL"
    )

    print("=" * 40)
    print(" PROJECT 11 AWS SAFETY GATE")
    print("=" * 40)

    print(f"Profile:   {profile}")

    identity = json.loads(
        aws(
            profile,
            "sts",
            "get-caller-identity",
        )
    )

    actual_account = identity["Account"]
    actual_principal = identity["Arn"]

    region_result = subprocess.run(
        [
            "aws",
            "configure",
            "get",
            "region",
            "--profile",
            profile,
        ],
        text=True,
        capture_output=True,
        check=True,
    )

    actual_region = (
        region_result.stdout.strip()
    )

    print()
    print(
        f"Account:   {actual_account}"
    )
    print(
        f"Principal: {actual_principal}"
    )
    print(
        f"Region:    {actual_region}"
    )

    print()

    if actual_account != expected_account:
        print(
            "AWS account identity: FAIL"
        )
        sys.exit(1)

    print(
        "AWS account identity: PASS"
    )

    if actual_principal != expected_principal:
        print(
            "AWS principal identity: FAIL"
        )
        sys.exit(1)

    print(
        "AWS principal identity: PASS"
    )

    if actual_region != expected_region:
        print(
            "AWS region validation: FAIL"
        )
        sys.exit(1)

    print(
        "AWS region validation: PASS"
    )

    confirmation = os.environ.get(
        "CONFIRM_AWS_COST",
        "no",
    ).lower()

    if args.require_confirmation:
        if confirmation != "yes":
            print(
                "Explicit AWS cost confirmation: FAIL"
            )
            print(
                "CONFIRM_AWS_COST must equal yes"
            )
            sys.exit(1)

        print(
            "Explicit AWS cost confirmation: PASS"
        )
    else:
        print(
            "Explicit AWS cost confirmation: "
            "NOT REQUIRED for read-only preflight"
        )

    print()
    print(
        "AWS safety gate: PASS"
    )


if __name__ == "__main__":
    main()
