"""FastAPI Server & Dual-Mode Gemini Enterprise Wire Adapter (`server.py`).

Supports BOTH official Google Cloud Gemini Enterprise (`discoveryengine.googleapis.com`)
integration channels out-of-the-box:
1. Channel A (`adkAgentDefinition` via Vertex AI ReasoningEngine BYOC):
   - Gemini Enterprise calls `streaming_agent_run_with_events` on `POST /api/stream_reasoning_engine`.
   - Every streamed line is wrapped in `{"events": [<ADK Event>], "session_id": ...}`.
   - `GET /is_busy` implements the `keepAliveProbe` hold (`KEEPALIVE_HOLD_SECONDS`, default 25s)
     so detached background video audits keep full CPU allocation after the turn response returns.
2. Channel B (`a2aAgentDefinition` via standalone Cloud Run `*.run.app`):
   - `GET /.well-known/agent-card.json` (and `/a2a/app/.well-known/agent-card.json`) serves the
     A2A Agent Card required by Gemini Enterprise A2A registration.
   - `POST /a2a` handles A2A JSON-RPC 2.0 requests routed from Gemini Enterprise using the
     `service-<PROJECT_NUMBER>@gcp-sa-discoveryengine.iam.gserviceaccount.com` service agent
     (`roles/run.invoker`).
     Only `SUPPORTED_A2A_METHODS` are executed; every other `method` is answered with a
     JSON-RPC `-32601 Method not found` instead of being silently coerced into a turn.
     In particular the Agent Card advertises `capabilities.streaming = false`, so
     `message/stream` is deliberately *not* served here — claiming it and then answering with a
     single non-streaming envelope would be a protocol lie.

Tenant identity (`REQ-008`) precedence, strongest evidence first:
   1. `X-Goog-Authenticated-User-Email` — injected by the Cloud Run IAM / IAP front end from the
      verified ID token and un-forgeable by the caller.
   2. Caller-supplied JSON-RPC `params.metadata` (Gemini Enterprise relays the supervisor here
      when it invokes with its own service-agent credentials).
   3. `X-Goog-Authenticated-User-Id` — opaque but stable; better than a shared bucket.
   4. `DEFAULT_TENANT_FALLBACK` — logged at WARNING because it merges callers into one bucket.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from typing import Any, Dict, List, Optional

try:
    from fastapi import FastAPI, Request
    from fastapi.responses import StreamingResponse
except ImportError:  # Graceful fallback when running unit tests without fastapi installed

    class Request:  # type: ignore[no-redef]
        base_url: str = "http://localhost:8080/"

        async def json(self) -> Dict[str, Any]:
            return {}

    class StreamingResponse:  # type: ignore[no-redef]
        def __init__(self, content: Any, media_type: str = "application/x-ndjson") -> None:
            self.body_iterator = content
            self.media_type = media_type

    class FastAPI:  # type: ignore[no-redef]
        def __init__(self, title: str = "") -> None:
            self.title = title

        def get(self, _path: str) -> Any:
            return lambda fn: fn

        def post(self, _path: str) -> Any:
            return lambda fn: fn


from pydantic import BaseModel, Field

from .audit_service import AuditService
from .turn import TurnAction, classify_turn_with_llm
from .video_ingestor import VideoIngestor, VideoMetadataItem, progress_unit_label

logger = logging.getLogger("cctv_audit.server")

app = FastAPI(title="Chagee CCTV AI Audit Worker (Zero-DB Workspace + Serverless)")

from .gcp import GoogleWorkspaceGateway
from .workspace_reporter import WorkspaceReporter
_gw = GoogleWorkspaceGateway()
audit_service = AuditService(
    ingestor=VideoIngestor(drive_reader=_gw),
    reporter=WorkspaceReporter(gateway=_gw),
)

AUTHOR = "chagee-cctv-audit"

# --- Tenant identity (REQ-008) -------------------------------------------------------------
# Cloud Run IAM and IAP strip any client-supplied `X-Goog-Authenticated-User-*` request headers
# and re-inject them from the verified ID token, so these two headers cannot be forged by the
# caller. The JSON-RPC body can be. Header therefore outranks body.
AUTHENTICATED_USER_EMAIL_HEADER = "x-goog-authenticated-user-email"
AUTHENTICATED_USER_ID_HEADER = "x-goog-authenticated-user-id"
GOOGLE_IDENTITY_PREFIX = "accounts.google.com:"
SERVICE_PRINCIPAL_SUFFIX = "gserviceaccount.com"
DEFAULT_TENANT_FALLBACK = "auditor@chagee.com"

# --- A2A JSON-RPC 2.0 ----------------------------------------------------------------------
# `message/stream` is intentionally absent: `build_a2a_agent_card` declares
# `capabilities.streaming = False`, and this endpoint answers with a single non-streaming
# object. Accepting `message/stream` would advertise one contract and honour another.
SUPPORTED_A2A_METHODS = frozenset({"message/send", "tasks/send"})
JSONRPC_PARSE_ERROR = -32700
JSONRPC_INVALID_REQUEST = -32600
JSONRPC_METHOD_NOT_FOUND = -32601
JSONRPC_INTERNAL_ERROR = -32603


def _header_value(request: Any, name: str) -> str:
    """Case-insensitively reads one header, tolerating the fastapi-less test shim."""
    headers = getattr(request, "headers", None)
    if headers is None:
        return ""
    value = None
    getter = getattr(headers, "get", None)
    if callable(getter):
        # Starlette `Headers` is already case-insensitive; a plain dict is not.
        value = getter(name) or getter(name.title())
    if value is None and hasattr(headers, "items"):
        for key, candidate in headers.items():
            if str(key).lower() == name:
                value = candidate
                break
    return str(value).strip() if value else ""


def extract_proxy_asserted_user(request: Any) -> tuple[str, str]:
    """Returns `(email, opaque_id)` asserted by the Cloud Run IAM / IAP front end.

    Gemini Enterprise invokes `POST /a2a` with the Discovery Engine *service agent*
    (`service-<PROJECT_NUMBER>@gcp-sa-discoveryengine.iam.gserviceaccount.com`). That is an
    infrastructure principal, not a supervisor — adopting it as the tenant key would collapse
    every supervisor into a single bucket and silently destroy the `REQ-008` isolation that
    `UserScopedJobStore` exists to enforce. Service principals are therefore discarded here so
    the caller-relayed `params.metadata` identity stays in effect for that topology.
    """
    resolved: List[str] = []
    for header in (AUTHENTICATED_USER_EMAIL_HEADER, AUTHENTICATED_USER_ID_HEADER):
        value = _header_value(request, header)
        if value.startswith(GOOGLE_IDENTITY_PREFIX):
            value = value[len(GOOGLE_IDENTITY_PREFIX) :].strip()
        if value.endswith(SERVICE_PRINCIPAL_SUFFIX):
            logger.debug("Ignoring service-agent principal in %s for tenant keying", header)
            value = ""
        resolved.append(value)
    return resolved[0], resolved[1]


def resolve_tenant_identity(
    *,
    proxy_email: str = "",
    body_identity: str = "",
    proxy_user_id: str = "",
    channel: str = "a2a",
) -> str:
    """Picks the tenant key by strength of evidence; never returns an empty string."""
    if proxy_email:
        if body_identity and body_identity.lower() != proxy_email.lower():
            # Not fatal, but it means the body is asserting somebody else. Record it.
            logger.warning(
                "[%s] body identity %r overridden by proxy-asserted %r",
                channel,
                body_identity,
                proxy_email,
            )
        return proxy_email
    if body_identity:
        return body_identity
    if proxy_user_id:
        return proxy_user_id
    logger.warning(
        "[%s] no verifiable caller identity; falling back to the shared %r tenant bucket",
        channel,
        DEFAULT_TENANT_FALLBACK,
    )
    return DEFAULT_TENANT_FALLBACK



STREAM_HEARTBEAT_DELAY_SEC: float = 2.5
STREAM_HEARTBEAT_INTERVAL_SEC: float = 2.5


def build_adk_event_envelope(
    text: str,
    invocation_id: str,
    session_id: str = "",
) -> Dict[str, Any]:
    """Formats a response into the exact ADK Event envelope expected by Gemini Enterprise."""
    event: Dict[str, Any] = {
        "content": {"parts": [{"text": text}], "role": "model"},
        "partial": False,
        "turn_complete": True,
        "invocation_id": invocation_id,
        "author": AUTHOR,
        "actions": {
            "state_delta": {},
            "artifact_delta": {},
            "requested_auth_configs": {},
            "requested_tool_confirmations": {},
        },
        "id": uuid.uuid4().hex[:8],
        "timestamp": time.time(),
    }
    envelope: Dict[str, Any] = {"events": [event]}
    if session_id:
        envelope["session_id"] = session_id
    return envelope


def build_a2a_agent_card(base_url: str) -> Dict[str, Any]:
    """Builds the A2A Agent Card (`/.well-known/agent-card.json`) for Gemini Enterprise Cloud Run registration."""
    normalized_base = base_url.rstrip("/")
    return {
        "name": "门店视频稽核",
        "description": (
            "对霸王茶姬 (CHAGEE) 门店监控录像做 APAC 合规稽核。发送 Google Drive 门店监控文件夹链接，"
            "先执行分辨率与时长预检；回复「确认稽核」后在后台拉取 SOP Sheet 动态规则、逐窗执行多模态稽核、"
            "截取 [T-2s, T+20s] 22秒证据视频切片并在原文件夹内生成《AI稽核报告与Token账单》。"
        ),
        "url": f"{normalized_base}/a2a",
        "version": "1.0.0",
        "protocolVersion": "0.2.1",
        "capabilities": {
            "streaming": False,
            "pushNotifications": False,
            "stateTransitionHistory": False,
        },
        "defaultInputModes": ["text", "text/plain"],
        "defaultOutputModes": ["text", "text/plain"],
        "skills": [
            {
                "id": "chagee-store-cctv-audit",
                "name": "门店视频稽核",
                "description": "输入 Google Drive 门店监控视频文件夹链接，执行 SOP 合规预检、启动后台多模态稽核或查询任务进度。",
                "tags": ["cctv", "chagee", "sop-audit", "video-understanding"],
                "examples": [
                    "稽核这个门店监控文件夹：https://drive.google.com/drive/folders/1--hbVk8xE5i-aZrph7Pgy4vZwZdp9-NJ",
                    "确认，开始稽核",
                    "刚才那单现在怎么样了？",
                ],
            }
        ],
    }


async def _handle_conversation_turn(
    user_id: str,
    session_id: str,
    message_text: str,
) -> str:
    """Unified turn handler shared by both `POST /api/stream_reasoning_engine` (ADK) and `POST /a2a` (A2A).

    `AuditService.start_audit` raises `ValueError` carrying *supervisor-facing* Chinese guidance
    (`audit_service.py:L108`, `L112`). Letting it propagate turns that guidance into an HTTP 500
    on the A2A channel, and — worse — into a truncated NDJSON body on the ADK channel, because
    the exception fires after the stream has already begun. It is a reply, not a fault.
    """
    decision = await classify_turn_with_llm(message_text)
    if decision.action == TurnAction.INSPECT:
        job = await audit_service.preflight(
            user_id=user_id,
            drive_url=decision.drive_url,
            session_id=session_id,
        )
        return (
            job.preflight_report.message_to_user
            if job.preflight_report
            else f"预检单号 `{job.job_id}` 状态：{job.state.value}"
        )
    if decision.action == TurnAction.CONFIRM:
        try:
            job = await audit_service.start_audit(
                user_id=user_id,
                job_id=decision.job_id or None,
                session_id=session_id,
            )
        except ValueError as exc:
            logger.info("start_audit rejected turn for %s: %s", user_id, exc)
            return f"⚠️ {exc}"
        resume_banner = (
            f"• **断点续跑恢复**：已从 GCS 恢复前序 `{len(job.completed_segments)}` 个已完成切片（零重复 Token 消耗），从第 `{len(job.completed_segments) + 1}` 段继续执行\n"
            if len(job.completed_segments) > 0
            else ""
        )
        return (
            f"🚀 **后台 AI 稽核已正式启动（单号 `{job.job_id}`）**\n"
            f"{resume_banner}"
            f"• **生效模型版本**：`{job.active_model_version}`\n"
            f"• **生效提示词版本**：`{job.active_prompt_version}`\n"
            f"• 稽核完成后将在您的原 Google Drive 文件夹内自动生成 `📁 违规证据切片_Evidence/` 与《门店稽核报告与 Token 账单 Sheet》；期间可随时在本对话中询问「进度怎么样了」查看实时进度。"
        )
    if decision.action == TurnAction.STATUS:
        job = await audit_service.get_status(user_id, decision.job_id or None)
        if job is None:
            return "您名下暂无正在运行或已完成的稽核任务。"
        total_segs = (
            job.preflight_report.planned_segments_count
            if job.preflight_report is not None
            else 0
        )
        unit = progress_unit_label(
            job.preflight_report.videos if job.preflight_report is not None else None
        )
        done_segs = len(job.completed_segments)
        if job.state.value == "done":
            return (
                f"✅ **任务 `{job.job_id}` 已完成！**\n"
                f"• 已完成：`{done_segs}/{total_segs}` {unit}\n"
                f"• 检出违规事件：`{job.violations_found}` 项\n"
                f"• 累计消耗 Token：`{job.total_tokens_used:,}`\n"
                f"• 专属报告 Sheet：{job.report_sheet_url}"
            )
        if job.state.value == "failed":
            if job.needs_operator_fix:
                return (
                    f"⚠️ **任务 `{job.job_id}` 暂停：Google Drive 权限或配置需要处理（已完成 `{done_segs}/{total_segs}` {unit}）**\n"
                    f"• 需要处理：{job.error_message or '未知配置问题'}\n"
                    "• 处理好之后回复「**确认开始**」即可继续，已完成的部分不会重复消耗 Token。"
                )
            return (
                f"⚠️ **任务 `{job.job_id}` 执行遇到异常（已完成 `{done_segs}/{total_segs}` {unit}）**\n"
                f"• 异常详情：`{job.error_message or '未知错误'}`\n"
                f"• 已完成的 `{done_segs}` {unit}结果已安全保存在 GCS，您可以直接回复「**确认开始**」从断点处零重复 Token 续跑。"
            )
        return (
            f"⏳ **任务 `{job.job_id}` 当前状态：`{job.state.value}`**\n"
            f"• **稽核进度**：已完成 `{done_segs}/{total_segs}` {unit}（已消耗 `{job.total_tokens_used:,}` Tokens）\n"
            f"• **自动续跑次数**：`{job.resume_count}` 次"
        )
    return decision.reply_summary or "请粘贴您的 Google Drive 监控视频文件夹链接以启动预检。"


class InspectRequestPayload(BaseModel):
    user_email: str = Field(description="Caller's Google Workspace email")
    drive_url: str = Field(description="Google Drive folder URL or ID")
    session_id: str = Field(default="")
    preloaded_items: Optional[List[VideoMetadataItem]] = Field(default=None)


class ExecuteRequestPayload(BaseModel):
    user_email: str = Field(description="Caller's Google Workspace email")
    job_id: Optional[str] = Field(default=None)
    session_id: str = Field(default="")
    wait_for_completion: bool = Field(default=False)


class StreamReasoningRequest(BaseModel):
    class_method: str = Field(default="streaming_agent_run_with_events")
    input: Dict[str, Any] = Field(default_factory=dict)


@app.get("/healthz")
async def healthz() -> Dict[str, str]:
    return {"status": "ok", "service": AUTHOR}


@app.get("/is_busy")
async def is_busy() -> Dict[str, Any]:
    """Keep-alive probe endpoint (`keepAliveProbe`) for detached background audits.

    When a detached video audit is running in the background (`audit_service.has_active_work()`),
    holds the HTTP response open for up to `KEEPALIVE_HOLD_SECONDS` (default 25s) so Cloud Run /
    Agent Engine keeps a request in flight and allocates unthrottled CPU to the container.
    """
    raw_hold = os.environ.get("KEEPALIVE_HOLD_SECONDS", "25")
    try:
        hold_seconds = float(raw_hold)
    except (TypeError, ValueError):
        # A typo in one env var must not take down the probe Agent Engine uses to decide
        # whether this container is alive; degrade to the documented default and say so.
        logger.warning("Invalid KEEPALIVE_HOLD_SECONDS=%r; defaulting to 25s", raw_hold)
        hold_seconds = 25.0
    if audit_service.has_active_work() and hold_seconds > 0:
        deadline = time.monotonic() + min(hold_seconds, 55.0)
        while audit_service.has_active_work() and time.monotonic() < deadline:
            await asyncio.sleep(0.5)
    active = audit_service.active_jobs_count()
    from .cpuprobe import cpu_quota, runqueue_wait_seconds

    return {
        "busy": active > 0,
        "active_jobs": active,
        "cpu_quota_cores": cpu_quota(),
        "runqueue_wait_seconds": runqueue_wait_seconds(),
    }


@app.get("/.well-known/agent-card.json")
@app.get("/a2a/app/.well-known/agent-card.json")
async def get_agent_card(request: Request) -> Dict[str, Any]:
    """Serves the A2A Agent Card so Gemini Enterprise can register and call this Cloud Run service."""
    configured_url = os.environ.get("CLOUD_RUN_SERVICE_URL", "").strip()
    base_url = configured_url if configured_url else str(request.base_url).rstrip("/")
    return build_a2a_agent_card(base_url)


def build_jsonrpc_error(rpc_id: Any, code: int, message: str) -> Dict[str, Any]:
    """Builds a JSON-RPC 2.0 error response (`id` is `None` when it could not be recovered)."""
    return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": message}}


@app.post("/a2a")
async def a2a_jsonrpc_endpoint(request: Request) -> Dict[str, Any]:
    """Handles Gemini Enterprise A2A JSON-RPC 2.0 (`message/send` / `tasks/send`) requests on Cloud Run.

    Hardening notes (see module docstring):
    * `method` is validated against `SUPPORTED_A2A_METHODS`. Previously *every* method — including
      `tasks/cancel` and `tasks/pushNotificationConfig/set` — fell through into a live turn, so a
      cancel request could start a billable audit. Unknown methods now get `-32601`.
    * A malformed or non-object body yields `-32700` / `-32600` rather than an HTTP 500, which the
      A2A client cannot interpret.
    * The tenant key prefers the Cloud Run IAM / IAP asserted header over `params.metadata`, which
      is caller-controlled and is the sole key guarding `UserScopedJobStore` (`REQ-008`).
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - any decode failure is a JSON-RPC parse error
        logger.warning("Rejected /a2a request with an undecodable JSON body")
        return build_jsonrpc_error(None, JSONRPC_PARSE_ERROR, "Parse error: body is not valid JSON")

    if not isinstance(body, dict):
        # JSON-RPC batch (a list) is not supported by the A2A transport either.
        return build_jsonrpc_error(
            None, JSONRPC_INVALID_REQUEST, "Invalid Request: expected a single JSON-RPC object"
        )

    rpc_id = body.get("id")
    method = str(body.get("method") or "").strip()
    if method not in SUPPORTED_A2A_METHODS:
        logger.warning("Rejected unsupported A2A method %r", method)
        return build_jsonrpc_error(
            rpc_id,
            JSONRPC_METHOD_NOT_FOUND,
            f"Method not found: {method or '<missing>'}. "
            f"Supported: {', '.join(sorted(SUPPORTED_A2A_METHODS))}.",
        )

    params = body.get("params") if isinstance(body.get("params"), dict) else {}
    message_obj = params.get("message") if isinstance(params.get("message"), dict) else {}

    parts = message_obj.get("parts") if isinstance(message_obj.get("parts"), list) else []
    text_fragments: List[str] = []
    for part in parts:
        if isinstance(part, dict) and isinstance(part.get("text"), str):
            text_fragments.append(part["text"])
    message_text = "\n".join(text_fragments).strip() or str(params.get("query") or "")

    metadata = params.get("metadata") if isinstance(params.get("metadata"), dict) else {}
    proxy_email, proxy_user_id = extract_proxy_asserted_user(request)
    user_id = resolve_tenant_identity(
        proxy_email=proxy_email,
        body_identity=str(
            metadata.get("user_id")
            or metadata.get("user_email")
            or message_obj.get("user_id")
            or ""
        ).strip(),
        proxy_user_id=proxy_user_id,
        channel="a2a",
    )
    session_id = str(
        params.get("sessionId")
        or message_obj.get("contextId")
        or metadata.get("session_id")
        or uuid.uuid4().hex[:8]
    )
    task_id = str(params.get("id") or message_obj.get("taskId") or uuid.uuid4().hex[:8])

    try:
        reply_text = await _handle_conversation_turn(
            user_id=user_id,
            session_id=session_id,
            message_text=message_text,
        )
    except Exception:  # noqa: BLE001 - an unexpected fault must stay a JSON-RPC error, not a 500
        logger.exception("Unhandled failure while serving A2A task %s", task_id)
        return build_jsonrpc_error(
            rpc_id, JSONRPC_INTERNAL_ERROR, "Internal error while handling the audit turn"
        )

    return {
        "jsonrpc": "2.0",
        "id": rpc_id,
        "result": {
            "id": task_id,
            "contextId": session_id,
            "status": {
                "state": "completed",
                "message": {
                    "role": "agent",
                    "messageId": uuid.uuid4().hex[:12],
                    "taskId": task_id,
                    "contextId": session_id,
                    "parts": [{"kind": "text", "text": reply_text}],
                },
            },
        },
    }


@app.post("/inspect")
async def http_inspect(payload: InspectRequestPayload) -> Dict[str, Any]:
    job = await audit_service.preflight(
        user_id=payload.user_email,
        drive_url=payload.drive_url,
        session_id=payload.session_id,
        preloaded_items=payload.preloaded_items,
    )
    return job.model_dump()


@app.post("/execute")
async def http_execute(payload: ExecuteRequestPayload) -> Dict[str, Any]:
    job = await audit_service.start_audit(
        user_id=payload.user_email,
        job_id=payload.job_id,
        session_id=payload.session_id,
        wait_for_completion=payload.wait_for_completion,
    )
    return job.model_dump()


_HISTORICAL_URL_RE = re.compile(r"https?://\S+")


def _extract_adk_turn_text(inner: Dict[str, Any]) -> str:
    """Unpacks the conversation text from GE's `request_json` (`message.parts[].text` + compact `events[]` history).

    Keeps at most the last 4 historical events, truncates each historical event to its first
    non-empty line (`<= 160` chars), and masks historical URLs (`<历史链接>`) so:
    1. Multi-kilobyte assistant markdown tables do not bloat the router prompt.
    2. Old Drive URLs from previous turns in `events[]` cannot re-trigger `TurnAction.INSPECT`
       or bypass `verify_url_verbatim` when the current user message has no URL (e.g. "确认开始").
    """
    raw_msg = inner.get("message")
    current_text = ""
    if isinstance(raw_msg, dict):
        parts = raw_msg.get("parts")
        if isinstance(parts, list):
            current_text = " ".join(
                str(p.get("text") or "") for p in parts if isinstance(p, dict) and p.get("text")
            ).strip()
    elif isinstance(raw_msg, str):
        current_text = raw_msg.strip()
    if not current_text:
        current_text = str(inner.get("query") or "").strip()

    events = inner.get("events")
    if isinstance(events, list) and events:
        history_lines: List[str] = []
        for ev in events[-4:]:
            if not isinstance(ev, dict):
                continue
            content = ev.get("content")
            if isinstance(content, dict) and isinstance(content.get("parts"), list):
                ev_text = " ".join(
                    str(p.get("text") or "")
                    for p in content["parts"]
                    if isinstance(p, dict) and p.get("text")
                ).strip()
                if ev_text:
                    first_line = next(
                        (ln.strip() for ln in ev_text.splitlines() if ln.strip()), ""
                    )
                    compact = _HISTORICAL_URL_RE.sub("<历史链接>", first_line)[:160]
                    role = "user" if ev.get("author") == "user" else "assistant"
                    history_lines.append(f"[{role}] {compact}")
        if history_lines and current_text:
            return "\n".join(history_lines + [f"[user] {current_text}"])
    return current_text


async def _parse_reasoning_engine_request(request: Any) -> StreamReasoningRequest:
    """Decodes both standard JSON object bodies AND double-encoded JSON string bodies
    sent by the Vertex AI `ReasoningEngine` C++ sidecar proxy (`POST /api/reasoning_engine`
    and `POST /api/stream_reasoning_engine`), while supporting direct Pydantic request objects in tests.
    """
    if isinstance(request, StreamReasoningRequest):
        return request
    if not hasattr(request, "body"):
        return StreamReasoningRequest()
    raw_bytes = await request.body()
    if not raw_bytes:
        return StreamReasoningRequest()
    try:
        parsed: Any = json.loads(raw_bytes.decode("utf-8", errors="replace"))
        if isinstance(parsed, str):
            parsed = json.loads(parsed)
        if isinstance(parsed, dict):
            return StreamReasoningRequest(
                class_method=str(parsed.get("class_method") or "streaming_agent_run_with_events"),
                input=parsed.get("input") if isinstance(parsed.get("input"), dict) else {},
            )
    except Exception as exc:
        logger.warning("Failed to parse ReasoningEngine request body: %s", exc)
    return StreamReasoningRequest()


@app.post("/api/stream_reasoning_engine")
async def ge_stream_endpoint(request: Request) -> StreamingResponse:
    """Handles Gemini Enterprise `streaming_agent_run_with_events` wire requests (ReasoningEngine ADK channel)."""
    req = await _parse_reasoning_engine_request(request)
    raw_input = req.input
    if isinstance(raw_input.get("request_json"), str):
        try:
            inner = json.loads(raw_input["request_json"])
        except json.JSONDecodeError:
            inner = raw_input
    else:
        inner = raw_input

    user_id = resolve_tenant_identity(
        body_identity=str(inner.get("user_id") or inner.get("user_email") or "").strip(),
        channel="adk",
    )
    session_id = str(inner.get("session_id") or uuid.uuid4().hex[:8])
    message_text = _extract_adk_turn_text(inner)
    invocation_id = uuid.uuid4().hex[:8]

    async def _event_stream():
        turn_task = asyncio.create_task(
            _handle_conversation_turn(
                user_id=user_id,
                session_id=session_id,
                message_text=message_text,
            )
        )
        try:
            delay = STREAM_HEARTBEAT_DELAY_SEC
            while not turn_task.done():
                done, _ = await asyncio.wait({turn_task}, timeout=delay)
                if not done:
                    # Emit the ADK empty-envelope heartbeat (`{}\n`) matching Discovery Engine's
                    # `api_http_server.py:_event_stream_with_heartbeat`, keeping the GE SSE stream
                    # flushed and alive when preflight/start_audit takes >2.5s.
                    yield b"{}\n"
                    delay = STREAM_HEARTBEAT_INTERVAL_SEC
            try:
                reply = turn_task.result()
            except Exception as exc:
                logger.exception("Unhandled error in ge_stream_endpoint turn for %s", user_id)
                reply = f"⚠️ 系统处理请求时遇到异常：`{exc}`"
            envelope = build_adk_event_envelope(reply, invocation_id, session_id)
            yield (json.dumps(envelope, ensure_ascii=False) + "\n").encode("utf-8")
        finally:
            if not turn_task.done():
                turn_task.cancel()

    # `application/json`, not ndjson -- matches ADK server & ReasoningEngine wire contract (phase0/README.md:L327)
    return StreamingResponse(_event_stream(), media_type="application/json")


@app.post("/api/reasoning_engine")
async def ge_unary_endpoint(request: Request) -> Dict[str, Any]:
    """Handles direct `:query` unary RPCs (`preflight`, `start_audit`, `get_status`, `hello`) on ReasoningEngine."""
    req = await _parse_reasoning_engine_request(request)
    method = req.class_method
    payload = req.input
    user_id = resolve_tenant_identity(
        body_identity=str(payload.get("user_id") or payload.get("user_email") or "").strip(),
        channel="adk_unary",
    )
    session_id = str(payload.get("session_id") or "")
    if method == "hello":
        return {"output": {"status": "ok", "service": AUTHOR}}
    if method == "preflight":
        drive_url = str(payload.get("target") or payload.get("drive_url") or "")
        job = await audit_service.preflight(user_id=user_id, drive_url=drive_url, session_id=session_id)
        return {"output": job.model_dump()}
    if method == "start_audit":
        job = await audit_service.start_audit(
            user_id=user_id,
            job_id=str(payload.get("job_id") or "") or None,
            session_id=session_id,
        )
        return {"output": job.model_dump()}
    if method == "get_status":
        job = await audit_service.get_status(
            user_id=user_id,
            job_id=str(payload.get("job_id") or "") or None,
        )
        return {"output": job.model_dump() if job else {"error": "no such job"}}
    return {"output": {"error": f"unknown method: {method}"}}


class InternalJobExecuteRequest(BaseModel):
    user_id: str = Field(description="Verified supervisor email")
    job_id: str = Field(description="Job ID stored in Zero-DB GCS/disk store")
    hold_connection: bool = Field(
        default=True,
        description="Hold HTTP request open during execution so Cloud Run (--concurrency=1) pins 1 container per job",
    )


@app.post("/internal/jobs/execute")
async def internal_execute_job(req: InternalJobExecuteRequest) -> Any:
    """Elastic Cloud Run Worker Pool endpoint invoked by ReasoningEngine (`CLOUD_RUN_WORKER_URL`).

    Rehydrates `AuditJob` from `gs://${STAGING_BUCKET}/jobs/{user_slug}/{job_id}.json`,
    loads `PromptManager.load_active_config()`, and executes heavy FFmpeg `.mov -> .mp4` slicing,
    concurrent `gemini-3.8-flash` (`location="global"`) multimodal inference, and Workspace reporting.
    Enforces strict 1-job-per-container isolation (`--concurrency=1`): if this container already has
    an active job running, returns HTTP 429 so the caller/load-balancer routes to a fresh instance.
    """
    from fastapi.responses import JSONResponse

    if audit_service.has_active_work():
        return JSONResponse(
            status_code=429,
            content={
                "dispatched": False,
                "busy": True,
                "error": "Container already executing an active job; scale out to another instance",
            },
        )
    job = await audit_service.jobs.get(req.user_id, req.job_id)
    if job is None:
        return {"dispatched": False, "error": f"job {req.job_id} not found for {req.user_id}"}
    prompt_cfg = await audit_service.prompt_manager.load_active_config()
    if req.hold_connection:
        await audit_service._run_detached_audit(job, prompt_cfg)
        return {"dispatched": True, "completed": True, "job_id": job.job_id, "user_id": job.user_id}
    task = asyncio.create_task(audit_service._run_detached_audit(job, prompt_cfg))
    audit_service._background_tasks.add(task)
    task.add_done_callback(audit_service._background_tasks.discard)
    return {"dispatched": True, "job_id": job.job_id, "user_id": job.user_id}


@app.post("/internal/jobs/sweep")
async def internal_sweep_stale_jobs() -> Dict[str, Any]:
    """Unattended Watchdog endpoint (`POST /internal/jobs/sweep`), invoked every 2 minutes by
    Google Cloud Scheduler (main.tf `<name_prefix>-watchdog`) and the Tier-1 background watchdog loop.

    If a Tier-2 worker crashes 20 minutes into a long audit while the supervisor is away from GE,
    this sweep detects the stale `heartbeat_at` (`> 180s`) or `FAILED` status in GCS and automatically
    resumes the job from the last completed 30-minute `SegmentCheckpoint` with zero human intervention.
    """
    resumed = await audit_service.sweep_and_resume_stale_jobs()
    return {
        "swept": True,
        "resumed_count": len(resumed),
        "resumed_jobs": [
            {
                "job_id": j.job_id,
                "user_id": j.user_id,
                "resume_count": j.resume_count,
                "completed_segments": len(j.completed_segments),
            }
            for j in resumed
        ],
    }


async def _tier1_unattended_watchdog_loop() -> None:
    """Always-on background watchdog on Tier-1 (`minInstances=1`) that sweeps GCS every 60s
    as a second layer of protection alongside Cloud Scheduler (`POST /internal/jobs/sweep`).
    """
    while True:
        try:
            await asyncio.sleep(60.0)
            await audit_service.sweep_and_resume_stale_jobs()
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.debug("Tier-1 background watchdog sweep warning: %s", exc)


async def _start_unattended_watchdog() -> None:
    import os

    if os.environ.get("ENABLE_BACKGROUND_WATCHDOG", "true").lower() in ("1", "true", "yes"):
        asyncio.create_task(_tier1_unattended_watchdog_loop())


if hasattr(app, "on_event"):
    app.on_event("startup")(_start_unattended_watchdog)


