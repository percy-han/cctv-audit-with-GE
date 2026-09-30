#!/usr/bin/env bash
# cloudbuild.yaml step "build-image" (gcr.io/cloud-builders/docker, pinned by digest).
# Builds and pushes the worker image only if step "resolve-image" found none for this source tree.
# No digest is read here: the terraform step asks Artifact Registry for it (ci_pipeline.py pin), so
# the registry, not this builder's local image store, decides what gets deployed.
set -euo pipefail

out_dir="${OUT_DIR:?}"
image_repo="${IMAGE_REPO:?}"

if [[ -s "$out_dir/image_ref.txt" ]]; then
  echo "Image for this source tree already exists, not rebuilding: $(cat "$out_dir/image_ref.txt")"
  exit 0
fi

tag_ref="$(cat "$out_dir/image_tag.txt")"
case "$tag_ref" in
  "$image_repo":src-*) ;;
  *)
    echo "refusing to push '$tag_ref': expected $image_repo:src-<sha256>" >&2
    exit 1
    ;;
esac

# Default Docker network on purpose (no --network=cloudbuild): the RUN steps of the image build
# (apt, pip) need only the internet, not the build's credentials, which Cloud Build serves to steps
# on its `cloudbuild` network. Docker 20.10's classic builder pushes a single image manifest, so the
# tag's digest is the image digest.
docker build --tag "$tag_ref" .
docker push "$tag_ref"
echo "Built and pushed $tag_ref"
