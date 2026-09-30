#!/usr/bin/env bash
# cloudbuild.yaml step "terraform" (python:3.12-slim, pinned by digest).
# terraform init / plan / apply for one environment (= one stack: <env>.tfvars names its
# name_prefix, <env>.gcs.tfbackend its own state prefix), then proves the result converged: a fresh plan
# must be empty, otherwise the code can no longer recreate what is running and the build fails.
set -euo pipefail

: "${PROJECT_ID:?}" "${EXPECTED_SA:?}" "${IMAGE_REPO:?}" "${TF_VERSION:?}" "${TF_SHA256:?}" "${OUT_DIR:?}"
env_name="${ENV_NAME:-}"
env_name="${env_name:-$PROJECT_ID}"
apply="${APPLY:-true}"
tfvars="${env_name}.tfvars"
backend="${env_name}.gcs.tfbackend"

# example.* are templates with <placeholders>, never a deployable environment.
if [[ "$env_name" == "example" ]]; then
  echo "_ENV=example selects the templates; copy them to <env>.tfvars / <env>.gcs.tfbackend first" >&2
  exit 1
fi
for f in "$tfvars" "$backend"; do
  [[ -f "$f" ]] || { echo "missing $f in $(pwd)" >&2; exit 1; }
done
# The environment file must describe the project this build runs in.
if ! grep -Eq "^project_id[[:space:]]*=[[:space:]]*\"${PROJECT_ID}\"" "$tfvars"; then
  echo "$tfvars does not set project_id = \"$PROJECT_ID\"; refusing to deploy it from this project" >&2
  exit 1
fi

python3 deploy/ci_pipeline.py check-identity --expected "$EXPECTED_SA"
# The digest to deploy is whatever Artifact Registry holds for this source tree's tag (pushed by the
# previous step now, or by an earlier build of the same tree).
python3 deploy/ci_pipeline.py pin --image-repo "$IMAGE_REPO" --out-dir "$OUT_DIR"
image_ref="$(cat "$OUT_DIR/image_ref.txt")"

python3 deploy/ci_pipeline.py install-terraform \
  --version "$TF_VERSION" --sha256 "$TF_SHA256" --dest /usr/local/bin/terraform
# Needed by deploy/deploy_reasoning_engine.py, which main.tf runs via local-exec. This step holds
# the deployer's credentials, so only the hashed lock may be installed (see requirements-deploy.in).
PIP_ROOT_USER_ACTION=ignore pip install --quiet --no-cache-dir --disable-pip-version-check \
  --require-hashes -r deploy/requirements-deploy.txt

export TF_IN_AUTOMATION=1 TF_INPUT=0
tf_vars=(-var-file="$tfvars" -var="container_image=$image_ref")

terraform version
terraform init -lockfile=readonly -backend-config="$backend"
terraform plan -lock-timeout=5m "${tf_vars[@]}" -out="$OUT_DIR/deploy.tfplan"

if [[ "$apply" != "true" ]]; then
  echo "APPLY=$apply: plan only, nothing was changed."
  exit 0
fi

terraform apply -lock-timeout=5m "$OUT_DIR/deploy.tfplan"

set +e
terraform plan -lock-timeout=5m -detailed-exitcode "${tf_vars[@]}" > "$OUT_DIR/converge_plan.txt"
rc=$?
set -e
cat "$OUT_DIR/converge_plan.txt"
if [[ $rc -ne 0 ]]; then
  echo "NOT CONVERGED: post-apply plan exit code $rc (2 = live environment still differs from code)" >&2
  exit 1
fi
echo "CONVERGED: post-apply plan is empty."
terraform output
