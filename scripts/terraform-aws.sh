#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TF_DIR="$ROOT/terraform/environments/aws"
ENV_FILE="$ROOT/.env.aws"
BACKEND_CONFIG="$TF_DIR/backend.local.hcl"
PLAN_VALIDATOR="$ROOT/automation/validate_aws_plan.py"

if [ ! -f "$ENV_FILE" ]; then
  echo "SAFETY BLOCK: $ENV_FILE does not exist."
  exit 1
fi

set -a
source "$ENV_FILE"
set +a

COMMAND="${1:-}"

export_aws_credentials() {
  eval "$(aws configure export-credentials --profile "$P11_AWS_PROFILE" --format env)"
  export AWS_REGION="$P11_AWS_REGION"
  export AWS_DEFAULT_REGION="$P11_AWS_REGION"
}

ensure_remote_backend() {
  if [ ! -f "$BACKEND_CONFIG" ]; then
    echo "SAFETY BLOCK: backend.local.hcl does not exist."
    exit 1
  fi

  terraform     -chdir="$TF_DIR"     init     -reconfigure     -input=false     -backend-config=backend.local.hcl     >/dev/null
}

case "$COMMAND" in
  fmt|providers|show)
    terraform -chdir="$TF_DIR" "$@"
    ;;

  init)
    python3.11 "$ROOT/automation/aws_safety.py"
    export_aws_credentials
    terraform -chdir="$TF_DIR" "$@"
    ;;

  validate)
    python3.11 "$ROOT/automation/aws_safety.py"
    export_aws_credentials
    terraform -chdir="$TF_DIR" "$@"
    ;;

  plan)
    python3.11 "$ROOT/automation/aws_safety.py"
    export_aws_credentials
    ensure_remote_backend
    terraform -chdir="$TF_DIR" "$@"
    ;;

  apply)
    if [ "$#" -ne 2 ]; then
      echo "SAFETY BLOCK: apply requires exactly one saved plan."
      echo "Usage: bash scripts/terraform-aws.sh apply <saved-plan.tfplan>"
      exit 2
    fi

    PLAN_NAME="$2"

    case "$PLAN_NAME" in
      */*)
        echo "SAFETY BLOCK: saved plan must be located directly in terraform/environments/aws."
        exit 2
        ;;
      *.tfplan)
        ;;
      *)
        echo "SAFETY BLOCK: apply requires a .tfplan saved plan."
        exit 2
        ;;
    esac

    if [ ! -f "$TF_DIR/$PLAN_NAME" ]; then
      echo "SAFETY BLOCK: saved plan not found: $PLAN_NAME"
      exit 2
    fi

    python3.11 "$ROOT/automation/aws_safety.py" --require-confirmation
    export_aws_credentials
    ensure_remote_backend

    python3.11 "$PLAN_VALIDATOR" "$PLAN_NAME"

    echo
    echo "========================================"
    echo " PROJECT 11 AWS APPLY GATE: PASS"
    echo "========================================"
    echo "Applying validated saved plan: $PLAN_NAME"

    terraform -chdir="$TF_DIR" apply "$PLAN_NAME"
    ;;

  destroy)
    python3.11 "$ROOT/automation/aws_safety.py"
    export_aws_credentials
    ensure_remote_backend

    echo
    echo "========================================"
    echo " PROJECT 11 AWS DESTROY"
    echo "========================================"
    echo "AWS identity validation: PASS"
    echo "Remote backend initialization: PASS"
    echo

    terraform -chdir="$TF_DIR" destroy "${@:2}"
    ;;

  *)
    echo "Unsupported AWS Terraform command: $COMMAND"
    echo
    echo "Allowed:"
    echo "  init"
    echo "  validate"
    echo "  plan"
    echo "  show"
    echo "  fmt"
    echo "  providers"
    echo "  apply <saved-plan.tfplan>"
    echo "  destroy"
    exit 2
    ;;
esac
