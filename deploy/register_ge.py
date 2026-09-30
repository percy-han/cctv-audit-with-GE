"""Register the Chagee CCTV Audit Agent into Gemini Enterprise (Discovery Engine).

Supports both physical invocation channels in Google Cloud Discovery Engine:
1. Channel A (`create-a2a`): Direct Cloud Run (`https://xxx.run.app`) invocation via
   `a2aAgentDefinition` (Agent-to-Agent JSON-RPC 2.0 `POST /a2a` + `/.well-known/agent-card.json`).
2. Channel B (`create-adk`): Vertex AI Agent Engine (`ReasoningEngine` BYOC container)
   invocation via `adkAgentDefinition` (`POST /api/stream_reasoning_engine` + `GET /is_busy`).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any
from urllib.parse import urlencode

import google.auth
import google.auth.transport.requests
import requests

# Authorization resources live per project (not per app), so the default is derived from the app ID
# to keep two stacks in one project apart; GE_AUTH_ID / --auth-id override it.
DEFAULT_AUTH_ID = os.environ.get("GE_AUTH_ID", "")

# Defaults for --agent-display-name / --agent-description (or GE_AGENT_DISPLAY_NAME /
# GE_AGENT_DESCRIPTION). Give each stack's agent its own name when several share a project.
AGENT_DISPLAY_NAME = os.environ.get("GE_AGENT_DISPLAY_NAME", "门店视频稽核")
AGENT_DESCRIPTION = os.environ.get(
    "GE_AGENT_DESCRIPTION",
    "自动读取并切片 Google Drive 门店监控视频，依据动态 Google Sheet SOP 清单执行 Gemini 3.1 Pro "
    "多模态合规稽核、截取违规证据片段并生成复核报表。",
)


def default_auth_id(app_id: str) -> str:
    return DEFAULT_AUTH_ID or f"{app_id}-drive-auth"
OAUTH_SCOPES = (
    "https://www.googleapis.com/auth/drive "
    "https://www.googleapis.com/auth/spreadsheets"
)


def _require_ge_urls(project_id: str, location: str, app_id: str) -> tuple[str, str]:
    if not project_id or not app_id:
        raise ValueError(
            "Both GCP_PROJECT (--project-id) and GE_APP_ID (--app-id) must be explicitly specified "
            "to prevent registering customer agents into an unintended tenant."
        )
    base_url = (
        f"https://discoveryengine.googleapis.com/v1alpha/"
        f"projects/{project_id}/locations/{location}"
    )
    agents_url = (
        f"{base_url}/collections/default_collection/"
        f"engines/{app_id}/assistants/default_assistant/agents"
    )
    auths_url = f"{base_url}/authorizations"
    return agents_url, auths_url


def _get_headers(project_id: str) -> dict[str, str]:
    credentials, _ = google.auth.default()
    auth_req = google.auth.transport.requests.Request()
    credentials.refresh(auth_req)
    return {
        "Authorization": f"Bearer {credentials.token}",
        "Content-Type": "application/json",
        "X-Goog-User-Project": project_id,
    }


def _fetch_or_build_agent_card(cloud_run_url: str) -> dict[str, Any]:
    """Fetch live Agent Card from `{cloud_run_url}/.well-known/agent-card.json` (SSOT), falling back to local builder."""
    base = cloud_run_url.rstrip("/")
    card_url = f"{base}/.well-known/agent-card.json"
    try:
        resp = requests.get(card_url, timeout=10)
        if resp.status_code == 200:
            card = resp.json()
            if isinstance(card, dict) and card.get("url"):
                return card
    except requests.RequestException:
        pass
    from cctv_audit.server import build_a2a_agent_card

    return build_a2a_agent_card(base)


def list_agents(project_id: str, location: str, app_id: str) -> list[dict[str, Any]]:
    agents_url, _ = _require_ge_urls(project_id, location, app_id)
    resp = requests.get(agents_URL := agents_url, headers=_get_headers(project_id), timeout=30)
    resp.raise_for_status()
    agents = resp.json().get("agents", [])
    print(f"Found {len(agents)} registered GE agent(s) at {agents_URL}:")
    for ag in agents:
        mode = "A2A (Cloud Run)" if "a2aAgentDefinition" in ag else "ADK (ReasoningEngine)"
        print(f"  - {ag.get('displayName')!r} [{mode}]: {ag.get('name')}")
    return agents


def create_auth(
    project_id: str,
    location: str,
    app_id: str,
    client_id: str,
    client_secret: str,
    auth_id: str = "",
) -> dict[str, Any]:
    _, auths_url = _require_ge_urls(project_id, location, app_id)
    auth_id = auth_id or default_auth_id(app_id)
    auth_uri = "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(
        {
            "client_id": client_id,
            "redirect_uri": "https://vertexaisearch.cloud.google.com/static/oauth/oauth.html",
            "scope": OAUTH_SCOPES,
            "include_granted_scopes": "true",
            "response_type": "code",
            "access_type": "offline",
            "prompt": "consent",
        }
    )
    payload = {
        "serverSideOauth2": {
            "clientId": client_id,
            "clientSecret": client_secret,
            "authorizationUri": auth_uri,
            "tokenUri": "https://oauth2.googleapis.com/token",
        }
    }
    resp = requests.post(
        auths_url,
        headers=_get_headers(project_id),
        params={"authorizationId": auth_id},
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()
    result = resp.json()
    print(f"Created Authorization resource: {result.get('name')}")
    return result


def create_a2a_agent(
    project_id: str,
    location: str,
    app_id: str,
    cloud_run_url: str,
    auth_id: str | None = None,
    display_name: str = AGENT_DISPLAY_NAME,
    description: str = AGENT_DESCRIPTION,
) -> dict[str, Any]:
    """Register a standalone Cloud Run service into Gemini Enterprise via a2aAgentDefinition."""
    agents_url, auths_url = _require_ge_urls(project_id, location, app_id)
    card = _fetch_or_build_agent_card(cloud_run_url)
    body: dict[str, Any] = {
        "displayName": display_name,
        "description": description,
        "a2aAgentDefinition": {
            "jsonAgentCard": json.dumps(card, ensure_ascii=False),
        },
    }
    if auth_id:
        body["authorizationConfig"] = {
            "toolAuthorizations": [f"{auths_url.split('/v1alpha/')[1]}/{auth_id}"]
        }
    resp = requests.post(agents_url, headers=_get_headers(project_id), json=body, timeout=30)
    resp.raise_for_status()
    result = resp.json()
    print(f"Registered Cloud Run A2A Agent into Gemini Enterprise: {result.get('name')}")
    return result


def create_adk_agent(
    project_id: str,
    location: str,
    app_id: str,
    reasoning_engine_resource: str,
    auth_id: str | None = None,
    display_name: str = AGENT_DISPLAY_NAME,
    description: str = AGENT_DESCRIPTION,
) -> dict[str, Any]:
    """Register a Vertex AI ReasoningEngine BYOC container into Gemini Enterprise via adkAgentDefinition."""
    agents_url, auths_url = _require_ge_urls(project_id, location, app_id)
    body: dict[str, Any] = {
        "displayName": display_name,
        "description": description,
        "adkAgentDefinition": {
            "toolSettings": {"toolDescription": description},
            "provisionedReasoningEngine": {
                "reasoningEngine": reasoning_engine_resource,
            },
        },
    }
    if auth_id:
        body["authorizationConfig"] = {
            "toolAuthorizations": [f"{auths_url.split('/v1alpha/')[1]}/{auth_id}"]
        }
    resp = requests.post(agents_url, headers=_get_headers(project_id), json=body, timeout=30)
    resp.raise_for_status()
    result = resp.json()
    print(f"Registered Vertex AI ReasoningEngine ADK Agent into Gemini Enterprise: {result.get('name')}")
    return result


def delete_agent(project_id: str, location: str, app_id: str, agent_id: str) -> None:
    agents_url, _ = _require_ge_urls(project_id, location, app_id)
    url = agent_id if agent_id.startswith("http") else f"{agents_url}/{agent_id}"
    resp = requests.delete(url, headers=_get_headers(project_id), timeout=30)
    resp.raise_for_status()
    print(f"Deleted GE agent: {agent_id}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Register Chagee CCTV Audit Agent into Gemini Enterprise (Cloud Run A2A or ReasoningEngine ADK)."
    )
    parser.add_argument(
        "--project-id",
        default=os.environ.get("GCP_PROJECT", ""),
        help="Target GCP Project ID hosting the Gemini Enterprise App (or set GCP_PROJECT)",
    )
    parser.add_argument(
        "--location",
        default=os.environ.get("GE_LOCATION", "global"),
        help="Discovery Engine location (default: global)",
    )
    parser.add_argument(
        "--app-id",
        default=os.environ.get("GE_APP_ID", ""),
        help="Gemini Enterprise Engine / App ID (or set GE_APP_ID)",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="List agents registered in the Gemini Enterprise Assistant")

    p_auth = sub.add_parser("create-auth", help="Create Google Workspace 3LO OAuth2 Authorization")
    p_auth.add_argument("--client-id", required=True, help="OAuth 2.0 Web Client ID")
    p_auth.add_argument("--client-secret", required=True, help="OAuth 2.0 Web Client Secret")
    p_auth.add_argument("--auth-id", default="", help="Authorization ID (default: GE_AUTH_ID or <app-id>-drive-auth)")

    p_a2a = sub.add_parser(
        "create-a2a",
        help="Register standalone Cloud Run URL (https://xxx.run.app) via a2aAgentDefinition",
    )
    p_a2a.add_argument("cloud_run_url", help="Base HTTPS URL of the Cloud Run service")
    p_a2a.add_argument("--auth-id", default=None, help="Optional Workspace OAuth Authorization ID")
    p_a2a.add_argument("--agent-display-name", default=AGENT_DISPLAY_NAME)
    p_a2a.add_argument("--agent-description", default=AGENT_DESCRIPTION)

    p_adk = sub.add_parser(
        "create-adk",
        help="Register Vertex AI ReasoningEngine resource via adkAgentDefinition",
    )
    p_adk.add_argument(
        "reasoning_engine",
        help="Full resource name: projects/{PROJECT}/locations/{REGION}/reasoningEngines/{ID}",
    )
    p_adk.add_argument("--auth-id", default=None, help="Optional Workspace OAuth Authorization ID")
    p_adk.add_argument("--agent-display-name", default=AGENT_DISPLAY_NAME)
    p_adk.add_argument("--agent-description", default=AGENT_DESCRIPTION)

    p_del = sub.add_parser("delete", help="Delete a registered GE agent by ID")
    p_del.add_argument("agent_id", help="Agent numeric ID or full resource URL")

    args = parser.parse_args(argv)
    if args.cmd == "list":
        list_agents(args.project_id, args.location, args.app_id)
    elif args.cmd == "create-auth":
        create_auth(
            args.project_id,
            args.location,
            args.app_id,
            args.client_id,
            args.client_secret,
            args.auth_id,
        )
    elif args.cmd == "create-a2a":
        create_a2a_agent(
            args.project_id,
            args.location,
            args.app_id,
            args.cloud_run_url,
            args.auth_id,
            args.agent_display_name,
            args.agent_description,
        )
    elif args.cmd == "create-adk":
        create_adk_agent(
            args.project_id,
            args.location,
            args.app_id,
            args.reasoning_engine,
            args.auth_id,
            args.agent_display_name,
            args.agent_description,
        )
    elif args.cmd == "delete":
        delete_agent(args.project_id, args.location, args.app_id, args.agent_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
