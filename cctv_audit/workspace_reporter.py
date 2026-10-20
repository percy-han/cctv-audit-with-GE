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
from typing import Any, Dict, List, Optional, Protocol, Sequence, Tuple

import httpx
from pydantic import BaseModel, Field

from .agentic_auditor import Finding, Status, TokenLedgerRow
from .config import config
from .gcp import resolve_chat_user_id
from .gcs_gateway import (
    build_gcs_console_folder_url,
    build_source_video_url,
    is_gcs_target,
    safe_storage_id_slug,
)
from .portal import build_portal_url

logger = logging.getLogger("cctv_audit.workspace_reporter")

INITIAL_REVIEW_STATUS = "⏳ 待人工复核 (PENDING_HUMAN_REVIEW)"
EVIDENCE_SUBFOLDER_NAME = "📁 违规证据切片_Evidence"
REPORT_SHEET_TITLE = "📊 AI稽核报告与Token账单"
_WEBHOOK_URI_CACHE: Dict[str, Tuple[float, str]] = {}
_WEBHOOK_CACHE_TTL_SEC = 300.0


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
    human_review_note: str = Field(default="")


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
        audited_rule_ids: Optional[Sequence[str]] = None,
    ) -> tuple[str, str]: ...
    async def read_report_sheet_data(
        self, folder_id: str, report_sheet_url: str = ""
    ) -> Tuple[List[List[Any]], List[List[Any]]]: ...
    async def update_report_human_reviews(
        self,
        folder_id: str,
        report_sheet_url: str,
        updates: List[Dict[str, str]],
    ) -> int: ...


class WorkspaceReporter:
    """Writes permanent MP4 evidence clips and Dual-Tab Google Sheet directly into the user's Drive Folder."""

    def __init__(
        self,
        gateway: Optional[WorkspaceDriveSheetsGatewayProtocol] = None,
        webhook_url: Optional[str] = None,
        enable_notification: Optional[bool] = None,
    ) -> None:
        self._gateway = gateway
        self._explicit_webhook_url = webhook_url
        self._enable_notification = enable_notification

    @property
    def _webhook_url(self) -> str:
        return (
            self._explicit_webhook_url
            if self._explicit_webhook_url is not None
            else config.google_chat_webhook_url
        )

    @_webhook_url.setter
    def _webhook_url(self, value: Optional[str]) -> None:
        self._explicit_webhook_url = value

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
            build_source_video_url(source_video_file_id) if source_video_file_id else ""
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
        suppress_notification: bool = False,
        audited_rule_ids: Optional[Sequence[str]] = None,
    ) -> AuditReportArtifact:
        """Creates `Evidence/` subfolder + Dual-Tab Google Sheet inside `parent_folder_id` and notifies user."""
        sheet_title = REPORT_SHEET_TITLE

        if is_gcs_target(parent_folder_id):
            evidence_subfolder_id = f"ev_{safe_storage_id_slug(parent_folder_id)[:12]}"
        else:
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
            sheet_kwargs: Dict[str, Any] = {}
            if audited_rule_ids:
                sheet_kwargs["audited_rule_ids"] = list(audited_rule_ids)
            try:
                sheet_id, sheet_url = await asyncio.wait_for(
                    self._gateway.create_dual_tab_report_sheet(
                        parent_folder_id,
                        sheet_title,
                        all_tab1_rows,
                        ledger_rows,
                        **sheet_kwargs,
                    ),
                    timeout=60.0,
                )
            except TypeError:
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

        if suppress_notification:
            notified = False
        else:
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

    def _is_notification_enabled(self) -> bool:
        return (
            self._enable_notification
            if self._enable_notification is not None
            else (
                config.enable_google_chat_notification
                if "ENABLE_GOOGLE_CHAT_NOTIFICATION" in os.environ
                else bool(self._webhook_url)
            )
        )

    def _resolve_webhook_url_sync(self) -> str:
        """Resolves `self._webhook_url` directly or from a `gs://<bucket>/<object>` URI (cached 300s)."""
        raw = (self._webhook_url or "").strip()
        if not raw:
            return ""
        if raw.startswith("https://chat.googleapis.com/"):
            return raw
        if raw.startswith("gs://"):
            import time
            from .gcs_gateway import GcsStorageGateway, parse_gcs_uri

            now = time.monotonic()
            cached = _WEBHOOK_URI_CACHE.get(raw)
            if cached and (now - cached[0]) < _WEBHOOK_CACHE_TTL_SEC:
                return cached[1]
            try:
                bucket, obj = parse_gcs_uri(raw)
                gw = GcsStorageGateway()
                status, body = gw._request("GET", gw._media_url(bucket, obj))
                if status >= 400:
                    logger.warning(
                        "Failed to read Google Chat webhook secret from %s (HTTP %d)",
                        raw,
                        status,
                    )
                    return ""
                resolved = body.decode("utf-8", errors="replace").strip()
                if not resolved.startswith("https://chat.googleapis.com/"):
                    logger.warning(
                        "Invalid Google Chat webhook URL inside %s (must start with https://chat.googleapis.com/)",
                        raw,
                    )
                    return ""
                _WEBHOOK_URI_CACHE[raw] = (now, resolved)
                return resolved
            except Exception as exc:
                logger.warning("Failed to resolve Google Chat webhook URI %s: %s", raw, exc)
                return ""
        return raw

    async def send_job_start_notification(
        self,
        *,
        user_email: str,
        audit_id: str,
        folder_id: str,
        folder_name: str = "",
        video_count: int = 0,
        total_duration_sec: float = 0.0,
        estimated_minutes: float = 0.0,
        model_version: str = "",
        prompt_version: str = "",
    ) -> bool:
        """Sends a Google Chat start notification when a single-store interactive job starts (`Job ID`)."""
        enabled = self._is_notification_enabled()
        webhook_url = (
            await asyncio.to_thread(self._resolve_webhook_url_sync) if enabled else ""
        )
        if not enabled or not webhook_url:
            logger.info(
                "Google Chat start notification disabled or webhook unconfigured (enabled=%s); skipping push for job %s",
                enabled,
                audit_id,
            )
            return False

        chat_user_id = await asyncio.to_thread(resolve_chat_user_id, user_email)
        initiator_line = f"<code>{user_email}</code>"
        folder_url = (
            build_gcs_console_folder_url(folder_id)
            if is_gcs_target(folder_id)
            else f"https://drive.google.com/drive/folders/{folder_id}"
        )
        store_label = folder_name or folder_id[:16]
        duration_min = round(max(0.0, float(total_duration_sec)) / 60.0, 1)

        buttons: List[dict] = [
            {
                "text": "📁 打开门店视频目录",
                "onClick": {"openLink": {"url": folder_url}},
            }
        ]

        header_title = f"🚀 门店视频 AI 稽核已启动 (#{audit_id})"
        payload: Dict[str, Any] = {
            **(
                {"text": f"🔔 <users/{chat_user_id}> {header_title}"}
                if chat_user_id
                else {}
            ),
            "cardsV2": [
                {
                    "cardId": f"job-start-{audit_id}",
                    "card": {
                        "header": {
                            "title": header_title,
                            "subtitle": f"门店：{store_label} · 共 {video_count} 支视频（约 {duration_min} 分钟）· 预计耗时约 {estimated_minutes:.1f} 分钟",
                        },
                        "sections": [
                            {
                                "widgets": [
                                    {
                                        "decoratedText": {
                                            "topLabel": "任务标识与生效配置",
                                            "text": (
                                                f"<b>发起督导</b>：{initiator_line}<br>"
                                                f"<b>Job ID</b>：<code>{audit_id}</code><br>"
                                                f"<b>模型</b>：<code>{model_version or 'gemini-3.8-flash'}</code> · "
                                                f"<b>SOP</b>：<code>{prompt_version or 'APAC SOP'}</code>"
                                            ),
                                        }
                                    },
                                    {"buttonList": {"buttons": buttons}},
                                ]
                            }
                        ],
                    },
                }
            ],
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(webhook_url, json=payload)
                resp.raise_for_status()
            return True
        except Exception as exc:
            logger.warning("Failed to send Google Chat job start notification: %s", exc)
            return False

    async def send_completion_notification(
        self,
        *,
        user_email: str,
        audit_id: str,
        sheet_url: str,
        violations_count: int,
        total_tokens: int,
    ) -> bool:
        """Sends Google Chat Webhook completion alert with both the HTML Supervisor Workbench and Google Sheet."""
        enabled = self._is_notification_enabled()
        webhook_url = (
            await asyncio.to_thread(self._resolve_webhook_url_sync) if enabled else ""
        )
        if not enabled or not webhook_url:
            logger.info(
                "Google Chat notification disabled or webhook unconfigured (enabled=%s); skipping push for job %s (user=%s)",
                enabled,
                audit_id,
                user_email,
            )
            return False

        chat_user_id = await asyncio.to_thread(resolve_chat_user_id, user_email)
        initiator_line = f"<code>{user_email}</code>"
        portal_url = build_portal_url(job_id=audit_id, user_id=user_email)
        buttons: List[dict] = []
        if portal_url.startswith("http"):
            buttons.append(
                {
                    "text": "🖥️ 打开 HTML 督导复核工作台",
                    "onClick": {"openLink": {"url": portal_url}},
                }
            )
        if sheet_url:
            buttons.append(
                {
                    "text": "📊 打开原 Google Sheet 账单",
                    "onClick": {"openLink": {"url": sheet_url}},
                }
            )

        header_title = f"✅ 门店视频 AI 稽核已完成 (#{audit_id})"
        payload: Dict[str, Any] = {
            **(
                {"text": f"🔔 <users/{chat_user_id}> {header_title}"}
                if chat_user_id
                else {}
            ),
            "cardsV2": [
                {
                    "cardId": f"job-done-{audit_id}",
                    "card": {
                        "header": {
                            "title": header_title,
                            "subtitle": f"共检出 {violations_count} 项待复核事件 · 消耗 {total_tokens:,} Tokens",
                        },
                        "sections": [
                            {
                                "header": "督导 3 秒复核入口（支持页内播放 20s 切片、一键生成 48h 整改单并实时回写 Sheet）",
                                "widgets": [
                                    {
                                        "decoratedText": {
                                            "topLabel": f"Job ID: {audit_id}",
                                            "text": (
                                                f"<b>发起督导</b>：{initiator_line}<br>"
                                                f"<b>待复核违规事件</b>：{violations_count} 项 | "
                                                f"<b>总 Token</b>：{total_tokens:,}"
                                            ),
                                        }
                                    },
                                    *(
                                        [{"buttonList": {"buttons": buttons}}]
                                        if buttons
                                        else []
                                    ),
                                ],
                            }
                        ],
                    },
                }
            ],
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(webhook_url, json=payload)
                resp.raise_for_status()
            return True
        except Exception as exc:
            logger.warning("Failed to send Google Chat webhook notification: %s", exc)
            return False

    async def send_batch_start_notification(
        self,
        *,
        batch_id: str,
        rule_id: str,
        rule_name: str,
        supervisor_email: str,
        root_folder_url: str,
        store_summaries: List[dict],
    ) -> bool:
        """Sends a Google Chat start notification when a scheduled multi-store batch starts (`Batch ID`)."""
        enabled = self._is_notification_enabled()
        webhook_url = (
            await asyncio.to_thread(self._resolve_webhook_url_sync) if enabled else ""
        )
        if not enabled or not webhook_url:
            logger.info(
                "Google Chat batch start notification disabled or webhook unconfigured (enabled=%s); skipping push for batch %s",
                enabled,
                batch_id,
            )
            return False

        chat_user_id = await asyncio.to_thread(resolve_chat_user_id, supervisor_email)
        initiator_line = f"<code>{supervisor_email}</code>"
        total_stores = len(store_summaries)
        running_stores = sum(
            1 for s in store_summaries if str(s.get("state") or "").upper() == "RUNNING"
        )
        failed_stores = total_stores - running_stores
        total_videos = sum(int(s.get("video_count") or 0) for s in store_summaries)

        widgets: List[dict] = [
            {
                "decoratedText": {
                    "topLabel": f"Batch ID: {batch_id}",
                    "text": (
                        f"<b>负责督导</b>：{initiator_line}<br>"
                        f"<b>命中门店</b>：共 {total_stores} 家（已启动 {running_stores} 家 / 预检拦截 {failed_stores} 家）· 合计 {total_videos} 支新增视频"
                    ),
                }
            }
        ]
        if root_folder_url and root_folder_url.startswith("http"):
            widgets.append(
                {
                    "buttonList": {
                        "buttons": [
                            {
                                "text": "📁 打开巡检根目录",
                                "onClick": {"openLink": {"url": root_folder_url}},
                            }
                        ]
                    }
                }
            )

        for idx, st in enumerate(store_summaries, start=1):
            fname = str(st.get("folder_name") or st.get("folder_id") or f"Store-{idx}")
            st_state = str(st.get("state") or "RUNNING").upper()
            v_cnt = int(st.get("video_count") or 0)
            st_job_id = str(st.get("job_id") or "")
            err_msg = str(st.get("error_message") or "").replace("\n", " ")
            skipped_low_res = list(st.get("skipped_low_res_videos") or [])
            skipped_note = (
                f"<br>⚠️ 已自动跳过 {len(skipped_low_res)} 支 &lt;720P 低清视频：{', '.join(skipped_low_res[:3])}"
                if skipped_low_res and st_state == "RUNNING"
                else ""
            )
            detail_txt = (
                f"<b>{fname}</b> — 新增视频 {v_cnt} 支（后台稽核中）{skipped_note}"
                if st_state == "RUNNING"
                else f"<b>{fname}</b> — ❌ 预检拦截：{err_msg[:220] or '详见日志'}"
            )
            widgets.append(
                {
                    "decoratedText": {
                        "topLabel": f"门店 #{idx} · Job ID: {st_job_id} ({st_state})",
                        "text": detail_txt,
                    }
                }
            )

        header_title = f"🚀 定时自动巡检已启动：{rule_name} ({rule_id})"
        payload: Dict[str, Any] = {
            **(
                {"text": f"🔔 <users/{chat_user_id}> {header_title}"}
                if chat_user_id
                else {}
            ),
            "cardsV2": [
                {
                    "cardId": f"scheduled-batch-start-{batch_id}",
                    "card": {
                        "header": {
                            "title": header_title,
                            "subtitle": f"Batch ID: {batch_id} · 共 {total_stores} 家门店 / {total_videos} 支新增视频",
                        },
                        "sections": [
                            {
                                "header": "本批次派发门店清单",
                                "widgets": widgets[:30],
                            }
                        ],
                    },
                }
            ],
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(webhook_url, json=payload)
                resp.raise_for_status()
            return True
        except Exception as exc:
            logger.warning("Failed to send Google Chat batch start notification: %s", exc)
            return False

    async def send_batch_summary_notification(
        self,
        *,
        batch_id: str,
        rule_id: str,
        rule_name: str,
        supervisor_email: str,
        root_folder_url: str,
        store_summaries: List[dict],
    ) -> bool:
        """Sends a single consolidated multi-store Google Chat card when a scheduled batch completes."""
        enabled = self._is_notification_enabled()
        webhook_url = (
            await asyncio.to_thread(self._resolve_webhook_url_sync) if enabled else ""
        )
        if not enabled or not webhook_url:
            logger.info(
                "Google Chat batch notification disabled or webhook unconfigured (enabled=%s); skipping push for batch %s",
                enabled,
                batch_id,
            )
            return False

        chat_user_id = await asyncio.to_thread(resolve_chat_user_id, supervisor_email)
        initiator_line = f"<code>{supervisor_email}</code>"
        total_stores = len(store_summaries)
        succeeded_stores = sum(1 for s in store_summaries if str(s.get("state") or "").upper() == "DONE")
        failed_stores = total_stores - succeeded_stores
        total_videos = sum(int(s.get("video_count") or 0) for s in store_summaries)
        total_violations = sum(int(s.get("violations_found") or 0) for s in store_summaries)
        total_tokens = sum(int(s.get("total_tokens") or 0) for s in store_summaries)
        total_cost_usd = sum(float(s.get("cost_usd") or 0.0) for s in store_summaries)
        batch_portal_url = build_portal_url(batch_id=batch_id)

        widgets: List[dict] = [
            {
                "decoratedText": {
                    "topLabel": f"Batch ID: {batch_id}",
                    "text": (
                        f"<b>负责督导</b>：{initiator_line}<br>"
                        f"<b>覆盖门店</b>：共 {total_stores} 家（成功 {succeeded_stores} 家 / 失败 {failed_stores} 家）· {total_videos} 支视频<br>"
                        f"<b>汇总检出</b>：{total_violations} 项待复核 | <b>总 Token</b>：{total_tokens:,}（约 ${total_cost_usd:.4f}）"
                    ),
                }
            }
        ]
        top_buttons: List[dict] = []
        if batch_portal_url.startswith("http"):
            top_buttons.append(
                {
                    "text": "🌐 打开本批次多门店 HTML 复核工作台",
                    "onClick": {"openLink": {"url": batch_portal_url}},
                }
            )
        if root_folder_url and root_folder_url.startswith("http"):
            top_buttons.append(
                {
                    "text": "📁 打开巡检根目录",
                    "onClick": {"openLink": {"url": root_folder_url}},
                }
            )
        if top_buttons:
            widgets.append({"buttonList": {"buttons": top_buttons}})

        for idx, st in enumerate(store_summaries, start=1):
            fname = str(st.get("folder_name") or st.get("folder_id") or f"Store-{idx}")
            st_state = str(st.get("state") or "DONE").upper()
            st_job_id = str(st.get("job_id") or "")
            v_cnt = int(st.get("video_count") or 0)
            viol_cnt = int(st.get("violations_found") or 0)
            tok_cnt = int(st.get("total_tokens") or 0)
            s_url = str(st.get("report_sheet_url") or "")
            err_msg = str(st.get("error_message") or "").replace("\n", " ")
            skipped_low_res = list(st.get("skipped_low_res_videos") or [])
            skipped_note = (
                f"<br>⚠️ 已自动跳过 {len(skipped_low_res)} 支 &lt;720P 低清视频：{', '.join(skipped_low_res[:3])}"
                if skipped_low_res and st_state == "DONE"
                else ""
            )
            store_portal_url = (
                build_portal_url(batch_id=batch_id, store_job_id=st_job_id)
                if st_job_id
                else ""
            )
            btn_target = (
                store_portal_url
                if store_portal_url.startswith("http")
                else s_url
            )
            btn_label = (
                "🖥️ HTML 复核台"
                if store_portal_url.startswith("http")
                else "📊 打开报告 Sheet"
            )
            if st_state == "DONE":
                row_text = (
                    f"<b>{fname}</b> — 视频 {v_cnt} 支 | 待复核 {viol_cnt} 项 | {tok_cnt:,} Tokens{skipped_note}"
                )
            elif st_state == "REJECTED":
                row_text = (
                    f"<b>{fname}</b> — ❌ <b>预检未通过（0 Token 消耗）</b><br>{err_msg[:220] or '视频分辨率低于 720P 或目录缺少编辑权限'}"
                )
            else:
                row_text = (
                    f"<b>{fname}</b> — ❌ <b>执行异常中断</b><br>{err_msg[:220] or '详见日志'}"
                )
            widgets.append(
                {
                    "decoratedText": {
                        "topLabel": f"门店 #{idx} · Job ID: {st_job_id} ({st_state})",
                        "text": row_text,
                        **(
                            {
                                "button": {
                                    "text": btn_label,
                                    "onClick": {"openLink": {"url": btn_target}},
                                }
                            }
                            if btn_target and st_state == "DONE"
                            else {}
                        ),
                    }
                }
            )

        if succeeded_stores == total_stores:
            header_title = f"✅ 定时巡检已完成：{rule_name} ({rule_id})"
        elif succeeded_stores == 0:
            header_title = f"⚠️ 定时巡检未通过预检/执行异常：{rule_name} ({rule_id})"
        else:
            header_title = f"⚠️ 定时巡检部分完成（含预检拦截）：{rule_name} ({rule_id})"

        payload: Dict[str, Any] = {
            **(
                {"text": f"🔔 <users/{chat_user_id}> {header_title}"}
                if chat_user_id
                else {}
            ),
            "cardsV2": [
                {
                    "cardId": f"scheduled-batch-{batch_id}",
                    "card": {
                        "header": {
                            "title": header_title,
                            "subtitle": f"Batch ID: {batch_id} · 覆盖 {total_stores} 家门店 / {total_videos} 支视频 · 待复核 {total_violations} 项",
                        },
                        "sections": [
                            {
                                "header": "门店巡检结果明细（支持顶部一键切换各门店 HTML 复核台）",
                                "widgets": widgets[:30],
                            }
                        ],
                    },
                }
            ],
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(webhook_url, json=payload)
                resp.raise_for_status()
            return True
        except Exception as exc:
            logger.warning("Failed to send Google Chat batch summary notification: %s", exc)
            return False

    async def send_eval_start_notification(
        self,
        *,
        eval_id: str,
        eval_name: str,
        run_id: str,
        prompt_tab_name: str,
        model_version: str,
        supervisor_email: str,
        golden_videos_folder_url: str = "",
        human_labels_sheet_url: str = "",
        results_folder_url: str = "",
    ) -> bool:
        """Sends a Google Chat start card when a `Tab3_黄金集评测工作台` row transitions `START_EVAL -> RUNNING`."""
        enabled = self._is_notification_enabled()
        webhook_url = (
            await asyncio.to_thread(self._resolve_webhook_url_sync) if enabled else ""
        )
        if not enabled or not webhook_url:
            logger.info(
                "Google Chat eval start notification disabled or webhook unconfigured (enabled=%s); skipping push for %s.",
                enabled,
                eval_id,
            )
            return False

        chat_user_id = await asyncio.to_thread(resolve_chat_user_id, supervisor_email)
        initiator_line = f"<code>{supervisor_email or '督导'}</code>"

        widgets: list[dict[str, Any]] = [
            {
                "decoratedText": {
                    "topLabel": "评测任务 / 负责人",
                    "text": f"<b>{eval_name} ({eval_id})</b> · <b>负责督导</b>：{initiator_line}",
                }
            },
            {
                "decoratedText": {
                    "topLabel": "待测 SOP 分表 / 模型版本",
                    "text": f"SOP: <code>{prompt_tab_name}</code> · 模型: <code>{model_version}</code>",
                }
            },
        ]
        buttons: list[dict[str, Any]] = []
        if golden_videos_folder_url and golden_videos_folder_url.startswith("https://"):
            buttons.append(
                {
                    "text": "🎬 打开黄金集视频目录",
                    "onClick": {"openLink": {"url": golden_videos_folder_url}},
                }
            )
        if human_labels_sheet_url and human_labels_sheet_url.startswith("https://"):
            buttons.append(
                {
                    "text": "📋 打开人工标注表",
                    "onClick": {"openLink": {"url": human_labels_sheet_url}},
                }
            )
        if results_folder_url and results_folder_url.startswith("https://"):
            buttons.append(
                {
                    "text": "📂 打开测评报告归档目录",
                    "onClick": {"openLink": {"url": results_folder_url}},
                }
            )
        if buttons:
            widgets.append({"buttonList": {"buttons": buttons}})

        header_title = f"🚀 黄金集评测已启动：{eval_name} ({eval_id})"
        payload: Dict[str, Any] = {
            **(
                {"text": f"🔔 <users/{chat_user_id}> {header_title}"}
                if chat_user_id
                else {}
            ),
            "cardsV2": [
                {
                    "cardId": f"scheduled-eval-start-{run_id or eval_id}",
                    "card": {
                        "header": {
                            "title": header_title,
                            "subtitle": f"Run ID: {run_id} · SOP: {prompt_tab_name} · 模型: {model_version}",
                        },
                        "sections": [
                            {
                                "header": "⏳ 正在后台 24h Cloud Run Job 执行黄金集评测（完成后将自动推送召回率与 5-Tab 报告链接）",
                                "widgets": widgets,
                            }
                        ],
                    },
                }
            ],
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(webhook_url, json=payload)
                resp.raise_for_status()
            return True
        except Exception as exc:
            logger.warning("Failed to send Google Chat eval start notification: %s", exc)
            return False

    async def send_eval_completion_notification(
        self,
        *,
        eval_id: str,
        eval_name: str,
        run_id: str,
        prompt_tab_name: str,
        model_version: str,
        supervisor_email: str,
        status: str,
        summary: dict[str, Any] | None = None,
        error_message: str = "",
    ) -> bool:
        """Sends a Google Chat card notification for a Sheet-triggered golden-set evaluation (`Tab3_黄金集评测工作台`)."""
        enabled = self._is_notification_enabled()
        webhook_url = (
            await asyncio.to_thread(self._resolve_webhook_url_sync) if enabled else ""
        )
        if not enabled or not webhook_url:
            logger.info(
                "Google Chat eval notification disabled or webhook unconfigured (enabled=%s); skipping push for %s.",
                enabled,
                eval_id,
            )
            return False

        chat_user_id = await asyncio.to_thread(resolve_chat_user_id, supervisor_email)
        initiator_line = f"<code>{supervisor_email or '督导'}</code>"

        is_done = str(status).upper() == "DONE"
        summary = summary or {}
        sheet_url = str(summary.get("spreadsheet_url") or "")
        overall_recall = float(summary.get("overall_recall", 0.0)) * 100.0
        confirmed_only_recall = float(summary.get("confirmed_only_recall", summary.get("overall_recall", 0.0))) * 100.0
        findings_per_clip = float(summary.get("findings_per_clip", 0.0))
        hit_rate = float(summary.get("hit_rate", 0.0)) * 100.0
        total_cost_usd = float(summary.get("total_cost_usd", 0.0))
        total_tokens = int(summary.get("total_tokens", 0))
        guardrails_ok = bool(summary.get("guardrails_passed", False))

        widgets: list[dict[str, Any]] = [
            {
                "decoratedText": {
                    "topLabel": "评测负责人 / 配方版本",
                    "text": f"<b>负责督导</b>：{initiator_line} · SOP: <code>{prompt_tab_name}</code> · 模型: <code>{model_version}</code>",
                }
            },
        ]
        if is_done:
            widgets.append(
                {
                    "decoratedText": {
                        "topLabel": "核心召回率指标 (Recall)",
                        "text": (
                            f"总体召回率: <b>{overall_recall:.1f}%</b> | "
                            f"仅 CONFIRMED 召回率: <b>{confirmed_only_recall:.1f}%</b>"
                        ),
                    }
                }
            )
            widgets.append(
                {
                    "decoratedText": {
                        "topLabel": "护栏与成本指标 (Guardrails & FinOps)",
                        "text": (
                            f"护栏状态: <b>{'✅ 通过' if guardrails_ok else '⚠️ 触发护栏'}</b> | "
                            f"告警密度: <b>{findings_per_clip:.2f} 条/段</b> | "
                            f"片段命中率: <b>{hit_rate:.1f}%</b> | "
                            f"成本: <b>${total_cost_usd:.2f}</b> ({total_tokens:,} Tokens)"
                        ),
                        **(
                            {
                                "button": {
                                    "text": "📊 打开 5-Tab 黄金集测评报告 Sheet",
                                    "onClick": {"openLink": {"url": sheet_url}},
                                }
                            }
                            if sheet_url
                            else {}
                        ),
                    }
                }
            )
        else:
            widgets.append(
                {
                    "decoratedText": {
                        "topLabel": "失败原因 (Failure Details)",
                        "text": f"❌ <b>{(error_message or '评测任务执行异常')[:260]}</b>",
                    }
                }
            )

        header_title = (
            f"✅ 黄金集评测已完成：{eval_name} ({eval_id})"
            if is_done
            else f"⚠️ 黄金集评测未通过/执行异常：{eval_name} ({eval_id})"
        )
        payload: Dict[str, Any] = {
            **(
                {"text": f"🔔 <users/{chat_user_id}> {header_title}"}
                if chat_user_id
                else {}
            ),
            "cardsV2": [
                {
                    "cardId": f"scheduled-eval-{run_id or eval_id}",
                    "card": {
                        "header": {
                            "title": header_title,
                            "subtitle": f"Run ID: {run_id} · SOP: {prompt_tab_name} · 模型: {model_version}",
                        },
                        "sections": [
                            {
                                "header": "黄金集评测结果摘要（零污染源视频目录，已归档至专属测评文件夹）",
                                "widgets": widgets,
                            }
                        ],
                    },
                }
            ],
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(webhook_url, json=payload)
                resp.raise_for_status()
            return True
        except Exception as exc:
            logger.warning("Failed to send Google Chat eval notification: %s", exc)
            return False
