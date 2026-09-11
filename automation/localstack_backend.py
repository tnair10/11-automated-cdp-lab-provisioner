#!/usr/bin/env python3

import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

TERRAFORM = (
    ROOT
    / "scripts"
    / "terraform-local.sh"
)

RESULTS = ROOT / "results"

PLAN_NAME = "labctl-localstack.tfplan"


def command_text(command):
    return " ".join(
        str(item)
        for item in command
    )


def run(
    command,
    *,
    check=True,
    capture=False,
):
    print()
    print("$ " + command_text(command))

    return subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        check=check,
        capture_output=capture,
    )


def timed_stage(name, function):
    print()
    print(
        "========================================"
    )
    print(
        f" {name.upper()}"
    )
    print(
        "========================================"
    )

    started = time.monotonic()

    result = function()

    elapsed = (
        time.monotonic()
        - started
    )

    print()
    print(
        f"{name}: PASS "
        f"({elapsed:.2f}s)"
    )

    return result, elapsed


def ensure_localstack():
    """
    Reuse the existing Project 11 LocalStack container when possible.

    This matters when labctl is executed from Jenkins because Docker
    Compose would otherwise derive a different project name from
    /workspace and attempt to recreate p11-localstack.
    """

    inspect = subprocess.run(
        [
            "docker",
            "inspect",
            "p11-localstack",
        ],
        cwd=ROOT,
        text=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    if inspect.returncode == 0:
        state = subprocess.run(
            [
                "docker",
                "inspect",
                "-f",
                "{{.State.Running}}",
                "p11-localstack",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=True,
        )

        if state.stdout.strip() != "true":
            print(
                "Existing p11-localstack "
                "container is stopped; starting it."
            )

            run(
                [
                    "docker",
                    "start",
                    "p11-localstack",
                ]
            )
        else:
            print(
                "Existing p11-localstack "
                "container found; reusing it."
            )

    else:
        print(
            "p11-localstack does not exist; "
            "creating it with the fixed "
            "Project 11 Compose project name."
        )

        run(
            [
                "docker",
                "compose",
                "--project-name",
                "p11",
                "--env-file",
                ".env",
                "-f",
                "docker-compose.local.yml",
                "up",
                "-d",
                "localstack",
            ]
        )

    for attempt in range(1, 31):
        result = subprocess.run(
            [
                "docker",
                "inspect",
                "-f",
                (
                    "{{if .State.Health}}"
                    "{{.State.Health.Status}}"
                    "{{else}}none{{end}}"
                ),
                "p11-localstack",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )

        status = result.stdout.strip()

        print(
            f"LocalStack health "
            f"{attempt:02d}/30: "
            f"{status}"
        )

        if status == "healthy":
            break

        time.sleep(2)

    else:
        raise RuntimeError(
            "LocalStack failed to become healthy"
        )

    subprocess.run(
        [
            "docker",
            "network",
            "create",
            "p11-control",
        ],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    subprocess.run(
        [
            "docker",
            "network",
            "connect",
            "p11-control",
            "p11-localstack",
        ],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

def validate_localstack_identity():
    result = run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "p11-control",
            "-e",
            "AWS_ACCESS_KEY_ID=test",
            "-e",
            "AWS_SECRET_ACCESS_KEY=test",
            "-e",
            "AWS_DEFAULT_REGION=us-east-1",
            "project11-lab-runner:1.0",
            "aws",
            "--endpoint-url="
            "http://p11-localstack:4566",
            "sts",
            "get-caller-identity",
            "--output",
            "json",
        ],
        capture=True,
    )

    identity = json.loads(
        result.stdout
    )

    account = identity.get(
        "Account"
    )

    print(
        f"Account: {account}"
    )

    if account != "000000000000":
        raise RuntimeError(
            "SAFETY BLOCK: target is "
            "not the LocalStack account"
        )

    print(
        "LocalStack identity: PASS"
    )
    print(
        "Real AWS account: NOT USED"
    )


def terraform(
    *args,
    check=True,
    capture=False,
):
    return run(
        [
            "bash",
            str(TERRAFORM),
            *args,
        ],
        check=check,
        capture=capture,
    )


def terraform_output():
    result = terraform(
        "output",
        "-json",
        capture=True,
    )

    return json.loads(
        result.stdout
    )


def validate_estate():
    output = terraform_output()

    def value(name):
        return output[name]["value"]

    account = value(
        "account_id"
    )

    if account != "000000000000":
        raise RuntimeError(
            f"Unexpected account: {account}"
        )

    if value(
        "is_localstack"
    ) is not True:
        raise RuntimeError(
            "Terraform does not report "
            "LocalStack"
        )

    summary = value(
        "estate_summary"
    )

    if summary[
        "node_count"
    ] != 3:
        raise RuntimeError(
            "Expected exactly "
            "3 lab nodes"
        )

    if summary[
        "real_aws"
    ] is not False:
        raise RuntimeError(
            "Real AWS flag must "
            "remain false"
        )

    states = value(
        "instance_states"
    )

    failed = {
        node: state
        for node, state
        in states.items()
        if state != "running"
    }

    if failed:
        raise RuntimeError(
            f"Non-running nodes: "
            f"{failed}"
        )

    print()
    print("Validated estate:")

    for node, state in sorted(
        states.items()
    ):
        print(
            f"  {node:<18} "
            f"{state}"
        )

    print()
    print(
        "Nodes validated: "
        f"{len(states)}/3"
    )
    print(
        "Real AWS resources: NO"
    )
    print(
        "AWS cost: $0"
    )

    return output


def idempotency_check():
    print()
    print(
        "Running Terraform "
        "second-plan check..."
    )

    result = subprocess.run(
        [
            "bash",
            str(TERRAFORM),
            "plan",
            "-detailed-exitcode",
            "-no-color",
        ],
        cwd=ROOT,
        text=True,
    )

    if result.returncode == 0:
        print()
        print(
            "Terraform infrastructure "
            "idempotency: PASS"
        )
        return

    if result.returncode == 2:
        raise RuntimeError(
            "Terraform infrastructure "
            "idempotency: FAIL - "
            "changes detected"
        )

    raise RuntimeError(
        "Terraform second-plan "
        "returned an error"
    )


def run_cost_guard():
    python = sys.executable

    run(
        [
            python,
            str(
                ROOT
                / "automation"
                / "validate_cost_guard.py"
            ),
        ]
    )


def up():
    RESULTS.mkdir(
        parents=True,
        exist_ok=True,
    )

    started_at = datetime.now(
        timezone.utc
    )

    total_started = (
        time.monotonic()
    )

    timings = {}

    _, timings[
        "localstack_start"
    ] = timed_stage(
        "LocalStack startup",
        ensure_localstack,
    )

    _, timings[
        "identity_validation"
    ] = timed_stage(
        "Safety identity validation",
        validate_localstack_identity,
    )

    _, timings[
        "cost_guard"
    ] = timed_stage(
        "Cost guard",
        run_cost_guard,
    )

    _, timings[
        "terraform_init"
    ] = timed_stage(
        "Terraform init",
        lambda: terraform(
            "init",
            "-input=false",
        ),
    )

    _, timings[
        "terraform_validate"
    ] = timed_stage(
        "Terraform validate",
        lambda: terraform(
            "validate",
        ),
    )

    plan_path = (
        ROOT
        / "terraform"
        / "environments"
        / "localstack"
        / PLAN_NAME
    )

    if plan_path.exists():
        plan_path.unlink()

    _, timings[
        "terraform_plan"
    ] = timed_stage(
        "Terraform plan",
        lambda: terraform(
            "plan",
            "-input=false",
            f"-out={PLAN_NAME}",
        ),
    )

    _, timings[
        "terraform_apply"
    ] = timed_stage(
        "Terraform apply",
        lambda: terraform(
            "apply",
            "-auto-approve",
            PLAN_NAME,
        ),
    )

    output, timings[
        "estate_validation"
    ] = timed_stage(
        "Estate validation",
        validate_estate,
    )

    _, timings[
        "idempotency"
    ] = timed_stage(
        "Terraform idempotency",
        idempotency_check,
    )

    total_seconds = (
        time.monotonic()
        - total_started
    )

    states = output[
        "instance_states"
    ][
        "value"
    ]

    report = {
        "scenario":
            "localstack_infrastructure_validation",

        "backend":
            "localstack",

        "started_at":
            started_at.isoformat(),

        "completed_at":
            datetime.now(
                timezone.utc
            ).isoformat(),

        "real_aws":
            False,

        "aws_cost_usd":
            0,

        "node_count":
            3,

        "instance_states":
            states,

        "stage_seconds": {
            name: round(
                seconds,
                3,
            )
            for name, seconds
            in timings.items()
        },

        "total_seconds":
            round(
                total_seconds,
                3,
            ),

        "terraform_idempotency":
            True,

        "overall":
            "PASS",
    }

    evidence = (
        RESULTS
        / "localstack_lab_run.json"
    )

    evidence.write_text(
        json.dumps(
            report,
            indent=2,
        )
    )

    if plan_path.exists():
        plan_path.unlink()

    print()
    print(
        "========================================"
    )
    print(
        " PROJECT 11 LOCAL LAB READY"
    )
    print(
        "========================================"
    )

    print(
        "Nodes:          3"
    )
    print(
        "Backend:        LocalStack"
    )
    print(
        "Real AWS:       NO"
    )
    print(
        "AWS cost:       $0"
    )
    print(
        f"Elapsed:        "
        f"{total_seconds:.2f}s"
    )
    print(
        "Idempotency:    PASS"
    )
    print(
        "Evidence:       "
        "results/localstack_lab_run.json"
    )


def validate():
    validate_localstack_identity()

    run_cost_guard()

    terraform(
        "validate"
    )

    validate_estate()

    idempotency_check()

    print()
    print(
        "========================================"
    )
    print(
        " LOCALSTACK LAB VALIDATION: PASS"
    )
    print(
        "========================================"
    )


def destroy():
    ensure_localstack()

    validate_localstack_identity()

    terraform(
        "destroy",
        "-auto-approve",
    )

    print()
    print(
        "========================================"
    )
    print(
        " LOCALSTACK ESTATE DESTROYED"
    )
    print(
        "========================================"
    )
    print(
        "Real AWS resources affected: NO"
    )
    print(
        "AWS cost: $0"
    )
