# One-time, per-project foundation for the deploy pipeline (../cloudbuild.yaml).
#
# Applied by a human project owner, never by the pipeline. What lives here: everything that grants
# IAM on the project or on a service account, plus what must exist before the first pipeline run.
# The deployer SA that runs the pipeline therefore holds no role that can change project or
# service-account IAM: it cannot widen its own permissions or anyone else's.
#
# New stack (new project, or another copy next to an existing one: every name here and in ../main.tf
# derives from var.name_prefix, so copies with different prefixes never share a resource):
#   ./create_state_bucket.sh <project_id> <region>      # once per project: the GCS bucket holding all state
#   cp example.tfvars <env>.tfvars; cp example.gcs.tfbackend <env>.gcs.tfbackend   # fill in placeholders
#   terraform init -backend-config=<env>.gcs.tfbackend
#   terraform apply -var-file=<env>.tfvars
# (same two files, from ../example.*, next to ../main.tf). After that, deploying = pushing a commit
# (trigger below) or running output manual_deploy_command.
#
# Project whose image repository and worker SA were created by ../main.tf before this root existed
# (a legacy environment only; new projects skip this). Once, with owner credentials, before the first pipeline run:
#   1. here: adopt_existing = true in <project_id>.tfvars, then the three commands above. The repo
#      and the worker SA are imported, not re-created; IAM grants that already exist are merged.
#   2. in ..: terraform init -migrate-state -force-copy -backend-config=<project_id>.gcs.tfbackend
#   3. in ..: terraform state rm google_artifact_registry_repository.cctv_audit_repo \
#               google_service_account.audit_worker_sa \
#               google_service_account_iam_member.worker_self_token_creator \
#               google_project_iam_member.vertex_ai_user
#      The deployer may not read service-account IAM, so the pipeline's first plan would fail on
#      these entries. (The `removed` blocks in ../main.tf guarantee no apply ever deletes them.)

terraform {
  required_version = ">= 1.7.0" # for_each in import blocks
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }
  backend "gcs" {}
}

variable "project_id" {
  description = "Target GCP project ID"
  type        = string
}

variable "name_prefix" {
  description = <<-EOT
    Stack identifier; must equal name_prefix in ../<env>.tfvars. Names derived from it: worker SA
    <p>-worker, deployer SA <p>-deployer, image repository <p>-images, trigger <p>-deploy.
    2-20 chars so that <p>-deployer stays within the 30-char service-account limit.
  EOT
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{0,18}[a-z0-9]$", var.name_prefix))
    error_message = "name_prefix must be 2-20 chars: lowercase letters, digits and '-', starting with a letter and not ending with '-'."
  }
}

variable "region" {
  description = "Region of the image repository and of the Cloud Build trigger/builds (CON-003: data residency)"
  type        = string
}

variable "display_label" {
  description = "Human-readable stack label used in resource display names and descriptions. Empty = name_prefix."
  type        = string
  default     = ""
}

# ---- Name overrides. Empty = derived from name_prefix. Only for adopting names that existed before
# ---- name_prefix did (e.g. a pre-existing production environment); new stacks leave them empty.

variable "artifact_repository_id" {
  description = "Image repository ID; must equal artifact_repository_id in ../<env>.tfvars. Empty = <name_prefix>-images."
  type        = string
  default     = ""
}

variable "deployer_account_id" {
  description = "Deployer service account ID. Empty = <name_prefix>-deployer."
  type        = string
  default     = ""
}

variable "image_name" {
  description = "Image (package) name inside the repository (cloudbuild.yaml _IMAGE)"
  type        = string
  default     = "cctv-audit-worker"
}

variable "state_bucket" {
  description = "Terraform state bucket (bootstrap/create_state_bucket.sh); also stages build sources. Empty = <project_id>-tfstate."
  type        = string
  default     = ""
}

variable "adopt_existing" {
  description = <<-EOT
    true = the image repository and the worker SA already exist (created by ../main.tf before this
    root existed) and are imported into this state instead of created, which would fail with 409.
    Harmless to leave on afterwards: an import whose target is already in state is skipped.
  EOT
  type        = bool
  default     = false
}

variable "trigger_repository" {
  description = <<-EOT
    Cloud Build 2nd-gen repository to deploy from on push:
    projects/<project>/locations/<region>/connections/<connection>/repositories/<repo>.
    The connection (GitHub/GitLab OAuth) is made once in the Cloud Build console.
    Empty = no trigger; deploy with output manual_deploy_command.
  EOT
  type        = string
  default     = ""

  validation {
    condition     = var.trigger_repository == "" || can(regex("^projects/[^/]+/locations/[^/]+/connections/[^/]+/repositories/[^/]+$", var.trigger_repository))
    error_message = "trigger_repository must be empty or projects/<p>/locations/<l>/connections/<c>/repositories/<r>."
  }
}

variable "trigger_branch" {
  description = "Branch regex whose pushes deploy"
  type        = string
  default     = "^main$"
}

variable "trigger_env" {
  description = "Environment deployed by the trigger and by manual_deploy_command (<env>.tfvars / <env>.gcs.tfbackend next to ../main.tf). Empty = project_id."
  type        = string
  default     = ""
}

variable "iac_dir" {
  description = "Path of the directory holding ../main.tf, relative to the repository root"
  type        = string
  default     = "."
}

variable "trigger_require_approval" {
  description = "true = each triggered deploy waits for a human to approve it in the Cloud Build console"
  type        = bool
  default     = false
}

locals {
  # Same derivation as ../main.tf (local.artifact_repository_id, local.worker_account_id); passed to
  # ../cloudbuild.yaml as _REPO / _DEPLOYER_SA_ID by the trigger and by manual_deploy_command.
  repository_id       = var.artifact_repository_id != "" ? var.artifact_repository_id : "${var.name_prefix}-images"
  worker_account_id   = "${var.name_prefix}-worker"
  deployer_account_id = var.deployer_account_id != "" ? var.deployer_account_id : "${var.name_prefix}-deployer"
  trigger_name        = "${var.name_prefix}-deploy"
  display_label       = var.display_label != "" ? var.display_label : var.name_prefix
  state_bucket        = var.state_bucket != "" ? var.state_bucket : "${var.project_id}-tfstate"
  trigger_env         = var.trigger_env != "" ? var.trigger_env : var.project_id

  # Every cloudbuild.yaml substitution that selects this stack. Always passed, so the yaml's defaults
  # never decide which stack a build deploys.
  build_substitutions = {
    _ENV            = local.trigger_env
    _REGION         = var.region
    _REPO           = local.repository_id
    _IMAGE          = var.image_name
    _DEPLOYER_SA_ID = local.deployer_account_id
  }

  # One import per adopted resource, or none: importing an object that does not exist fails.
  adopt = var.adopt_existing ? toset(["existing"]) : toset([])

  pipeline_apis = toset([
    "cloudbuild.googleapis.com",
    "artifactregistry.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "iam.googleapis.com",
  ])

  # What ../main.tf and deploy/deploy_reasoning_engine.py call, narrowest predefined role per need.
  # Deliberately absent: every role that can change project or service-account IAM
  # (projectIamAdmin, serviceAccountAdmin, project-wide serviceAccountUser). The few IAM grants the
  # application needs are made in this file, by a human.
  deployer_project_roles = toset([
    "roles/serviceusage.serviceUsageAdmin", # google_project_service; X-Goog-User-Project quota header
    "roles/browser",                        # data.google_project (resourcemanager.projects.get)
    "roles/storage.admin",                  # staging bucket + its IAM; Terraform state; build source
    "roles/run.admin",                      # Cloud Run service + invoker bindings
    "roles/cloudscheduler.admin",           # watchdog job
    "roles/aiplatform.user",                # ReasoningEngine create/update/delete
    "roles/discoveryengine.admin",          # Gemini Enterprise engine + agent binding
    "roles/logging.logWriter",              # build logs (cloudbuild.yaml: CLOUD_LOGGING_ONLY)
  ])
}

provider "google" {
  project = var.project_id
  region  = var.region
}

resource "google_project_service" "pipeline" {
  for_each           = local.pipeline_apis
  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

# --------------------------------------------------------------------------- image repository

import {
  for_each = local.adopt
  to       = google_artifact_registry_repository.images
  id       = "projects/${var.project_id}/locations/${var.region}/repositories/${local.repository_id}"
}

# Worker images, one immutable tag per source tree (src-<sha256>, see deploy/ci_pipeline.py).
# Immutable tags make "a tag means one exact image" a registry-enforced fact: `:latest`-style
# re-pointing is rejected at push time.
resource "google_artifact_registry_repository" "images" {
  project       = var.project_id
  location      = var.region
  repository_id = local.repository_id
  description   = "${local.display_label} Audit Agent container images (for Vertex AI ReasoningEngine BYOC & Cloud Run Worker Pool)"
  format        = "DOCKER"

  docker_config {
    immutable_tags = true
  }

  # Deleting the repository deletes every image the running services pull.
  lifecycle {
    prevent_destroy = true
  }

  depends_on = [google_project_service.pipeline]
}

# --------------------------------------------------------------------------- worker (runtime) SA

import {
  for_each = local.adopt
  to       = google_service_account.worker
  id       = "projects/${var.project_id}/serviceAccounts/${local.worker_account_id}@${var.project_id}.iam.gserviceaccount.com"
}

# Runtime identity of both tiers (Cloud Run worker and ReasoningEngine, CON-004). It lives here so
# that all of its IAM is granted by a human; ../main.tf only reads it, and the pipeline only gets
# actAs on it. Its unique_id is the OAuth client ID a Workspace super admin authorises for
# domain-wide delegation: re-creating the account gives it a new ID and silently voids that grant.
resource "google_service_account" "worker" {
  project      = var.project_id
  account_id   = local.worker_account_id
  display_name = "${local.display_label} AI Audit Agent Engine Worker SA"

  lifecycle {
    prevent_destroy = true
  }

  depends_on = [google_project_service.pipeline]
}

# Keyless domain-wide delegation: the worker signs its own DWD JWT through the IAM Credentials API
# (signJwt), which needs TokenCreator on itself. No key file is ever created.
resource "google_service_account_iam_member" "worker_self_token_creator" {
  service_account_id = google_service_account.worker.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:${google_service_account.worker.email}"
}

# Gemini (Vertex AI) calls from both tiers.
resource "google_project_iam_member" "worker_vertex_ai_user" {
  project = var.project_id
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.worker.email}"
}

# --------------------------------------------------------------------------- deployer SA

# Identity of every deploy. No keys are ever created; only Cloud Build runs as it, and only
# principals with actAs on it (project owners) can start a build as it.
resource "google_service_account" "deployer" {
  account_id   = local.deployer_account_id
  display_name = "${local.display_label} deploy pipeline (Cloud Build)"
  description  = "Runs cloudbuild.yaml: builds the worker image and applies main.tf. Keyless."

  depends_on = [google_project_service.pipeline]
}

resource "google_project_iam_member" "deployer" {
  for_each = local.deployer_project_roles
  project  = var.project_id
  role     = each.value
  member   = "serviceAccount:${google_service_account.deployer.email}"
}

# ../main.tf deploys Cloud Run, the Scheduler OIDC job and the ReasoningEngine to run *as* the
# worker, which needs iam.serviceAccounts.actAs on it. Granted on that one account only: the same
# role at project level would let a compromised pipeline act as every SA in the project.
# The role also carries iam.serviceAccounts.get, which ../main.tf's data source needs.
resource "google_service_account_iam_member" "deployer_act_as_worker" {
  service_account_id = google_service_account.worker.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.deployer.email}"
}

# Push images, read tags, and manage the ReasoningEngine service agent's pull binding
# (../main.tf reasoning_engine_image_puller) -- on this repository only.
resource "google_artifact_registry_repository_iam_member" "deployer_repo_admin" {
  project    = var.project_id
  location   = google_artifact_registry_repository.images.location
  repository = google_artifact_registry_repository.images.name
  role       = "roles/artifactregistry.admin"
  member     = "serviceAccount:${google_service_account.deployer.email}"
}

# --------------------------------------------------------------------------- push trigger

resource "google_cloudbuild_trigger" "deploy_on_push" {
  count           = var.trigger_repository == "" ? 0 : 1
  project         = var.project_id
  location        = var.region
  name            = local.trigger_name
  description     = "Push to ${var.trigger_branch}: build image for that commit, terraform apply (${local.trigger_env}), verify no drift"
  service_account = google_service_account.deployer.id
  filename        = "${var.iac_dir}/cloudbuild.yaml"
  included_files  = ["${var.iac_dir}/**"]

  repository_event_config {
    repository = var.trigger_repository
    push {
      branch = var.trigger_branch
    }
  }

  substitutions = merge(local.build_substitutions, { _IAC_DIR = var.iac_dir })

  approval_config {
    approval_required = var.trigger_require_approval
  }

  depends_on = [
    google_project_iam_member.deployer,
    google_service_account_iam_member.deployer_act_as_worker,
    google_artifact_registry_repository_iam_member.deployer_repo_admin,
  ]
}

output "deployer_service_account" {
  description = "Identity every pipeline run uses"
  value       = google_service_account.deployer.email
}

output "worker_service_account" {
  description = "Runtime identity of both tiers; its IAM is managed here, ../main.tf only reads it"
  value       = google_service_account.worker.email
}

output "image_repository" {
  description = "Where the pipeline pushes worker images"
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/${local.repository_id}"
}

output "deploy_trigger" {
  description = "Push-to-deploy trigger name, or null when var.trigger_repository is empty"
  value       = var.trigger_repository == "" ? null : google_cloudbuild_trigger.deploy_on_push[0].name
}

output "manual_deploy_command" {
  description = "Deploy without a trigger (run from the directory holding main.tf). Add --substitutions=_APPLY=false to preview only."
  value = join(" ", [
    "gcloud builds submit --project=${var.project_id} --region=${var.region} --config=cloudbuild.yaml",
    "--gcs-source-staging-dir=gs://${local.state_bucket}/source",
    "--service-account=${google_service_account.deployer.id}",
    "--substitutions=${join(",", [for k, v in local.build_substitutions : "${k}=${v}"])}",
  ])
}
