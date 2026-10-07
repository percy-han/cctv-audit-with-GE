# Template for one more copy of the stack: cp example.tfvars <env>.tfvars and replace every <...>.
# <env> is any name; deploy it with _ENV=<env> (bootstrap/ passes it: var.trigger_env). Two copies
# can share one project as long as their name_prefix differs; every resource name derives from it
# (worker SA / Cloud Run <p>-worker, watchdog <p>-watchdog, images <p>-images, bucket
# <project>-<p>-staging, ReasoningEngine <p>-agent, Gemini Enterprise app <p>-ge).
# Must match bootstrap/<env>.tfvars (same project_id, name_prefix, region).
# container_image is intentionally absent: the pipeline passes the digest it built.

project_id                 = "<project-id>"
name_prefix                = "<prefix>" # 2-20 chars [a-z0-9-], e.g. "cctv-store-b"; never reuse another stack's
region                     = "<region>" # e.g. asia-southeast1 (data residency of bucket, Cloud Run, images)
reasoning_engine_location  = "<region>" # Vertex AI Agent Engine region, e.g. us-central1
master_prompt_sheet_id     = "<master-prompt-sheet-id>"
workspace_impersonate_user = "<bot-user@customer-domain>" # "" = the worker SA itself (Shared Drives only)

# Optional
# vertex_model_location = "global"
# scheduler_time_zone   = "Etc/UTC"
# ge_company_name       = "<Company>"          # shown by the new Gemini Enterprise app
# ge_tenant_label       = "<brand>"            # used in agent descriptions / default display names
# ge_agent_display_name = "<agent name>"       # default: "<brand> 门店监控 AI 稽核专家 [<name_prefix>]"; keep it unique per stack
# ge_example_folder_url = "https://drive.google.com/drive/folders/<id>"
# extra_ge_engine_ids      = []                   # only Gemini Enterprise apps this stack owns
# enable_google_chat_notification = false
# google_chat_webhook_url         = "https://chat.googleapis.com/v1/spaces/<space>/messages?key=...&token=..."
