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
from .prompt_manager import TAB4_SPOT_CHECK_TITLE

logger = logging.getLogger("cctv_audit.turn")

_SPOT_CHECK_KEYWORDS: tuple[str, ...] = (
    "仅测 tab4_专项测试表（非全量）",
    "tab4_专项测试表",
    "tab4",
    "tab 4",
    "专项测试表",
    "专项抽检",
)


def _extract_latest_user_utterance(conversation_text: str) -> str:
    """Extracts the latest user message from a formatted `[assistant]/[user]` transcript or raw string."""
    raw = (conversation_text or "").strip()
    if "\n[user] " in raw:
        return raw.rsplit("\n[user] ", 1)[1].strip()
    if raw.startswith("[user] "):
        return raw[len("[user] ") :].strip()
    return raw


def detect_spot_check_tab(conversation_text: str) -> str:
    """Deterministically returns `TAB4_SPOT_CHECK_TITLE` (`'Tab4_专项测试表'`) if and only if the
    current user turn explicitly requests the fixed spot-check / new-SOP test tab (`Tab4_专项测试表`),
    preventing both prompt injection and LLM routing ambiguity.
    """
    latest = _extract_latest_user_utterance(conversation_text)
    lowered = latest.lower()
    if any(kw in lowered for kw in _SPOT_CHECK_KEYWORDS):
        return TAB4_SPOT_CHECK_TITLE
    return ""


class TurnAction(str, Enum):
    INSPECT = "inspect"
    CONFIRM = "confirm"
    STATUS = "status"
    UNCLEAR = "unclear"


class TurnDecision(BaseModel):
    """Structured semantic decision for a single Gemini Enterprise chat turn."""

    action: TurnAction = Field(
        description=(
            "本轮用户意图分类：inspect=提供新的 Google Drive 文件夹/视频链接，或 GCS 目录（gs://存储桶/目录 或 Cloud Console Storage 链接）发起预检；"
            "confirm=同意/确认启动刚才预检通过的稽核任务（如'确认开始'、'好'、'行吧那就跑'）；"
            "status=询问当前正在运行的稽核任务进度或结果；"
            "unclear=意图不明或缺少 Drive 链接 / GCS 目录，需礼貌反问。"
        )
    )
    drive_url: str = Field(
        default="",
        description=(
            "当 action=inspect 时，逐字符原样提取对话中的 Google Drive 链接或文件夹 ID，或 GCS 路径（gs://... 或 Cloud Console Storage 链接）；"
            "严禁改写或臆造。若对话中无链接则留空。"
        ),
    )
    job_id: str = Field(
        default="",
        description="当用户明确提及 6 位任务单号时提取该单号，否则留空。",
    )
    target_prompt_tab: str = Field(
        default="",
        description=(
            "当用户明确要求使用 Tab4_专项测试表 进行专项抽检或新 SOP 测试时填入 'Tab4_专项测试表'；"
            "日常完整全量稽核一律留空 ''。"
        ),
    )
    reply_summary: str = Field(
        default="",
        description="用一句中文总结理解到的用户意图，或当 action=unclear 时向用户发出的追问提示。",
    )


_ROUTER_SYSTEM_INSTRUCTION = """你是霸王茶姬 (CHAGEE) 门店视频 AI 稽核助手的会话路由器。
本系统采用严格的两步式受控工作流：
- 第一步 (`inspect`)：督导粘贴个人专属 Google Drive 文件夹链接，或无 Google Workspace 时粘贴 GCS 目录（`gs://存储桶/目录` 或 Cloud Console Storage 链接），系统执行秒级 `<720P` 分辨率预检与 Token 报数；若用户消息中明确包含 `Tab4`、`Tab4_专项测试表` 或 `专项抽检`，则将 `target_prompt_tab` 设为 `"Tab4_专项测试表"`，否则 `target_prompt_tab` 必须为空字符串 `""`（日常全量稽核）；
- 第二步 (`confirm`)：督导看到预检报告后表示同意（例如说“确认开始”、“好的”、“可以，跑吧”、“开始稽核”），系统才正式启动后台消音切片与 AI 稽核；
- 查询进度 (`status`)：督导询问“跑完了吗”、“进度如何”。
请严格按 `TurnDecision` JSON Schema 输出分类结果。注意：`drive_url`（Drive 链接或 GCS 路径）必须逐字符出现在用户输入中，严禁自己编造链接！"""


def verify_url_verbatim(decision: TurnDecision, raw_conversation_text: str) -> TurnDecision:
    """Enforces verbatim URL verification from `percy-han/cctv-audit/cctv_audit/turn.py`
    and deterministically locks `target_prompt_tab` from the current user turn text.

    If the model returned a `drive_url` that does not literally appear in `raw_conversation_text`,
    reject it into `TurnAction.UNCLEAR` so we never audit a hallucinated folder.
    """
    if decision.action == TurnAction.INSPECT:
        candidate = (decision.drive_url or "").strip()
        if not candidate or candidate not in raw_conversation_text:
            agent_handle = f"@{config.agent_display_name}"
            return TurnDecision(
                action=TurnAction.UNCLEAR,
                drive_url="",
                job_id="",
                target_prompt_tab="",
                reply_summary=(
                    "未在您的消息中识别到有效的 Google Drive 文件夹链接或 GCS 目录。\n"
                    f"• 💡 **如果您想完整稽核整个视频（日常全量稽核）**，请在 GE 对话框 `@` 本 Agent 输入：「`{agent_handle} 请帮忙稽核视频，<视频文件夹链接>`」\n"
                    f"• 🧪 **如果您想做专项抽检或测试新 SOP**，请先在总控表 `Tab4_专项测试表` 更新规则，再 `@` 本 Agent（或点击【仅测 Tab4_专项测试表】气泡）输入：「`{agent_handle} 【仅测 Tab4_专项测试表】请帮忙稽核视频，<视频文件夹链接>`」"
                ),
            )
        return decision.model_copy(
            update={"target_prompt_tab": detect_spot_check_tab(raw_conversation_text)}
        )
    return decision.model_copy(update={"target_prompt_tab": ""})


async def classify_turn_with_llm(
    conversation_text: str,
    model_name: Optional[str] = None,
) -> TurnDecision:
    """Uses Gemini structured output (`TurnDecision` schema) to route the user's natural language turn."""
    cleaned = (conversation_text or "").strip()
    if not cleaned:
        agent_handle = f"@{config.agent_display_name}"
        return TurnDecision(
            action=TurnAction.UNCLEAR,
            reply_summary=(
                f"请先在 Gemini Enterprise 对话框确认已选中或 `{agent_handle}`，并粘贴您需要稽核的监控视频文件夹链接：\n"
                f"• 💡 **如果您想完整稽核整个视频（日常全量稽核）**，请在提示词中输入：「`{agent_handle} 请帮忙稽核视频，<视频文件夹链接>`」\n"
                f"• 🧪 **如果您想做专项抽检或测试新 SOP**，请点击【仅测 Tab4_专项测试表】气泡（或 `{agent_handle}`）并贴上视频文件夹链接。"
            ),
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
