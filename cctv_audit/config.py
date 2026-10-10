"""Strongly-typed configuration contract for Chagee CCTV AI Audit (SDD SSOT)."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from .gcs_uri import normalize_gcs_target

# Accepts both `/spreadsheets/d/<ID>` and the multi-account `/spreadsheets/u/<N>/d/<ID>`
# form that Google emits for users signed into more than one account.
_SHEET_URL_PATTERN = re.compile(r"/spreadsheets/(?:u/\d+/)?d/([a-zA-Z0-9_-]{15,})")
# A bare Spreadsheet ID never contains a scheme or a path separator.
_URL_SHAPED_PATTERN = re.compile(r"://|/")
# Object suffixes accepted for a GCS-hosted Master SOP workbook (Zero-GWS mode).
GCS_SOP_SUFFIXES = (".xlsx", ".json")


def extract_spreadsheet_id(raw_value: str) -> str:
    """Extract a clean Google Spreadsheet ID from a full docs.google.com URL or a bare ID.

    Fails fast on URL-shaped input we cannot parse (e.g. a Drive *folder* link pasted into
    `MASTER_PROMPT_SHEET_ID`) so the mistake surfaces as a container start-up crash, rather
    than being handed to the Sheets API verbatim and silently degrading every later audit to
    the bundled baseline prompt via the `load_active_config` exception fallback.
    """
    cleaned = raw_value.strip()
    if not cleaned:
        raise ValueError("MASTER_PROMPT_SHEET_ID 不能为空")
    # Zero-GWS: a GCS-hosted Master SOP workbook (`gs://bucket/sop/master_sheet.xlsx` / `.json`,
    # or the Cloud Console / storage URL of that object) is normalised to `gs://bucket/<object>`.
    gcs_target = normalize_gcs_target(cleaned)
    if gcs_target is not None:
        obj = gcs_target[len("gs://"):].partition("/")[2]
        if not obj or not obj.lower().endswith(GCS_SOP_SUFFIXES):
            raise ValueError(
                f"MASTER_PROMPT_SHEET_ID 的 GCS 路径必须指向具体的 .xlsx 或 .json 文件对象"
                f"（如 gs://bucket/sop/master_sheet.xlsx），当前为: {cleaned!r}"
            )
        return gcs_target
    match = _SHEET_URL_PATTERN.search(cleaned)
    if match:
        return match.group(1)
    if _URL_SHAPED_PATTERN.search(cleaned):
        raise ValueError(
            f"无法从 MASTER_PROMPT_SHEET_ID 解析出合法的 Google Spreadsheet ID: {cleaned!r}。"
            "请传入裸 ID，或形如 https://docs.google.com/spreadsheets/d/<ID>/edit 的完整链接。"
        )
    return cleaned


class AuditConfig(BaseModel):
    """Immutable runtime configuration aligned with Phase 2 SDD and CON-003."""

    # Deployment-specific values have no code default: main.tf sets them on the Cloud Run worker and
    # deploy/deploy_reasoning_engine.py on the ReasoningEngine. Empty = not configured; the code that
    # needs a value fails with the variable's name (gcp.get_genai_client, load_active_config).
    gcp_project: str = Field(
        default_factory=lambda: os.environ.get("GCP_PROJECT")
        or os.environ.get("GOOGLE_CLOUD_PROJECT")
        or ""
    )
    # The stack's region (main.tf var.region; CON-003 data residency of Cloud Run & GCS)
    gcp_location: str = Field(
        default_factory=lambda: os.environ.get("GCP_LOCATION", "")
    )
    # Vertex AI Gemini 3.8 Flash endpoint location (Gemini 3.x publisher models are served on 'global')
    vertex_model_location: str = Field(
        default_factory=lambda: os.environ.get("VERTEX_MODEL_LOCATION", "global")
    )
    staging_bucket: str = Field(
        default_factory=lambda: os.environ.get("STAGING_BUCKET", "")
    )
    # Empty = no Master Prompt Sheet configured (see PromptManager.load_active_config).
    master_prompt_sheet_id: str = Field(
        default_factory=lambda: os.environ.get("MASTER_PROMPT_SHEET_ID", "")
    )
    fallback_model_version: str = Field(
        default_factory=lambda: os.environ.get(
            "FALLBACK_MODEL_VERSION", "gemini-3.8-flash"
        )
    )
    default_prompt_version: str = Field(
        default_factory=lambda: os.environ.get(
            "DEFAULT_PROMPT_VERSION", "Prompt_v2.5_V10全量17条标准版"
        )
    )
    # SDD 1.1: Minimum resolution gate (>= 720P)
    min_short_edge_px: int = Field(default=720, ge=360)
    # SDD 1.1 / ADR-003: 10-minute (600s) segment duration with 1-minute (60s) overlap window for E2E multi-slice testing
    segment_duration_sec: int = Field(
        default_factory=lambda: int(os.environ.get("SEGMENT_DURATION_SEC", "600")),
        ge=60,
    )
    segment_overlap_sec: int = Field(
        default_factory=lambda: int(os.environ.get("SEGMENT_OVERLAP_SEC", "60")),
        ge=0,
    )
    # SDD 1.1 & 1.2: Dual-layer concurrency semaphores
    ffmpeg_concurrency: int = Field(default=2, ge=1, le=8)
    gemini_concurrency: int = Field(default=5, ge=1, le=20)
    # SDD 1.2: Explicit HTTPX timeout in milliseconds for one Gemini call on one video slice.
    # 750,000 ms = 12.5 min (2026-10-06, user decision): the customer's CCTV exports are at most
    # 5 min long (a few 10-min ones), and healthy agentic calls on such slices took 106-598 s in
    # production. The previous 30 min let one hung call stall a job for 32 min before the in-container
    # watchdog (`audit_service._slice_stall_timeout_sec`, this value + 120 s) cut it and the
    # sweep resumed it (job 42e5d3). Raise via env GEMINI_TIMEOUT_MS if longer clips become common.
    gemini_timeout_ms: int = Field(
        default_factory=lambda: int(os.environ.get("GEMINI_TIMEOUT_MS", "750000")),
        ge=30_000,
    )
    # How Gemini samples each video slice:
    #   "agentic" = the model picks moments to zoom into and pulls extra frames itself (Gemini 3.x only; default).
    #   "static"  = uniform frame sampling (~1 fps).
    video_media_processing: Literal["static", "agentic"] = Field(
        default_factory=lambda: os.environ.get("VIDEO_MEDIA_PROCESSING", "agentic").strip().lower(),
        validate_default=True,
    )
    # Token cost estimation rates (USD per 1M tokens for Gemini 3.8 Flash Standard Paid Tier:
    # https://ai.google.dev/gemini-api/docs/pricing#gemini-3.8-flash — $0.75 input / $3.75 output through Dec 31, 2026)
    input_cost_per_million_usd: float = Field(default=0.75, ge=0.0)
    output_cost_per_million_usd: float = Field(default=3.75, ge=0.0)
    enable_google_chat_notification: bool = Field(
        default_factory=lambda: os.environ.get("ENABLE_GOOGLE_CHAT_NOTIFICATION", "false").strip().lower()
        in ("1", "true", "yes", "on")
    )
    google_chat_webhook_url: str = Field(
        default_factory=lambda: os.environ.get("GOOGLE_CHAT_WEBHOOK_URL", "")
    )
    local_work_dir: Path = Field(
        default_factory=lambda: Path(os.environ.get("LOCAL_WORK_DIR", "/tmp/chagee_audit"))
    )

    @field_validator("master_prompt_sheet_id", mode="before")
    @classmethod
    def _normalize_sheet_id(cls, v: str) -> str:
        return extract_spreadsheet_id(str(v)) if str(v).strip() else ""


config = AuditConfig()
