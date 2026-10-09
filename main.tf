# Terraform IaC for Chagee Store CCTV AI Audit (Vertex AI Agent Engine BYOC + Zero-DB GCS State)
# Enforces CON-003 (Singapore Data Residency), REQ-008 (Multi-User Concurrency), REQ-012/013 (Pure Workspace Stack)

terraform {
  required_version = ">= 1.7.0" # `removed` block below
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }

  # State lives in GCS, never on a laptop. Bucket + prefix come from the per-project file:
  #   terraform init -backend-config=<project_id>.gcs.tfbackend
  # The bucket is created once per project by bootstrap/create_state_bucket.sh.
  # Routine deploys run in Cloud Build (cloudbuild.yaml) as the deployer SA from bootstrap/.
  backend "gcs" {}
}

variable "project_id" {
  description = "Target GCP Project ID"
  type        = string
}

variable "name_prefix" {
  description = <<-EOT
    Stack identifier. Every resource name of this stack is derived from it, so several copies of the
    stack can live side by side in one project: worker SA / Cloud Run service <p>-worker, watchdog
    job <p>-watchdog, image repository <p>-images, staging bucket <project>-<p>-staging,
    ReasoningEngine <p>-agent, Gemini Enterprise app <p>-ge. Must equal name_prefix in
    bootstrap/<env>.tfvars (the worker SA and the image repository are created there).
    2-20 chars so that <p>-deployer (bootstrap/) stays within the 30-char service-account limit.
  EOT
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{0,18}[a-z0-9]$", var.name_prefix))
    error_message = "name_prefix must be 2-20 chars: lowercase letters, digits and '-', starting with a letter and not ending with '-'."
  }
}

variable "region" {
  description = "GCP region of the staging bucket, image repository, Cloud Run worker and watchdog (CON-003: data residency)"
  type        = string
}

variable "reasoning_engine_location" {
  description = "Vertex AI Agent Engine (ReasoningEngine) region; may differ from var.region (Agent Engine is not offered in every region)"
  type        = string
}

variable "vertex_model_location" {
  description = "Vertex AI location of the Gemini publisher models both tiers call (Gemini 3.x is served on global)"
  type        = string
  default     = "global"
}

# ---- Name overrides. Empty = derived from name_prefix. Only for adopting names that existed before
# ---- name_prefix did (see study-project-496907.tfvars); new stacks leave them empty.

variable "artifact_repository_id" {
  description = "Image repository ID (created by bootstrap/; must equal its artifact_repository_id). Empty = <name_prefix>-images."
  type        = string
  default     = ""
}

variable "staging_bucket_name" {
  description = "Staging / job-state bucket name. Empty = <project_id>-<name_prefix>-staging."
  type        = string
  default     = ""
}

variable "scheduler_time_zone" {
  description = "Time zone of the watchdog cron (runs every 2 minutes, so this only affects how the schedule is displayed)"
  type        = string
  default     = "Etc/UTC"
}

variable "reasoning_engine_display_name" {
  description = "ReasoningEngine display name; deploy/deploy_reasoning_engine.py finds the engine to update by it. Empty = <name_prefix>-agent."
  type        = string
  default     = ""
}

variable "legacy_reasoning_engine_display_names" {
  description = "Extra display names that also identify this stack's ReasoningEngine (engines deployed under an older name). Never list another stack's name."
  type        = list(string)
  default     = []
}

variable "container_image" {
  description = <<-EOT
    Worker image for both tiers, pinned by digest:
    <region>-docker.pkg.dev/<project>/<repo>/<image>@sha256:<64 hex>.
    Supplied by the deploy pipeline (cloudbuild.yaml), which builds the image for the exact source
    tree being deployed and passes the digest Artifact Registry returned. Deliberately not in tfvars.
    The placeholder default only allows `terraform destroy -var-file=<env>.tfvars` to run without
    passing `-var container_image=...`; `deploy_reasoning_engine.py create` refuses the placeholder.
  EOT
  type    = string
  default = "placeholder-docker.pkg.dev/project/repo/image@sha256:0000000000000000000000000000000000000000000000000000000000000000"

  # A mutable tag (`:latest`) keeps this string identical after a new push, so Terraform sees
  # "no changes" and new code silently never reaches Cloud Run or the ReasoningEngine.
  validation {
    condition     = can(regex("^[a-z0-9-]+-docker\\.pkg\\.dev/[^/@:]+/[^/@:]+/[^@:]+@sha256:[0-9a-f]{64}$", var.container_image))
    error_message = "container_image must be pinned by digest (.../<image>@sha256:<64 hex>), not a tag such as :latest. Deploy through cloudbuild.yaml, which resolves the digest."
  }
}

variable "master_prompt_sheet_id" {
  description = "Google Sheet ID (bare ID or full docs.google.com/spreadsheets URL) for the Master Prompt & Model Config Center (REQ-013)"
  type        = string

  # Mirrors cctv_audit/config.py::extract_spreadsheet_id so a mis-pasted Drive *folder*
  # link is rejected at `terraform plan`, not at cold start.
  validation {
    condition = can(regex(
      "^([a-zA-Z0-9_-]{15,}|https://docs\\.google\\.com/spreadsheets/(u/[0-9]+/)?d/[a-zA-Z0-9_-]{15,}.*)$",
      var.master_prompt_sheet_id
    ))
    error_message = "master_prompt_sheet_id must be a bare Spreadsheet ID (>=15 chars) or a https://docs.google.com/spreadsheets/[u/N/]d/<ID>/... URL."
  }
}

variable "enable_standalone_cloud_run" {
  description = "Deploy the Elastic Cloud Run Worker Pool (min=0, max=20, 4 vCPU / 8 GiB) for heavy FFmpeg .mov->.mp4 slicing and team-scale parallel Gemini multimodal inference, dispatched by the Vertex AI ReasoningEngine GE Gateway."
  type        = bool
  default     = true
}

variable "ge_engine_id" {
  description = "Gemini Enterprise (Discovery Engine) App/Engine ID this stack creates and binds its agent to. Empty = <name_prefix>-ge."
  type        = string
  default     = ""
}

variable "extra_ge_engine_ids" {
  description = <<-EOT
    Further, already existing Gemini Enterprise apps that should also expose this stack's agent.
    Nothing is created there; an app that does not exist is skipped. Only list apps this stack owns:
    the agent bound to this stack's ReasoningEngine (or named ge_agent_display_name) is updated in each.
  EOT
  type        = list(string)
  default     = []
}

variable "ge_company_name" {
  description = "Company name shown by a newly created Gemini Enterprise app (commonConfig.companyName) and in agent texts; empty = none"
  type        = string
  default     = ""
}

variable "ge_tenant_label" {
  description = "Brand label used in the agent's descriptions and in default display names (e.g. the customer's store brand); empty = none"
  type        = string
  default     = ""
}

variable "ge_app_display_name" {
  description = "Display name of a newly created Gemini Enterprise app. Empty = derived from ge_tenant_label / ge_company_name / name_prefix."
  type        = string
  default     = ""
}

variable "ge_agent_display_name" {
  description = "Display name of a newly created agent; also identifies this stack's agent in its apps. Empty = derived like ge_app_display_name."
  type        = string
  default     = ""
}

variable "ge_example_folder_url" {
  description = "Drive folder link used in the agent's first starter prompt; empty = a placeholder text"
  type        = string
  default     = ""
}

variable "workspace_impersonate_user" {
  description = <<-EOT
    Google Workspace bot user (e.g. cctv-audit-bot@customer.com) that owns every report Sheet and
    evidence clip. Both tiers act as this user through keyless domain-wide delegation: the runtime SA
    signs a JWT via the IAM Credentials API every hour -- no key file, no refresh token, no human
    login, nothing expires. One-time prerequisite: a Workspace super admin authorises output
    `workspace_dwd_client_id` for output `workspace_dwd_scopes` in admin.google.com, and the bot has
    Drive storage. Supervisors share their folders (Editor) and the SOP Sheet (Viewer) with this user.
    Empty = the runtime SA acts as itself, which Drive only allows inside Shared Drives.
  EOT
  type        = string
  default     = ""

  validation {
    condition = var.workspace_impersonate_user == "" || (
      can(regex("^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$", var.workspace_impersonate_user)) &&
      !endswith(lower(var.workspace_impersonate_user), ".gserviceaccount.com") &&
      !endswith(lower(var.workspace_impersonate_user), "@gmail.com")
    )
    error_message = "workspace_impersonate_user must be empty or a Google Workspace user email (not a service account or @gmail.com)."
  }
}

variable "enable_google_chat_notification" {
  description = "Deployment-time switch to enable or disable sending job completion notifications (with @-mention of the initiating supervisor) to the Google Chat space."
  type        = bool
  default     = false
}

variable "google_chat_webhook_url" {
  description = "Google Chat incoming webhook URL (`https://chat.googleapis.com/v1/spaces/.../messages?key=...&token=...`) used when `enable_google_chat_notification = true`."
  type        = string
  default     = ""
  sensitive   = true

  validation {
    condition     = var.google_chat_webhook_url == "" || can(regex("^https://chat\\.googleapis\\.com/", var.google_chat_webhook_url))
    error_message = "google_chat_webhook_url must be empty or a https://chat.googleapis.com/... incoming webhook URL."
  }
}

locals {
  # Scopes the Workspace super admin authorises for the runtime SA's OAuth client ID
  # (must stay identical to cctv_audit/gcp.py::_WORKSPACE_SCOPES + _CHAT_MENTION_SCOPES).
  workspace_dwd_scopes = "https://www.googleapis.com/auth/drive,https://www.googleapis.com/auth/spreadsheets,https://www.googleapis.com/auth/userinfo.profile"

  # Every per-stack name, derived from var.name_prefix (overrides only where old names are adopted).
  # The worker SA and the image repository are created by bootstrap/ with the same derivation.
  artifact_repository_id = var.artifact_repository_id != "" ? var.artifact_repository_id : "${var.name_prefix}-images"
  worker_account_id      = "${var.name_prefix}-worker"
  worker_service_name    = "${var.name_prefix}-worker"
  watchdog_job_name      = "${var.name_prefix}-watchdog"
  staging_bucket_name    = var.staging_bucket_name != "" ? var.staging_bucket_name : "${var.project_id}-${var.name_prefix}-staging"
  ge_engine_id           = var.ge_engine_id != "" ? var.ge_engine_id : "${var.name_prefix}-ge"
  re_display_name        = var.reasoning_engine_display_name != "" ? var.reasoning_engine_display_name : "${var.name_prefix}-agent"

  # "<label> (<company>)", "<label>", "<company>" or, with neither set, the stack name.
  ge_brand = (
    var.ge_tenant_label != "" && var.ge_company_name != "" ? "${var.ge_tenant_label} (${var.ge_company_name})" :
    var.ge_tenant_label != "" ? var.ge_tenant_label :
    var.ge_company_name != "" ? var.ge_company_name : var.name_prefix
  )
  # Default display names always carry name_prefix, so two stacks with the same brand never share an
  # agent/app display name (the agent display name also identifies this stack's agent). Only an
  # explicit ge_*_display_name (e.g. prod's pinned legacy names) can drop it.
  ge_name_suffix        = local.ge_brand == var.name_prefix ? "" : " [${var.name_prefix}]"
  ge_app_display_name   = var.ge_app_display_name != "" ? var.ge_app_display_name : "${local.ge_brand} 门店 CCTV AI 稽核工作台${local.ge_name_suffix}"
  ge_agent_display_name = var.ge_agent_display_name != "" ? var.ge_agent_display_name : "${local.ge_brand} 门店监控 AI 稽核专家${local.ge_name_suffix}"

  # --legacy-display-name / --extra-ge-engine-id flags for deploy/deploy_reasoning_engine.py.
  re_legacy_name_flags  = join(" ", [for n in var.legacy_reasoning_engine_display_names : "--legacy-display-name \"${n}\""])
  extra_ge_engine_flags = join(" ", [for e in var.extra_ge_engine_ids : "--extra-ge-engine-id \"${e}\""])

  # Every Google API the application stack calls. Pipeline prerequisites (Cloud Build, the image
  # repo, the deployer SA) and every project/service-account IAM grant, including the worker SA
  # itself, come from bootstrap/, which must be applied once before the first deploy.
  required_apis = toset([
    "aiplatform.googleapis.com",
    "artifactregistry.googleapis.com",
    "run.googleapis.com",
    "storage.googleapis.com",
    "iam.googleapis.com",
    "iamcredentials.googleapis.com", # keyless DWD: signJwt
    "drive.googleapis.com",
    "sheets.googleapis.com",
    "discoveryengine.googleapis.com",
    "monitoring.googleapis.com",
  ])
}

provider "google" {
  project = var.project_id
  region  = var.region
}

data "google_project" "current" {
  project_id = var.project_id
}

resource "google_project_service" "required" {
  for_each           = local.required_apis
  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

# 1. Dedicated Least-Privilege Service Account (CON-004): runtime identity of both tiers.
# Created by bootstrap/ together with all of its IAM (self-TokenCreator for keyless DWD,
# roles/aiplatform.user, actAs for the deployer). The pipeline that applies this file can use the
# account but never change who holds which role.
# No depends_on on purpose: a data source that depends on a managed resource with pending changes
# is read only during apply, which makes its values unknown at plan time and would force a
# ReasoningEngine redeploy (terraform_data.vertex_reasoning_engine.triggers_replace).
data "google_service_account" "audit_worker_sa" {
  project    = var.project_id
  account_id = local.worker_account_id
}

# Previously managed here, now by bootstrap/. `removed` stops this stack from managing them
# without deleting them (on an existing project they leave this state once: bootstrap/main.tf).
removed {
  from = google_service_account.audit_worker_sa

  lifecycle {
    destroy = false
  }
}

removed {
  from = google_service_account_iam_member.worker_self_token_creator

  lifecycle {
    destroy = false
  }
}

removed {
  from = google_project_iam_member.vertex_ai_user

  lifecycle {
    destroy = false
  }
}

# 2. Regional GCS Staging + Zero-DB Job State Bucket with 30-Day Auto-Delete TTL (CON-003 / ADR-006)
# Stores both ephemeral 720p video slices AND multi-instance job state (`gs://<bucket>/jobs/<user>/<job_id>.json`).
resource "google_storage_bucket" "staging_bucket" {
  name                        = local.staging_bucket_name
  location                    = var.region
  uniform_bucket_level_access = true
  force_destroy               = true

  lifecycle_rule {
    condition {
      age = 30
    }
    action {
      type = "Delete"
    }
  }
}

resource "google_storage_bucket_iam_member" "staging_bucket_rw" {
  bucket = google_storage_bucket.staging_bucket.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${data.google_service_account.audit_worker_sa.email}"
}

# Bucket-metadata read (storage.buckets.get) for the worker SA. The Step 2 prompt-tuning rounds
# (eval/cloudbuild_round.yaml) run as the worker SA and use this bucket for source staging and
# build logs; Cloud Build rejects such a bucket with "service account ... does not have access to
# the bucket" unless the build SA can read bucket metadata, which objectAdmin does not include.
resource "google_storage_bucket_iam_member" "staging_bucket_meta_reader" {
  bucket = google_storage_bucket.staging_bucket.name
  role   = "roles/storage.legacyBucketReader"
  member = "serviceAccount:${data.google_service_account.audit_worker_sa.email}"
}

# 3. Reasoning Engine Service Agent Pull Permission on the image repository
# Mandatory for Vertex AI Agent Engine (`ReasoningEngine` BYOC `containerSpec`) so the deployed agent
# appears directly in the Gemini Enterprise Console -> `Agents` dropdown menu!
#
# The repository itself is created by bootstrap/ (the pipeline has to push the image before this
# stack's first apply). This stack used to own it; `removed` hands it over without deleting it or
# any image in it.
removed {
  from = google_artifact_registry_repository.cctv_audit_repo

  lifecycle {
    destroy = false
  }
}

# Per percy-han/cctv-audit/deploy/phase0/README.md:L169-L195:
# `service-<PROJECT_NUMBER>@gcp-sa-aiplatform-re.iam.gserviceaccount.com` holds no artifactregistry.*
# permissions by default; granting roles/artifactregistry.reader allows Vertex AI ReasoningEngine
# to pull our containerSpec image.
resource "google_artifact_registry_repository_iam_member" "reasoning_engine_image_puller" {
  project  = var.project_id
  location = var.region
  # Full resource name: the exact form already in state (it came from the repository's `name`
  # attribute when this stack still owned the repo), so the binding needs no update.
  repository = "projects/${var.project_id}/locations/${var.region}/repositories/${local.artifact_repository_id}"
  role       = "roles/artifactregistry.reader"
  member     = "serviceAccount:service-${data.google_project.current.number}@gcp-sa-aiplatform-re.iam.gserviceaccount.com"
}

# The Step 2 prompt-tuning rounds (eval/cloudbuild_round.yaml) run as the worker SA and use the
# digest-pinned worker image as their build-step image, so the worker SA must pull from this repo.
# (Cloud Run pulls through its own service agent, which is why the worker never needed this before.)
resource "google_artifact_registry_repository_iam_member" "worker_image_puller" {
  project    = var.project_id
  location   = var.region
  repository = "projects/${var.project_id}/locations/${var.region}/repositories/${local.artifact_repository_id}"
  role       = "roles/artifactregistry.reader"
  member     = "serviceAccount:${data.google_service_account.audit_worker_sa.email}"
}

# 4. Tier 2 Compute: Elastic Cloud Run Media Slicing & Multimodal Inference Worker Pool (`max_instance_count = 20`)
# Scales from 0 to 20 instances (strictly 1 active job per container via `max_instance_request_concurrency = 1`
# + `hold_connection = true`, up to 20 x 5 = 100 concurrent video slice streams across the audit team).
resource "google_cloud_run_v2_service" "cctv_audit_worker" {
  count               = var.enable_standalone_cloud_run ? 1 : 0
  name                = local.worker_service_name
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_ALL"
  deletion_protection = false

  template {
    service_account                  = data.google_service_account.audit_worker_sa.email
    timeout                          = "3600s"
    session_affinity                 = true
    max_instance_request_concurrency = 1

    scaling {
      min_instance_count = 0
      max_instance_count = 20
    }

    containers {
      image = var.container_image

      resources {
        cpu_idle = false
        limits = {
          cpu    = "4"
          memory = "16Gi"
        }
      }

      env {
        name  = "GCP_PROJECT"
        value = var.project_id
      }
      env {
        name  = "GCP_LOCATION"
        value = var.region
      }
      env {
        name  = "VERTEX_MODEL_LOCATION"
        value = var.vertex_model_location
      }
      env {
        name  = "STAGING_BUCKET"
        value = google_storage_bucket.staging_bucket.name
      }
      env {
        name  = "MASTER_PROMPT_SHEET_ID"
        value = var.master_prompt_sheet_id
      }
      env {
        name  = "CLOUD_RUN_WORKER_URL"
        value = "https://${local.worker_service_name}-${data.google_project.current.number}.${var.region}.run.app"
      }
      env {
        name  = "ENABLE_BACKGROUND_WATCHDOG"
        value = "false"
      }
      env {
        name  = "WORKSPACE_DWD_SERVICE_ACCOUNT"
        value = data.google_service_account.audit_worker_sa.email
      }
      dynamic "env" {
        for_each = var.workspace_impersonate_user == "" ? [] : [var.workspace_impersonate_user]
        content {
          name  = "WORKSPACE_IMPERSONATE_USER"
          value = env.value
        }
      }
      env {
        name  = "ENABLE_GOOGLE_CHAT_NOTIFICATION"
        value = tostring(var.enable_google_chat_notification)
      }
      dynamic "env" {
        for_each = var.enable_google_chat_notification && var.google_chat_webhook_url != "" ? [var.google_chat_webhook_url] : []
        content {
          name  = "GOOGLE_CHAT_WEBHOOK_URL"
          value = env.value
        }
      }
    }
  }

  # Cloud Run fills in a service-level `scaling.maxInstanceCount` on its own (live value 37; no
  # request in the service's audit log ever set it). google provider 6.50 has no field for it, so
  # without this every plan tries to delete the block and never converges. No effect in practice:
  # the per-revision cap in `template.scaling` above (20) is lower and stays fully managed here.
  lifecycle {
    ignore_changes = [scaling]
  }

  depends_on = [google_project_service.required]
}

resource "google_cloud_run_v2_service_iam_member" "ge_discovery_engine_invoker" {
  count    = var.enable_standalone_cloud_run ? 1 : 0
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.cctv_audit_worker[0].name
  role     = "roles/run.invoker"
  member   = "serviceAccount:service-${data.google_project.current.number}@gcp-sa-discoveryengine.iam.gserviceaccount.com"
}

resource "google_cloud_run_v2_service_iam_member" "reasoning_engine_worker_invoker" {
  count    = var.enable_standalone_cloud_run ? 1 : 0
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.cctv_audit_worker[0].name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${data.google_service_account.audit_worker_sa.email}"
}

# 5. Tier 1 Entrypoint: Vertex AI Agent Engine (`ReasoningEngine` BYOC Container)
# + Automatic Gemini Enterprise (Discovery Engine) App Creation & ADK Agent Integration
resource "terraform_data" "vertex_reasoning_engine" {
  triggers_replace = [
    var.project_id,
    var.reasoning_engine_location,
    var.container_image,
    var.master_prompt_sheet_id,
    local.ge_engine_id,
    google_storage_bucket.staging_bucket.name,
    data.google_service_account.audit_worker_sa.email,
    var.enable_standalone_cloud_run ? google_cloud_run_v2_service.cctv_audit_worker[0].uri : "",
    var.workspace_impersonate_user,
  ]

  provisioner "local-exec" {
    command = <<-EOT
      python3 ${path.module}/deploy/deploy_reasoning_engine.py \
        --project-id "${var.project_id}" \
        --location "${var.reasoning_engine_location}" \
        create \
        --display-name "${local.re_display_name}" ${local.re_legacy_name_flags} \
        --gcp-location "${var.region}" \
        --image-uri "${var.container_image}" \
        --master-prompt-sheet-id "${var.master_prompt_sheet_id}" \
        --staging-bucket "${google_storage_bucket.staging_bucket.name}" \
        --service-account "${data.google_service_account.audit_worker_sa.email}" \
        --cloud-run-worker-url "${var.enable_standalone_cloud_run ? google_cloud_run_v2_service.cctv_audit_worker[0].uri : ""}" \
        --vertex-model-location "${var.vertex_model_location}" \
        --workspace-dwd-service-account "${data.google_service_account.audit_worker_sa.email}" \
        --workspace-impersonate-user "${var.workspace_impersonate_user}" \
        --google-chat-webhook-url "${var.enable_google_chat_notification ? var.google_chat_webhook_url : ""}" \
        --wait-and-bind-ge \
        --ge-engine-id "${local.ge_engine_id}" ${local.extra_ge_engine_flags} \
        --ge-app-display-name "${local.ge_app_display_name}" \
        --ge-agent-display-name "${local.ge_agent_display_name}" \
        --ge-company-name "${var.ge_company_name}" \
        --ge-tenant-label "${var.ge_tenant_label}" \
        --ge-example-folder-url "${var.ge_example_folder_url}"
    EOT
  }

  provisioner "local-exec" {
    when    = destroy
    command = <<-EOT
      python3 ${path.module}/deploy/deploy_reasoning_engine.py \
        --project-id "${self.triggers_replace[0]}" \
        --location "${self.triggers_replace[1]}" \
        destroy-stack \
        --image-uri "${self.triggers_replace[2]}" \
        --ge-engine-id "${self.triggers_replace[4]}" \
        --staging-bucket "${self.triggers_replace[5]}" \
        --service-account "${self.triggers_replace[6]}"
    EOT
  }

  # The worker's own IAM (roles/aiplatform.user, self-TokenCreator) is not listed: bootstrap/
  # grants it before the pipeline can run at all.
  depends_on = [
    google_project_service.required,
    google_artifact_registry_repository_iam_member.reasoning_engine_image_puller,
    google_storage_bucket_iam_member.staging_bucket_rw,
    google_cloud_run_v2_service.cctv_audit_worker,
    google_cloud_run_v2_service_iam_member.reasoning_engine_worker_invoker,
  ]
}

# 5. Unattended Watchdog (Cloud Scheduler -> POST /internal/jobs/sweep every 2 minutes)
# Ensures that if a Tier-2 Cloud Run Worker crashes 20+ minutes after the supervisor clicked "确认开始"
# (when the supervisor has already closed GE chat), Cloud Scheduler automatically sweeps GCS for any
# RUNNING job with a stale heartbeat (>180s) or FAILED state and resumes it from the last 30-min
# SegmentCheckpoint with zero human intervention and zero duplicate token charges.
resource "google_project_service" "cloud_scheduler_api" {
  project            = var.project_id
  service            = "cloudscheduler.googleapis.com"
  disable_on_destroy = false
}

resource "google_cloud_scheduler_job" "cctv_audit_watchdog" {
  count       = var.enable_standalone_cloud_run ? 1 : 0
  name        = local.watchdog_job_name
  description = "Sweeps GCS every 2 minutes to auto-resume any crashed/stale Tier-2 CCTV audit job from its last 30-min SegmentCheckpoint without waiting for human interaction."
  schedule    = "*/2 * * * *"
  time_zone   = var.scheduler_time_zone
  region      = var.region

  http_target {
    http_method = "POST"
    uri         = "${google_cloud_run_v2_service.cctv_audit_worker[0].uri}/internal/jobs/sweep"
    oidc_token {
      service_account_email = data.google_service_account.audit_worker_sa.email
      audience              = google_cloud_run_v2_service.cctv_audit_worker[0].uri
    }
  }

  depends_on = [
    google_project_service.cloud_scheduler_api,
    google_cloud_run_v2_service.cctv_audit_worker,
    google_cloud_run_v2_service_iam_member.reasoning_engine_worker_invoker,
  ]
}

# 6. Cloud Monitoring Evaluation & Operations Dashboard (24-month metric retention in GCM)
# Tracks Model Version x SOP Version recall (Overall / Holdout / Dev / Confirmed-Only 2-point PR),
# SOP category & Outlet/Focus drill-downs, per-video recall & alert counts, point-action timestamp
# drift (±20s window for 1.5 Handwashing vs ±60s window for continuous processes), alert density
# guardrail (<= 8.0/clip), stability flip rate, token cost/latency per clip, and Cloud Run health.
resource "google_monitoring_dashboard" "cctv_audit_dashboard" {
  project = var.project_id
  dashboard_json = jsonencode({
    displayName = "${local.ge_brand} CCTV AI 稽核测评与运行监控大盘 [${var.name_prefix}]"
    dashboardFilters = [
      {
        filterType = "METRIC_LABEL"
        labelKey   = "model_version"
      },
      {
        filterType = "METRIC_LABEL"
        labelKey   = "sop_version"
      },
      {
        filterType = "METRIC_LABEL"
        labelKey   = "media_mode"
      },
      {
        filterType = "METRIC_LABEL"
        labelKey   = "round_id"
      },
      {
        filterType = "METRIC_LABEL"
        labelKey   = "sop_category"
      },
      {
        filterType = "METRIC_LABEL"
        labelKey   = "outlet_focus"
      },
      {
        filterType = "METRIC_LABEL"
        labelKey   = "video_name"
      },
    ]
    mosaicLayout = {
      columns = 12
      tiles = [
        # ---- Row 1 (yPos=0): 1x1 维度拆分 —— 固定模型看 SOP 演进连续曲线 (1.1) vs 固定模型看不同 SOP 柱状对比 (1.2)
        {
          xPos   = 0
          yPos   = 0
          width  = 6
          height = 4
          widget = {
            title = "1.1 [1×1 连续趋势] 固定模型下随轮次演进的召回率连续曲线 (按轮次均值聚合，消除单点断线)"
            xyChart = {
              dataSets = [
                {
                  legendTemplate = "总体召回 Overall ($${metric.labels.model_version})"
                  plotType       = "LINE"
                  timeSeriesQuery = {
                    timeSeriesFilter = {
                      filter = "metric.type=\"custom.googleapis.com/cctv_audit/eval_round/overall_recall\" resource.type=\"global\""
                      aggregation = {
                        alignmentPeriod    = "3600s"
                        perSeriesAligner   = "ALIGN_MAX"
                        crossSeriesReducer = "REDUCE_MEAN"
                        groupByFields      = ["metric.label.model_version"]
                      }
                    }
                  }
                },
                {
                  legendTemplate = "留出集 Holdout ($${metric.labels.model_version})"
                  plotType       = "LINE"
                  timeSeriesQuery = {
                    timeSeriesFilter = {
                      filter = "metric.type=\"custom.googleapis.com/cctv_audit/eval_round/holdout_recall\" resource.type=\"global\""
                      aggregation = {
                        alignmentPeriod    = "3600s"
                        perSeriesAligner   = "ALIGN_MAX"
                        crossSeriesReducer = "REDUCE_MEAN"
                        groupByFields      = ["metric.label.model_version"]
                      }
                    }
                  }
                },
                {
                  legendTemplate = "开发集 Dev ($${metric.labels.model_version})"
                  plotType       = "LINE"
                  timeSeriesQuery = {
                    timeSeriesFilter = {
                      filter = "metric.type=\"custom.googleapis.com/cctv_audit/eval_round/dev_recall\" resource.type=\"global\""
                      aggregation = {
                        alignmentPeriod    = "3600s"
                        perSeriesAligner   = "ALIGN_MAX"
                        crossSeriesReducer = "REDUCE_MEAN"
                        groupByFields      = ["metric.label.model_version"]
                      }
                    }
                  }
                },
              ]
              thresholds = [
                {
                  label = "目标召回线 (0.75)"
                  value = 0.75
                },
              ]
              yAxis = {
                label = "Recall (0.0 - 1.0)"
                scale = "LINEAR"
              }
            }
          }
        },
        {
          xPos   = 6
          yPos   = 0
          width  = 6
          height = 4
          widget = {
            title = "1.2 [1×1 跨 SOP 对比] 同一模型下不同 SOP 版本召回率柱状对比 (v2.5_r00 vs v2.6_r01 vs v2.6_r02)"
            xyChart = {
              dataSets = [
                {
                  legendTemplate = "SOP: $${metric.labels.sop_version} (轮次 $${metric.labels.round_id} 均值)"
                  plotType       = "STACKED_BAR"
                  timeSeriesQuery = {
                    timeSeriesFilter = {
                      filter = "metric.type=\"custom.googleapis.com/cctv_audit/eval_round/overall_recall\" resource.type=\"global\""
                      aggregation = {
                        alignmentPeriod    = "3600s"
                        perSeriesAligner   = "ALIGN_MAX"
                        crossSeriesReducer = "REDUCE_MEAN"
                        groupByFields      = ["metric.label.sop_version", "metric.label.round_id"]
                      }
                    }
                  }
                },
              ]
              thresholds = [
                {
                  label = "目标召回线 (0.75)"
                  value = 0.75
                },
              ]
              yAxis = {
                label = "Overall Recall (0.0 - 1.0)"
                scale = "LINEAR"
              }
            }
          }
        },
        # ---- Row 2 (yPos=4): 1x1 跨模型对比 (2.1) & 置信度双工作点 PR 连续趋势 (2.2)
        {
          xPos   = 0
          yPos   = 4
          width  = 6
          height = 4
          widget = {
            title = "2.1 [1×1 跨模型对比] 同一 SOP 版本下不同模型版本召回率对比 (可通过顶部 sop_version 筛选指定 SOP)"
            xyChart = {
              dataSets = [
                {
                  legendTemplate = "模型总体召回: $${metric.labels.model_version} ($${metric.labels.media_mode})"
                  plotType       = "STACKED_BAR"
                  timeSeriesQuery = {
                    timeSeriesFilter = {
                      filter = "metric.type=\"custom.googleapis.com/cctv_audit/eval_round/overall_recall\" resource.type=\"global\""
                      aggregation = {
                        alignmentPeriod    = "3600s"
                        perSeriesAligner   = "ALIGN_MAX"
                        crossSeriesReducer = "REDUCE_MEAN"
                        groupByFields      = ["metric.label.model_version", "metric.label.media_mode"]
                      }
                    }
                  }
                },
                {
                  legendTemplate = "模型留出集 Holdout: $${metric.labels.model_version} ($${metric.labels.media_mode})"
                  plotType       = "LINE"
                  timeSeriesQuery = {
                    timeSeriesFilter = {
                      filter = "metric.type=\"custom.googleapis.com/cctv_audit/eval_round/holdout_recall\" resource.type=\"global\""
                      aggregation = {
                        alignmentPeriod    = "3600s"
                        perSeriesAligner   = "ALIGN_MAX"
                        crossSeriesReducer = "REDUCE_MEAN"
                        groupByFields      = ["metric.label.model_version", "metric.label.media_mode"]
                      }
                    }
                  }
                },
              ]
              thresholds = [
                {
                  label = "目标召回线 (0.75)"
                  value = 0.75
                },
              ]
              yAxis = {
                label = "Recall (0.0 - 1.0)"
                scale = "LINEAR"
              }
            }
          }
        },
        {
          xPos   = 6
          yPos   = 4
          width  = 6
          height = 4
          widget = {
            title = "2.2 [1×1 连续趋势] 置信度双工作点 PR 与有效告警命中率 (Hit Rate) 演进曲线"
            xyChart = {
              dataSets = [
                {
                  legendTemplate = "标准工作点 CONFIRMED+SUSPECTED ($${metric.labels.model_version})"
                  plotType       = "LINE"
                  timeSeriesQuery = {
                    timeSeriesFilter = {
                      filter = "metric.type=\"custom.googleapis.com/cctv_audit/eval_round/overall_recall\" resource.type=\"global\""
                      aggregation = {
                        alignmentPeriod    = "3600s"
                        perSeriesAligner   = "ALIGN_MAX"
                        crossSeriesReducer = "REDUCE_MEAN"
                        groupByFields      = ["metric.label.model_version"]
                      }
                    }
                  }
                },
                {
                  legendTemplate = "严格工作点 仅 CONFIRMED ($${metric.labels.model_version})"
                  plotType       = "LINE"
                  timeSeriesQuery = {
                    timeSeriesFilter = {
                      filter = "metric.type=\"custom.googleapis.com/cctv_audit/eval_round/confirmed_only_recall\" resource.type=\"global\""
                      aggregation = {
                        alignmentPeriod    = "3600s"
                        perSeriesAligner   = "ALIGN_MAX"
                        crossSeriesReducer = "REDUCE_MEAN"
                        groupByFields      = ["metric.label.model_version"]
                      }
                    }
                  }
                },
                {
                  legendTemplate = "有效告警命中率 Hit Rate ($${metric.labels.model_version})"
                  plotType       = "LINE"
                  timeSeriesQuery = {
                    timeSeriesFilter = {
                      filter = "metric.type=\"custom.googleapis.com/cctv_audit/eval_round/hit_rate\" resource.type=\"global\""
                      aggregation = {
                        alignmentPeriod    = "3600s"
                        perSeriesAligner   = "ALIGN_MAX"
                        crossSeriesReducer = "REDUCE_MEAN"
                        groupByFields      = ["metric.label.model_version"]
                      }
                    }
                  }
                },
              ]
              yAxis = {
                label = "Ratio (0.0 - 1.0)"
                scale = "LINEAR"
              }
            }
          }
        },
        # ---- Row 3 (yPos=8): 1x1 维度拆分 —— 按 SOP 大类连续趋势 (3.1) & 按门店机位连续趋势 (3.2)
        {
          xPos   = 0
          yPos   = 8
          width  = 6
          height = 4
          widget = {
            title = "3.1 [1×1 连续趋势] 按 SOP 违规大类分组召回率演进曲线 (A_Handwashing / B_IceMaker / C_TeaBar_Hygiene)"
            xyChart = {
              dataSets = [
                {
                  legendTemplate = "SOP 大类: $${metric.labels.sop_category}"
                  plotType       = "LINE"
                  timeSeriesQuery = {
                    timeSeriesFilter = {
                      filter = "metric.type=\"custom.googleapis.com/cctv_audit/eval_round/sop_category_recall\" resource.type=\"global\""
                      aggregation = {
                        alignmentPeriod    = "3600s"
                        perSeriesAligner   = "ALIGN_MAX"
                        crossSeriesReducer = "REDUCE_MEAN"
                        groupByFields      = ["metric.label.sop_category"]
                      }
                    }
                  }
                },
              ]
              yAxis = {
                label = "Category Recall (0.0 - 1.0)"
                scale = "LINEAR"
              }
            }
          }
        },
        {
          xPos   = 6
          yPos   = 8
          width  = 6
          height = 4
          widget = {
            title = "3.2 [1×1 连续趋势] 按门店与机位视角分组召回率演进曲线 (Bau Cat / Cantavil D2 × 洗手 / 制冰机)"
            xyChart = {
              dataSets = [
                {
                  legendTemplate = "门店×机位: $${metric.labels.outlet_focus}"
                  plotType       = "LINE"
                  timeSeriesQuery = {
                    timeSeriesFilter = {
                      filter = "metric.type=\"custom.googleapis.com/cctv_audit/eval_round/outlet_focus_recall\" resource.type=\"global\""
                      aggregation = {
                        alignmentPeriod    = "3600s"
                        perSeriesAligner   = "ALIGN_MAX"
                        crossSeriesReducer = "REDUCE_MEAN"
                        groupByFields      = ["metric.label.outlet_focus"]
                      }
                    }
                  }
                },
              ]
              yAxis = {
                label = "Outlet×Focus Recall (0.0 - 1.0)"
                scale = "LINEAR"
              }
            }
          }
        },
        # ---- Row 4 (yPos=12): 质量守卫连续趋势 (4.1) & Token 成本/耗时/Cloud Run 实例监控 (4.2)
        {
          xPos   = 0
          yPos   = 12
          width  = 6
          height = 4
          widget = {
            title = "4.1 [1×1 连续趋势] 告警密度守卫 (<=8.0条/视频)、瞬时动作时间定位误差 (秒, ±20s) 与跨 Run 翻转率"
            xyChart = {
              dataSets = [
                {
                  legendTemplate = "单视频平均告警数 Findings/Clip ($${metric.labels.model_version})"
                  plotType       = "LINE"
                  timeSeriesQuery = {
                    timeSeriesFilter = {
                      filter = "metric.type=\"custom.googleapis.com/cctv_audit/eval_round/findings_per_clip\" resource.type=\"global\""
                      aggregation = {
                        alignmentPeriod    = "3600s"
                        perSeriesAligner   = "ALIGN_MAX"
                        crossSeriesReducer = "REDUCE_MEAN"
                        groupByFields      = ["metric.label.model_version"]
                      }
                    }
                  }
                },
                {
                  legendTemplate = "瞬时动作平均时间误差秒数 Point Drift ($${metric.labels.model_version})"
                  plotType       = "LINE"
                  timeSeriesQuery = {
                    timeSeriesFilter = {
                      filter = "metric.type=\"custom.googleapis.com/cctv_audit/eval_round/mean_point_timestamp_drift_sec\" resource.type=\"global\""
                      aggregation = {
                        alignmentPeriod    = "3600s"
                        perSeriesAligner   = "ALIGN_MAX"
                        crossSeriesReducer = "REDUCE_MEAN"
                        groupByFields      = ["metric.label.model_version"]
                      }
                    }
                  }
                },
                {
                  legendTemplate = "跨 Run 翻转率 Flip Rate ($${metric.labels.model_version})"
                  plotType       = "LINE"
                  timeSeriesQuery = {
                    timeSeriesFilter = {
                      filter = "metric.type=\"custom.googleapis.com/cctv_audit/eval_round/flip_rate\" resource.type=\"global\""
                      aggregation = {
                        alignmentPeriod    = "3600s"
                        perSeriesAligner   = "ALIGN_MAX"
                        crossSeriesReducer = "REDUCE_MEAN"
                        groupByFields      = ["metric.label.model_version"]
                      }
                    }
                  }
                },
                {
                  legendTemplate = "稳定项回退数 Regressed Stable Items ($${metric.labels.model_version})"
                  plotType       = "STACKED_BAR"
                  timeSeriesQuery = {
                    timeSeriesFilter = {
                      filter = "metric.type=\"custom.googleapis.com/cctv_audit/eval_round/regressed_stable_items\" resource.type=\"global\""
                      aggregation = {
                        alignmentPeriod    = "3600s"
                        perSeriesAligner   = "ALIGN_MAX"
                        crossSeriesReducer = "REDUCE_MAX"
                        groupByFields      = ["metric.label.model_version"]
                      }
                    }
                  }
                },
              ]
              thresholds = [
                {
                  label = "单视频告警上限 (8.0)"
                  value = 8.0
                },
                {
                  label = "瞬时动作时间容差上限 (20s)"
                  value = 20.0
                },
              ]
              yAxis = {
                label = "Count / Seconds / Ratio"
                scale = "LINEAR"
              }
            }
          }
        },
        {
          xPos   = 6
          yPos   = 12
          width  = 6
          height = 4
          widget = {
            title = "4.2 [1×1 连续趋势] 单段 5 分钟视频平均成本 (USD)、推理耗时 (秒) 与 Cloud Run 活跃实例数"
            xyChart = {
              dataSets = [
                {
                  legendTemplate = "单视频平均成本 USD ($${metric.labels.model_version})"
                  plotType       = "LINE"
                  timeSeriesQuery = {
                    timeSeriesFilter = {
                      filter = "metric.type=\"custom.googleapis.com/cctv_audit/eval_round/cost_per_clip_usd\" resource.type=\"global\""
                      aggregation = {
                        alignmentPeriod    = "3600s"
                        perSeriesAligner   = "ALIGN_MAX"
                        crossSeriesReducer = "REDUCE_MEAN"
                        groupByFields      = ["metric.label.model_version"]
                      }
                    }
                  }
                },
                {
                  legendTemplate = "单视频平均耗时秒数 ($${metric.labels.model_version})"
                  plotType       = "LINE"
                  timeSeriesQuery = {
                    timeSeriesFilter = {
                      filter = "metric.type=\"custom.googleapis.com/cctv_audit/eval_round/mean_clip_latency_sec\" resource.type=\"global\""
                      aggregation = {
                        alignmentPeriod    = "3600s"
                        perSeriesAligner   = "ALIGN_MAX"
                        crossSeriesReducer = "REDUCE_MEAN"
                        groupByFields      = ["metric.label.model_version"]
                      }
                    }
                  }
                },
                {
                  legendTemplate = "Cloud Run 活跃容器实例数 ($${metric.labels.state})"
                  plotType       = "LINE"
                  timeSeriesQuery = {
                    timeSeriesFilter = {
                      filter = "metric.type=\"run.googleapis.com/container/instance_count\" resource.type=\"cloud_run_revision\" resource.label.\"service_name\"=\"${local.worker_service_name}\""
                      aggregation = {
                        alignmentPeriod    = "60s"
                        perSeriesAligner   = "ALIGN_MAX"
                        crossSeriesReducer = "REDUCE_SUM"
                        groupByFields      = ["metric.label.state"]
                      }
                    }
                  }
                },
              ]
              yAxis = {
                label = "USD / Seconds / Instances"
                scale = "LINEAR"
              }
            }
          }
        },
        # ---- Row 5 (yPos=16): 清单 A —— 轮次均值汇总清单 (Model × SOP × Round)
        {
          xPos   = 0
          yPos   = 16
          width  = 6
          height = 5
          widget = {
            title = "5.1 清单 A（静态速览）：各轮次平均汇总表 (Model × SOP × Round，同轮次多次 Run 已取均值)"
            text = {
              format  = "MARKDOWN"
              content = <<-EOT
**说明**：当同一轮次（同一个 `Model + SOP` 组合）为测试稳定性跑了多次 `Run` 时（如 `r00` 跑了 2 次、`r02` 跑了 2 次），上方趋势图与本表自动按轮次取算术平均值，确保每一轮只有一个代表性数据点：

| 轮次 (`round_id`) | 模型版本 (`model_version`) | SOP 版本 (`sop_version`) | 模式 (`media_mode`) | 包含 Run 数 | 总体召回率 (Overall) | 留出集召回 (Holdout) | 开发集召回 (Dev) | 洗手召回 (`A`) | 制冰机召回 (`B`) | 吧台卫生 (`C`) | 单视频告警数 | 瞬时时间误差 (±20s) | 跨 Run 翻转率 | 单视频成本 |
| :--- | :--- | :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **`r00`** (基线) | `gemini-3.8-flash` | `v2.5_r00` | `static` | 2 (`v6_0928_0811`, `r_0928_1009`) | **42.11%** | **59.62%** | 4.17% | 42.50% | 39.29% | 50.00% | 6.72 条 | 2.88s | 21.05% | $0.138 |
| **`r01`** (首轮调优) | `gemini-3.8-flash` | `Prompt_v2.6_r01` | `agentic` | 1 (`r01_run1`) | **35.53%** | **51.92%** | 0.00% | 35.00% | 32.14% | 50.00% | 6.75 条 | 0.33s | 0.00% (单次) | $0.144 |
| **`r02`** (二轮调优) | `gemini-3.8-flash` | `Prompt_v2.6_r02` | `agentic` | 2 (`r02_run1`, `r02_run2`) | **42.76%** | **58.65%** | **8.33%** | 40.00% | 37.50% | **75.00%** | 7.13 条 | 3.75s | 31.58% | $0.358 |
EOT
            }
          }
        },
        {
          xPos   = 6
          yPos   = 16
          width  = 6
          height = 5
          widget = {
            title = "5.2 清单 A（实时指标表）：各轮次 (Model × SOP × Round) 均值动态数据表 [自动随新轮次更新]"
            timeSeriesTable = {
              dataSets = [
                {
                  minAlignmentPeriod  = "3600s"
                  tableDisplayOptions = {}
                  timeSeriesQuery = {
                    timeSeriesFilter = {
                      filter = "metric.type=\"custom.googleapis.com/cctv_audit/eval_round/overall_recall\" resource.type=\"global\""
                      aggregation = {
                        alignmentPeriod    = "3600s"
                        perSeriesAligner   = "ALIGN_MAX"
                        crossSeriesReducer = "REDUCE_MEAN"
                        groupByFields      = ["metric.label.round_id", "metric.label.model_version", "metric.label.sop_version", "metric.label.media_mode"]
                      }
                    }
                  }
                },
                {
                  minAlignmentPeriod  = "3600s"
                  tableDisplayOptions = {}
                  timeSeriesQuery = {
                    timeSeriesFilter = {
                      filter = "metric.type=\"custom.googleapis.com/cctv_audit/eval_round/holdout_recall\" resource.type=\"global\""
                      aggregation = {
                        alignmentPeriod    = "3600s"
                        perSeriesAligner   = "ALIGN_MAX"
                        crossSeriesReducer = "REDUCE_MEAN"
                        groupByFields      = ["metric.label.round_id", "metric.label.model_version", "metric.label.sop_version", "metric.label.media_mode"]
                      }
                    }
                  }
                },
                {
                  minAlignmentPeriod  = "3600s"
                  tableDisplayOptions = {}
                  timeSeriesQuery = {
                    timeSeriesFilter = {
                      filter = "metric.type=\"custom.googleapis.com/cctv_audit/eval_round/dev_recall\" resource.type=\"global\""
                      aggregation = {
                        alignmentPeriod    = "3600s"
                        perSeriesAligner   = "ALIGN_MAX"
                        crossSeriesReducer = "REDUCE_MEAN"
                        groupByFields      = ["metric.label.round_id", "metric.label.model_version", "metric.label.sop_version", "metric.label.media_mode"]
                      }
                    }
                  }
                },
                {
                  minAlignmentPeriod  = "3600s"
                  tableDisplayOptions = {}
                  timeSeriesQuery = {
                    timeSeriesFilter = {
                      filter = "metric.type=\"custom.googleapis.com/cctv_audit/eval_round/findings_per_clip\" resource.type=\"global\""
                      aggregation = {
                        alignmentPeriod    = "3600s"
                        perSeriesAligner   = "ALIGN_MAX"
                        crossSeriesReducer = "REDUCE_MEAN"
                        groupByFields      = ["metric.label.round_id", "metric.label.model_version", "metric.label.sop_version", "metric.label.media_mode"]
                      }
                    }
                  }
                },
              ]
            }
          }
        },
        # ---- Row 6 (yPos=21): 清单 B —— 每次具体执行 (Per-Run) 完整历史明细清单
        {
          xPos   = 0
          yPos   = 21
          width  = 6
          height = 5
          widget = {
            title = "6.1 清单 B（静态速览）：历史全部 5 次独立测试 (Per-Run) 明细清单 [含真实执行时间与单次波动]"
            text = {
              format  = "MARKDOWN"
              content = <<-EOT
**说明**：为什么 `r00` 和 `r02` 各跑了 2 遍？因为视频多模态推理存在采样随机性，同一套 `Model + SOP` 跑 2 遍是为了测量**跨次翻转率 (`Flip Rate`)**。下方完整保留全部 5 次独立运行的原始明细：

| 运行 ID (`run_id`) | 所属轮次 | 真实测试时间 (UTC) | 模型版本 | SOP 版本 | 模式 | 总体召回率 | 留出集 (Holdout) | 开发集 (Dev) | 单视频告警数 | 有效命中率 (Hit Rate) | 瞬时时间误差 (±20s) | 稳定项回退 | 单视频成本 | 平均耗时 |
| :--- | :---: | :--- | :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **`v6_0928_0811`** | `r00` | `2026-09-28 18:12` | `gemini-3.8-flash` | `v2.5_r00` | `static` | **42.11%** | **61.54%** | 0.00% | 6.38 条 | 8.82% | 2.25s (max 6s) | 0 ✅ | $0.127 | 356.7s |
| **`r_0928_1009`** | `r00` | `2026-09-28 18:15` | `gemini-3.8-flash` | `v2.5_r00` | `static` | **42.11%** | **57.69%** | 8.33% | 7.06 条 | 7.96% | 3.50s (max 11s) | 0 ✅ | $0.148 | 361.7s |
| **`r01_run1`** | `r01` | `2026-09-29 08:06` | `gemini-3.8-flash` | `Prompt_v2.6_r01` | `agentic` | **35.53%** | **51.92%** | 0.00% | 6.75 条 | 8.33% | 0.33s (max 1s) | 1 ⚠️ | $0.144 | 426.7s |
| **`r02_run1`** | `r02` | `2026-09-29 09:50` | `gemini-3.8-flash` | `Prompt_v2.6_r02` | `agentic` | **43.42%** | **55.77%** | **16.67%** | 7.63 条 | 9.02% | 7.00s (max 20s) | 1 ⚠️ | $0.346 | 318.8s |
| **`r02_run2`** | `r02` | `2026-09-29 09:14` | `gemini-3.8-flash` | `Prompt_v2.6_r02` | `agentic` | **42.11%** | **61.54%** | 0.00% | 6.63 条 | 8.49% | 0.50s (max 1s) | 0 ✅ | $0.370 | 414.5s |
EOT
            }
          }
        },
        {
          xPos   = 6
          yPos   = 21
          width  = 6
          height = 5
          widget = {
            title = "6.2 清单 B（实时指标表）：每次独立运行 (run_id × round_id × model × SOP) 原始指标表"
            timeSeriesTable = {
              dataSets = [
                {
                  minAlignmentPeriod  = "3600s"
                  tableDisplayOptions = {}
                  timeSeriesQuery = {
                    timeSeriesFilter = {
                      filter = "metric.type=\"custom.googleapis.com/cctv_audit/eval/overall_recall\" resource.type=\"global\""
                      aggregation = {
                        alignmentPeriod    = "3600s"
                        perSeriesAligner   = "ALIGN_MAX"
                        crossSeriesReducer = "REDUCE_MEAN"
                        groupByFields      = ["metric.label.round_id", "metric.label.run_id", "metric.label.model_version", "metric.label.sop_version", "metric.label.media_mode"]
                      }
                    }
                  }
                },
                {
                  minAlignmentPeriod  = "3600s"
                  tableDisplayOptions = {}
                  timeSeriesQuery = {
                    timeSeriesFilter = {
                      filter = "metric.type=\"custom.googleapis.com/cctv_audit/eval/holdout_recall\" resource.type=\"global\""
                      aggregation = {
                        alignmentPeriod    = "3600s"
                        perSeriesAligner   = "ALIGN_MAX"
                        crossSeriesReducer = "REDUCE_MEAN"
                        groupByFields      = ["metric.label.round_id", "metric.label.run_id", "metric.label.model_version", "metric.label.sop_version", "metric.label.media_mode"]
                      }
                    }
                  }
                },
                {
                  minAlignmentPeriod  = "3600s"
                  tableDisplayOptions = {}
                  timeSeriesQuery = {
                    timeSeriesFilter = {
                      filter = "metric.type=\"custom.googleapis.com/cctv_audit/eval/findings_per_clip\" resource.type=\"global\""
                      aggregation = {
                        alignmentPeriod    = "3600s"
                        perSeriesAligner   = "ALIGN_MAX"
                        crossSeriesReducer = "REDUCE_MEAN"
                        groupByFields      = ["metric.label.round_id", "metric.label.run_id", "metric.label.model_version", "metric.label.sop_version", "metric.label.media_mode"]
                      }
                    }
                  }
                },
              ]
            }
          }
        },
        # ---- Row 7 (yPos=26): 清单 C —— 单段视频 (16 支测试视频) 细粒度表现明细表
        {
          xPos   = 0
          yPos   = 26
          width  = 6
          height = 5
          widget = {
            title = "7.1 清单 C（左表）：单段视频 (video_name) 各轮次平均召回率明细表 [定位具体哪支视频漏检/提升]"
            timeSeriesTable = {
              dataSets = [
                {
                  minAlignmentPeriod  = "3600s"
                  tableDisplayOptions = {}
                  timeSeriesQuery = {
                    timeSeriesFilter = {
                      filter = "metric.type=\"custom.googleapis.com/cctv_audit/eval_round/video_recall\" resource.type=\"global\""
                      aggregation = {
                        alignmentPeriod    = "3600s"
                        perSeriesAligner   = "ALIGN_MAX"
                        crossSeriesReducer = "REDUCE_MEAN"
                        groupByFields      = ["metric.label.video_name", "metric.label.round_id", "metric.label.sop_version", "metric.label.model_version"]
                      }
                    }
                  }
                },
              ]
            }
          }
        },
        {
          xPos   = 6
          yPos   = 26
          width  = 6
          height = 5
          widget = {
            title = "7.2 清单 C（右表）：单段视频 (video_name) 各轮次平均告警条数明细表 [定位哪支视频误报偏高]"
            timeSeriesTable = {
              dataSets = [
                {
                  minAlignmentPeriod  = "3600s"
                  tableDisplayOptions = {}
                  timeSeriesQuery = {
                    timeSeriesFilter = {
                      filter = "metric.type=\"custom.googleapis.com/cctv_audit/eval_round/video_findings_count\" resource.type=\"global\""
                      aggregation = {
                        alignmentPeriod    = "3600s"
                        perSeriesAligner   = "ALIGN_MAX"
                        crossSeriesReducer = "REDUCE_MEAN"
                        groupByFields      = ["metric.label.video_name", "metric.label.round_id", "metric.label.sop_version", "metric.label.model_version"]
                      }
                    }
                  }
                },
              ]
            }
          }
        },
      ]
    }
  })

  depends_on = [google_project_service.required]
}

output "staging_bucket_name" {
  description = "Regional GCS bucket for ephemeral video slices and Zero-DB cross-instance job state"
  value       = google_storage_bucket.staging_bucket.name
}

output "artifact_registry_repo" {
  description = "Artifact Registry Docker repository (created by bootstrap/) with ReasoningEngine Service Agent pull permissions"
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/${local.artifact_repository_id}"
}

output "container_image" {
  description = "Digest-pinned image currently deployed to Cloud Run and the ReasoningEngine"
  value       = var.container_image
}

output "cloud_run_worker_pool_url" {
  description = "Elastic Cloud Run Worker Pool URL (max_instance_count=20) invoked by ReasoningEngine for heavy FFmpeg slicing and multimodal inference"
  value       = var.enable_standalone_cloud_run ? google_cloud_run_v2_service.cctv_audit_worker[0].uri : null
}

output "worker_service_account_email" {
  description = "Runtime SA of both tiers. With workspace_impersonate_user set, share folders/SOP Sheet with that bot instead; otherwise with this SA (Shared Drives only)."
  value       = data.google_service_account.audit_worker_sa.email
}

output "workspace_dwd_client_id" {
  description = "Client ID to enter in admin.google.com -> Security -> Access and data control -> API controls -> Manage Domain Wide Delegation -> Add new (one-time, never expires)"
  value       = data.google_service_account.audit_worker_sa.unique_id
}

output "workspace_dwd_scopes" {
  description = "OAuth scopes to authorise for workspace_dwd_client_id (comma-separated, paste as-is)"
  value       = local.workspace_dwd_scopes
}

output "workspace_identity" {
  description = "Account that owns report Sheets/evidence clips and must be granted access to supervisor folders"
  value       = var.workspace_impersonate_user != "" ? var.workspace_impersonate_user : data.google_service_account.audit_worker_sa.email
}

output "gemini_enterprise_engine_id" {
  description = "Gemini Enterprise (Discovery Engine) App ID where the CCTV Audit Agent is registered"
  value       = local.ge_engine_id
}

output "gemini_enterprise_console_url" {
  description = "GCP Console URL to open the Gemini Enterprise App & Agents panel"
  value       = "https://console.cloud.google.com/gen-app-builder/engines?project=${var.project_id}"
}

output "monitoring_dashboard_console_url" {
  description = "GCP Console URL to open the CCTV AI Audit Evaluation & Operations Dashboard in Cloud Monitoring"
  value       = "https://console.cloud.google.com/monitoring/dashboards?project=${var.project_id}"
}
