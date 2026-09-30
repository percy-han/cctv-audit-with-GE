"""Semantic Turn Router (`turn.py`, adapted from `percy-han/cctv-audit/cctv_audit/turn.py`).

Why this exists (from `percy-han/cctv-audit/deploy/phase0/README.md` empirical wire probe):
- Gemini Enterprise (GE) calls ONLY ONE method (`streaming_agent_run_with_events`) and passes
  the raw conversation history.
- Therefore, routing between `inspect` (Step 1 preflight), `confirm` (Step 2 start batch audit),
  `status` (check progress), and `unclear` MUST happen inside our container using a structured
  Pydantic `response_schema` LLM call (`Axiom 2: Problem-Solution Model Isomorphism`),
  NEVER via brittle keyword matching (`"确认" in text`).
- Furthermore, any `drive_url` returned by the router MUST appear verbatim in the conversation
  to prevent hallucinated folder IDs.
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import Optional

from google.genai import types
from pydantic import BaseModel, Field

from .config import config
from .gcp import generate_content_with_retry

logger = logging.getLogger("cctv_audit.turn")


class TurnAction(str, Enum):
    INSPECT = "inspect"
    CONFIRM = "confirm"
    STATUS = "status"
    UNCLEAR = "unclear"


class TurnDecision(BaseModel):
    """Structured semantic decision for a single Gemini Enterprise chat turn."""

    action: TurnAction = Field(
        description=(
            "本轮用户意图分类：inspect=提供新的 Google Drive 文件夹/视频链接发起预检；"
            "confirm=同意/确认启动刚才预检通过的稽核任务（如'确认开始'、'好'、'行吧那就跑'）；"
            "status=询问当前正在运行的稽核任务进度或结果；"
            "unclear=意图不明或缺少 Drive 链接，需礼貌反问。"
        )
    )
    drive_url: str = Field(
        default="",
        description=(
            "当 action=inspect 时，逐字符原样提取对话中的 Google Drive 链接或文件夹 ID；"
            "严禁改写或臆造。若对话中无链接则留空。"
        ),
    )
    job_id: str = Field(
        default="",
        description="当用户明确提及 6 位任务单号时提取该单号，否则留空。",
    )
    reply_summary: str = Field(
        default="",
        description="用一句中文总结理解到的用户意图，或当 action=unclear 时向用户发出的追问提示。",
    )


_ROUTER_SYSTEM_INSTRUCTION = """你是霸王茶姬 (CHAGEE) 门店视频 AI 稽核助手的会话路由器。
本系统采用严格的两步式受控工作流：
- 第一步 (`inspect`)：督导粘贴个人专属 Google Drive 文件夹链接，系统执行秒级 `<720P` 分辨率预检与 Token 报数；
- 第二步 (`confirm`)：督导看到预检报告后表示同意（例如说“确认开始”、“好的”、“可以，跑吧”、“开始稽核”），系统才正式启动后台消音切片与 AI 稽核；
- 查询进度 (`status`)：督导询问“跑完了吗”、“进度如何”。
请严格按 `TurnDecision` JSON Schema 输出分类结果。注意：`drive_url` 必须逐字符出现在用户输入中，严禁自己编造链接！"""


def verify_url_verbatim(decision: TurnDecision, raw_conversation_text: str) -> TurnDecision:
    """Enforces verbatim URL verification from `percy-han/cctv-audit/cctv_audit/turn.py`.

    If the model returned a `drive_url` that does not literally appear in `raw_conversation_text`,
    reject it into `TurnAction.UNCLEAR` so we never audit a hallucinated folder.
    """
    if decision.action == TurnAction.INSPECT:
        candidate = (decision.drive_url or "").strip()
        if not candidate or candidate not in raw_conversation_text:
            return TurnDecision(
                action=TurnAction.UNCLEAR,
                drive_url="",
                job_id="",
                reply_summary="未在您的消息中识别到真实的 Google Drive 文件夹链接，请粘贴完整的 Google Drive 文件夹链接（如 `https://drive.google.com/drive/folders/...`）。",
            )
    return decision


async def classify_turn_with_llm(
    conversation_text: str,
    model_name: Optional[str] = None,
) -> TurnDecision:
    """Uses Gemini structured output (`TurnDecision` schema) to route the user's natural language turn."""
    cleaned = (conversation_text or "").strip()
    if not cleaned:
        return TurnDecision(
            action=TurnAction.UNCLEAR,
            reply_summary="请粘贴您需要稽核的 Google Drive 监控视频文件夹链接。",
        )

    gen_config = types.GenerateContentConfig(
        system_instruction=_ROUTER_SYSTEM_INSTRUCTION,
        response_mime_type="application/json",
        response_schema=TurnDecision,
        temperature=0.0,
        thinking_config=types.ThinkingConfig(thinking_budget=0),
    )
    resp = await generate_content_with_retry(
        model=model_name or config.fallback_model_version,
        contents=[cleaned],
        gen_config=gen_config,
    )
    parsed: TurnDecision = (
        resp.parsed
        if getattr(resp, "parsed", None) is not None
        else TurnDecision.model_validate_json(resp.text)
    )
    return verify_url_verbatim(parsed, cleaned)
