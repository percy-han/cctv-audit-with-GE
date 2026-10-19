"""ADK local development entry point (`agent.py`, compatible with `adk web cctv_audit`)."""

from __future__ import annotations

import os
from typing import AsyncGenerator

from google.adk.agents.base_agent import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event
from google.genai import types

from .server import audit_service
from .turn import detect_spot_check_tab


class ChageeCctvAuditAdkAgent(BaseAgent):
    """Thin ADK BaseAgent wrapper for local `adk web` interactive testing on Cloudtop."""

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        user_text = ""
        if ctx.user_content and ctx.user_content.parts:
            user_text = "".join(p.text or "" for p in ctx.user_content.parts).strip()

        user_email = getattr(ctx, "user_id", None) or os.environ.get("LOCAL_ADK_USER_ID", "local-user@example.com")
        if any(k in user_text for k in ("drive.google.com", "gs://", "console.cloud.google.com/storage")):
            job = await audit_service.preflight(
                user_id=user_email,
                drive_url=user_text,
                session_id=ctx.session.id if ctx.session else "local",
                target_prompt_tab=detect_spot_check_tab(user_text),
            )
            reply = (
                job.preflight_report.message_to_user
                if job.preflight_report
                else f"Job {job.job_id}: {job.state.value}"
            )
        else:
            reply = (
                "请粘贴您的 Google Drive 监控视频文件夹链接（如 `https://drive.google.com/drive/folders/...`）或 GCS 目录（如 `gs://存储桶/门店目录`），"
                "系统将先执行 `<720P` 分辨率秒级预检与 Token 估算。\n"
                "• 💡 **如果您想完整稽核整个视频（日常全量稽核）**，请输入：「`请帮忙稽核视频，<视频文件夹链接>`」\n"
                "• 🧪 **如果您想做专项抽检或测试新 SOP**，请先在总控表 `Tab4_专项测试表` 更新规则，再输入：「`【仅测 Tab4_专项测试表】请帮忙稽核视频，<视频文件夹链接>`」"
            )

        yield Event(
            author=self.name,
            invocation_id=ctx.invocation_id,
            content=types.Content(role="model", parts=[types.Part.from_text(text=reply)]),
        )


root_agent = ChageeCctvAuditAdkAgent(
    name="chagee_cctv_audit",
    description="霸王茶姬门店 CCTV 视频 AI 自动稽核 Agent (Zero-DB Google Workspace + Agent Platform)",
)
