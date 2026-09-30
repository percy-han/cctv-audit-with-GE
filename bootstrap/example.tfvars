# Template: cp example.tfvars <env>.tfvars and replace every <...>. Applied once by a project owner:
#   terraform init -backend-config=<env>.gcs.tfbackend
#   terraform apply -var-file=<env>.tfvars
# Must match ../<env>.tfvars (same project_id, name_prefix, region). Names derived from name_prefix:
# worker SA <p>-worker, deployer SA <p>-deployer, image repository <p>-images, trigger <p>-deploy.

project_id  = "<project-id>"
name_prefix = "<prefix>" # 2-20 chars [a-z0-9-]; never reuse another stack's
region      = "<region>" # e.g. asia-southeast1

# Environment file pair (../<env>.tfvars, ../<env>.gcs.tfbackend) the trigger and
# manual_deploy_command deploy. Empty = project_id; set it when the project holds more than one stack.
trigger_env = "<env>"

# Push-to-deploy (optional): Cloud Build 2nd-gen repository, connected once in the console.
trigger_repository = ""
# trigger_branch           = "^main$"
# trigger_require_approval = false
# display_label            = "<Human readable label>" # default: name_prefix
