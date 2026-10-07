"""Module 4: Folder-as-a-Workspace Dual-Tab Sheet Reporter & Notifier (`workspace_reporter.py`).

Implements `REQ-006`, `REQ-007`, `REQ-008`, `REQ-012`, and `CON-007` (Zero-DB Pure Workspace Stack):
1. Creates `📁 违规证据切片_Evidence/` directly inside the supervisor's Google Drive folder (`folder_id`),
   automatically inheriting the parent folder's ACL for multi-user isolation (`Folder-as-a-Workspace`).
2. Uploads 5-10s MP4 evidence clips to `Evidence/` and records permanent `webViewLink` URLs
   (solving GCS Signed URL 7-day expiration and enabling hover-play in Google Sheets).
3. Creates a dedicated dual-tab Google Sheet (`📊 AI稽核报告与Token账单_{date}`) inside `folder_id`:
   - `Tab 1【违规事件 3 秒复核台】`: Every row defaults to `⏳ 待人工复核 (PENDING_HUMAN_REVIEW)` (`CON-007`).
   - `Tab 2【本次视频 Token 消耗与耗时账单】`: Records `model_version_used`, `prompt_version_used`,
     token breakdown, latency, and USD cost (`REQ-012`, replacing BigQuery).
4. Sends completion notification via Google Chat Webhook (`httpx.AsyncClient` with explicit timeout).
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import List, Optional, Protocol

import httpx
from pydantic import BaseModel, Field

from .agentic_auditor import Finding, Status, TokenLedgerRow
from .config import config
from .gcp import resolve_chat_user_id

logger = logging.getLogger("cctv_audit.workspace_reporter")

INITIAL_REVIEW_STATUS = "⏳ 待人工复核 (PENDING_HUMAN_REVIEW)"
EVIDENCE_SUBFOLDER_NAME = "📁 违规证据切片_Evidence"
REPORT_SHEET_TITLE = "📊 AI稽核报告与Token账单"


class ViolationSheetRow(BaseModel):
    """Row schema written to `Tab 1【违规事件 3 秒复核台】`."""

    audit_id: str
    video_filename: str
    timestamp_in_clip: str
    rule_id: str
    disposition: str
    severity: str
    confidence: float
    evidence_description: str
    evidence_drive_url: str
    human_review_status: str = Field(default=INITIAL_REVIEW_STATUS)


class AuditReportArtifact(BaseModel):
    """Summary of the generated in-folder Google Sheet and uploaded evidence clips."""

    audit_id: str
    parent_folder_id: str
    evidence_subfolder_id: str
    report_sheet_id: str
    report_sheet_url: str
    violation_rows_count: int
    ledger_rows_count: int
    notification_sent: bool = False


class WorkspaceDriveSheetsGatewayProtocol(Protocol):
    """Protocol for Google Drive & Google Sheets API operations inside `parent_folder_id`."""

    async def probe_write_access(self, folder_id: str) -> str: ...
    async def check_sheet_readable(self, sheet_id: str) -> str: ...
    async def ensure_subfolder(self, parent_folder_id: str, name: str) -> str: ...
    async def upload_evidence_mp4(self, subfolder_id: str, local_path: Path) -> str: ...
    async def create_dual_tab_report_sheet(
        self,
        parent_folder_id: str,
        title: str,
        tab1_rows: List[ViolationSheetRow],
        tab2_rows: List[TokenLedgerRow],
        *,
        reuse_suffix: str = "",
    ) -> tuple[str, str]: ...


class WorkspaceReporter:
    """Writes permanent MP4 evidence clips and Dual-Tab Google Sheet directly into the user's Drive Folder."""

    def __init__(
        self,
        gateway: Optional[WorkspaceDriveSheetsGatewayProtocol] = None,
        webhook_url: Optional[str] = None,
        enable_notification: Optional[bool] = None,
    ) -> None:
        self._gateway = gateway
        self._webhook_url = (
            webhook_url if webhook_url is not None else config.google_chat_webhook_url
        )
        self._enable_notification = enable_notification

    def build_tab1_rows(
        self,
        audit_id: str,
        video_filename: str,
        findings: List[Finding],
        source_video_file_id: str = "",
    ) -> List[ViolationSheetRow]:
        """Filters reportable findings (`Status.VIOLATION`) and enforces `PENDING_HUMAN_REVIEW` (`CON-007`)."""
        rows: List[ViolationSheetRow] = []
        fallback_url = (
            f"https://drive.google.com/file/d/{source_video_file_id}/view"
            if source_video_file_id
            else ""
        )
        for f in findings:
            clean = f.sanitise()
            if clean.status != Status.VIOLATION:
                continue
            rows.append(
                ViolationSheetRow(
                    audit_id=audit_id,
                    video_filename=video_filename,
                    timestamp_in_clip=clean.timestamp_in_clip,
                    rule_id=clean.rule_id,
                    disposition=clean.disposition.value,
                    severity=clean.severity.value,
                    confidence=clean.confidence,
                    evidence_description=clean.evidence,
                    evidence_drive_url=clean.evidence_drive_url or fallback_url,
                    human_review_status=INITIAL_REVIEW_STATUS,
                )
            )
        return rows

    async def publish_in_folder_report(
        self,
        *,
        audit_id: str,
        user_email: str,
        parent_folder_id: str,
        findings_by_video: List[tuple[str, List[Finding]]],
        ledger_rows: List[TokenLedgerRow],
    ) -> AuditReportArtifact:
        """Creates `Evidence/` subfolder + Dual-Tab Google Sheet inside `parent_folder_id` and notifies user."""
        sheet_title = REPORT_SHEET_TITLE

        evidence_subfolder_id = f"ev_{parent_folder_id[:8]}"
        if self._gateway is not None:
            evidence_subfolder_id = await asyncio.wait_for(
                self._gateway.ensure_subfolder(
                    parent_folder_id, EVIDENCE_SUBFOLDER_NAME
                ),
                timeout=15.0,
            )

        all_tab1_rows: List[ViolationSheetRow] = []
        for video_name, findings in findings_by_video:
            updated_findings: List[Finding] = []
            for f in findings:
                if f.evidence_clip_local_path and self._gateway is not None:
                    clip_path = Path(f.evidence_clip_local_path)
                    if clip_path.exists():
                        drive_url = await asyncio.wait_for(
                            self._gateway.upload_evidence_mp4(
                                evidence_subfolder_id, clip_path
                            ),
                            timeout=60.0,
                        )
                        if drive_url:
                            f = f.model_copy(update={"evidence_drive_url": drive_url})
                            try:
                                clip_path.unlink(missing_ok=True)
                            except Exception:
                                pass
                updated_findings.append(f)
            all_tab1_rows.extend(
                self.build_tab1_rows(
                    audit_id,
                    video_name,
                    updated_findings,
                )
            )

        if self._gateway is not None:
            sheet_id, sheet_url = await asyncio.wait_for(
                self._gateway.create_dual_tab_report_sheet(
                    parent_folder_id,
                    sheet_title,
                    all_tab1_rows,
                    ledger_rows,
                ),
                timeout=60.0,
            )
        else:
            sheet_id = f"sheet_{audit_id}"
            sheet_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit"

        notified = await self.send_completion_notification(
            user_email=user_email,
            audit_id=audit_id,
            sheet_url=sheet_url,
            violations_count=len(all_tab1_rows),
            total_tokens=sum(r.total_token_count for r in ledger_rows),
        )

        return AuditReportArtifact(
            audit_id=audit_id,
            parent_folder_id=parent_folder_id,
            evidence_subfolder_id=evidence_subfolder_id,
            report_sheet_id=sheet_id,
            report_sheet_url=sheet_url,
            violation_rows_count=len(all_tab1_rows),
            ledger_rows_count=len(ledger_rows),
            notification_sent=notified,
        )

    async def send_completion_notification(
        self,
        *,
        user_email: str,
        audit_id: str,
        sheet_url: str,
        violations_count: int,
        total_tokens: int,
    ) -> bool:
        """Sends Google Chat Webhook completion alert with direct link to the in-folder Google Sheet."""
        enabled = (
            self._enable_notification
            if self._enable_notification is not None
            else (
                config.enable_google_chat_notification
                if "ENABLE_GOOGLE_CHAT_NOTIFICATION" in os.environ
                else bool(self._webhook_url)
            )
        )
        if not enabled or not self._webhook_url:
            logger.info(
                "Google Chat notification disabled or webhook unconfigured (enabled=%s); skipping push for job %s (user=%s)",
                enabled,
                audit_id,
                user_email,
            )
            return False

        chat_user_id = await asyncio.to_thread(resolve_chat_user_id, user_email)
        initiator_line = (
            f"<users/{chat_user_id}> (`{user_email}`)"
            if chat_user_id
            else f"`{user_email}`"
        )
        payload = {
            "text": (
                f"🔔 *【霸王茶姬门店 CCTV AI 稽核完成通知】*\n"
                f"• **发起督导**：{initiator_line}\n"
                f"• **稽核单号**：`{audit_id}`\n"
                f"• **待人工 3 秒复核事件数**：`{violations_count}` 项（全部含 5~10s 永久视频切片）\n"
                f"• **总计 Token 消耗**：`{total_tokens:,}` Tokens\n"
                f"👉 **[点击打开您原文件夹内的《门店稽核报告与 Token 账单 Sheet》]({sheet_url})**"
            )
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(self._webhook_url, json=payload)
                resp.raise_for_status()
            return True
        except Exception as exc:
            logger.warning("Failed to send Google Chat webhook notification: %s", exc)
            return False
