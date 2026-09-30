"""ADK local development entry point (`agent.py`, compatible with `adk web cctv_audit`)."""

from __future__ import annotations

import os
from typing import AsyncGenerator

from google.adk.agents.base_agent import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event
from google.genai import types

from .server import audit_service


class ChageeCctvAuditAdkAgent(BaseAgent):
    """Thin ADK BaseAgent wrapper for local `adk web` interactive testing on Cloudtop."""

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        user_text = ""
        if ctx.user_content and ctx.user_content.parts:
            user_text = "".join(p.text or "" for p in ctx.user_content.parts).strip()

        user_email = getattr(ctx, "user_id", None) or os.environ.get("LOCAL_ADK_USER_ID", "local-user@example.com")
        if "drive.google.com" in user_text:
            job = await audit_service.preflight(
                user_id=user_email,
                drive_url=user_text,
                session_id=ctx.session.id if ctx.session else "local",
            )
            reply = (
                job.preflight_report.message_to_user
                if job.preflight_report
                else f"Job {job.job_id}: {job.state.value}"
            )
        else:
            reply = (
                "请粘贴您的 Google Drive 监控视频文件夹链接（如 `https://drive.google.com/drive/folders/...`），"
                "系统将先执行 `<720P` 分辨率秒级预检与 Token 估算。"
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
