#!/usr/bin/env bash

SCRIPT_DIR="$(
  cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1
  pwd
)"

if grep -q '^SECURE_AUTOMATION_READY = False$' \
  "$SCRIPT_DIR/automation/p11_lab.py"
then
  echo "P11 secure automation is currently safety-gated."
  echo "No infrastructure or cluster changes were made."
  echo
  exit 2
fi

set -Eeuo pipefail

if [[ "${BASH_SOURCE[0]}" != "$0" ]]; then
  echo "Do not source this script. Execute it directly."
  return 1
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INFRA="$ROOT/scripts/p11_aws_infra.sh"
PYTHON_BIN="$ROOT/.venv/bin/python"

export AWS_PROFILE="${AWS_PROFILE:-p11-lab}"
export AWS_REGION="${AWS_REGION:-us-east-1}"
export AWS_DEFAULT_REGION="$AWS_REGION"

RESULTS="$ROOT/results/p11-live/secure"
mkdir -p "$RESULTS"

echo "========================================"
echo " PROJECT 11 SECURE CLUSTER"
echo "========================================"

"$INFRA" preflight

"$INFRA" init

"$INFRA" plan secure

"$INFRA" apply secure

echo
echo "========================================"
echo " DISTRIBUTED PLATFORM PROVISIONING"
echo "========================================"

cd "$ROOT"

set -o pipefail

"$PYTHON_BIN" automation/p11_lab.py \
  full \
  --profile secure \
  2>&1 | tee "$RESULTS/run.log"

echo
echo "========================================"
echo " PLATFORM STATUS"
echo "========================================"

"$PYTHON_BIN" automation/p11_lab.py \
  status \
  --profile secure \
  2>&1 | tee "$RESULTS/status.log"

"$INFRA" idempotency secure \
  2>&1 | tee "$RESULTS/terraform-idempotency.log"

echo
echo "========================================"
echo " SECURE CLUSTER: PASS"
echo "========================================"

echo "Evidence:"
echo "$RESULTS"

if [[ "${P11_AUTO_DESTROY:-0}" == "1" ]]; then
  echo
  echo "P11_AUTO_DESTROY=1 - destroying AWS lab..."

  "$INFRA" destroy
else
  echo
  echo "Cluster left available for inspection."
  echo "The EC2 auto-stop timer remains active."
  echo
  echo "Destroy explicitly with:"
  echo
  echo "P11_ALLOW_AWS=YES scripts/p11_aws_infra.sh destroy"
fi
