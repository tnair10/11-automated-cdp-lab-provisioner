#!/usr/bin/env bash
set -Eeuo pipefail

if [[ "${BASH_SOURCE[0]}" != "$0" ]]; then
  echo "Do not source this script. Execute it directly."
  return 1
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TF_DIR="$ROOT/terraform/environments/aws"

PYTHON_BIN="$ROOT/.venv/bin/python"

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "ERROR: Project 11 Python environment is missing:"
  echo "  $PYTHON_BIN"
  echo
  echo "Create it with:"
  echo "  python3.11 -m venv .venv"
  echo "  .venv/bin/python -m pip install -r requirements-p11.txt"
  return 1 2>/dev/null || false
fi

AWS_PROFILE="${AWS_PROFILE:-p11-lab}"
AWS_REGION="${AWS_REGION:-us-east-1}"
AWS_DEFAULT_REGION="$AWS_REGION"

EXPECTED_ACCOUNT="${P11_EXPECTED_ACCOUNT:-288761733640}"
PROJECT="${P11_PROJECT:-p11-cdp-lab}"
AMI="${P11_AMI:-ami-03b03811941a02056}"
KEY_NAME="${P11_KEY_NAME:-p11-cdp-lab}"
MAX_RUNTIME="${P11_MAX_RUNTIME_MINUTES:-60}"

export AWS_PROFILE AWS_REGION AWS_DEFAULT_REGION


banner() {
  echo
  echo "========================================"
  echo " $1"
  echo "========================================"
}


auth() {
  banner "AWS AUTHENTICATION"

  if ! aws sts get-caller-identity \
      --profile "$AWS_PROFILE" \
      --region "$AWS_REGION" >/dev/null 2>&1; then

    echo "AWS session unavailable/expired."
    echo "Starting AWS login..."

    aws login --profile "$AWS_PROFILE"
  fi

  local account

  account="$(aws sts get-caller-identity \
    --profile "$AWS_PROFILE" \
    --region "$AWS_REGION" \
    --query Account \
    --output text)"

  echo "AWS_ACCOUNT=$account"

  if [[ "$account" != "$EXPECTED_ACCOUNT" ]]; then
    echo "ERROR: expected account $EXPECTED_ACCOUNT"
    echo "ERROR: authenticated account is $account"
    return 1
  fi

  aws sts get-caller-identity \
    --profile "$AWS_PROFILE" \
    --region "$AWS_REGION"

  echo "AWS AUTHENTICATION: PASS"
}


account_id() {
  aws sts get-caller-identity \
    --profile "$AWS_PROFILE" \
    --region "$AWS_REGION" \
    --query Account \
    --output text
}


state_bucket() {
  echo "${PROJECT}-tfstate-$(account_id)-${AWS_REGION}"
}


admin_cidr() {
  if [[ -n "${P11_ADMIN_CIDR:-}" ]]; then
    echo "$P11_ADMIN_CIDR"
    return
  fi

  local ip

  ip="$(curl -fsS https://checkip.amazonaws.com \
    | tr -d '[:space:]')"

  if [[ -z "$ip" ]]; then
    echo "ERROR: unable to determine public IP" >&2
    return 1
  fi

  echo "${ip}/32"
}


set_tf_vars() {
  export TF_VAR_region="$AWS_REGION"
  export TF_VAR_admin_cidr
  export TF_VAR_ami_id="$AMI"
  export TF_VAR_key_name="$KEY_NAME"
  export TF_VAR_max_runtime_minutes="$MAX_RUNTIME"

  TF_VAR_admin_cidr="$(admin_cidr)"

  echo "TF_VAR_region=$TF_VAR_region"
  echo "TF_VAR_admin_cidr=$TF_VAR_admin_cidr"
  echo "TF_VAR_ami_id=$TF_VAR_ami_id"
  echo "TF_VAR_key_name=$TF_VAR_key_name"
  echo "TF_VAR_max_runtime_minutes=$TF_VAR_max_runtime_minutes"
}


preflight() {
  auth

  banner "PROJECT 11 PREFLIGHT"

  command -v aws
  command -v terraform
  test -x "$PYTHON_BIN"
  command -v ssh
  command -v curl

  echo
  "$PYTHON_BIN" --version
  terraform version | head -2
  aws --version

  echo
  echo "--- SSH key pair ---"

  aws ec2 describe-key-pairs \
    --profile "$AWS_PROFILE" \
    --region "$AWS_REGION" \
    --key-names "$KEY_NAME" \
    --query 'KeyPairs[0].{Name:KeyName,Id:KeyPairId}' \
    --output table

  if [[ ! -f "$HOME/.ssh/p11-cdp-lab" ]]; then
    echo "ERROR: $HOME/.ssh/p11-cdp-lab does not exist"
    return 1
  fi

  chmod 600 "$HOME/.ssh/p11-cdp-lab"

  echo
  echo "--- AMI ---"

  local ami_state

  ami_state="$(aws ec2 describe-images \
    --profile "$AWS_PROFILE" \
    --region "$AWS_REGION" \
    --image-ids "$AMI" \
    --query 'Images[0].State' \
    --output text)"

  echo "AMI=$AMI"
  echo "AMI_STATE=$ami_state"

  if [[ "$ami_state" != "available" ]]; then
    echo "ERROR: AMI is not available"
    return 1
  fi

  echo
  echo "--- Cost guard ---"

  cd "$ROOT"
  "$PYTHON_BIN" automation/validate_cost_guard.py

  set_tf_vars

  echo
  echo "PROJECT 11 PREFLIGHT: PASS"
}


ensure_state_bucket() {
  auth

  banner "TERRAFORM STATE BUCKET"

  local bucket
  bucket="$(state_bucket)"

  echo "BUCKET=$bucket"

  if aws s3api head-bucket \
      --profile "$AWS_PROFILE" \
      --region "$AWS_REGION" \
      --bucket "$bucket" >/dev/null 2>&1; then

    echo "State bucket already exists."
  else
    echo "Creating state bucket..."

    aws s3api create-bucket \
      --profile "$AWS_PROFILE" \
      --region "$AWS_REGION" \
      --bucket "$bucket" >/dev/null
  fi

  aws s3api put-bucket-versioning \
    --profile "$AWS_PROFILE" \
    --region "$AWS_REGION" \
    --bucket "$bucket" \
    --versioning-configuration Status=Enabled

  aws s3api put-public-access-block \
    --profile "$AWS_PROFILE" \
    --region "$AWS_REGION" \
    --bucket "$bucket" \
    --public-access-block-configuration \
'{
  "BlockPublicAcls": true,
  "IgnorePublicAcls": true,
  "BlockPublicPolicy": true,
  "RestrictPublicBuckets": true
}'

  aws s3api put-bucket-encryption \
    --profile "$AWS_PROFILE" \
    --region "$AWS_REGION" \
    --bucket "$bucket" \
    --server-side-encryption-configuration \
'{
  "Rules": [
    {
      "ApplyServerSideEncryptionByDefault": {
        "SSEAlgorithm": "AES256"
      }
    }
  ]
}'

  echo "Terraform state bucket: READY"
}


tf_init() {
  ensure_state_bucket
  set_tf_vars

  banner "TERRAFORM INIT"

  local bucket
  bucket="$(state_bucket)"

  rm -rf "$TF_DIR/.terraform"

  terraform -chdir="$TF_DIR" init \
    -reconfigure \
    -input=false \
    -backend-config="bucket=$bucket" \
    -backend-config="key=project11/terraform.tfstate" \
    -backend-config="region=$AWS_REGION" \
    -backend-config="profile=$AWS_PROFILE"

  terraform -chdir="$TF_DIR" validate

  echo "Terraform init: PASS"
}


tf_plan() {
  local cluster_profile="${1:-nonsecure}"

  set_tf_vars

  banner "TERRAFORM PLAN: $cluster_profile"

  terraform -chdir="$TF_DIR" plan \
    -input=false \
    -out="p11-${cluster_profile}.tfplan"

  echo
  echo "Plan saved:"
  echo "$TF_DIR/p11-${cluster_profile}.tfplan"
}


require_aws_confirmation() {
  if [[ "${P11_ALLOW_AWS:-NO}" != "YES" ]]; then
    echo
    echo "REAL AWS CREATION IS BLOCKED."
    echo
    echo "Run with:"
    echo
    echo "  P11_ALLOW_AWS=YES ..."
    echo
    return 1
  fi
}


tf_apply() {
  local cluster_profile="${1:-nonsecure}"

  require_aws_confirmation
  set_tf_vars

  banner "TERRAFORM APPLY: REAL AWS"

  local plan="p11-${cluster_profile}.tfplan"

  if [[ ! -f "$TF_DIR/$plan" ]]; then
    echo "ERROR: Terraform plan does not exist:"
    echo "$TF_DIR/$plan"
    return 1
  fi

  terraform -chdir="$TF_DIR" apply \
    -input=false \
    "$plan"

  echo "Terraform apply: PASS"
}


tf_idempotency() {
  local cluster_profile="${1:-nonsecure}"

  set_tf_vars

  banner "TERRAFORM IDEMPOTENCY"

  set +e

  terraform -chdir="$TF_DIR" plan \
    -input=false \
    -detailed-exitcode \
    -out="p11-${cluster_profile}-idempotency.tfplan"

  local rc=$?

  set -e

  echo "TERRAFORM_IDEMPOTENCY_RC=$rc"

  if [[ "$rc" -eq 0 ]]; then
    echo "Terraform idempotency: PASS"
    return 0
  fi

  if [[ "$rc" -eq 2 ]]; then
    echo "Terraform idempotency: FAIL - changes detected"
    return 2
  fi

  echo "Terraform idempotency: ERROR"
  return "$rc"
}


tf_destroy() {
  require_aws_confirmation
  set_tf_vars

  banner "TERRAFORM DESTROY"

  terraform -chdir="$TF_DIR" destroy \
    -auto-approve \
    -input=false

  echo "Terraform destroy: PASS"
}


status() {
  auth

  banner "PROJECT 11 AWS STATUS"

  echo "--- EC2 ---"

  aws ec2 describe-instances \
    --profile "$AWS_PROFILE" \
    --region "$AWS_REGION" \
    --filters \
      "Name=tag:Project,Values=$PROJECT" \
      "Name=instance-state-name,Values=pending,running,stopping,stopped" \
    --query 'Reservations[].Instances[].{
      Name:Tags[?Key==`Name`]|[0].Value,
      ID:InstanceId,
      State:State.Name,
      PublicIP:PublicIpAddress,
      PrivateIP:PrivateIpAddress,
      Type:InstanceType
    }' \
    --output table

  echo
  echo "--- EBS ---"

  aws ec2 describe-volumes \
    --profile "$AWS_PROFILE" \
    --region "$AWS_REGION" \
    --filters "Name=tag:Project,Values=$PROJECT" \
    --query 'Volumes[].{
      ID:VolumeId,
      State:State,
      SizeGiB:Size
    }' \
    --output table

  echo
  echo "--- Elastic IPs ---"

  aws ec2 describe-addresses \
    --profile "$AWS_PROFILE" \
    --region "$AWS_REGION" \
    --filters "Name=tag:Project,Values=$PROJECT" \
    --query 'Addresses[].{
      AllocationId:AllocationId,
      IP:PublicIp
    }' \
    --output table

  echo
  echo "--- VPC ---"

  aws ec2 describe-vpcs \
    --profile "$AWS_PROFILE" \
    --region "$AWS_REGION" \
    --filters "Name=tag:Name,Values=${PROJECT}-vpc" \
    --query 'Vpcs[].{
      ID:VpcId,
      CIDR:CidrBlock,
      State:State
    }' \
    --output table

  echo
  echo "--- P11 S3 ---"

  aws s3api list-buckets \
    --profile "$AWS_PROFILE" \
    --query "Buckets[?starts_with(Name, \`${PROJECT}\`)].Name" \
    --output text
}


case "${1:-}" in
  auth)
    auth
    ;;

  preflight)
    preflight
    ;;

  ensure-state)
    ensure_state_bucket
    ;;

  init)
    tf_init
    ;;

  plan)
    tf_plan "${2:-nonsecure}"
    ;;

  apply)
    tf_apply "${2:-nonsecure}"
    ;;

  idempotency)
    tf_idempotency "${2:-nonsecure}"
    ;;

  destroy)
    tf_destroy
    ;;

  status)
    status
    ;;

  *)
    cat <<EOF

Usage:

  $0 auth
  $0 preflight
  $0 ensure-state
  $0 init
  $0 plan [nonsecure|secure]
  $0 apply [nonsecure|secure]
  $0 idempotency [nonsecure|secure]
  $0 destroy
  $0 status

Real AWS apply/destroy requires:

  P11_ALLOW_AWS=YES

EOF
    ;;
esac
