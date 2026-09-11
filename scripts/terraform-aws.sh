#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TF_DIR="$ROOT/terraform/environments/aws"
ENV_FILE="$ROOT/.env.aws"

if [ ! -f "$ENV_FILE" ]; then
  echo "SAFETY BLOCK: $ENV_FILE does not exist."
  exit 1
fi

set -a
source "$ENV_FILE"
set +a

COMMAND="${1:-}"

case "$COMMAND" in
  init|validate|plan|show|fmt|providers)
    ;;
  apply|destroy)
    echo "========================================"
    echo " PROJECT 11 AWS SAFETY BLOCK"
    echo "========================================"
    echo
    echo "terraform $COMMAND is DISABLED during Stage 5."
    echo "No real AWS resources were changed."
    exit 2
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
    exit 2
    ;;
esac

python3.11 "$ROOT/automation/aws_safety.py"

eval "$(aws configure export-credentials --profile "$P11_AWS_PROFILE" --format env)"

export AWS_REGION="$P11_AWS_REGION"
export AWS_DEFAULT_REGION="$P11_AWS_REGION"

terraform -chdir="$TF_DIR" "$@"
