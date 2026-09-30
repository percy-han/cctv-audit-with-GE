#!/usr/bin/env bash
# Creates the GCS bucket that holds all Terraform state for one project. Run once, by a project
# owner, before `terraform init` in bootstrap/ or in the main stack. Safe to re-run.
# Several stacks in one project share this bucket; each keeps its state under its own prefix
# (<name_prefix>/main and <name_prefix>/bootstrap in <env>.gcs.tfbackend).
#
#   ./create_state_bucket.sh <project_id> <region> [bucket]    # bucket default: <project_id>-tfstate
set -euo pipefail

project="${1:?usage: $0 <project_id> <region> [bucket]}"
region="${2:?usage: $0 <project_id> <region> [bucket]}"
bucket="gs://${3:-${project}-tfstate}"

if gcloud storage buckets describe "$bucket" --project="$project" --format='value(name)' >/dev/null 2>&1; then
  echo "exists: $bucket"
else
  gcloud storage buckets create "$bucket" --project="$project" --location="$region" \
    --uniform-bucket-level-access --public-access-prevention
fi

# Every state write keeps the previous version, so a bad apply's state can be restored.
gcloud storage buckets update "$bucket" --project="$project" --versioning

echo "state bucket ready: $bucket"
