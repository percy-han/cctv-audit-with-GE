"""Deploys the Chagee CCTV Audit container to Vertex AI ReasoningEngine (Tier 1)
and automatically provisions + binds the Gemini Enterprise (Discovery Engine) App & Agent.

Directly reusable across environments (Sandbox & Customer Production via `terraform apply` in `main.tf`),
including several stacks side by side in one project: every name comes from a flag that main.tf
derives from its `name_prefix`, and nothing here touches a resource another stack owns.
1. Creates or updates the Vertex AI Agent Engine (`ReasoningEngine` BYOC container `--display-name`),
   injecting `VERTEX_MODEL_LOCATION`, `CLOUD_RUN_WORKER_URL`, `STAGING_BUCKET` and
   `MASTER_PROMPT_SHEET_ID`, and waits for the LRO operation to complete. The engine to update is the
   one whose display name is `--display-name` (or a `--legacy-display-name`) and which runs as
   `--service-account`; a same-named engine running as another account is refused, never patched.
2. Creates (if not already present) the Gemini Enterprise App (`Discovery Engine` Engine:
   `projects/{project}/locations/global/collections/default_collection/engines/{ge_engine_id}`)
   with `gemini-3.8-flash` enabled.
3. Registers or updates the CCTV Audit ADK Agent (`adkAgentDefinition.provisionedReasoningEngine`)
   under `assistants/default_assistant/agents` of that app and of every `--extra-ge-engine-id` that
   exists. Only this stack's agent is updated: the one bound to this ReasoningEngine, or named
   `--ge-agent-display-name` while bound to an engine that is gone or runs as `--service-account`.
   A same-named agent of another stack is never touched; the deploy then fails instead.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

import google.auth
import google.auth.transport.requests

CLASS_METHODS: list[dict[str, Any]] = [
    {
        "name": "streaming_agent_run_with_events",
        "api_mode": "async_stream",
        "description": (
            "The Gemini Enterprise entry point. Takes one conversation turn "
            "and streams ADK events back."
        ),
        "parameters": {
            "type": "object",
            "required": ["request_json"],
            "properties": {"request_json": {"type": "string"}},
        },
    },
    {
        "name": "preflight",
        "api_mode": "",
        "description": "Probes a Google Drive CCTV folder for resolution (>=720p) and duration.",
        "parameters": {
            "type": "object",
            "required": ["user_id", "target"],
            "properties": {
                "user_id": {"type": "string"},
                "target": {"type": "string"},
                "session_id": {"type": "string"},
            },
        },
    },
    {
        "name": "start_audit",
        "api_mode": "",
        "description": "Starts detached background multimodal video audit for a READY preflight job.",
        "parameters": {
            "type": "object",
            "required": ["user_id", "job_id"],
            "properties": {
                "user_id": {"type": "string"},
                "job_id": {"type": "string"},
            },
        },
    },
    {
        "name": "get_status",
        "api_mode": "",
        "description": "Queries progress or completed Google Sheet report URL for an audit job.",
        "parameters": {
            "type": "object",
            "required": ["user_id"],
            "properties": {
                "user_id": {"type": "string"},
                "job_id": {"type": "string"},
                "session_id": {"type": "string"},
            },
        },
    },
]


def _get_access_token() -> str:
    """Token for whoever runs this script.

    Application Default Credentials first: in the Cloud Build pipeline (cloudbuild.yaml) that is the
    deployer SA via the metadata server, and the pipeline image has no gcloud. The gcloud fallback
    only serves manual runs on a workstation whose ADC has gone stale.
    """
    adc_error: object = "ADC returned no token"
    try:
        creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        creds.refresh(google.auth.transport.requests.Request())
        if creds.token:
            return creds.token
    except Exception as exc:  # noqa: BLE001 - reported below together with the fallback's error
        adc_error = exc
    try:
        return subprocess.check_output(["gcloud", "auth", "print-access-token"], text=True).strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise SystemExit(
            f"No credentials: Application Default Credentials failed ({adc_error}); "
            f"gcloud fallback failed ({exc})."
        ) from exc


def _call(
    method: str,
    url: str,
    body: dict[str, Any] | None = None,
    project_id: str = "",
    ignore_errors: bool = False,
) -> dict[str, Any]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {_get_access_token()}")
    req.add_header("Content-Type", "application/json")
    if project_id:
        req.add_header("X-Goog-User-Project", project_id)
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        err_text = exc.read().decode(errors="replace")
        if ignore_errors:
            return {"_http_error": exc.code, "_error_body": err_text}
        print(f"HTTP {exc.code} on {method} {url}\n{err_text}", file=sys.stderr)
        raise SystemExit(1)


def _brand(company_name: str, tenant_label: str) -> str:
    """"<company> <label>" with empty parts dropped (e.g. "CHAGEE 霸王茶姬", or "")."""
    return " ".join(part for part in (company_name.strip(), tenant_label.strip()) if part)


def build_reasoning_engine_body(
    project_id: str,
    location: str,
    image_uri: str,
    master_prompt_sheet_id: str,
    *,
    display_name: str,
    gcp_location: str,
    staging_bucket: str = "",
    service_account: str = "",
    cloud_run_worker_url: str = "",
    vertex_model_location: str = "global",
    workspace_impersonate_user: str = "",
    workspace_dwd_service_account: str = "",
    google_chat_webhook_url: str = "",
    company_name: str = "",
    tenant_label: str = "",
    gcs_sop_uri: str = "",
) -> dict[str, Any]:
    if not display_name:
        raise ValueError("display_name is required: it is how this stack's ReasoningEngine is found again")
    if not gcp_location:
        raise ValueError("gcp_location (the stack's region, GCP_LOCATION) is required")
    env_list = [
        {"name": "GCP_PROJECT", "value": project_id},
        {"name": "GCP_LOCATION", "value": gcp_location},
        {"name": "VERTEX_MODEL_LOCATION", "value": vertex_model_location},
        {"name": "MASTER_PROMPT_SHEET_ID", "value": master_prompt_sheet_id},
        {"name": "KEEPALIVE_HOLD_SECONDS", "value": "25"},
        {"name": "ENABLE_BACKGROUND_WATCHDOG", "value": "false"},
    ]
    if staging_bucket:
        env_list.append({"name": "STAGING_BUCKET", "value": staging_bucket})
    # Terraform-seeded gs://<workspace_bucket>/sop/master_sheet.xlsx (Zero-GWS / GCS-mode SOP source).
    if gcs_sop_uri:
        env_list.append({"name": "GCS_SOP_URI", "value": gcs_sop_uri})
    if cloud_run_worker_url:
        env_list.append({"name": "CLOUD_RUN_WORKER_URL", "value": cloud_run_worker_url})
    # Same Workspace identity as the Tier-2 worker (main.tf): keyless DWD when a bot user is set.
    if workspace_dwd_service_account:
        env_list.append(
            {"name": "WORKSPACE_DWD_SERVICE_ACCOUNT", "value": workspace_dwd_service_account}
        )
    if workspace_impersonate_user:
        env_list.append({"name": "WORKSPACE_IMPERSONATE_USER", "value": workspace_impersonate_user})
    if google_chat_webhook_url:
        env_list.append({"name": "GOOGLE_CHAT_WEBHOOK_URL", "value": google_chat_webhook_url})

    deployment_spec: dict[str, Any] = {
        "resourceLimits": {"cpu": "4", "memory": "8Gi"},
        "containerConcurrency": 10,
        "keepAliveProbe": {
            "httpGet": {"path": "/is_busy", "port": 8080},
            "maxSeconds": 3600,
        },
        "minInstances": 1,
        "maxInstances": 10,
        "env": env_list,
    }
    spec: dict[str, Any] = {
        "agentFramework": "custom",
        "containerSpec": {"imageUri": image_uri, "port": 8080},
        "classMethods": CLASS_METHODS,
        "deploymentSpec": deployment_spec,
    }
    if service_account:
        spec["serviceAccount"] = service_account

    return {
        "displayName": display_name,
        "description": (
            f"{_brand(company_name, tenant_label)}门店 CCTV AI 合规稽核 Agent（Tier-1 GE 网关 + Tier-2 Cloud Run 弹性切片推理）。"
            "支持 Google Drive 监控文件夹零 Token 预检、确认启动、进度查询及自动回写双页签 Google Sheet 报表。"
        ),
        "spec": spec,
    }


def wait_for_reasoning_engine_op(host: str, op_name: str, project_id: str, timeout_sec: int = 900) -> str:
    deadline = time.time() + timeout_sec
    url = f"https://{host}/v1/{op_name}"
    while time.time() < deadline:
        res = _call("GET", url, project_id=project_id)
        if res.get("done"):
            if "error" in res:
                raise RuntimeError(f"ReasoningEngine operation failed: {json.dumps(res['error'])}")
            re_name = res.get("response", {}).get("name", "")
            print(f"✅ ReasoningEngine ready: {re_name}")
            return re_name
        print(f"⏳ Waiting for ReasoningEngine LRO ({op_name})...")
        time.sleep(10)
    raise TimeoutError(f"Timed out waiting for {op_name}")


def find_stack_reasoning_engines(
    engines: list[dict[str, Any]],
    display_names: list[str],
    service_account: str,
) -> list[dict[str, Any]]:
    """This stack's ReasoningEngines among `engines`, newest first.

    An engine is this stack's when its display name is one of `display_names`. Display names are
    derived from the stack's name_prefix, so another stack's engine never matches; as a second guard,
    a same-named engine that runs as a different service account (i.e. belongs to another stack) is
    refused instead of being patched or deleted.
    """
    wanted = {n for n in display_names if n}
    matches = [eng for eng in engines if eng.get("displayName") in wanted]
    if service_account:
        foreign = [
            eng
            for eng in matches
            if eng.get("spec", {}).get("serviceAccount") not in ("", None, service_account)
        ]
        if foreign:
            details = ", ".join(
                f"{eng.get('name')} (runs as {eng.get('spec', {}).get('serviceAccount')})" for eng in foreign
            )
            raise SystemExit(
                f"Refusing to update ReasoningEngine(s) named {sorted(wanted)} that run as another "
                f"service account than {service_account}: {details}. They belong to another stack; "
                "give this stack a different name_prefix / reasoning_engine_display_name."
            )
    matches.sort(key=lambda e: e.get("updateTime", e.get("createTime", "")), reverse=True)
    return matches


def reasoning_engine_url(reasoning_engine_name: str) -> str:
    """REST URL of a ReasoningEngine resource name (projects/.../locations/<loc>/reasoningEngines/<id>)."""
    parts = reasoning_engine_name.split("/")
    location = parts[3] if len(parts) > 3 and parts[2] == "locations" else "global"
    host = "aiplatform.googleapis.com" if location == "global" else f"{location}-aiplatform.googleapis.com"
    return f"https://{host}/v1/{reasoning_engine_name}"


def select_stack_agents(
    agents: list[dict[str, Any]],
    reasoning_engine_name: str,
    agent_display_name: str,
    *,
    service_account: str,
    get_engine: Callable[[str], dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[tuple[dict[str, Any], str]]]:
    """Splits the ADK agents of one GE app into (claimed, refused) for this stack.

    Claimed (will be PATCHed):
      * an agent bound to `reasoning_engine_name` (this stack's engine);
      * an agent named `agent_display_name` whose bound engine
          - no longer exists (GET returns 404; e.g. this stack re-created its engine), or
          - exists and runs as `service_account` (this stack's worker SA; the same ownership test as
            find_stack_reasoning_engines), or
          - is not set at all (an agent without an engine binding has no owner to protect; the display
            name is the only identity it has).
    Refused (never touched, returned with the reason): an agent with the same display name whose bound
    engine runs as another (or no) service account, or whose engine GET fails with anything but 404 --
    ownership then cannot be proven, so it is treated as another stack's (e.g. production's) agent.
    Agents with neither this engine nor this display name are ignored.
    `get_engine(name)` returns the engine JSON or {"_http_error": code, ...}.
    """
    claimed: list[dict[str, Any]] = []
    refused: list[tuple[dict[str, Any], str]] = []
    for ag in agents:
        definition = ag.get("adkAgentDefinition")
        if definition is None:
            continue
        bound_to = definition.get("provisionedReasoningEngine", {}).get("reasoningEngine", "")
        if bound_to and bound_to == reasoning_engine_name:
            claimed.append(ag)
            continue
        if not agent_display_name or ag.get("displayName") != agent_display_name:
            continue
        if not bound_to:
            claimed.append(ag)
            continue
        engine = get_engine(bound_to)
        code = engine.get("_http_error")
        if code == 404:
            claimed.append(ag)
        elif code:
            refused.append((ag, f"its ReasoningEngine {bound_to} could not be read (HTTP {code})"))
        else:
            owner = engine.get("spec", {}).get("serviceAccount", "")
            if service_account and owner == service_account:
                claimed.append(ag)
            else:
                refused.append(
                    (ag, f"it is bound to ReasoningEngine {bound_to} running as {owner or '<default SA>'}, "
                     f"not this stack's {service_account or '<unknown SA>'}")
                )
    return claimed, refused


def _report(action: str, eng: str, result: dict[str, Any], reasoning_engine_name: str) -> None:
    if result.get("_http_error"):
        print(
            f"⚠️ {action} ADK Agent in GE Engine `{eng}` FAILED: HTTP {result['_http_error']} "
            f"{result.get('_error_body', '')}",
            file=sys.stderr,
        )
    else:
        print(f"✅ {action} ADK Agent in GE Engine `{eng}` -> {result.get('name')} (bound to {reasoning_engine_name})")


# Second DataStore ID used while `<ge_engine_id>-store` is still being deleted (deletion takes hours).
GE_DATASTORE_FALLBACK_SUFFIX = "-store-v2"


def ensure_gemini_enterprise_and_bind_agent(
    project_id: str,
    reasoning_engine_name: str,
    ge_engine_id: str,
    *,
    ge_display_name: str,
    agent_display_name: str,
    company_name: str = "",
    tenant_label: str = "",
    example_folder_url: str = "",
    extra_engine_ids: tuple[str, ...] | list[str] = (),
    service_account: str = "",
) -> dict[str, Any]:
    """Ensures the Gemini Enterprise (Discovery Engine) App `ge_engine_id` exists and binds
    `reasoning_engine_name` into `assistants/default_assistant/agents` of it and of every
    `extra_engine_ids` app that already exists (those are never created).

    Only agents owned by this stack are updated (see select_stack_agents; `service_account` is the
    stack's worker SA). If an app holds an agent with this stack's display name that belongs to another
    stack and this stack has no agent of its own there, nothing is created in that app (a second agent
    with an identical name would be indistinguishable for users) and SystemExit is raised after all apps
    are processed, so the deploy fails loudly instead of silently rebinding or duplicating.
    """
    if not ge_engine_id:
        raise ValueError("ge_engine_id is required")
    de_base = f"https://discoveryengine.googleapis.com/v1alpha/projects/{project_id}/locations/global/collections/default_collection"
    engine_create_error = ""

    # 1. Ensure a backing DataStore exists if we need to create a new Engine. A DataStore ID that was
    #    just deleted (e.g. by destroy-stack) stays reserved for hours ("is being deleted"), so fall back
    #    to `<ge_engine_id>-store-v2` instead of leaving the app uncreatable until then.
    ds_body = {
        "displayName": f"{ge_display_name} Data Store",
        "industryVertical": "GENERIC",
        "solutionTypes": ["SOLUTION_TYPE_SEARCH"],
        "contentConfig": "NO_CONTENT",
    }
    ds_id = f"{ge_engine_id}-store"
    ds_check = _call("GET", f"{de_base}/dataStores/{ds_id}", project_id=project_id, ignore_errors=True)
    if ds_check.get("_http_error") == 404:
        fallback_id = f"{ge_engine_id}{GE_DATASTORE_FALLBACK_SUFFIX}"
        fb_check = _call("GET", f"{de_base}/dataStores/{fallback_id}", project_id=project_id, ignore_errors=True)
        if not fb_check.get("_http_error"):
            ds_id = fallback_id  # created by an earlier fallback run
        else:
            print(f"📦 Creating Discovery Engine DataStore `{ds_id}`...")
            created_ds = _call(
                "POST", f"{de_base}/dataStores?dataStoreId={ds_id}", body=ds_body, project_id=project_id, ignore_errors=True
            )
            if created_ds.get("_http_error") and "being deleted" in str(created_ds.get("_error_body", "")):
                print(f"♻️ DataStore `{ds_id}` is still being deleted; using `{fallback_id}` instead.")
                ds_id = fallback_id
                created_ds = _call(
                    "POST",
                    f"{de_base}/dataStores?dataStoreId={ds_id}",
                    body=ds_body,
                    project_id=project_id,
                    ignore_errors=True,
                )
            if created_ds.get("_http_error"):
                print(
                    f"❌ Creating DataStore `{ds_id}` FAILED: HTTP {created_ds['_http_error']} "
                    f"{created_ds.get('_error_body', '')}",
                    file=sys.stderr,
                )

    # 2. Check or create Gemini Enterprise Engine (`ge_engine_id`)
    eng_url = f"{de_base}/engines/{ge_engine_id}"
    eng_check = _call("GET", eng_url, project_id=project_id, ignore_errors=True)
    if eng_check.get("_http_error") == 404:
        print(f"🚀 Creating Gemini Enterprise App (`Engine`: `{ge_engine_id}`)...")
        engine_body: dict[str, Any] = {
            "displayName": ge_display_name,
            "solutionType": "SOLUTION_TYPE_SEARCH",
            "industryVertical": "GENERIC",
            "appType": "APP_TYPE_INTRANET",
            "dataStoreIds": [ds_id],
            "searchEngineConfig": {
                "searchTier": "SEARCH_TIER_ENTERPRISE",
                "searchAddOns": ["SEARCH_ADD_ON_LLM"],
            },
            "modelConfigs": {
                "gemini-3.8-flash": "MODEL_ENABLED",
                "gemini-3.7-flash": "MODEL_ENABLED",
                "gemini-3.1-pro-preview": "MODEL_ENABLED",
            },
        }
        if company_name:
            engine_body["commonConfig"] = {"companyName": company_name}
        op = _call(
            "POST",
            f"{de_base}/engines?engineId={ge_engine_id}",
            body=engine_body,
            project_id=project_id,
            ignore_errors=True,
        )
        print("Engine creation response:", json.dumps(op, ensure_ascii=False))
        if op.get("_http_error"):
            # Fail the deploy (after binding the extra apps below) instead of reporting success
            # while the stack's own GE app does not exist.
            engine_create_error = f"HTTP {op['_http_error']} {op.get('_error_body', '')}"
            print(f"❌ Creating GE Engine `{ge_engine_id}` FAILED: {engine_create_error}", file=sys.stderr)
        time.sleep(5)
    else:
        print(f"✅ Gemini Enterprise App `{ge_engine_id}` already exists.")

    # 3. Bind/Update this stack's ADK Agent on `ge_engine_id` + each existing `extra_engine_ids` app.
    target_engines = [] if engine_create_error else [ge_engine_id]
    for extra in extra_engine_ids:
        if not extra or extra in target_engines:
            continue
        found = _call("GET", f"{de_base}/engines/{extra}", project_id=project_id, ignore_errors=True)
        if found.get("_http_error"):
            print(f"ℹ️ Extra GE Engine `{extra}` not found (HTTP {found['_http_error']}); skipped.")
            continue
        target_engines.append(extra)

    folder_example = example_folder_url or "<粘贴门店监控 Google Drive 文件夹链接>"
    agent_payload = {
        "displayName": agent_display_name,
        "description": (
            f"负责{tenant_label}门店 CCTV 监控视频 SOP 合规稽核。当督导发送 Google Drive 监控视频文件夹链接时，"
            "先执行零 Token 分辨率(>=720P)与时长预检；督导回复「确认开始」后，调度 Cloud Run 弹性算力池与 "
            "gemini-3.8-flash (global agentic) 分析视频，自动在原 Drive 文件夹生成 20 秒违规证据切片与双页签复核 Sheet。"
        ),
        "adkAgentDefinition": {
            "provisionedReasoningEngine": {
                "reasoningEngine": reasoning_engine_name,
            }
        },
        "state": "ENABLED",
        "languageCode": "zh-CN",
        "sharingConfig": {"scope": "ALL_USERS"},
        "starterPrompts": [
            {"text": f"帮我预检这个门店监控 Google Drive 文件夹：{folder_example}"},
            {"text": "确认开始稽核"},
            {"text": "查询我刚才提交的稽核任务进度"},
        ],
        "customPlaceholderText": "粘贴 Google Drive 门店监控文件夹链接启动预检，或输入「确认开始」「查询进度」",
    }

    engine_cache: dict[str, dict[str, Any]] = {}

    def _get_engine(name: str) -> dict[str, Any]:
        if name not in engine_cache:
            engine_cache[name] = _call("GET", reasoning_engine_url(name), project_id=project_id, ignore_errors=True)
        return engine_cache[name]

    bound_agents: dict[str, Any] = {}
    blocked_engines: list[str] = []
    for eng in target_engines:
        agents_url = f"{de_base}/engines/{eng}/assistants/default_assistant/agents"
        listed = _call("GET", agents_url, project_id=project_id, ignore_errors=True)
        stack_agents, foreign_agents = select_stack_agents(
            listed.get("agents", []),
            reasoning_engine_name,
            agent_display_name,
            service_account=service_account,
            get_engine=_get_engine,
        )
        for ag, reason in foreign_agents:
            print(
                f"⛔ Not touching agent {ag.get('name')} ({ag.get('displayName')!r}) in GE Engine `{eng}`: "
                f"{reason}. It belongs to another stack.",
                file=sys.stderr,
            )

        if stack_agents:
            for ag in stack_agents:
                existing_agent_name = ag["name"]
                patch_body = dict(agent_payload)
                # `state` is immutable on PATCH (HTTP 400 "updateMask contains an immutable path"); an agent
                # created ENABLED stays ENABLED, so it is only sent when the agent is first created.
                patch_body.pop("state", None)
                # Preserve existing custom displayName if already set on a legacy engine
                if ag.get("displayName"):
                    patch_body["displayName"] = ag["displayName"]
                patch_url = (
                    f"https://discoveryengine.googleapis.com/v1alpha/{existing_agent_name}"
                    "?updateMask=displayName,description,adkAgentDefinition,starterPrompts,customPlaceholderText"
                )
                updated = _call(
                    "PATCH",
                    patch_url,
                    body=patch_body,
                    project_id=project_id,
                    ignore_errors=True,
                )
                _report("Updated", eng, {"name": existing_agent_name, **updated}, reasoning_engine_name)
                bound_agents[f"{eng}:{existing_agent_name}"] = updated
        elif foreign_agents:
            blocked_engines.append(eng)
        else:
            created = _call(
                "POST",
                agents_url,
                body=agent_payload,
                project_id=project_id,
                ignore_errors=True,
            )
            _report("Registered new", eng, created, reasoning_engine_name)
            bound_agents[eng] = created

    if blocked_engines:
        raise SystemExit(
            f"GE app(s) {blocked_engines} already hold an agent named {agent_display_name!r} that belongs to "
            "another stack; no agent was created or changed there. Give this stack its own "
            "ge_agent_display_name (the default includes name_prefix) and remove foreign apps from "
            "ge_engine_id / extra_ge_engine_ids."
        )
    if engine_create_error:
        raise SystemExit(
            f"GE app `{ge_engine_id}` could not be created ({engine_create_error}); agents in the extra "
            f"apps {target_engines} were bound. Re-run this deploy once the cause is fixed."
        )
    return bound_agents


def _add_ge_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--ge-engine-id",
        required=True,
        help="This stack's Gemini Enterprise app ID (main.tf: <name_prefix>-ge); created if missing",
    )
    p.add_argument(
        "--extra-ge-engine-id",
        action="append",
        default=[],
        help="Existing GE app that should also expose this stack's agent (repeatable; never created)",
    )
    p.add_argument("--ge-app-display-name", default="", help="Display name of a newly created GE app (default: the app ID)")
    p.add_argument(
        "--ge-agent-display-name",
        default="",
        help="Display name of a newly created agent; also identifies this stack's agent (default: '<app ID> agent')",
    )
    p.add_argument("--ge-company-name", default="", help="commonConfig.companyName of a newly created GE app")
    p.add_argument("--ge-tenant-label", default="", help="Brand label used in agent/engine descriptions")
    p.add_argument("--ge-example-folder-url", default="", help="Drive folder link used in the first starter prompt")


def _bind_from_args(args: argparse.Namespace, reasoning_engine_name: str) -> dict[str, Any]:
    return ensure_gemini_enterprise_and_bind_agent(
        project_id=args.project_id,
        reasoning_engine_name=reasoning_engine_name,
        ge_engine_id=args.ge_engine_id,
        ge_display_name=args.ge_app_display_name or args.ge_engine_id,
        agent_display_name=args.ge_agent_display_name or f"{args.ge_engine_id} agent",
        company_name=args.ge_company_name,
        tenant_label=args.ge_tenant_label,
        example_folder_url=args.ge_example_folder_url,
        extra_engine_ids=args.extra_ge_engine_id,
        service_account=args.service_account,
    )


def _resolve_gcp_location(
    *,
    gcp_location: str,
    image_uri: str,
    own_engines: list[dict[str, Any]],
    staging_bucket: str,
    project_id: str,
) -> str:
    if gcp_location:
        return gcp_location
    for eng in own_engines:
        for item in eng.get("spec", {}).get("deploymentSpec", {}).get("env", []):
            if item.get("name") == "GCP_LOCATION" and item.get("value"):
                return str(item["value"])
    if "-docker.pkg.dev/" in image_uri:
        candidate = image_uri.split("-docker.pkg.dev/", 1)[0].rsplit("/", 1)[-1]
        if candidate and candidate != "placeholder":
            return candidate
    if staging_bucket:
        meta = _call(
            "GET",
            f"https://storage.googleapis.com/storage/v1/b/{urllib.parse.quote(staging_bucket, safe='')}",
            project_id=project_id,
            ignore_errors=True,
        )
        loc = str(meta.get("location", "")).strip().lower()
        if loc:
            return loc
    return ""


def _bucket_owned_by_stack(
    *,
    bucket: str,
    project_id: str,
    name_prefix: str,
    service_account: str,
) -> bool:
    if not bucket or bucket.endswith("-tfstate"):
        return False
    if name_prefix and bucket == f"{project_id}-{name_prefix}-staging":
        return True
    iam = _call(
        "GET",
        f"https://storage.googleapis.com/storage/v1/b/{urllib.parse.quote(bucket, safe='')}/iam",
        project_id=project_id,
        ignore_errors=True,
    )
    if iam.get("_http_error"):
        return False
    wanted_member = f"serviceAccount:{service_account}"
    for binding in iam.get("bindings", []):
        if wanted_member in binding.get("members", []):
            return True
    return False


def empty_gcs_bucket(bucket: str, *, project_id: str) -> int:
    """Deletes all object versions in `bucket` so `google_storage_bucket` destroy succeeds."""
    deleted = 0
    q_bucket = urllib.parse.quote(bucket, safe="")
    page_token = ""
    while True:
        url = f"https://storage.googleapis.com/storage/v1/b/{q_bucket}/o?versions=true"
        if page_token:
            url += f"&pageToken={urllib.parse.quote(page_token, safe='')}"
        listing = _call("GET", url, project_id=project_id, ignore_errors=True)
        if listing.get("_http_error"):
            break
        items = listing.get("items", [])
        for item in items:
            obj_name = item.get("name", "")
            if not obj_name:
                continue
            q_obj = urllib.parse.quote(obj_name, safe="")
            del_url = f"https://storage.googleapis.com/storage/v1/b/{q_bucket}/o/{q_obj}"
            gen = item.get("generation")
            if gen:
                del_url += f"?generation={urllib.parse.quote(str(gen), safe='')}"
            res = _call("DELETE", del_url, project_id=project_id, ignore_errors=True)
            if not res.get("_http_error"):
                deleted += 1
        page_token = str(listing.get("nextPageToken", "")).strip()
        if not page_token or not items:
            break
    return deleted


def seed_gcs_object(bucket: str, name: str, source: str, content_type: str) -> str:
    """Uploads `source` to gs://`bucket`/`name` only if no live object exists (`ifGenerationMatch=0`).

    Returns "created" or "exists" (HTTP 412: the object is already there and is left untouched, e.g. a
    customer-edited SOP workbook). Any other failure is fatal.
    """
    data = Path(source).read_bytes()
    url = (
        "https://storage.googleapis.com/upload/storage/v1/b/"
        f"{urllib.parse.quote(bucket, safe='')}/o?uploadType=media"
        f"&name={urllib.parse.quote(name, safe='')}&ifGenerationMatch=0"
    )
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Authorization", f"Bearer {_get_access_token()}")
    req.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            gen = json.loads(resp.read() or b"{}").get("generation", "")
        print(f"🌱 Seeded gs://{bucket}/{name} from {source} (generation {gen}).")
        return "created"
    except urllib.error.HTTPError as exc:
        if exc.code == 412:
            print(f"✅ gs://{bucket}/{name} already exists; left untouched.")
            return "exists"
        raise SystemExit(f"Seeding gs://{bucket}/{name} failed: HTTP {exc.code} {exc.read().decode(errors='replace')}")


def destroy_stack(
    *,
    project_id: str,
    location: str,
    service_account: str,
    ge_engine_id: str,
    staging_bucket: str = "",
    gcp_location: str = "",
    image_uri: str = "",
) -> dict[str, Any]:
    """Tears down the non-Terraform-native resources owned by this stack (`service_account`) during
    `terraform destroy`:
      1. Deletes this stack's ADK agent(s) in `ge_engine_id`, and if no foreign stack's agents live in
         `ge_engine_id`, deletes the GE Engine `ge_engine_id` and DataStore `<ge_engine_id>-store`.
      2. Deletes every ReasoningEngine in `location` whose `spec.serviceAccount == service_account`.
      3. Empties `staging_bucket` (after verifying stack ownership and refusing `-tfstate` buckets).
      4. Deletes the stack's Cloud Run v2 worker service (`<name_prefix>-worker`) if it runs as
         `service_account`.
    """
    if not service_account or "@" not in service_account:
        raise ValueError(
            "service_account (<name_prefix>-worker@<project>.iam.gserviceaccount.com) is required "
            "for destroy-stack so another stack's resources are never touched"
        )
    worker_account_id = service_account.split("@", 1)[0]
    name_prefix = (
        worker_account_id[: -len("-worker")]
        if worker_account_id.endswith("-worker")
        else worker_account_id
    )

    host = (
        "aiplatform.googleapis.com"
        if location == "global"
        else f"{location}-aiplatform.googleapis.com"
    )
    re_base = f"https://{host}/v1/projects/{project_id}/locations/{location}/reasoningEngines"
    re_list = _call("GET", re_base, project_id=project_id, ignore_errors=True).get("reasoningEngines", [])
    own_engines = [
        eng for eng in re_list if eng.get("spec", {}).get("serviceAccount") == service_account
    ]
    own_engine_names = {eng["name"] for eng in own_engines if eng.get("name")}

    summary: dict[str, Any] = {
        "deleted_agents": [],
        "deleted_ge_engine": False,
        "deleted_ge_datastore": False,
        "deleted_reasoning_engines": [],
        "deleted_bucket_objects": 0,
        "deleted_cloud_run_service": "",
    }

    # 1. Gemini Enterprise agent(s) + Engine + DataStore
    if ge_engine_id:
        de_base = (
            f"https://discoveryengine.googleapis.com/v1alpha/projects/{project_id}"
            "/locations/global/collections/default_collection"
        )
        engine_cache: dict[str, dict[str, Any]] = {
            eng["name"]: eng for eng in re_list if eng.get("name")
        }

        def _get_engine(name: str) -> dict[str, Any]:
            if name not in engine_cache:
                engine_cache[name] = _call(
                    "GET", reasoning_engine_url(name), project_id=project_id, ignore_errors=True
                )
            return engine_cache[name]

        agents_url = f"{de_base}/engines/{ge_engine_id}/assistants/default_assistant/agents"
        listed = _call("GET", agents_url, project_id=project_id, ignore_errors=True)
        stack_agents: list[dict[str, Any]] = []
        foreign_agents: list[dict[str, Any]] = []
        if not listed.get("_http_error"):
            for ag in listed.get("agents", []):
                defn = ag.get("adkAgentDefinition")
                if defn is None:
                    continue
                bound_to = defn.get("provisionedReasoningEngine", {}).get("reasoningEngine", "")
                if bound_to in own_engine_names:
                    stack_agents.append(ag)
                    continue
                if not bound_to:
                    stack_agents.append(ag)
                    continue
                eng_obj = _get_engine(bound_to)
                code = eng_obj.get("_http_error")
                if code == 404:
                    stack_agents.append(ag)
                elif not code and eng_obj.get("spec", {}).get("serviceAccount") == service_account:
                    stack_agents.append(ag)
                else:
                    foreign_agents.append(ag)

            for ag in stack_agents:
                ag_name = ag.get("name", "")
                if not ag_name:
                    continue
                print(f"🧹 Deleting ADK Agent `{ag_name}` from GE Engine `{ge_engine_id}`...")
                res = _call(
                    "DELETE",
                    f"https://discoveryengine.googleapis.com/v1alpha/{ag_name}",
                    project_id=project_id,
                    ignore_errors=True,
                )
                if not res.get("_http_error"):
                    summary["deleted_agents"].append(ag_name)

        if foreign_agents:
            print(
                f"⛔ Keeping GE Engine `{ge_engine_id}` and DataStore `{ge_engine_id}-store`: "
                f"{len(foreign_agents)} agent(s) belong to another stack.",
                file=sys.stderr,
            )
        else:
            eng_meta = _call("GET", f"{de_base}/engines/{ge_engine_id}", project_id=project_id, ignore_errors=True)
            print(f"🧹 Deleting Gemini Enterprise Engine `{ge_engine_id}`...")
            eng_del = _call(
                "DELETE",
                f"{de_base}/engines/{ge_engine_id}",
                project_id=project_id,
                ignore_errors=True,
            )
            if not eng_del.get("_http_error") or eng_del.get("_http_error") == 404:
                summary["deleted_ge_engine"] = True
            # The app's own DataStore(s): whatever the engine lists, plus both IDs this script may create.
            ds_ids: list[str] = []
            for candidate in [
                *[str(d).rsplit("/", 1)[-1] for d in (eng_meta.get("dataStoreIds") or [])],
                f"{ge_engine_id}-store",
                f"{ge_engine_id}{GE_DATASTORE_FALLBACK_SUFFIX}",
            ]:
                if candidate and candidate not in ds_ids:
                    ds_ids.append(candidate)
            all_ok = True
            for ds_id in ds_ids:
                print(f"🧹 Deleting Discovery Engine DataStore `{ds_id}`...")
                ds_del = _call(
                    "DELETE",
                    f"{de_base}/dataStores/{ds_id}",
                    project_id=project_id,
                    ignore_errors=True,
                )
                if ds_del.get("_http_error") and ds_del.get("_http_error") != 404:
                    all_ok = False
            summary["deleted_ge_datastore"] = all_ok

    # 2. Vertex AI ReasoningEngine(s) running as `service_account`
    for eng in own_engines:
        re_name = eng.get("name", "")
        if not re_name:
            continue
        print(f"🧹 Deleting ReasoningEngine `{re_name}` (force=true)...")
        res = _call(
            "DELETE",
            f"https://{host}/v1/{re_name}?force=true",
            project_id=project_id,
            ignore_errors=True,
        )
        if not res.get("_http_error"):
            summary["deleted_reasoning_engines"].append(re_name)

    # 3. Staging bucket contents (so Terraform can delete the bucket even if force_destroy was false in state)
    resolved_region = _resolve_gcp_location(
        gcp_location=gcp_location,
        image_uri=image_uri,
        own_engines=own_engines,
        staging_bucket=staging_bucket,
        project_id=project_id,
    )
    if staging_bucket:
        if _bucket_owned_by_stack(
            bucket=staging_bucket,
            project_id=project_id,
            name_prefix=name_prefix,
            service_account=service_account,
        ):
            print(f"🧹 Emptying staging bucket `gs://{staging_bucket}`...")
            summary["deleted_bucket_objects"] = empty_gcs_bucket(
                staging_bucket, project_id=project_id
            )
        else:
            print(
                f"⛔ Refusing to empty bucket `{staging_bucket}`: ownership by `{service_account}` "
                "could not be verified.",
                file=sys.stderr,
            )

    # 4. Cloud Run v2 worker service (only if its template.serviceAccount matches `service_account`)
    if resolved_region and worker_account_id:
        cr_url = (
            f"https://run.googleapis.com/v2/projects/{project_id}"
            f"/locations/{resolved_region}/services/{worker_account_id}"
        )
        svc = _call("GET", cr_url, project_id=project_id, ignore_errors=True)
        if not svc.get("_http_error"):
            svc_sa = svc.get("template", {}).get("serviceAccount", "")
            if svc_sa == service_account:
                print(f"🧹 Deleting Cloud Run service `{worker_account_id}` ({resolved_region})...")
                del_res = _call("DELETE", cr_url, project_id=project_id, ignore_errors=True)
                if not del_res.get("_http_error"):
                    summary["deleted_cloud_run_service"] = worker_account_id
            else:
                print(
                    f"⛔ Refusing to delete Cloud Run service `{worker_account_id}`: runs as "
                    f"`{svc_sa}`, not `{service_account}`.",
                    file=sys.stderr,
                )

    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Deploy Chagee CCTV Audit BYOC container to Vertex AI ReasoningEngine & bind to Gemini Enterprise."
    )
    parser.add_argument(
        "--project-id",
        default=os.environ.get("GCP_PROJECT", ""),
        required=not bool(os.environ.get("GCP_PROJECT")),
        help="Target GCP Project ID",
    )
    parser.add_argument(
        "--location",
        default=os.environ.get("ENGINE_LOCATION", ""),
        required=not bool(os.environ.get("ENGINE_LOCATION")),
        help="Vertex AI ReasoningEngine region (or set ENGINE_LOCATION)",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_create = sub.add_parser(
        "create",
        help="Create or update ReasoningEngine and optionally bind to Gemini Enterprise (Discovery Engine)",
    )
    p_create.add_argument("--image-uri", required=True, help="Artifact Registry Docker image URI")
    p_create.add_argument(
        "--display-name",
        required=True,
        help="This stack's ReasoningEngine display name (main.tf: <name_prefix>-agent); the engine to update is found by it",
    )
    p_create.add_argument(
        "--legacy-display-name",
        action="append",
        default=[],
        help="Older display name of this stack's engine (repeatable); never another stack's name",
    )
    p_create.add_argument(
        "--gcp-location",
        default=os.environ.get("GCP_LOCATION", ""),
        required=not bool(os.environ.get("GCP_LOCATION")),
        help="The stack's region (GCP_LOCATION inside the container; main.tf var.region)",
    )
    p_create.add_argument(
        "--master-prompt-sheet-id",
        default=os.environ.get("MASTER_PROMPT_SHEET_ID", ""),
        required=False,
        help=(
            "Master SOP Google Sheet ID or URL; empty = Zero-GWS mode "
            "(the engine uses the bundled V25 baseline rules)"
        ),
    )
    p_create.add_argument("--staging-bucket", default=os.environ.get("STAGING_BUCKET", ""))
    p_create.add_argument(
        "--gcs-sop-uri",
        default="",
        help="gs://<workspace_bucket>/sop/master_sheet.xlsx seeded by main.tf (GCS mode SOP source); empty = unset",
    )
    p_create.add_argument("--service-account", default=os.environ.get("ENGINE_SERVICE_ACCOUNT", ""))
    p_create.add_argument("--cloud-run-worker-url", default=os.environ.get("CLOUD_RUN_WORKER_URL", ""))
    p_create.add_argument("--vertex-model-location", default="global")
    p_create.add_argument("--wait-and-bind-ge", action="store_true", default=True)
    _add_ge_arguments(p_create)
    p_create.add_argument(
        "--workspace-impersonate-user",
        default=os.environ.get("WORKSPACE_IMPERSONATE_USER", ""),
        help="Workspace bot user impersonated via keyless domain-wide delegation ('' = runtime SA)",
    )
    p_create.add_argument(
        "--workspace-dwd-service-account",
        default=os.environ.get("WORKSPACE_DWD_SERVICE_ACCOUNT", ""),
        help="Service account authorised for domain-wide delegation (signs the DWD JWT)",
    )
    p_create.add_argument(
        "--google-chat-webhook-url",
        default=os.environ.get("GOOGLE_CHAT_WEBHOOK_URL", ""),
        help="Optional Google Chat incoming webhook URL for job completion notifications",
    )

    p_bind = sub.add_parser("bind-ge", help="Ensure Gemini Enterprise Engine exists and bind ReasoningEngine")
    p_bind.add_argument("--reasoning-engine", required=True, help="Full ReasoningEngine resource name")
    p_bind.add_argument(
        "--service-account",
        default=os.environ.get("ENGINE_SERVICE_ACCOUNT", ""),
        help="The stack's worker SA; a same-named agent is only adopted if its engine runs as this SA",
    )
    _add_ge_arguments(p_bind)

    sub.add_parser("list", help="List ReasoningEngines in the project/location")

    p_poll = sub.add_parser("poll", help="Poll a ReasoningEngine LRO operation")
    p_poll.add_argument("operation_name", help="Full operation name returned by create")

    p_del = sub.add_parser("delete", help="Delete a ReasoningEngine by ID or full resource name")
    p_del.add_argument("engine_id", help="ReasoningEngine numeric ID or full resource name")

    p_destroy = sub.add_parser(
        "destroy-stack",
        help="Tear down this stack's GE agent/app/datastore, ReasoningEngine, staging bucket objects, and Cloud Run service",
    )
    p_destroy.add_argument("--ge-engine-id", required=True, help="This stack's Gemini Enterprise app ID")
    p_destroy.add_argument("--staging-bucket", default="", help="This stack's GCS staging bucket name")
    p_destroy.add_argument(
        "--service-account",
        required=True,
        help="This stack's worker service account email (mandatory ownership guard)",
    )
    p_destroy.add_argument("--gcp-location", default="", help="Optional stack region override")
    p_destroy.add_argument("--image-uri", default="", help="Optional container image URI to infer region")

    p_seed = sub.add_parser(
        "seed-object",
        help="Upload a file to GCS only if the object does not exist yet (never overwrites; e.g. the default SOP)",
    )
    p_seed.add_argument("--bucket", required=True)
    p_seed.add_argument("--name", required=True)
    p_seed.add_argument("--source", required=True)
    p_seed.add_argument("--content-type", default="application/octet-stream")

    args = parser.parse_args(argv)
    host = (
        "aiplatform.googleapis.com"
        if args.location == "global"
        else f"{args.location}-aiplatform.googleapis.com"
    )
    base = f"https://{host}/v1/projects/{args.project_id}/locations/{args.location}/reasoningEngines"

    if args.cmd == "create":
        if args.image_uri.startswith("placeholder-docker.pkg.dev/"):
            raise SystemExit(
                "container_image was not provided (still at the destroy-time placeholder default). "
                "Deploy via cloudbuild.yaml or pass -var=container_image=<digest-pinned image URI>."
            )
        body = build_reasoning_engine_body(
            project_id=args.project_id,
            location=args.location,
            image_uri=args.image_uri,
            master_prompt_sheet_id=args.master_prompt_sheet_id,
            staging_bucket=args.staging_bucket,
            service_account=args.service_account,
            cloud_run_worker_url=args.cloud_run_worker_url,
            vertex_model_location=args.vertex_model_location,
            display_name=args.display_name,
            gcp_location=args.gcp_location,
            workspace_impersonate_user=args.workspace_impersonate_user,
            google_chat_webhook_url=args.google_chat_webhook_url,
            workspace_dwd_service_account=args.workspace_dwd_service_account,
            company_name=args.ge_company_name,
            tenant_label=args.ge_tenant_label,
            gcs_sop_uri=args.gcs_sop_uri,
        )
        # This stack's engine (by display name, owned by its service account) is PATCHed in place.
        existing_list = _call("GET", base, project_id=args.project_id).get("reasoningEngines", [])
        matching_engines = find_stack_reasoning_engines(
            existing_list,
            [args.display_name, *args.legacy_display_name],
            args.service_account,
        )
        target_re = matching_engines[0]["name"] if matching_engines else None
        stale_duplicates = [eng["name"] for eng in matching_engines[1:]]

        if target_re:
            patch_url = f"https://{host}/v1/{target_re}?updateMask=displayName,description,spec"
            print(f"PATCH {patch_url}")
            op = _call("PATCH", patch_url, body, project_id=args.project_id)
        else:
            print(f"POST {base}")
            op = _call("POST", base, body, project_id=args.project_id)

        print(json.dumps(op, indent=2))
        if args.wait_and_bind_ge and op.get("name"):
            re_name = wait_for_reasoning_engine_op(host, op["name"], args.project_id)
            _bind_from_args(args, re_name)
            for stale_re in stale_duplicates:
                print(f"🧹 Removing duplicate ReasoningEngine {stale_re}...")
                _call("DELETE", f"https://{host}/v1/{stale_re}", project_id=args.project_id, ignore_errors=True)
    elif args.cmd == "bind-ge":
        _bind_from_args(args, args.reasoning_engine)
    elif args.cmd == "list":
        res = _call("GET", base, project_id=args.project_id)
        for eng in res.get("reasoningEngines", []):
            print(f"{eng['name']}  ({eng.get('displayName')})")
    elif args.cmd == "poll":
        print(json.dumps(_call("GET", f"https://{host}/v1/{args.operation_name}", project_id=args.project_id), indent=2))
    elif args.cmd == "delete":
        target = (
            args.engine_id
            if "/" in args.engine_id
            else f"projects/{args.project_id}/locations/{args.location}/reasoningEngines/{args.engine_id}"
        )
        print(json.dumps(_call("DELETE", f"https://{host}/v1/{target}", project_id=args.project_id), indent=2))
    elif args.cmd == "destroy-stack":
        res = destroy_stack(
            project_id=args.project_id,
            location=args.location,
            service_account=args.service_account,
            ge_engine_id=args.ge_engine_id,
            staging_bucket=args.staging_bucket,
            gcp_location=args.gcp_location,
            image_uri=args.image_uri,
        )
        print(json.dumps(res, indent=2))
    elif args.cmd == "seed-object":
        seed_gcs_object(args.bucket, args.name, args.source, args.content_type)
    return 0


if __name__ == "__main__":
    sys.exit(main())

