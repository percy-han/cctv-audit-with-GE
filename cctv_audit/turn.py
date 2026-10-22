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
import re
from enum import Enum
from typing import Optional

from google.genai import types
from pydantic import BaseModel, Field

from .config import config
from .gcp import generate_content_with_retry
from .prompt_manager import TAB4_SPOT_CHECK_TITLE

logger = logging.getLogger("cctv_audit.turn")

_APPROVE_RE = re.compile(
    r"^\s*(?:confirm\s+and\s+start|confirm|start|yes|go\s+ahead|proceed|run|ok|okay|确认开始|开始|确认|好的|好)\s*[.!。！]*\s*$",
    re.IGNORECASE,
)
_CANCEL_RE = re.compile(
    r"^\s*(?:cancel|stop|abort|nevermind|never\s+mind|停止|取消)\s*[.!。！]*\s*$",
    re.IGNORECASE,
)

_SPOT_CHECK_KEYWORDS: tuple[str, ...] = (
    "spot-check",
    "spot check",
    "only test tab4",
    "tab4_spot_check",
    "tab4 spot check",
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
    """Deterministically returns `TAB4_SPOT_CHECK_TITLE` (`'Tab4_Spot_Check'`) if and only if the
    current user turn explicitly requests the fixed spot-check / new-SOP test tab (`Tab4_Spot_Check`),
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
            "Intent classification for the current user turn: "
            "inspect = user provides a new Google Drive folder/video URL or GCS directory (gs://bucket/prefix or Cloud Console Storage URL) to run preflight; "
            "confirm = user approves/confirms starting the audit job that just passed preflight (e.g., 'Confirm and start', 'start', 'confirm', 'yes', 'go ahead', 'proceed', 'run', 'ok', 'okay', '确认开始', '开始'); "
            "status = user asks for the progress or result of a running/completed audit job; "
            "unclear = user intent is unclear or missing a Drive link / GCS path (or user asks to cancel/stop without a running job), requiring a polite clarification prompt."
        )
    )
    drive_url: str = Field(
        default="",
        description=(
            "When action=inspect, extract the exact Google Drive URL/folder ID or GCS path (gs://... or Cloud Console Storage URL) character-for-character from the user turn; "
            "never rewrite or fabricate URLs. Leave empty if no link is present."
        ),
    )
    job_id: str = Field(
        default="",
        description="Extract the 6-character hex job ID if explicitly mentioned by the user; otherwise leave empty.",
    )
    target_prompt_tab: str = Field(
        default="",
        description=(
            "Set to 'Tab4_Spot_Check' when the user explicitly requests a targeted spot-check or new SOP test using Tab4_Spot_Check (or legacy Tab4_专项测试表); "
            "always leave empty '' for a standard full daily audit."
        ),
    )
    reply_summary: str = Field(
        default="",
        description="A concise one-sentence English summary of the recognized user intent, or a helpful English clarification prompt when action=unclear.",
    )


TURN_CLASSIFIER_SYSTEM_INSTRUCTION = """You are the conversation turn router for the CHAGEE Store CCTV AI Auditor.
This system enforces a strict two-step controlled workflow and understands both English and Chinese user inputs:
- Step 1 (`inspect`): The supervisor pastes a Google Drive folder/video link or a GCS directory (`gs://bucket/prefix` or Cloud Console Storage URL). The system runs a fast `<720P` resolution preflight and token estimate. If the user's message explicitly mentions `Tab4`, `Tab4_Spot_Check`, `Spot-Check`, `Tab4_专项测试表`, or `专项抽检`, set `target_prompt_tab` to `"Tab4_Spot_Check"`; otherwise `target_prompt_tab` MUST be an empty string `""` (Full Daily Audit).
- Step 2 (`confirm`): After reviewing the preflight report, the supervisor confirms starting the audit (e.g., "Confirm and start", "confirm", "start", "yes", "go ahead", "proceed", "run", "ok", "okay", "确认开始", "开始", "好的", "开始稽核"). Only then does the system launch background audio stripping, video slicing, and multimodal AI auditing.
- Check Progress (`status`): The supervisor asks about progress or results (e.g., "What is the current progress?", "Is it done?", "进度如何", "跑完了吗").
Output your classification strictly following the `TurnDecision` JSON Schema. IMPORTANT: `drive_url` (Drive link or GCS path) must appear character-for-character in the user's input — never fabricate or guess a link! Always write `reply_summary` in English."""

_ROUTER_SYSTEM_INSTRUCTION = TURN_CLASSIFIER_SYSTEM_INSTRUCTION


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
                    "No valid Google Drive folder link or GCS directory was detected in your message.\n"
                    f"• 💡 **For a Full Daily Audit (all active SOP rules)**, `@` this Agent in the Gemini Enterprise chat box and enter: \"`{agent_handle} [Full Daily Audit] Please audit store videos: <video folder link>`\"\n"
                    f"• 🧪 **For a Targeted Spot-Check or New SOP Test**, update rules in `{TAB4_SPOT_CHECK_TITLE}` of the Master Control Sheet first, then `@` this Agent (or click the `[Spot-Check Tab4 Only]` chip) and enter: \"`{agent_handle} [Spot-Check Tab4 Only (Non-Full)] Please audit store videos: <video folder link>`\""
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
                f"Please make sure you have selected or mentioned `{agent_handle}` in the Gemini Enterprise chat box and paste the store CCTV video folder link you want to audit:\n"
                f"• 💡 **For a Full Daily Audit (all active SOP rules)**, enter: \"`{agent_handle} [Full Daily Audit] Please audit store videos: <video folder link>`\"\n"
                f"• 🧪 **For a Targeted Spot-Check or New SOP Test**, click the `[Spot-Check Tab4 Only (Non-Full)]` chip (or mention `{agent_handle}` with `{TAB4_SPOT_CHECK_TITLE}`) and paste your video folder link."
            ),
        )

    latest_utterance = _extract_latest_user_utterance(cleaned)
    if _CANCEL_RE.match(latest_utterance):
        return TurnDecision(
            action=TurnAction.UNCLEAR,
            reply_summary=(
                "Audit launch cancelled. Whenever you are ready, paste a Google Drive folder link or GCS directory (`gs://bucket/prefix`) to run a new preflight check, or reply '**Confirm and start**' to launch an existing preflighted job."
            ),
        )

    gen_config = types.GenerateContentConfig(
        system_instruction=TURN_CLASSIFIER_SYSTEM_INSTRUCTION,
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
