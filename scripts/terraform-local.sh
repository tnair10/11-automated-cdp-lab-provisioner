#!/usr/bin/env bash
set -euo pipefail

ROOT="$(
  cd "$(dirname "${BASH_SOURCE[0]}")/.."
  pwd
)"

TF_DIR="$ROOT/terraform/environments/localstack"

#
# Jenkins / containerized execution mode.
#
# Terraform is already installed in the execution environment,
# so do not create a nested Docker container.
#
if [ "${P11_DIRECT_TERRAFORM:-0}" = "1" ]; then
  cd "$TF_DIR"
  exec terraform "$@"
fi

#
# Local macOS development mode.
#
CACHE_VOLUME="p11-terraform-plugin-cache"

docker volume inspect \
  "$CACHE_VOLUME" >/dev/null 2>&1 \
  || docker volume create \
       "$CACHE_VOLUME" >/dev/null

docker run --rm \
  --network p11-control \
  -v "$ROOT:/workspace" \
  -v "$CACHE_VOLUME:/terraform-plugin-cache" \
  -w /workspace/terraform/environments/localstack \
  -e TF_PLUGIN_CACHE_DIR=/terraform-plugin-cache \
  -e AWS_ACCESS_KEY_ID=test \
  -e AWS_SECRET_ACCESS_KEY=test \
  -e AWS_DEFAULT_REGION=us-east-1 \
  project11-lab-runner:1.0 \
  terraform "$@"
