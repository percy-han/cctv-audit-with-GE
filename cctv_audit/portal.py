"""REQ-015: Dynamic HTML Supervisor Audit Portal & Sheet Write-Back (`portal.py`).

Provides:
1. HMAC-SHA256 URL signing and verification (`build_portal_url`, `verify_portal_signature`) so
   supervisors clicking links from Google Chat can open `/portal` directly in their browser
   without a GFE 403 while preventing unauthorized access or ID enumeration (`[P0-2]`).
2. Single-store (`/portal?job={job_id}&user={user_id}&exp={exp}&sig={sig}`) and Multi-store
   Scheduled Batch (`/portal?batch={batch_id}&exp={exp}&sig={sig}&store={store_job_id}`) data
   hydration from the in-folder Google Sheet (or `.xlsx`) + `AuditJob` / `ScheduledBatchRecord`.
3. Composite-key human review write-back (`apply_portal_reviews` -> `POST /api/portal/review`)
   that updates `Tab 1` Columns J (`人工复核状态`) and K (`人工复核备注`) in the source Google
   Sheet without row-index drift (`[P0-1]`).
4. Full self-contained HTML rendering (`render_supervisor_portal_html`) preserving the approved
   CHAGEE Supervisor Workbench UI (`chagee_supervisor_audit_portal.html`) plus the multi-store
   Batch Store Switcher (`[P1-2]`).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import json
import logging
import os
from pathlib import Path
import re
import time
import urllib.parse
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .agentic_auditor import Status
from .config import config
from .gcs_gateway import (
    build_gcs_console_folder_url,
    build_incident_composite_key,
    build_source_video_url,
    is_gcs_target,
)
from .jobs import AuditJob
from .portal_auth import DriveAccessChecker, denied_message, filter_visible_stores, require_store_access
from .review_audit_log import (
    PHASE_REQUESTED,
    PHASE_SHEET_WRITE_FAILED,
    ReviewAuditLogError,
    audit_log_bucket,
    build_review_audit_record,
    write_review_audit_records,
)

INITIAL_REVIEW_STATUS: str = "PENDING"

logger = logging.getLogger("cctv_audit.portal")

DEFAULT_PORTAL_TTL_SEC: int = 30 * 86400  # 30 days


def get_portal_ttl_sec() -> int:
    """Returns the portal URL TTL in seconds (default 30 days, overridable via `PORTAL_URL_TTL_SEC`)."""
    raw = os.environ.get("PORTAL_URL_TTL_SEC", "").strip()
    if raw:
        try:
            val = int(raw)
            if val > 0:
                return val
        except ValueError:
            pass
    return DEFAULT_PORTAL_TTL_SEC


PORTAL_STATUS_TO_SHEET_LABEL: Dict[str, str] = {
    "confirmed": "✅ CONFIRMED",
    "false_pos": "❌ FALSE_POSITIVE",
    "followup": "⚠️ FOLLOWUP",
    "pending": "PENDING",
    "CONFIRMED_VIOLATION": "✅ CONFIRMED",
    "CONFIRMED": "✅ CONFIRMED",
    "DISMISSED": "❌ FALSE_POSITIVE",
    "FALSE_POSITIVE": "❌ FALSE_POSITIVE",
    "FALSE_POS": "❌ FALSE_POSITIVE",
    "FOLLOWUP": "⚠️ FOLLOWUP",
    "PENDING": "PENDING",
}

_TAB1_CANDIDATE_TITLES: Tuple[str, ...] = (
    "Tab1_Violation_Review",
    "违规事件 3 秒复核台",
    "Violation_Review",
    "Findings",
    "违规明细",
)

_TAB2_CANDIDATE_TITLES: Tuple[str, ...] = (
    "Tab2_Token_Ledger",
    "本次视频 Token 消耗与耗时账单",
    "Token_Ledger",
    "Token账单",
)

_DETECTED_BASE_URL: str = ""


def set_detected_base_url(base_url: str) -> None:
    """Caches the external Cloud Run `.run.app` base URL detected from incoming HTTP requests."""
    global _DETECTED_BASE_URL
    cleaned = (base_url or "").strip().rstrip("/")
    if not cleaned:
        return
    if cleaned.startswith("http://") and ".run.app" in cleaned:
        cleaned = "https://" + cleaned[len("http://") :]
    if "localhost" in cleaned or "127.0.0.1" in cleaned:
        if _DETECTED_BASE_URL:
            return
    _DETECTED_BASE_URL = cleaned


def get_portal_base_url() -> str:
    """Resolves the base URL for `/portal` links in Google Chat notifications."""
    for env_key in ("PORTAL_BASE_URL", "CLOUD_RUN_SERVICE_URL", "CLOUD_RUN_WORKER_URL"):
        val = os.environ.get(env_key, "").strip().rstrip("/")
        if val:
            return val
    if _DETECTED_BASE_URL:
        return _DETECTED_BASE_URL
    return ""


def _portal_hmac_key() -> bytes:
    secret = os.environ.get("PORTAL_HMAC_SECRET", "").strip()
    if not secret:
        secret = (
            f"{config.staging_bucket or 'chagee-staging'}:"
            f"{config.gcp_project or 'chagee-project'}:"
            f"{config.master_prompt_sheet_id or 'chagee-sop'}:"
            "chagee-supervisor-portal-v1"
        )
    return secret.encode("utf-8")


def compute_portal_signature(
    *,
    job_id: str = "",
    user_id: str = "",
    batch_id: str = "",
    exp: int,
) -> str:
    """Computes the HMAC-SHA256 hex digest over the canonical portal target and expiration."""
    if batch_id:
        canonical = f"batch:{batch_id.strip()}:{int(exp)}"
    else:
        canonical = f"job:{job_id.strip()}:{user_id.strip().lower()}:{int(exp)}"
    return hmac.new(_portal_hmac_key(), canonical.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_portal_signature(
    *,
    job_id: str = "",
    user_id: str = "",
    batch_id: str = "",
    exp: int,
    sig: str,
    now: Optional[float] = None,
) -> Tuple[bool, str]:
    """Constant-time verification of the portal HMAC signature and expiration timestamp."""
    if not sig or not exp:
        return False, "Missing access signature parameters (sig / exp). Please click the link from Google Chat again. / 缺少访问签名参数（sig / exp），请从 Google Chat 通知卡片重新点击进入。"
    if not batch_id and not job_id:
        return False, "Missing job or batch identifier. / 缺少任务标识（job 或 batch 参数）。"
    now_ts = int(now if now is not None else time.time())
    if int(exp) < now_ts:
        return False, "This supervisor workbench link has expired (30-day TTL). / 该督导复核工作台链接已过期（有效期 30 天），请从最新通知或报告进入。"
    expected = compute_portal_signature(
        job_id=job_id,
        user_id=user_id,
        batch_id=batch_id,
        exp=int(exp),
    )
    cleaned_sig = sig.strip().rstrip(")*>]'\" ").lower()
    if not hmac.compare_digest(expected, cleaned_sig):
        return False, "Access signature verification failed (HMAC mismatch). / 访问签名校验未通过（HMAC 签名不匹配）。"
    return True, ""


def build_portal_url(
    *,
    job_id: str = "",
    user_id: str = "",
    batch_id: str = "",
    store_job_id: str = "",
    ttl_sec: Optional[int] = None,
    base_url: Optional[str] = None,
    now: Optional[float] = None,
) -> str:
    """Builds a signed `/portal?...` URL for either a single-store `job_id` or a multi-store `batch_id`."""
    resolved_base = (base_url if base_url is not None else get_portal_base_url()).strip().rstrip("/")
    effective_ttl = int(ttl_sec if ttl_sec is not None else get_portal_ttl_sec())
    exp = int(now if now is not None else time.time()) + effective_ttl
    sig = compute_portal_signature(
        job_id=job_id,
        user_id=user_id,
        batch_id=batch_id,
        exp=exp,
    )
    params: Dict[str, str] = {}
    if batch_id:
        params["batch"] = batch_id.strip()
        if store_job_id:
            params["store"] = store_job_id.strip()
    else:
        params["job"] = job_id.strip()
        if user_id:
            params["user"] = user_id.strip()
    params["exp"] = str(exp)
    params["sig"] = sig
    qs = urllib.parse.urlencode(params)
    return f"{resolved_base}/portal?{qs}" if resolved_base else f"/portal?{qs}"


def map_sheet_status_to_portal_code(raw_status: str) -> str:
    """Maps Google Sheet Column J text back to the portal's 4 state codes, supporting both English and legacy Chinese."""
    text = (raw_status or "").strip()
    upper = text.upper()
    if (
        not text
        or "PENDING_HUMAN_REVIEW" in upper
        or upper == "PENDING"
        or "待人工复核" in text
        or "待复核" in text
    ):
        return "PENDING"
    if (
        "FALSE_POSITIVE" in upper
        or "FALSE_POS" in upper
        or "DISMISSED" in upper
        or "误报" in text
        or "排除" in text
    ):
        return "DISMISSED"
    if (
        "FOLLOWUP" in upper
        or "FOLLOW-UP" in upper
        or "FOLLOW_UP" in upper
        or "后续录像" in text
        or "待调" in text
        or "待跟进" in text
    ):
        return "FOLLOWUP"
    if "CONFIRMED" in upper or "确认违规" in text or "属实" in text:
        return "CONFIRMED_VIOLATION"
    return "PENDING"


def map_portal_code_to_sheet_status(portal_code: str) -> str:
    """Maps a portal decision code (`confirmed`/`false_pos`/`followup`/`pending` or `CONFIRMED_VIOLATION`/`DISMISSED`/`FOLLOWUP`/`PENDING`) to Sheet Column J."""
    raw = (portal_code or "").strip()
    for key in (raw, raw.lower(), raw.upper()):
        if key in PORTAL_STATUS_TO_SHEET_LABEL:
            return PORTAL_STATUS_TO_SHEET_LABEL[key]
    mapped = map_sheet_status_to_portal_code(raw)
    return PORTAL_STATUS_TO_SHEET_LABEL.get(mapped, INITIAL_REVIEW_STATUS)


MODULE_META: Dict[str, Dict[str, Any]] = {
    "A": {
        "order": 1,
        "kicker": "MODULE A",
        "name": "Personal Hygiene & Handwashing",
        "name_en": "Personal Hygiene & Handwashing",
        "name_zh": "个人卫生与洗手池规范",
        "title": "Personal Hygiene & Handwashing",
        "title_en": "Personal Hygiene & Handwashing",
        "title_zh": "洗手与手部卫生",
        "desc": "Mandatory handwashing triggers (5-minute tracking window), 5-step handwashing verification, and disposable glove hygiene (APAC Parameters 1.5.1–1.5.5 & 1.6).",
        "desc_en": "Mandatory handwashing triggers (5-minute tracking window), 5-step handwashing verification, and disposable glove hygiene (APAC Parameters 1.5.1–1.5.5 & 1.6).",
        "desc_zh": "强制洗手触发场景、五步洗手法核对与一次性手套规范。对应 APAC 参数表 1.5.1–1.5.5 与 1.6，是污染链条的源头控制项。",
    },
    "B": {
        "order": 2,
        "kicker": "MODULE B",
        "name": "Ice Maker Cleaning & Sanitizing",
        "name_en": "Ice Maker Cleaning & Sanitizing",
        "name_zh": "制冰机拆洗与消毒规范",
        "title": "Ice Maker Cleaning & Sanitizing",
        "title_en": "Ice Maker Cleaning & Sanitizing",
        "title_zh": "制冰机与冰块管理",
        "desc": "Weekly ice maker disassembly & sanitizing workflow, contact time lockouts (5min/10min), KAY-5 chemical verification, 3-color towel separation, and ice scoop storage (SOP 8 / SOP 21 & Parameters 3.1 / 4.1 / 4.5).",
        "desc_en": "Weekly ice maker disassembly & sanitizing workflow, contact time lockouts (5min/10min), KAY-5 chemical verification, 3-color towel separation, and ice scoop storage (SOP 8 / SOP 21 & Parameters 3.1 / 4.1 / 4.5).",
        "desc_zh": "制冰机周清洁流程、部件静置锁定期、化学品与三色毛巾专用，以及冰铲取冰与存放规范。对应 SOP 8 / SOP 21 与参数表 3.1 / 4.1 / 4.5。",
    },
    "C": {
        "order": 3,
        "kicker": "MODULE C",
        "name": "Counter & Utensil Sanitation",
        "name_en": "Counter & Utensil Sanitation",
        "name_zh": "吧台器具与抹布消毒桶",
        "title": "Counter & Utensil Sanitation",
        "title_en": "Counter & Utensil Sanitation",
        "title_zh": "常态红线 · 仪容 · 泡茶设备",
        "desc": "Zero-tolerance cross-contamination scan, single-use cup & label rules, on-duty conduct, grooming & personal phone rules, waste bin closure, and automated tea maker icing (APAC Parameters 1.1–1.4, 4.x, 6.x–9.x & SOP 6.2).",
        "desc_en": "Zero-tolerance cross-contamination scan, single-use cup & label rules, on-duty conduct, grooming & personal phone rules, waste bin closure, and automated tea maker icing (APAC Parameters 1.1–1.4, 4.x, 6.x–9.x & SOP 6.2).",
        "desc_zh": "每周必跑的零容忍红线扫描：交叉污染、杯具、不当行为、仪容着装与私人手机，以及泡茶机出茶入冰时效。",
    },
    "D": {
        "order": 4,
        "kicker": "MODULE D",
        "name": "Beverage Prep & Store Environment",
        "name_en": "Beverage Prep & Store Environment",
        "name_zh": "茶汤制备与门店环境",
        "title": "Beverage Prep & Store Environment",
        "title_en": "Beverage Prep & Store Environment",
        "title_zh": "运营与人效 (IPLH)",
        "desc": "4-station closed-loop cup output counting, store & individual IPLH calculation, peak/off-peak staffing deployment, and beverage recipe & jigger/shaker compliance.",
        "desc_en": "4-station closed-loop cup output counting, store & individual IPLH calculation, peak/off-peak staffing deployment, and beverage recipe & jigger/shaker compliance.",
        "desc_zh": "有效出杯计数、每工时出杯率核算、人力投放合理性与出品规范抽查。",
    },
    "OTHER": {
        "order": 9,
        "kicker": "MODULE OTHER",
        "name": "General SOP Audit",
        "name_en": "General SOP Audit",
        "name_zh": "其他专项稽核",
        "title": "General SOP Audit",
        "title_en": "General SOP Audit",
        "title_zh": "专项测试与扩展条款",
        "desc": "Targeted spot-checks and extended SOP audit clauses.",
        "desc_en": "Targeted spot-checks and extended SOP audit clauses.",
        "desc_zh": "自定义专项抽检与扩展 SOP 条款。",
    },
}


_DEFAULT_DOC_REF_BY_PREFIX: Dict[str, str] = {
    "A1": "APAC Cloud Audit Parameters 2026 v1.0 · Table 1.5.5 & Reference Case 1",
    "A2": "APAC Cloud Audit Parameters 2026 v1.0 · Table 1.5.3 / 1.5.5",
    "A3": "APAC Cloud Audit Parameters 2026 v1.0 · Table 1.5.1–1.5.4",
    "A4": "APAC Cloud Audit Parameters 2026 v1.0 · Table 1.6",
    "A5": "APAC Cloud Audit Parameters 2026 v1.0 · Table 1.5.2 (2h Cycle)",
    "B0": "SOP 8 (Manitowoc) & SOP 21 (Langtuo)",
    "B1-1": "21. SOP Ice Maker Machine (Model: Langtuo) APAC 18082025",
    "B1-2": "8. SOP Manitowoc Ice Maker Machine APAC 06052026",
    "B2": "SOP 8 Step 7–12 & SOP 21 Step 7–19",
    "B3": "APAC Cloud Audit Parameters 2026 v1.0 · '0' Tolerance & SOP 8/21",
    "B4": "APAC Cloud Audit Parameters 2026 v1.0 · Table 3.1 & SOP 8/21",
    "B5": "APAC Cloud Audit Parameters 2026 v1.0 · Table 4.1 / 4.5",
    "C1": "APAC Cloud Audit Parameters 2026 v1.0 · '0' Tolerance Cross-Contamination",
    "C2": "APAC Cloud Audit Parameters 2026 v1.0 · '0' Tolerance Cup Hygiene",
    "C3": "APAC Cloud Audit Parameters 2026 v1.0 · '0' Tolerance Misconduct",
    "C4": "APAC Cloud Audit Parameters 2026 v1.0 · Table 1.1–1.4",
    "C5": "APAC Cloud Audit Parameters 2026 v1.0 · Table 4.3 / 4.4",
    "C6": "APAC Cloud Audit Parameters 2026 v1.0 · Table 6.x / 7.x / 8.x",
    "C7": "APAC Cloud Audit Parameters 2026 v1.0 · Table 9.x",
    "C8": "SOP 6.2 Automated Tea Maker Machine",
    "D1": "APAC Cloud Audit Reference v2 · Week 3 IPLH Module",
    "D2": "APAC Cloud Audit Reference v2 · Week 3 IPLH Module",
    "D3": "APAC Cloud Audit Reference v2 · Week 3 IPLH Module",
    "D4": "APAC Cloud Audit Parameters 2026 v1.0 · Table 8.4",
}

_CLEAN_SUBTITLE_BY_RULE: Dict[str, str] = {
    "A1": "Mandatory Handwashing Triggers (5-Min Tracking Window)",
    "A2": "Touching Food/Utensils or Donning Gloves After Contamination Without Handwashing",
    "A3": "Standard 5-Step Handwashing Sequence (Wet -> Soap -> Scrub >=20s -> Rinse -> Paper Towel Dry)",
    "A4": "Disposable Glove Wearing & Immediate Replacement After Contamination",
    "A5": "Scheduled Every-2-Hour On-Duty Handwashing Compliance",
    "B0": "Ice Maker Model Identification & 5 Detachable Core Parts Visual Verification",
    "B1-1": "Langtuo Split Ice Maker 15-Step Weekly Cleaning & Sanitizing Procedure",
    "B1-2": "Manitowoc Ice Maker 13-Step Weekly Cleaning & Self-Cleaning Cycle",
    "B2": "Ice Maker 5-Part Sanitizer Contact Time (5min/10min) & Full Perimeter Edge Coverage",
    "B3": "Ice Maker Chemical Bottle Verification (White/Clear KAY-5 Only; Yellow Bottle Prohibited)",
    "B4": "3-Color Towel Separation, Sanitizer Bucket Origin & Perimeter Frame Wiping",
    "B5": "Ice Scoop Dedicated Holder Storage & Sanitary Ice Scooping",
    "C1": "Zero-Tolerance Cross-Contamination Red Line (Dedicated Sinks / No Bare-Hand Contact / Cup Interior)",
    "C2": "Single-Use Customer Cups & Single-Label Compliance",
    "C3": "Zero-Tolerance On-Duty Misconduct (No Smoking / Gaming / Sleeping / Eating in Prep Area)",
    "C4": "Staff Grooming (Hairnet/Cap/Mask/Apron), Personal Items & No Personal Phone in Prep Area",
    "C5": "Step-On Trash Bin Lid Closure & Floor Hygiene (No Overflow or Standing Water >15 Min)",
    "C6": "Automated Tea Maker, Steamer, Blender & Chiller Door Operation & Maintenance",
    "C7": "Customer Service Etiquette (Greeting Within 10s, Two-Handed Handover, Receipt Check & Sealing)",
    "C8": "Tea Maker Dispensing Over Ice (Pre-Ice 2–3 Min Ahead) & Stirring Within 30s",
    "D1": "4-Station Closed-Loop Valid Cup Output Count (Label/Scan -> Ice/Shake -> Pour -> Seal/Serve)",
    "D2": "Store Total IPLH (Drinks Per Labor Hour) & Individual Hourly Output Calculation",
    "D3": "Peak vs. Off-Peak Staffing Deployment Assessment",
    "D4": "Beverage Recipe & Jigger/Shaker Compliance Spot-Check (Min 3 Cups)",
}

_CLEAN_SUBTITLE_ZH_BY_RULE: Dict[str, str] = {
    "A1": "强制洗手触发场景（5 分钟追踪监视窗）",
    "A2": "污染后未洗手直接戴手套或接触食品器具（三要素红线）",
    "A3": "标准五步洗手流程与首尾动作核对（湿手→皂液→搓洗≥20s→冲净→纸巾擦干）",
    "A4": "一次性手套佩戴与污染后立即更换规范",
    "A5": "在岗每 2 小时周期性洗手规范",
    "B0": "制冰机机型识别与五大核心可拆部件视觉核验",
    "B1-1": "Langtuo 双门立式分体制冰机周清洁 15 步完整流程",
    "B1-2": "Manitowoc 制冰机周清洁 13 步完整流程与自清洁循环",
    "B2": "制冰机五大部件消毒液停留静置时长（5min/10min）与四周侧面边缘全覆盖",
    "B3": "制冰机化学品容器视觉鉴别（仅限白底透明瓶 KAY-5 消毒液，严禁黄瓶多功能清洁剂）",
    "B4": "三色毛巾专布专用、消毒桶取用起点溯源与四周边框擦拭",
    "B5": "冰铲专用收纳盒存放与规范取冰（严禁冰铲留置冰仓或平放台面）",
    "C1": "零容忍交叉污染红线扫描（专槽专用/禁徒手抓冰果/禁碰客用杯内壁）",
    "C2": "客用杯具一次性使用与单张标签规范",
    "C3": "在岗零容忍不当行为（禁吸烟/玩牌/睡觉/备餐区饮食/冷落顾客）",
    "C4": "员工仪容着装（发网/帽/口罩/围裙）、个人物品存放与严禁在岗使用私人手机",
    "C5": "脚踏垃圾桶闭合与地面卫生（严禁溢满或积水超 15 分钟）",
    "C6": "自动泡茶机、蒸汽机、混合机与冷藏门操作维护规范",
    "C7": "顾客服务礼仪（10 秒内主动迎宾、双手递交、核对小票与出品密封）",
    "C8": "泡茶机出茶入冰（提前 2–3 分钟备冰）与出茶后 30 秒内搅拌冷却",
    "D1": "四工位完整闭环有效出杯计数（贴标扫码→加冰摇混→入杯→封口出餐）",
    "D2": "门店总 IPLH（每工时出杯数）与个人小时出杯率核算",
    "D3": "高平峰时段人力排班投放合理性评估",
    "D4": "出品制作配方与量杯/雪克杯规范抽查（至少 3 杯）",
}

_DEFAULT_PASS_EN_BY_RULE: Dict[str, str] = {
    "A1": "Within 5 minutes of touching hair/face/mask/phone/trash bin/cleaning tools, staff must complete the full 5-step handwashing procedure at the dedicated hand sink.",
    "A2": "After any contamination trigger, staff must wash hands at the handwashing sink before putting on new gloves or touching any food-contact utensils.",
    "A3": "Follow all 5 handwashing steps in order: wet hands with running water -> apply soap -> scrub foam across palms/backs/fingers/thumbs/wrists for >=20s -> rinse thoroughly -> dry with single-use paper towel.",
    "A4": "Wear disposable gloves throughout ready-to-eat food/ice handling and replace immediately after handwashing or any contamination.",
    "A5": "All on-duty staff complete scheduled handwashing at least once every 2 hours.",
    "B0": "Correctly identify ice maker model (Langtuo vs. Manitowoc) and visually verify all 5 core detachable parts during weekly cleaning.",
    "B1-1": "Execute all 15 steps of SOP 21 for Langtuo ice makers: power off, remove parts, spray KAY-5 and soak >=10 min, pour 8L sanitizer, and close doors.",
    "B1-2": "Execute all 13 steps of SOP 8 for Manitowoc ice makers: switch OFF, remove 3 internal parts, spray KAY-5 and soak >=5 min, pour 3L sanitizer, and run self-clean cycle.",
    "B2": "Ensure sanitizer sits undisturbed on all disassembled ice maker parts for the full required duration (Manitowoc >=5 min / Langtuo >=10 min) and wipe all 4 perimeter edges.",
    "B3": "Use only colorless KAY-5 sanitizer in the white/clear spray bottle for ice maker sanitizing; never use yellow multi-purpose cleaner bottles.",
    "B4": "Use a clean red-trimmed towel taken directly from the labeled red sanitizer bucket to wipe ice maker interior, water distributor, curtain, and door frame edges.",
    "B5": "Retrieve ice scoop from the covered dedicated holder, wear disposable gloves while scooping ice, and return the scoop to the holder immediately after use.",
    "C1": "Strictly separate handwashing, food-prep, and mop sinks; never touch ice/fruit with bare hands or touch the inner wall/rim of customer cups.",
    "C2": "Use each customer cup once only, apply a single order label cleanly, and never place receipts or pens inside food containers.",
    "C3": "Maintain professional conduct in prep and service areas; no smoking, gaming, sleeping, eating/drinking in prep zones, or ignoring customers.",
    "C4": "Wear hairnet, cap, mask covering nose and mouth, and clean apron properly (hang removed aprons in designated area); never use personal phones in prep/bar areas.",
    "C5": "Keep step-on trash bin lids closed, tie temporary trash bags, and clear any floor spills or overflow within 15 minutes.",
    "C6": "Wipe automated tea maker nozzles/latches with dedicated towel before and after brewing, purge steam wands, and close chiller doors promptly.",
    "C7": "Greet arriving customers within 10 seconds, verify receipts, seal drinks properly, and hand over orders with both hands.",
    "C8": "Prepare ice bucket 2–3 minutes before tea dispensing and stir brewed tea over ice within 30 seconds of completion.",
    "D1": "Complete all 4 beverage station steps (label/scan -> ice/shake -> pour -> seal/handover) for each valid cup counted.",
    "D2": "Achieve target store IPLH and individual hourly beverage output benchmarks.",
    "D3": "Align on-duty staffing headcount and station assignments with peak and off-peak order volume.",
    "D4": "Follow standard recipe cards using graduated jiggers and shaker cups across at least 3 spot-checked beverages.",
}

_DEFAULT_FAIL_EN_BY_RULE: Dict[str, str] = {
    "A1": "Failing to wash hands within 5 minutes after touching hair/face/mask/phone/trash bin/cleaning tools, or resuming prep without handwashing.",
    "A2": "Putting on gloves or touching food/utensils directly after a contamination event without first completing handwashing (Red Line).",
    "A3": "Skipping wet-hands step (dry soap application), scrubbing foam for <20s, washing with gloves on, or drying hands on apron/clothes.",
    "A4": "Handling ready-to-eat ingredients without gloves, reusing contaminated gloves, or failing to change gloves after touching non-food surfaces.",
    "A5": "No scheduled handwashing observed for on-duty staff across a 2-hour continuous window.",
    "B0": "Misidentifying ice maker model or failing to disassemble and show all required detachable parts.",
    "B1-1": "Skipping power-off, failing to soak parts for >=10 minutes, omitting 8L sanitizer pour, or leaving Langtuo doors open during sanitizing.",
    "B1-2": "Cleaning with power ON, soaking parts for <5 minutes, or skipping the 3L sanitizer self-cleaning cycle on Manitowoc ice makers.",
    "B2": "Rinsing or wiping sprayed ice maker parts before the 5-min / 10-min sanitizer contact lock period expires, or missing the 4 side perimeter edges.",
    "B3": "Spraying or cleaning the ice maker with a yellow bottle (multi-purpose detergent) or unapproved chemical container (Zero-Tolerance Red Line).",
    "B4": "Wiping ice maker interior/parts with a yellow/blue towel, an un-sanitized towel from the counter, or missing perimeter frame wiping.",
    "B5": "Leaving the ice scoop buried in the ice bin, laying it flat on top of the machine/counter, or scooping ice without gloves.",
    "C1": "Washing mops/cleaning tools or hands in food/utensil sinks, grabbing ice/garnishes with bare hands, or fingers touching the inside of customer cups.",
    "C2": "Reusing customer cups, stacking wet cups improperly, or placing receipts/pens inside cups or food containers.",
    "C3": "Smoking, playing cards/games, sleeping, eating/drinking in the bar/prep zone, or leaving customers unattended.",
    "C4": "Missing hairnet/cap, mask pulled below nose/chin, apron placed on food counter/equipment, or using a personal mobile phone in the work zone.",
    "C5": "Trash bin lid left open, overflowing waste, or standing water on the floor for more than 15 minutes.",
    "C6": "Failing to wipe tea maker outlet/latch, leaving chiller/freezer doors ajar, or improper blender/steamer sanitation.",
    "C7": "Ignoring customers for >10 seconds at the front counter, single-handed drink handover, or unsealed packaging.",
    "C8": "Dispensing hot tea without pre-prepared ice or failing to stir tea with ice within 30 seconds after dispensing.",
    "D1": "Incomplete station loop or unverified cup output count.",
    "D2": "Store or individual IPLH below benchmark threshold.",
    "D3": "Under-staffing during peak queue buildup or idle over-staffing during off-peak hours.",
    "D4": "Free-pouring ingredients without a jigger or deviating from standard shaker/recipe steps.",
}

_DEFAULT_RECTIFICATION_BY_RULE: Dict[str, str] = {
    "A1": "[48h Rectification] Retrain involved staff on mandatory handwashing triggers (must complete 5-step handwashing within 5 minutes after touching hair/face/mask/phone/trash bin/cleaning tools).",
    "A2": "[48h Red-Line Rectification] Strictly prohibit putting on gloves or touching food utensils after contamination without washing hands; store manager must supervise and sign off on 'Contamination Trigger -> 5-Step Handwash -> New Gloves'.",
    "A3": "Require involved staff to strictly follow the 5-step handwashing procedure (wet hands -> apply soap -> scrub foam >=20s -> rinse -> dry with paper towel); never wash with gloves on or apply soap to dry hands.",
    "A4": "Require staff to wear disposable gloves throughout ready-to-eat food handling and replace with new gloves immediately after handwashing or contamination.",
    "A5": "Enforce the store's 2-hour scheduled handwashing reminder mechanism for all on-duty staff.",
    "B0": "Verify ice maker model and ensure all 5 detachable core components are fully removed and visible during weekly sanitizing.",
    "B1-1": "[48h Red-Line Rectification] Re-execute the full 15-step Langtuo weekly cleaning per SOP 21: power off, place disassembled parts on top, spray & soak >=10 min, pour 8L sanitizer, and close doors.",
    "B1-2": "[48h Red-Line Rectification] Re-execute the full 13-step Manitowoc weekly cleaning per SOP 8: switch OFF, remove 3 internal parts, spray & soak >=5 min, pour 3L sanitizer, and run self-clean cycle.",
    "B2": "[48h Red-Line Rectification] Ensure sprayed sanitizer sits undisturbed for the full required duration (Manitowoc >=5 min / Langtuo >=10 min) and wipe all 4 side perimeter edges of the cover and water curtain.",
    "B3": "[48h Red-Line Rectification] Immediately remove and isolate the yellow multi-purpose cleaner bottle; reiterate that ice maker sanitizing must only use colorless KAY-5 sanitizer in the white/clear spray bottle.",
    "B4": "[48h Red-Line Rectification] Wipe ice maker interior, water pipe, water curtain, and inner door walls strictly using a clean red-trimmed towel taken directly from the labeled sanitizer bucket.",
    "B5": "Immediately wash and sanitize the ice scoop and return it to its covered dedicated holder; never leave the ice scoop inside the ice bin or flat on the counter.",
    "C1": "[48h Red-Line Rectification] Immediately eliminate cross-contamination hazards; strictly prohibit washing mops/cleaning tools in food sinks or touching ready-to-eat ingredients with bare hands.",
    "C2": "[48h Red-Line Rectification] Strictly prohibit reusing beverage cups or placing receipts/pens inside food containers.",
    "C3": "[48h Red-Line Rectification] Conduct formal disciplinary review with involved staff and eliminate on-duty misconduct in work areas.",
    "C4": "Ensure all staff properly wear hairnets, caps, masks, and clean aprons (hang removed aprons in the designated storage area); reiterate the zero-tolerance ban on personal mobile phones in kitchen/bar areas.",
    "C5": "Clear trash bins and floor standing water promptly; keep step-on trash bin lids closed and temporary trash bags tied.",
    "C6": "Strictly follow SOP to wipe automated tea maker hot water outlets and latches with a dedicated towel before and after brewing, and keep chiller doors tightly closed.",
    "C7": "Strengthen front-bar greeting awareness: greet arriving customers within 10 seconds and hand over beverages with both hands.",
    "C8": "Strictly enforce tea maker dispensing over ice (prepare ice 2–3 minutes ahead) and complete stirring within 30 seconds after dispensing.",
    "D1": "Review 4-station beverage output loop and ensure standardized station handoffs.",
    "D2": "Review store IPLH metrics and optimize station bottlenecks.",
    "D3": "Adjust shift scheduling to match peak and off-peak customer traffic.",
    "D4": "Reinforce standard recipe card and jigger measurement compliance during morning briefing.",
}

_DEFAULT_RECTIFICATION_ZH_BY_RULE: Dict[str, str] = {
    "A1": "【48h 整改】组织涉事员工复训强制洗手触发场景（触碰头面部/口罩/手机/垃圾桶/清洁工具后 5 分钟内必须完成五步洗手）。",
    "A2": "【48h 红线整改】严禁污染后未洗手直接戴手套或接触食品接触器具；店长现场督导考核「污染触发→五步洗手→换新手套」全流程。",
    "A3": "要求涉事员工严格执行标准五步洗手（先流水湿手→取皂液→纯泡沫对搓满 20 秒→冲净→抽取擦手纸彻底擦干），严禁戴手套冲水假洗手或干手按皂液。",
    "A4": "要求员工接触食品或即食原料全程规范佩戴一次性手套，洗手后及手套污染后立即更换新手套。",
    "A5": "落实门店在岗每 2 小时定时洗手提醒机制。",
    "B0": "核对制冰机机型并在周清时完整拆出五大核心可拆部件备查。",
    "B1-1": "【48h 红线整改】按 SOP 21 重新执行 Langtuo 制冰机完整周清流程，确保断电、拆件置顶喷洒静置满 10 分钟、倒入 8L 消毒液并关好门板。",
    "B1-2": "【48h 红线整改】按 SOP 8 重新执行 Manitowoc 制冰机完整周清流程，确保拨 OFF 断电、拆内部三件套喷洒静置满 5 分钟、倒入 3L 消毒液启动自清洁。",
    "B2": "【48h 红线整改】制冰机各部件喷洒消毒液后必须静置满规定时长（Manitowoc ≥5 分钟 / Langtuo ≥10 分钟），且擦拭白色外盖门板与挡水帘时必须完整覆盖四周侧面边缘。",
    "B3": "【48h 红线整改】立即停用并隔离黄色瓶身多功能清洁剂，重申制冰机清洁仅限使用白底透明喷壶盛装的无色 KAY-5 消毒液。",
    "B4": "【48h 红线整改】擦拭制冰机内腔、布水管、挡水帘与门板白色内壁必须使用从专用贴标消毒桶中随用随取的干净红边毛巾。",
    "B5": "立即清洗消毒冰铲并放回带盖专用收纳盒，严禁将冰铲存放在冰仓内或随手平放在操作台面。",
    "C1": "【48h 红线整改】立即消除交叉污染隐患，严禁在食品水槽清洗拖把/清洁工具或徒手接触即食原料。",
    "C2": "【48h 红线整改】严禁重复使用饮品杯或将收银小票、笔放入食品容器。",
    "C3": "【48h 红线整改】严肃约谈涉事员工，杜绝工作区不当行为。",
    "C4": "督促全员规范佩戴发网、工作帽、口罩与洁净围裙（脱下围裙须挂入指定存放区）；重申后厨与吧台严禁使用私人手机红线。",
    "C5": "及时清理垃圾桶与地面积水，保持脚踏垃圾桶盖闭合、临时垃圾袋扎口。",
    "C6": "严格按 SOP 在自动泡茶机冲泡前后用专用毛巾擦拭热水口与卡扣，随手关严冷藏门。",
    "C7": "加强前吧台迎宾意识，顾客到店 10 秒内主动招呼并双手递交。",
    "C8": "严格执行泡茶机出茶入冰（提前 2–3 分钟备冰）并在出茶提示后 30 秒内完成搅拌。",
    "D1": "复盘四工位闭环出杯流程，确保各工位动作衔接规范。",
    "D2": "复盘门店总 IPLH 与个人出杯效率，优化高峰期工位瓶颈。",
    "D3": "根据客流高低峰合理调配在岗排班人力。",
    "D4": "晨会重申出品标准配方与量杯/雪克杯规范使用要求。",
}


def _has_cjk(text: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", text or ""))


def _build_bilingual_sop_entry(
    *,
    rid: str,
    raw_name: str,
    raw_category: str,
    raw_severity: str,
    doc_ref: str,
    check_instruction: str,
    raw_pass: str,
    raw_fail: str,
) -> Dict[str, str]:
    m_code, m_name_en, m_name_zh = _module_code_and_names(rid)
    stripped_raw = re.sub(r"^[A-Z0-9-]+\s*·\s*", "", raw_name)
    if rid in _CLEAN_SUBTITLE_BY_RULE:
        sub_en = _CLEAN_SUBTITLE_BY_RULE[rid]
        name_en = f"{rid} · {sub_en}" if not raw_name or _has_cjk(raw_name) else raw_name
    else:
        sub_en = stripped_raw or f"{rid} · SOP Audit Clause"
        name_en = raw_name or f"{rid} · {sub_en}"
    sub_zh = _CLEAN_SUBTITLE_ZH_BY_RULE.get(
        rid,
        stripped_raw or raw_name or f"{rid} · SOP 稽核条款",
    )
    name_zh = raw_name if _has_cjk(raw_name) else f"{rid} · {sub_zh}"

    pass_en = (
        raw_pass
        if raw_pass and not _has_cjk(raw_pass)
        else _DEFAULT_PASS_EN_BY_RULE.get(rid, raw_pass or "Strictly follow official CHAGEE APAC SOP standards.")
    )
    pass_zh = (
        raw_pass
        if raw_pass and _has_cjk(raw_pass)
        else (raw_pass or "严格按照 CHAGEE APAC 官方 SOP 标准执行。")
    )
    fail_en = (
        raw_fail
        if raw_fail and not _has_cjk(raw_fail)
        else _DEFAULT_FAIL_EN_BY_RULE.get(rid, raw_fail or "Any deviation from official CHAGEE APAC SOP standards.")
    )
    fail_zh = (
        raw_fail
        if raw_fail and _has_cjk(raw_fail)
        else (raw_fail or "违反 CHAGEE APAC 官方 SOP 标准。")
    )
    rect_en = _DEFAULT_RECTIFICATION_BY_RULE.get(
        rid,
        "Store manager must review the 20s evidence clip with on-duty staff and reinforce compliance in the morning briefing.",
    )
    rect_zh = _DEFAULT_RECTIFICATION_ZH_BY_RULE.get(
        rid,
        "请店长对照 20 秒证据视频组织当班员工复盘纠偏，并在晨会宣导。",
    )
    return {
        "rule_id": rid,
        "name": name_en,
        "name_en": name_en,
        "name_zh": name_zh,
        "clean_subtitle": sub_en,
        "clean_subtitle_en": sub_en,
        "clean_subtitle_zh": sub_zh,
        "category": raw_category or m_name_en,
        "module_code": m_code,
        "module_name": m_name_en,
        "module_name_en": m_name_en,
        "module_name_zh": m_name_zh,
        "severity": raw_severity or "NORMAL",
        "doc_ref": doc_ref,
        "check_instruction": check_instruction,
        "pass_criteria": pass_en,
        "pass_criteria_en": pass_en,
        "pass_criteria_zh": pass_zh,
        "fail_criteria": fail_en,
        "fail_criteria_en": fail_en,
        "fail_criteria_zh": fail_zh,
        "rectification": rect_en,
        "rectification_en": rect_en,
        "rectification_zh": rect_zh,
    }


def load_sop_catalog(extra_rules: Optional[Sequence[Any]] = None) -> List[Dict[str, str]]:
    """Loads the built-in 24-rule SOP catalog from `sop/master_sheet.json` or `DEFAULT_CHAGEE_V25_RULES`,
    merging any custom/spot-check rules from `Tab4_Spot_Check` / `Tab4_专项测试表` or `extra_rules` by `rule_id`.
    """
    from .prompt_manager import DEFAULT_CHAGEE_V25_RULES, TAB4_SPOT_CHECK_TITLE

    sop_path = Path(__file__).resolve().parent.parent / "sop" / "master_sheet.json"
    rules_by_id: Dict[str, Dict[str, str]] = {}
    if sop_path.exists():
        try:
            doc = json.loads(sop_path.read_text(encoding="utf-8"))
            tabs = doc.get("tabs", [])
            for tab in tabs:
                if str(tab.get("title") or "").startswith("Prompt_"):
                    values = tab.get("values") or []
                    for row in values[1:]:
                        if len(row) >= 9 and str(row[0]).strip():
                            rid = str(row[0]).strip()
                            rules_by_id[rid] = _build_bilingual_sop_entry(
                                rid=rid,
                                raw_name=str(row[1]).strip(),
                                raw_category=str(row[2]).strip(),
                                raw_severity=str(row[3]).strip() or "NORMAL",
                                doc_ref=_DEFAULT_DOC_REF_BY_PREFIX.get(
                                    rid, "APAC Cloud Audit Parameters 2026 v1.0"
                                ),
                                check_instruction=str(row[6]).strip(),
                                raw_pass=str(row[7]).strip(),
                                raw_fail=str(row[8]).strip(),
                            )
                    break
            for tab in tabs:
                tab_title = str(tab.get("title") or "").strip()
                if tab_title in (TAB4_SPOT_CHECK_TITLE, "Tab4_Spot_Check", "Tab4_专项测试表"):
                    values = tab.get("values") or []
                    for row in values[1:]:
                        if len(row) >= 9 and str(row[0]).strip():
                            rid = str(row[0]).strip()
                            if rid not in rules_by_id:
                                rules_by_id[rid] = _build_bilingual_sop_entry(
                                    rid=rid,
                                    raw_name=str(row[1]).strip(),
                                    raw_category=str(row[2]).strip(),
                                    raw_severity=str(row[3]).strip() or "NORMAL",
                                    doc_ref=_DEFAULT_DOC_REF_BY_PREFIX.get(
                                        rid, f"{TAB4_SPOT_CHECK_TITLE} · Spot-Check & New SOP Testing"
                                    ),
                                    check_instruction=str(row[6]).strip(),
                                    raw_pass=str(row[7]).strip(),
                                    raw_fail=str(row[8]).strip(),
                                )
        except Exception as exc:
            logger.warning("Failed to read sop/master_sheet.json: %s", exc)
    if not rules_by_id:
        for item in DEFAULT_CHAGEE_V25_RULES:
            rid = str(item.rule_id).strip()
            rules_by_id[rid] = _build_bilingual_sop_entry(
                rid=rid,
                raw_name=str(item.name).strip(),
                raw_category=str(item.category).strip(),
                raw_severity=str(item.severity).strip() or "NORMAL",
                doc_ref=_DEFAULT_DOC_REF_BY_PREFIX.get(
                    rid, "APAC Cloud Audit Parameters 2026 v1.0"
                ),
                check_instruction=str(item.check_instruction).strip(),
                raw_pass=str(item.pass_criteria).strip(),
                raw_fail=str(item.fail_criteria).strip(),
            )
    for extra in extra_rules or []:
        rid = str(
            extra.get("rule_id", "") if isinstance(extra, dict) else getattr(extra, "rule_id", "")
        ).strip()
        if not rid:
            continue
        name = str(
            extra.get("name", "") if isinstance(extra, dict) else getattr(extra, "name", "")
        ).strip()
        category = str(
            extra.get("category", "") if isinstance(extra, dict) else getattr(extra, "category", "")
        ).strip()
        sev_raw = extra.get("severity", "NORMAL") if isinstance(extra, dict) else getattr(extra, "severity", "NORMAL")
        sev_val = str(getattr(sev_raw, "value", sev_raw) or "NORMAL").strip()
        check_inst = str(
            extra.get("check_instruction", "")
            if isinstance(extra, dict)
            else getattr(extra, "check_instruction", "")
        ).strip()
        pass_crit = str(
            extra.get("pass_criteria", "")
            if isinstance(extra, dict)
            else getattr(extra, "pass_criteria", "")
        ).strip()
        fail_crit = str(
            extra.get("fail_criteria", "")
            if isinstance(extra, dict)
            else getattr(extra, "fail_criteria", "")
        ).strip()
        r_name = name or f"{rid} · Spot-Check Rule"
        rules_by_id[rid] = _build_bilingual_sop_entry(
            rid=rid,
            raw_name=r_name,
            raw_category=category or "Spot-Check",
            raw_severity=sev_val,
            doc_ref=_DEFAULT_DOC_REF_BY_PREFIX.get(
                rid, f"{TAB4_SPOT_CHECK_TITLE} · Spot-Check & New SOP Testing"
            ),
            check_instruction=check_inst,
            raw_pass=pass_crit,
            raw_fail=fail_crit,
        )
    return list(rules_by_id.values())


_STAFF_RE = re.compile(r"(?:Staff|Employee|Partner|Worker|员工)\s*[A-Z0-9]+", re.IGNORECASE)
_MMSS_RE = re.compile(r"\b(\d{1,2}):(\d{2})(?::(\d{2}))?\b")
_OSD_CLOCK_RE = re.compile(r"\b(\d{2}:\d{2}:\d{2})\b")
_DRIVE_FILE_ID_RE = re.compile(r"/file/d/([a-zA-Z0-9_-]{10,})")


def _extract_mmss_and_seconds(timestamp_raw: str) -> Tuple[str, int]:
    match = _MMSS_RE.search(timestamp_raw or "")
    if not match:
        return "00:00", 0
    if match.group(3) is not None:
        hh = int(match.group(1))
        mm = int(match.group(2))
        ss = int(match.group(3))
        total = hh * 3600 + mm * 60 + ss
        return f"{hh * 60 + mm:02d}:{ss:02d}", total
    mm = int(match.group(1))
    ss = int(match.group(2))
    return f"{mm:02d}:{ss:02d}", mm * 60 + ss


def _extract_drive_preview_url(drive_url: str) -> Tuple[str, str]:
    cleaned = (drive_url or "").strip()
    if not cleaned:
        return "", ""
    m = _DRIVE_FILE_ID_RE.search(cleaned)
    if m:
        fid = m.group(1)
        return fid, f"https://drive.google.com/file/d/{fid}/preview"
    return "", cleaned


def _extract_staff_tags(description: str) -> Tuple[str, List[str]]:
    raw_matches = _STAFF_RE.findall(description or "")
    found: List[str] = []
    seen = set()
    for m in raw_matches:
        norm = re.sub(r"\s+", " ", m.strip())
        if norm.lower() not in seen:
            seen.add(norm.lower())
            found.append(norm)
    found.sort()
    if not found:
        lower_desc = (description or "").lower()
        if any(
            w in (description or "") or w in lower_desc
            for w in ("全员", "在岗员工", "多名员工", "两人", "多人", "multiple staff", "all staff", "two staff")
        ):
            return "Multiple Staff", ["Multiple Staff"]
        return "On-duty Staff", ["On-duty Staff"]
    if len(found) > 1:
        return ", ".join(found) + " (Multiple)", found
    return found[0], found


def _short_video_key(video_filename: str, idx: int) -> Tuple[str, str]:
    raw = (video_filename or "").strip()
    m = re.match(r"^(Footage\s*\d+|Clip\s*\d+|Video\s*\d+)", raw, re.IGNORECASE)
    if m:
        short = re.sub(r"\s+", "", m.group(1))
        return short, raw[:42]
    stem = Path(raw).stem if raw else f"Video{idx}"
    short = re.sub(r"[^a-zA-Z0-9\u4e00-\u9fa5_-]+", "", stem)[:18] or f"Video{idx}"
    display = stem if len(stem) <= 38 else (stem[:35] + "...")
    return short, display


def _module_code_and_names(rule_id: str) -> Tuple[str, str, str]:
    prefix = (rule_id or "C")[:1].upper()
    if prefix in MODULE_META:
        meta = MODULE_META[prefix]
        return prefix, str(meta["name"]), str(meta["name_zh"])
    meta = MODULE_META["OTHER"]
    return "C", str(meta["name"]), str(meta["name_zh"])


def _module_code_and_name(rule_id: str) -> Tuple[str, str]:
    code, name_en, _ = _module_code_and_names(rule_id)
    return code, name_en


def _infer_sub_clause_title_pair(rule_id: str, rule_name: str, description: str) -> Tuple[str, str]:
    desc = description or ""
    lower = desc.lower()
    if rule_id == "C4":
        if "手机" in desc or "phone" in lower or "mobile" in lower:
            return (
                "1.4 On-Duty Red Line: Personal Mobile Phone Used in Work Zone",
                "1.4 在岗行为红线：工作区使用私人手机",
            )
        if "围裙" in desc or "apron" in lower:
            return (
                "1.2 Apron Compliance: Improper Apron Wearing or Storage",
                "1.2 工装围裙规范：围裙穿戴或存放不合规",
            )
        if "口罩" in desc or "mask" in lower:
            return (
                "1.1 Personal Grooming: Mask Not Properly Covering Nose & Mouth",
                "1.1 个人仪容着装：口罩未规范遮挡口鼻",
            )
        if "帽" in desc or "发网" in desc or "头发" in desc or "hairnet" in lower or "cap" in lower or "hat" in lower or "hair" in lower:
            return (
                "1.1 Personal Grooming: Missing Work Cap or Hairnet",
                "1.1 个人仪容着装：未佩戴工作帽及发网",
            )
        if "倚靠" in desc or "lean" in lower:
            return (
                "1.4 On-Duty Conduct: Leaning Against Counter While Waiting",
                "1.4 在岗行为红线：等客时倚靠台面",
            )
    if rule_id == "A1":
        return (
            "1.5.5 Mandatory Handwashing Trigger: No 5-Step Handwash Within 5 Min of Contamination",
            "1.5.5 强制洗手触发场景：触碰污染源后 5 分钟内未执行五步洗手",
        )
    if rule_id == "A2":
        return (
            "1.5.3 / 1.5.5 Mandatory Handwashing Red Line: Donning Gloves or Touching Utensils Without Handwashing",
            "1.5.3 / 1.5.5 强制洗手红线：污染后未洗手直接戴手套或接触食品器具",
        )
    if rule_id == "A3":
        return (
            "1.5.1–1.5.4 5-Step Handwashing Check: Wet / Soap / Scrub >=20s / Rinse / Paper Towel Dry",
            "1.5.1–1.5.4 洗手五步法核对：湿手/皂液/搓洗≥20s/冲净/擦手纸擦干",
        )
    if rule_id == "B2":
        return (
            "SOP 8 / SOP 21 Soak Lockout Red Line: Insufficient Sanitizer Contact Time or Missed Perimeter Edges",
            "SOP 8 / SOP 21 静置锁定期红线：消毒液静置时长不足或侧面边缘漏擦",
        )
    if rule_id == "B3":
        return (
            "0-Tolerance Chemical Red Line: Yellow Multi-Purpose Cleaner Bottle or Chemical Mixing on Ice Maker",
            "0 Tolerance 化学品红线：使用黄色瓶身多功能清洁剂或混用化学品",
        )
    if rule_id == "B4":
        return (
            "3.1 3-Color Towel Rule: Red-Trimmed Towel Not Taken From Dedicated Sanitizer Bucket or Mixed",
            "3.1 三色毛巾规范：未从专用消毒桶取用干净红边毛巾或三色混用",
        )
    if rule_id == "B5":
        return (
            "4.1 / 4.5 Ice Scoop Hygiene: Scoop Not Returned to Dedicated Holder or Improper Ice Scooping",
            "4.1 / 4.5 冰铲卫生规范：冰铲未放回专用收纳盒或取冰不规范",
        )
    en_default = _CLEAN_SUBTITLE_BY_RULE.get(rule_id) or (
        rule_name if rule_name and not _has_cjk(rule_name) else f"SOP Clause {rule_id}"
    )
    zh_default = _CLEAN_SUBTITLE_ZH_BY_RULE.get(rule_id) or (
        rule_name if rule_name and _has_cjk(rule_name) else f"SOP 条款 {rule_id}"
    )
    return en_default, zh_default


def _infer_sub_clause_title(rule_id: str, rule_name: str, description: str = "") -> str:
    en_title, _ = _infer_sub_clause_title_pair(rule_id, rule_name, description)
    return en_title


def _build_findings_and_ledger_from_rows(
    *,
    audit_id: str,
    tab1_rows: List[List[Any]],
    tab2_rows: List[List[Any]],
    sop_catalog: List[Dict[str, str]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    sop_by_id = {r["rule_id"]: r for r in sop_catalog}
    occ_counter: Dict[str, int] = {}
    video_short_map: Dict[str, Tuple[str, str]] = {}
    findings: List[Dict[str, Any]] = []

    for row_idx, raw_row in enumerate(tab1_rows, start=1):
        row_audit_id = str(raw_row[0] if len(raw_row) > 0 else audit_id).strip() or audit_id
        v_file = str(raw_row[1] if len(raw_row) > 1 else "").strip()
        ts_raw = str(raw_row[2] if len(raw_row) > 2 else "").strip()
        rule_id = str(raw_row[3] if len(raw_row) > 3 else "C4").strip() or "C4"
        disposition = str(raw_row[4] if len(raw_row) > 4 else "CONFIRMED").strip().upper() or "CONFIRMED"
        sev_raw = str(raw_row[5] if len(raw_row) > 5 else "NORMAL").strip().upper() or "NORMAL"
        severity = "RED_LINE" if sev_raw in ("RED_LINE", "CRITICAL", "HIGH") else "NORMAL"
        try:
            confidence = float(raw_row[6]) if len(raw_row) > 6 and str(raw_row[6]).strip() else 0.95
        except (TypeError, ValueError):
            confidence = 0.95
        desc = str(raw_row[7] if len(raw_row) > 7 else "").strip()
        drive_url = str(raw_row[8] if len(raw_row) > 8 else "").strip()
        raw_status = str(raw_row[9] if len(raw_row) > 9 else INITIAL_REVIEW_STATUS).strip()
        raw_note = str(raw_row[10] if len(raw_row) > 10 else "").strip()

        base_tuple_key = f"{row_audit_id.lower()}::{v_file}::{ts_raw}::{rule_id.upper()}"
        occ_idx = occ_counter.get(base_tuple_key, 0)
        occ_counter[base_tuple_key] = occ_idx + 1
        incident_id = build_incident_composite_key(row_audit_id, v_file, ts_raw, rule_id, occ_idx)

        if v_file not in video_short_map:
            video_short_map[v_file] = _short_video_key(v_file, len(video_short_map) + 1)
        v_short, v_display = video_short_map[v_file]

        mmss, sec_in_clip = _extract_mmss_and_seconds(ts_raw)
        osd_m = _OSD_CLOCK_RE.search(desc)
        osd_clock = osd_m.group(1) if osd_m else ""
        file_id, preview_url = _extract_drive_preview_url(drive_url)
        staff_tag, staff_list = _extract_staff_tags(desc)
        mod_code, mod_name_en, mod_name_zh = _module_code_and_names(rule_id)
        sop_info = sop_by_id.get(rule_id, {})
        rule_name_en = sop_info.get("name_en") or sop_info.get("name") or f"{rule_id} · SOP Audit Clause"
        rule_name_zh = sop_info.get("name_zh") or f"{rule_id} · SOP 稽核条款"
        pass_crit_en = sop_info.get("pass_criteria_en") or sop_info.get("pass_criteria") or "Strictly follow official CHAGEE APAC SOP standards."
        pass_crit_zh = sop_info.get("pass_criteria_zh") or "严格按照 CHAGEE APAC 官方 SOP 标准执行。"
        fail_crit_en = sop_info.get("fail_criteria_en") or sop_info.get("fail_criteria") or "Any deviation from official CHAGEE APAC SOP standards."
        fail_crit_zh = sop_info.get("fail_criteria_zh") or "违反 CHAGEE APAC 官方 SOP 标准。"
        doc_ref = sop_info.get("doc_ref") or _DEFAULT_DOC_REF_BY_PREFIX.get(
            rule_id, "APAC Cloud Audit Parameters 2026 v1.0"
        )
        rect_en = sop_info.get("rectification_en") or _DEFAULT_RECTIFICATION_BY_RULE.get(
            rule_id, "Store manager must review the 20s evidence clip with on-duty staff and reinforce compliance in the morning briefing."
        )
        rect_zh = sop_info.get("rectification_zh") or _DEFAULT_RECTIFICATION_ZH_BY_RULE.get(
            rule_id, "请店长对照 20 秒证据视频组织当班员工复盘纠偏，并在晨会宣导。"
        )
        sub_clause_en, sub_clause_zh = _infer_sub_clause_title_pair(rule_id, rule_name_en, desc)
        clean_sub_en = sop_info.get("clean_subtitle_en") or sop_info.get("clean_subtitle") or _CLEAN_SUBTITLE_BY_RULE.get(rule_id) or sub_clause_en
        clean_sub_zh = sop_info.get("clean_subtitle_zh") or _CLEAN_SUBTITLE_ZH_BY_RULE.get(rule_id) or sub_clause_zh

        findings.append(
            {
                "id": row_idx,
                "incident_id": incident_id,
                "code": f"#{row_idx:02d}",
                "audit_id": row_audit_id,
                "video_filename": v_file,
                "video_short": v_short,
                "video_display": v_display,
                "timestamp_raw": ts_raw,
                "mmss": mmss,
                "sec_in_clip": sec_in_clip,
                "osd_clock": osd_clock,
                "rule_id": rule_id,
                "module_code": mod_code,
                "module_name": mod_name_en,
                "module_name_en": mod_name_en,
                "module_name_zh": mod_name_zh,
                "rule_name": rule_name_en,
                "rule_name_en": rule_name_en,
                "rule_name_zh": rule_name_zh,
                "rule_subtitle": clean_sub_en,
                "rule_subtitle_en": clean_sub_en,
                "rule_subtitle_zh": clean_sub_zh,
                "doc_ref": doc_ref,
                "sub_clause_title": sub_clause_en,
                "sub_clause_title_en": sub_clause_en,
                "sub_clause_title_zh": sub_clause_zh,
                "specific_pass": pass_crit_en,
                "specific_pass_en": pass_crit_en,
                "specific_pass_zh": pass_crit_zh,
                "sop_pass_criteria": pass_crit_en,
                "sop_pass_criteria_en": pass_crit_en,
                "sop_pass_criteria_zh": pass_crit_zh,
                "sop_fail_criteria": fail_crit_en,
                "sop_fail_criteria_en": fail_crit_en,
                "sop_fail_criteria_zh": fail_crit_zh,
                "rectification_advice": rect_en,
                "rectification_advice_en": rect_en,
                "rectification_advice_zh": rect_zh,
                "rectification": rect_en,
                "rectification_en": rect_en,
                "rectification_zh": rect_zh,
                "disposition": disposition,
                "severity": severity,
                "confidence": round(confidence, 2),
                "confidence_pct": f"{int(round(confidence * 100))}%",
                "description": desc,
                "drive_url": drive_url,
                "file_id": file_id,
                "preview_url": preview_url,
                "staff_tag": staff_tag,
                "staff_list": staff_list,
                "initial_review_status": map_sheet_status_to_portal_code(raw_status),
                "initial_review_note": raw_note,
            }
        )

    ledger_list: List[Dict[str, Any]] = []
    for l_idx, r in enumerate(tab2_rows, start=1):
        v_file = str(r[1] if len(r) > 1 else "").strip() or "Not recorded in Sheet (historical audit)"
        if v_file not in video_short_map:
            video_short_map[v_file] = _short_video_key(v_file, len(video_short_map) + 1)
        v_short, _ = video_short_map[v_file]
        try:
            dur_val = float(r[3]) if len(r) > 3 and str(r[3]).strip() else 300.0
        except (TypeError, ValueError):
            dur_val = 300.0
        try:
            p_tok = int(float(r[7])) if len(r) > 7 and str(r[7]).strip() else 0
            c_tok = int(float(r[8])) if len(r) > 8 and str(r[8]).strip() else 0
            t_tok = int(float(r[9])) if len(r) > 9 and str(r[9]).strip() else 0
            tot_tok = int(float(r[10])) if len(r) > 10 and str(r[10]).strip() else (p_tok + c_tok + t_tok)
            cost_val = float(r[11]) if len(r) > 11 and str(r[11]).strip() else 0.0
            lat_val = float(r[12]) if len(r) > 12 and str(r[12]).strip() else 0.0
            flag_cnt = int(float(r[13])) if len(r) > 13 and str(r[13]).strip() else 0
        except (TypeError, ValueError):
            p_tok = c_tok = t_tok = tot_tok = flag_cnt = 0
            cost_val = lat_val = 0.0
        ledger_list.append(
            {
                "video_filename": v_file,
                "video_short": v_short,
                "duration_sec": round(dur_val, 1),
                "resolution": str(r[4] if len(r) > 4 else "1920x1080").strip() or "1920x1080",
                "model_version": str(r[5] if len(r) > 5 else "gemini-3.8-flash").strip(),
                "prompt_version": str(r[6] if len(r) > 6 else "").strip(),
                "prompt_tokens": p_tok,
                "output_tokens": c_tok,
                "thoughts_tokens": t_tok,
                "total_tokens": tot_tok,
                "cost_usd": round(cost_val, 4),
                "latency_sec": round(lat_val, 1),
                "flagged_events": flag_cnt,
            }
        )

    return findings, ledger_list


def _fallback_rows_from_job(job: AuditJob) -> Tuple[List[List[Any]], List[List[Any]]]:
    """Extracts in-progress `Tab 1` and `Tab 2` rows from `job.completed_segments` when the final
    Google Sheet has not been generated yet (e.g., while `job.state == RUNNING`).
    """
    tab1: List[List[Any]] = []
    tab2: List[List[Any]] = []
    for ckpt in (job.completed_segments or {}).values():
        fallback_url = build_source_video_url(ckpt.file_id) if ckpt.file_id else ""
        for f in ckpt.findings:
            clean = f.sanitise()
            if clean.status != Status.VIOLATION:
                continue
            tab1.append(
                [
                    job.job_id,
                    ckpt.filename,
                    clean.timestamp_in_clip,
                    clean.rule_id,
                    clean.disposition.value,
                    clean.severity.value,
                    round(clean.confidence, 2),
                    clean.evidence,
                    clean.evidence_drive_url or fallback_url,
                    INITIAL_REVIEW_STATUS,
                    "",
                ]
            )
        lr = ckpt.ledger_row
        tab2.append(
            [
                lr.audit_id,
                lr.video_filename,
                ckpt.segment_index,
                round(lr.segment_duration_sec, 1),
                lr.resolution,
                lr.model_version_used,
                lr.prompt_version_used,
                lr.prompt_token_count,
                lr.candidates_token_count,
                lr.thoughts_token_count,
                lr.total_token_count,
                round(lr.estimated_cost_usd, 6),
                round(lr.processing_latency_sec, 2),
                lr.flagged_events_count,
            ]
        )
    return tab1, tab2


async def load_portal_payload(
    *,
    audit_service: Any = None,
    job_store: Any = None,
    state_store: Any = None,
    gateway: Any = None,
    job_id: str = "",
    user_id: str = "",
    batch_id: str = "",
    store_job_id: str = "",
    exp: int = 0,
    sig: str = "",
    viewer_email: str = "",
    drive_checker: Optional[DriveAccessChecker] = None,
) -> Dict[str, Any]:
    """Loads and validates the full JSON payload for `/portal` and `GET /api/portal/data`.

    `viewer_email` (IAP-verified, `PORTAL_MODE=iap` only) turns on the per-store Google Drive gate:
    a single-store view is refused (PermissionError -> 403) unless the viewer can open the store's
    report in Drive; a batch view lists only the stores the viewer can open (none -> 403).
    """
    if audit_service is not None:
        if job_store is None:
            job_store = getattr(audit_service, "jobs", None)
        if state_store is None:
            state_store = getattr(audit_service, "state_store", None)
    if state_store is None:
        from .scheduled_auditor import ScheduledStateStore

        state_store = ScheduledStateStore()
    ok, err_msg = verify_portal_signature(
        job_id=job_id,
        user_id=user_id,
        batch_id=batch_id,
        exp=exp,
        sig=sig,
    )
    if not ok:
        raise PermissionError(err_msg)

    sop_catalog = load_sop_catalog()
    batch_meta: Optional[Dict[str, Any]] = None
    batch_stores: List[Dict[str, Any]] = []
    target_job: Optional[AuditJob] = None

    if batch_id:
        from .scheduled_auditor import ScheduledBatchRecord

        b_key = state_store.batch_key(batch_id)
        raw_batch, _ = state_store.read_text_with_generation(b_key)
        if raw_batch is None:
            raise LookupError(f"Scheduled batch `{batch_id}` state record not found. / 未找到定时巡检批次 `{batch_id}` 的状态记录。")
        batch_rec = ScheduledBatchRecord.model_validate_json(raw_batch)
        sorted_tasks = sorted(
            batch_rec.store_tasks.values(),
            key=lambda x: (x.folder_name, x.job_id),
        )
        live_jobs: Dict[str, Optional[AuditJob]] = {}
        for t in sorted_tasks:
            live_jobs[t.job_id] = await job_store.get(t.user_id, t.job_id)
        if viewer_email:
            visibility = await filter_visible_stores(
                viewer_email,
                [
                    (
                        t.job_id,
                        (
                            (live_jobs[t.job_id].report_sheet_url or t.report_sheet_url)  # type: ignore[union-attr]
                            if live_jobs[t.job_id] is not None
                            else t.report_sheet_url
                        )
                        or "",
                        t.folder_id,
                    )
                    for t in sorted_tasks
                ],
                checker=drive_checker,
            )
            if store_job_id and store_job_id in batch_rec.store_tasks and not visibility.get(
                store_job_id, (False, "")
            )[0]:
                raise PermissionError(
                    denied_message(viewer_email, visibility.get(store_job_id, (False, ""))[1])
                )
            sorted_tasks = [t for t in sorted_tasks if visibility.get(t.job_id, (False, ""))[0]]
            if not sorted_tasks:
                raise PermissionError(
                    denied_message(viewer_email, "None of the stores in this batch are shared with you.")
                )
        selected_task = None
        if store_job_id:
            selected_task = next((t for t in sorted_tasks if t.job_id == store_job_id), None)
        if selected_task is None and sorted_tasks:
            done_tasks = [t for t in sorted_tasks if t.state.upper() == "DONE"]
            selected_task = done_tasks[0] if done_tasks else sorted_tasks[0]

        for t in sorted_tasks:
            live_job = live_jobs.get(t.job_id)
            st_state = (
                live_job.state.value.upper()
                if live_job is not None
                else t.state.upper()
            )
            st_viol = (
                live_job.violations_found
                if live_job is not None and st_state == "DONE"
                else t.violations_found
            )
            st_sheet = (
                (live_job.report_sheet_url or t.report_sheet_url)
                if live_job is not None
                else t.report_sheet_url
            )
            switch_qs = urllib.parse.urlencode(
                {
                    "batch": batch_id,
                    "store": t.job_id,
                    "exp": str(exp),
                    "sig": sig,
                }
            )
            batch_stores.append(
                {
                    "job_id": t.job_id,
                    "user_id": t.user_id,
                    "folder_id": t.folder_id,
                    "folder_name": t.folder_name,
                    "video_count": t.video_count,
                    "state": st_state,
                    "violations_found": st_viol,
                    "total_tokens": (
                        live_job.total_tokens_used if live_job is not None else t.total_tokens
                    ),
                    "report_sheet_url": st_sheet,
                    "error_message": (
                        (
                            live_job.error_message
                            or (
                                live_job.preflight_report.message_to_user
                                if live_job.preflight_report is not None
                                and not live_job.preflight_report.passed
                                else ""
                            )
                            or t.error_message
                        )
                        if live_job is not None
                        else t.error_message
                    ),
                    "active": bool(selected_task and t.job_id == selected_task.job_id),
                    "switch_url": f"/portal?{switch_qs}",
                }
            )

        batch_meta = {
            "batch_id": batch_rec.batch_id,
            "rule_id": batch_rec.rule_id,
            "rule_name": batch_rec.rule_name or "Scheduled Multi-Store Audit",
            "local_date": batch_rec.local_date,
            "supervisor_email": batch_rec.supervisor_email,
            "root_folder_url": batch_rec.root_folder_url,
            "total_stores": len(batch_stores),
        }
        if selected_task is not None:
            target_job = await job_store.get(selected_task.user_id, selected_task.job_id)
            if target_job is None:
                target_job = AuditJob(
                    job_id=selected_task.job_id,
                    user_id=selected_task.user_id,
                    folder_id=selected_task.folder_id,
                    folder_name=selected_task.folder_name,
                    drive_url=selected_task.folder_id,
                    batch_id=batch_id,
                    rule_id=batch_rec.rule_id,
                    report_sheet_url=selected_task.report_sheet_url or None,
                    error_message=selected_task.error_message or None,
                )
            elif not target_job.folder_name and selected_task.folder_name:
                target_job = target_job.model_copy(
                    update={"folder_name": selected_task.folder_name}
                )
    else:
        target_job = await job_store.get_by_job_id(job_id, user_id=user_id)
        if target_job is None:
            raise LookupError(f"Audit job `{job_id}` not found. / 未找到稽核单号 `{job_id}` 的任务记录。")
        if viewer_email:
            await require_store_access(
                viewer_email,
                target_job.report_sheet_url or "",
                target_job.folder_id,
                checker=drive_checker,
            )

    if target_job is None:
        raise LookupError("No store audit job available to display. / 未找到可展示的门店稽核任务。")

    tab1_rows: List[List[Any]] = []
    tab2_rows: List[List[Any]] = []
    if target_job.report_sheet_url and gateway is not None:
        try:
            tab1_rows, tab2_rows = await gateway.read_report_sheet_data(
                target_job.folder_id, target_job.report_sheet_url
            )
            # Filter to rows belonging to this audit_id if multiple audits shared one Sheet,
            # or show all rows in the store's report Sheet if none match `target_job.job_id`.
            matching_t1 = [
                r for r in tab1_rows if len(r) > 0 and str(r[0]).strip() == target_job.job_id
            ]
            matching_t2 = [
                r for r in tab2_rows if len(r) > 0 and str(r[0]).strip() == target_job.job_id
            ]
            if matching_t1 or matching_t2:
                tab1_rows = matching_t1
                tab2_rows = matching_t2
        except Exception as exc:
            logger.warning(
                "Portal Sheet read fallback for job %s (%s): %s",
                target_job.job_id,
                target_job.report_sheet_url,
                exc,
            )

    if not tab1_rows and not tab2_rows and target_job.completed_segments:
        tab1_rows, tab2_rows = _fallback_rows_from_job(target_job)

    spot_tab = (getattr(target_job, "target_prompt_tab", "") or "").strip()
    active_pv = str(getattr(target_job, "active_prompt_version", ""))
    if not spot_tab and (active_pv.startswith("Tab4_专项测试表") or active_pv.startswith("Tab4_Spot_Check")):
        spot_tab = active_pv.split(" ")[0]
    if spot_tab and audit_service is not None and getattr(audit_service, "prompt_manager", None) is not None:
        try:
            spot_cfg = await audit_service.prompt_manager.load_config_for_tab(
                spot_tab,
                sheet_id=config.effective_sop_source(target_job.folder_id),
                probe_model=False,
            )
            sop_catalog = load_sop_catalog(extra_rules=spot_cfg.rules)
        except Exception as exc:
            logger.debug("Portal dynamic SOP catalog lookup for %s skipped: %s", spot_tab, exc)

    findings, token_ledger = _build_findings_and_ledger_from_rows(
        audit_id=target_job.job_id,
        tab1_rows=tab1_rows,
        tab2_rows=tab2_rows,
        sop_catalog=sop_catalog,
    )

    folder_url = (
        build_gcs_console_folder_url(target_job.folder_id)
        if is_gcs_target(target_job.folder_id)
        else f"https://drive.google.com/drive/folders/{target_job.folder_id}"
    )
    store_name = (
        target_job.folder_name
        or (
            target_job.folder_id.rstrip("/").split("/")[-1]
            if is_gcs_target(target_job.folder_id)
            else f"Store Audit ({target_job.folder_id[:10]})"
        )
        or "Store Audit"
    )
    total_segs = (
        target_job.preflight_report.planned_segments_count
        if target_job.preflight_report is not None
        else len(target_job.completed_segments)
    )

    return {
        "store_name": store_name,
        "audit_id": target_job.job_id,
        "user_id": target_job.user_id,
        "job_state": target_job.state.value.upper(),
        "completed_segments_count": len(target_job.completed_segments),
        "planned_segments_count": total_segs,
        "error_message": (
            target_job.error_message
            or (
                target_job.preflight_report.message_to_user
                if target_job.preflight_report is not None
                and not target_job.preflight_report.passed
                else ""
            )
        ),
        "folder_id": target_job.folder_id,
        "folder_url": folder_url,
        "sheet_url": target_job.report_sheet_url or "",
        "active_model_version": target_job.active_model_version or "gemini-3.8-flash",
        "active_prompt_version": target_job.active_prompt_version or "APAC Cloud Audit Parameters 2026 v1.0",
        "findings": findings,
        "token_ledger": token_ledger,
        "sop_rules": sop_catalog,
        "batch_meta": batch_meta,
        "batch_stores": batch_stores,
        "auth": {
            "job": job_id if not batch_id else "",
            "job_id": job_id if not batch_id else "",
            "user": user_id if not batch_id else "",
            "user_id": user_id if not batch_id else "",
            "batch": batch_id,
            "batch_id": batch_id,
            "store": target_job.job_id,
            "store_job_id": target_job.job_id,
            "exp": int(exp),
            "sig": sig,
        },
    }


_audit_bucket_warned = False


def _warn_audit_bucket_missing_once() -> None:
    global _audit_bucket_warned
    if not _audit_bucket_warned:
        _audit_bucket_warned = True
        logger.warning(
            "AUDIT_LOG_BUCKET is not set: portal review actions are NOT being audit-logged "
            "(acceptable only for local development; Terraform sets it on every deployed service)."
        )


def _compute_review_audit_changes(
    tab1_rows: Sequence[Sequence[Any]],
    normalized_updates: List[Dict[str, str]],
) -> List[Tuple[Dict[str, str], List[Any], str, str, str, str]]:
    """`(update, row, old_status, new_status, old_note, new_note)` for every update that changes a row.

    Uses the same composite-key matcher as the Sheet write-back, applied update by update on a
    working copy so repeated edits of one row chain old -> new correctly. No-ops are dropped.
    """
    from .gcs_gateway import match_and_apply_tab1_reviews

    working: List[List[Any]] = [list(r) for r in tab1_rows]
    changes: List[Tuple[Dict[str, str], List[Any], str, str, str, str]] = []
    for upd in normalized_updates:
        updated_rows, patches = match_and_apply_tab1_reviews(working, [upd])
        if not patches:
            continue
        idx = patches[0][0] - 2
        before = [str(c) for c in list(working[idx])[:11]] + [""] * max(0, 11 - len(working[idx]))
        old_status, old_note = before[9], before[10]
        new_status, new_note = str(patches[0][1]), str(patches[0][2])
        working = updated_rows
        if old_status == new_status and old_note == new_note:
            continue
        changes.append((upd, before, old_status, new_status, old_note, new_note))
    return changes


async def apply_portal_reviews(
    *,
    audit_service: Any = None,
    job_store: Any = None,
    state_store: Any = None,
    gateway: Any = None,
    job_id: str = "",
    user_id: str = "",
    batch_id: str = "",
    store_job_id: str = "",
    exp: int = 0,
    sig: str = "",
    updates: Optional[List[Dict[str, str]]] = None,
    reviews: Optional[List[Dict[str, str]]] = None,
    viewer_email: str = "",
    drive_checker: Optional[DriveAccessChecker] = None,
    iap_mode: bool = False,
    client_ip: str = "",
    user_agent: str = "",
    audit_writer: Optional[Any] = None,
    audit_bucket: Optional[str] = None,
) -> Dict[str, Any]:
    """Verifies HMAC signature and writes supervisor review decisions + notes to Sheet `Tab 1` Columns J & K.

    Round 86A, fail-closed audit trail: the current J/K values are read first, one append-only GCS
    record per changed row is written (`phase=requested`), and only then is the Sheet updated. If
    the audit write fails, `ReviewAuditLogError` propagates (HTTP 500) and the Sheet is untouched.
    If the Sheet write fails afterwards, `phase=sheet_write_failed` records are written best-effort
    and the original error is re-raised. In `PORTAL_MODE=iap` (`iap_mode=True`) the reviewer is the
    IAP-verified `viewer_email`, the Drive gate applies, and a missing `AUDIT_LOG_BUCKET` is an error.
    """
    if audit_service is not None:
        if job_store is None:
            job_store = getattr(audit_service, "jobs", None)
        if state_store is None:
            state_store = getattr(audit_service, "state_store", None)
    if state_store is None:
        from .scheduled_auditor import ScheduledStateStore

        state_store = ScheduledStateStore()
    eff_updates = updates if updates is not None else (reviews or [])
    ok, err_msg = verify_portal_signature(
        job_id=job_id,
        user_id=user_id,
        batch_id=batch_id,
        exp=exp,
        sig=sig,
    )
    if not ok:
        raise PermissionError(err_msg)

    target_job: Optional[AuditJob] = None
    task_sheet_url = ""
    if batch_id:
        from .scheduled_auditor import ScheduledBatchRecord

        b_key = state_store.batch_key(batch_id)
        raw_batch, _ = state_store.read_text_with_generation(b_key)
        if raw_batch is None:
            raise LookupError(f"Scheduled batch `{batch_id}` not found. / 未找到定时巡检批次 `{batch_id}`。")
        batch_rec = ScheduledBatchRecord.model_validate_json(raw_batch)
        eff_store_job_id = store_job_id or next(iter(batch_rec.store_tasks.keys()), "")
        task = batch_rec.store_tasks.get(eff_store_job_id)
        if task is None:
            raise LookupError(f"Store job `{eff_store_job_id}` not found in batch `{batch_id}`. / 批次 `{batch_id}` 中未找到门店任务 `{eff_store_job_id}`。")
        task_sheet_url = task.report_sheet_url or ""
        target_job = await job_store.get(task.user_id, task.job_id)
        if target_job is None and task.report_sheet_url:
            target_job = AuditJob(
                job_id=task.job_id,
                user_id=task.user_id,
                folder_id=task.folder_id,
                folder_name=task.folder_name,
                drive_url=task.folder_id,
                report_sheet_url=task.report_sheet_url,
            )
    else:
        target_job = await job_store.get_by_job_id(job_id, user_id=user_id)

    if target_job is None:
        raise LookupError("Audit job record not found. / 未找到对应的稽核任务记录。")
    if viewer_email:
        await require_store_access(
            viewer_email,
            target_job.report_sheet_url or task_sheet_url,
            target_job.folder_id,
            checker=drive_checker,
        )
    if not target_job.report_sheet_url or gateway is None:
        return {"ok": False, "updated_count": 0, "persisted_to_sheet": False}

    normalized_updates: List[Dict[str, str]] = []
    for item in eff_updates or []:
        inc_id = str(item.get("incident_id") or "").strip()
        if not inc_id:
            continue
        upd_entry: Dict[str, str] = {"incident_id": inc_id}
        if "status" in item and item["status"] is not None:
            upd_entry["human_review_status"] = map_portal_code_to_sheet_status(str(item["status"]))
        elif "human_review_status" in item and item["human_review_status"] is not None:
            upd_entry["human_review_status"] = str(item["human_review_status"])
        if "note" in item and item["note"] is not None:
            upd_entry["human_review_note"] = str(item["note"])
        elif "human_review_note" in item and item["human_review_note"] is not None:
            upd_entry["human_review_note"] = str(item["human_review_note"])
        normalized_updates.append(upd_entry)

    # 1. Old values from the rows the composite-key matcher works on.
    current_tab1, _ = await gateway.read_report_sheet_data(
        target_job.folder_id, target_job.report_sheet_url
    )
    changes = _compute_review_audit_changes(current_tab1 or [], normalized_updates)
    if not changes:
        return {
            "ok": True,
            "updated_count": 0,
            "persisted_to_sheet": True,
            "job_id": target_job.job_id,
            "report_sheet_url": target_job.report_sheet_url,
            "audit_log_objects": [],
        }

    reviewer_source = "iap" if iap_mode else "hmac_link"
    reviewer = (viewer_email or "").strip().lower() if iap_mode else ""
    store_scope = target_job.job_id if batch_id else ""
    bucket = audit_bucket if audit_bucket is not None else audit_log_bucket()
    writer = audit_writer or write_review_audit_records

    def _records(phase: str) -> List[Dict[str, Any]]:
        return [
            build_review_audit_record(
                reviewer_email=reviewer,
                reviewer_source=reviewer_source,
                job_id=target_job.job_id,  # type: ignore[union-attr]
                batch_id=batch_id,
                store_job_id=store_scope,
                composite_key=upd["incident_id"],
                row=row,
                old_status=old_s,
                new_status=new_s,
                old_note=old_n,
                new_note=new_n,
                report_sheet_url=target_job.report_sheet_url or "",  # type: ignore[union-attr]
                client_ip=client_ip,
                user_agent=user_agent,
                phase=phase,
            )
            for upd, row, old_s, new_s, old_n, new_n in changes
        ]

    # 2. Audit first: no audit log, no change.
    audit_objects: List[str] = []
    if bucket:
        audit_objects = await asyncio.to_thread(writer, _records(PHASE_REQUESTED), bucket=bucket)
    elif iap_mode:
        raise ReviewAuditLogError(
            "AUDIT_LOG_BUCKET is not configured on the portal service; review not applied."
        )
    else:
        _warn_audit_bucket_missing_once()

    # 3. Sheet write-back of the changed rows only.
    changed_updates = [upd for upd, *_ in changes]
    try:
        updated_count = await gateway.update_report_human_reviews(
            target_job.folder_id,
            target_job.report_sheet_url,
            changed_updates,
        )
    except Exception:
        if bucket:
            try:
                await asyncio.to_thread(writer, _records(PHASE_SHEET_WRITE_FAILED), bucket=bucket)
            except Exception as follow_exc:  # noqa: BLE001 - best effort; keep the original error
                logger.error("Could not write sheet_write_failed audit records: %s", follow_exc)
        raise
    return {
        "ok": True,
        "updated_count": updated_count,
        "persisted_to_sheet": True,
        "job_id": target_job.job_id,
        "report_sheet_url": target_job.report_sheet_url,
        "audit_log_objects": audit_objects,
    }


_SUPERVISOR_PORTAL_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>CHAGEE · Store AI Audit Supervisor Workbench — __PORTAL_STORE_NAME__ #__PORTAL_AUDIT_ID__</title>
<meta name="description" content="CHAGEE · Store AI Audit Supervisor Workbench (CHAGEE 霸王茶姬 · 门店 AI 视频稽核督导工作台): SOP module evidence cards, 20-second video clip preview, and 48-hour rectification notice." />
<style>
  /* ============================================================
     Design tokens — 克制的三色语义体系（红线 / 偏差 / 合规）
     ============================================================ */
  :root {
    --canvas:        #F5F6F8;
    --surface:       #FFFFFF;
    --surface-sunk:  #FAFBFC;
    --surface-dark:  #14171C;

    --ink-900: #14171C;
    --ink-700: #3A4049;
    --ink-500: #697180;
    --ink-400: #969DA9;

    --line:        #E7E9ED;
    --line-strong: #D5D9E0;

    --red:        #C32B38;
    --red-deep:   #96131F;
    --red-soft:   #FDF3F4;
    --red-line:   #F3CDD1;

    --amber:      #A96410;
    --amber-soft: #FDF8EE;
    --amber-line: #EDDCBE;

    --green:      #0E7257;
    --green-soft: #EFF7F3;
    --green-line: #C8E4D8;

    --slate-soft: #F1F3F6;

    --r-sm: 8px;
    --r-md: 12px;
    --r-lg: 18px;

    --sh-1: 0 1px 2px rgba(20,23,28,.05);
    --sh-2: 0 4px 16px rgba(20,23,28,.07);
    --sh-3: 0 18px 48px rgba(20,23,28,.18);

    --gut: 32px;
    --maxw: 1400px;
  }

  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  html { scroll-behavior: smooth; scroll-padding-top: 150px; }

  body {
    font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Hiragino Sans GB",
                 "Microsoft YaHei", "Noto Sans CJK SC", sans-serif;
    background: var(--canvas);
    color: var(--ink-900);
    font-size: 14px;
    line-height: 1.65;
    -webkit-font-smoothing: antialiased;
    text-rendering: optimizeLegibility;
  }

  .wrap { max-width: var(--maxw); margin: 0 auto; padding: 0 var(--gut); }

  /* ============================================================
     Buttons & atoms
     ============================================================ */
  .btn {
    display: inline-flex; align-items: center; justify-content: center; gap: 7px;
    height: 36px; padding: 0 15px;
    border: 1px solid var(--line-strong); border-radius: var(--r-sm);
    background: var(--surface); color: var(--ink-700);
    font: inherit; font-size: 13px; font-weight: 600;
    cursor: pointer; white-space: nowrap; text-decoration: none;
    transition: background .15s, border-color .15s, color .15s, box-shadow .15s;
  }
  .btn:hover { background: var(--slate-soft); color: var(--ink-900); }
  .btn-solid { background: var(--red); border-color: var(--red); color: #fff; }
  .btn-solid:hover { background: var(--red-deep); border-color: var(--red-deep); color: #fff; }
  .btn-ghost-dark { background: transparent; border-color: rgba(255,255,255,.22); color: #E7E9ED; }
  .btn-ghost-dark:hover { background: rgba(255,255,255,.1); color: #fff; }
  .btn-sm { height: 30px; padding: 0 11px; font-size: 12px; }
  .btn-icon { width: 34px; height: 34px; padding: 0; font-size: 14px; }

  .tag {
    display: inline-flex; align-items: center; gap: 5px;
    height: 22px; padding: 0 9px; border-radius: 999px;
    font-size: 11.5px; font-weight: 700; letter-spacing: .01em;
    white-space: nowrap; font-variant-numeric: tabular-nums;
  }
  .tag-red    { background: var(--red-soft);   color: var(--red-deep); box-shadow: inset 0 0 0 1px var(--red-line); }
  .tag-amber  { background: var(--amber-soft); color: var(--amber);    box-shadow: inset 0 0 0 1px var(--amber-line); }
  .tag-green  { background: var(--green-soft); color: var(--green);    box-shadow: inset 0 0 0 1px var(--green-line); }
  .tag-slate  { background: var(--slate-soft); color: var(--ink-500); }
  .tag-solid-red   { background: var(--red);   color: #fff; }
  .tag-solid-amber { background: var(--amber); color: #fff; }
  .tag-solid-dark  { background: rgba(10,12,16,.78); color: #fff; backdrop-filter: blur(6px); }

  .dot { width: 7px; height: 7px; border-radius: 50%; flex: none; }
  .dot-red { background: var(--red); }
  .dot-amber { background: var(--amber); }

  .mono { font-variant-numeric: tabular-nums; }
  .muted { color: var(--ink-500); }

  /* ============================================================
     Header
     ============================================================ */
  .app-header {
    position: sticky; top: 0; z-index: 60;
    background: var(--surface-dark); color: #fff;
  }
  .app-header .wrap {
    min-height: 68px; display: flex; align-items: center;
    justify-content: space-between; gap: 24px; flex-wrap: wrap; padding-top: 12px; padding-bottom: 12px;
  }
  .ident { display: flex; align-items: center; gap: 16px; min-width: 0; }
  .logo {
    width: 38px; height: 38px; flex: none; border-radius: 10px;
    background: var(--red); color: #fff;
    display: grid; place-items: center;
    font-weight: 800; font-size: 15px; letter-spacing: -.02em;
  }
  .ident h1 { font-size: 16px; font-weight: 700; letter-spacing: -.01em; line-height: 1.3; }
  .ident .sub {
    margin-top: 2px; font-size: 12px; color: #9AA1AC;
    display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
  }
  .ident .sub b { color: #E7E9ED; font-weight: 600; font-variant-numeric: tabular-nums; }
  .ident .sub .sep { color: #4A515C; }
  .ident .sub a { color: #B9C2CF; text-decoration: none; border-bottom: 1px solid #39414C; }
  .ident .sub a:hover { color: #fff; border-color: #7E8794; }
  .head-actions { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }

  /* ============================================================
     Verdict hero
     ============================================================ */
  .hero { padding: 36px 0 4px; }
  .hero-grid {
    display: grid; grid-template-columns: minmax(0, 1.5fr) minmax(300px, .85fr);
    gap: 20px; align-items: stretch;
  }
  @media (max-width: 1040px) { .hero-grid { grid-template-columns: 1fr; } }

  .panel {
    background: var(--surface); border: 1px solid var(--line);
    border-radius: var(--r-lg); box-shadow: var(--sh-1);
  }

  .verdict { padding: 30px 32px; display: flex; flex-direction: column; gap: 22px; }
  .verdict-kicker {
    font-size: 11.5px; font-weight: 700; letter-spacing: .14em;
    text-transform: uppercase; color: var(--ink-400);
  }
  .verdict-main { display: flex; align-items: flex-end; gap: 28px; flex-wrap: wrap; }
  .figure { display: flex; align-items: flex-end; gap: 10px; }
  .figure .n {
    font-size: 64px; font-weight: 800; line-height: .88;
    letter-spacing: -.045em; font-variant-numeric: tabular-nums;
  }
  .figure .n.red { color: var(--red); }
  .figure .unit { font-size: 14px; font-weight: 600; color: var(--ink-500); padding-bottom: 7px; }
  .verdict-divider { width: 1px; align-self: stretch; background: var(--line); margin: 4px 0; }
  .figure .n.amber { color: var(--amber); }

  .verdict-note {
    font-size: 13px; color: var(--ink-700); line-height: 1.7;
    padding: 14px 16px; background: var(--red-soft);
    border: 1px solid var(--red-line); border-radius: var(--r-md);
  }
  .verdict-note b { color: var(--red-deep); }

  .batch-switcher-bar {
    background: #1C2028; border-bottom: 1px solid #2C323E; color: #D5D9E0;
    padding: 8px 0; font-size: 12px;
  }
  .batch-switcher-bar .wrap {
    display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
  }
  .batch-switcher-bar .bs-label {
    font-weight: 700; color: #9AA1AC; margin-right: 4px;
  }
  .batch-pill {
    display: inline-flex; align-items: center; gap: 6px;
    padding: 4px 11px; border-radius: 999px; font-size: 12px; font-weight: 600;
    text-decoration: none; color: #D5D9E0; background: rgba(255,255,255,.06);
    border: 1px solid rgba(255,255,255,.12); transition: all .14s;
  }
  .batch-pill:hover { background: rgba(255,255,255,.14); color: #fff; }
  .batch-pill.active {
    background: var(--red); border-color: var(--red); color: #fff; font-weight: 700;
  }
  .job-status-banner {
    margin-bottom: 18px; padding: 12px 16px; border-radius: var(--r-md);
    font-size: 13px; font-weight: 600; display: flex; align-items: center; gap: 10px;
  }
  .job-status-banner.running {
    background: var(--amber-soft); border: 1px solid var(--amber-line); color: var(--amber);
  }
  .job-status-banner.failed {
    background: var(--red-soft); border: 1px solid var(--red-line); color: var(--red-deep);
  }
  .verdict-note.is-clean {
    background: var(--green-soft); border-color: var(--green-line); color: var(--green);
  }
  .verdict-note.is-clean b { color: var(--green); }


  .modbars { display: flex; flex-direction: column; gap: 11px; }
  .modbar { display: grid; grid-template-columns: 170px 1fr 54px; gap: 14px; align-items: center; font-size: 12.5px; }
  .modbar .nm { color: var(--ink-700); font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .modbar .track { height: 8px; border-radius: 999px; background: var(--slate-soft); overflow: hidden; display: flex; }
  .modbar .fill-red { background: var(--red); }
  .modbar .fill-amber { background: var(--amber); }
  .modbar .vl { text-align: right; font-weight: 700; color: var(--ink-900); font-variant-numeric: tabular-nums; }

  /* progress panel */
  .prog { padding: 26px 28px; display: flex; flex-direction: column; gap: 20px; }
  .prog-top { display: flex; align-items: center; gap: 20px; }
  .ring {
    width: 92px; height: 92px; flex: none; border-radius: 50%;
    display: grid; place-items: center; position: relative;
    background: conic-gradient(var(--red) 0deg, var(--red) var(--deg, 0deg), var(--slate-soft) var(--deg, 0deg));
    transition: background .3s ease;
  }
  .ring::after {
    content: ""; position: absolute; inset: 10px;
    background: var(--surface); border-radius: 50%;
  }
  .ring span {
    position: relative; z-index: 1; font-size: 20px; font-weight: 800;
    font-variant-numeric: tabular-nums; letter-spacing: -.02em;
  }
  .prog-top h3 { font-size: 14px; font-weight: 700; }
  .prog-top p { font-size: 12.5px; color: var(--ink-500); margin-top: 3px; }
  .prog-rows { display: flex; flex-direction: column; gap: 2px; }
  .prog-row {
    display: flex; align-items: center; justify-content: space-between; gap: 10px;
    padding: 8px 0; border-top: 1px solid var(--line); font-size: 13px;
  }
  .prog-row .lb { display: flex; align-items: center; gap: 8px; color: var(--ink-700); }
  .prog-row .vv { font-weight: 700; font-variant-numeric: tabular-nums; }

  /* ============================================================
     Toolbar
     ============================================================ */
  .toolbar-shell { position: sticky; top: 68px; z-index: 50; padding-top: 24px; background: var(--canvas); }
  .toolbar {
    background: rgba(255,255,255,.88); backdrop-filter: blur(12px);
    border: 1px solid var(--line); border-radius: var(--r-md);
    box-shadow: var(--sh-1); padding: 12px 16px;
    display: flex; flex-direction: column; gap: 12px;
  }
  .tb-row { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
  .tb-row + .tb-row { padding-top: 12px; border-top: 1px dashed var(--line); }

  .seg { display: inline-flex; background: var(--slate-soft); border-radius: var(--r-sm); padding: 3px; gap: 3px; }
  .seg button {
    border: 0; background: transparent; font: inherit; font-size: 12.5px; font-weight: 600;
    color: var(--ink-500); padding: 6px 13px; border-radius: 6px; cursor: pointer;
    display: inline-flex; align-items: center; gap: 6px; transition: all .14s;
  }
  .seg button:hover { color: var(--ink-900); }
  .seg button[aria-pressed="true"] { background: var(--surface); color: var(--ink-900); box-shadow: var(--sh-1); }
  .seg button[aria-pressed="true"].is-red { color: var(--red); }
  .seg .ct { font-size: 11px; color: var(--ink-400); font-variant-numeric: tabular-nums; }
  .seg button[aria-pressed="true"] .ct { color: inherit; opacity: .7; }

  .jump { display: inline-flex; align-items: center; gap: 6px; flex-wrap: wrap; }
  .jump a {
    font-size: 12.5px; font-weight: 600; color: var(--ink-700); text-decoration: none;
    padding: 6px 12px; border-radius: 999px; border: 1px solid var(--line);
    background: var(--surface); transition: all .14s;
    display: inline-flex; align-items: center; gap: 7px;
  }
  .jump a:hover { border-color: var(--ink-400); color: var(--ink-900); }
  .jump a .n { font-variant-numeric: tabular-nums; color: var(--ink-400); }

  .search { position: relative; flex: 1 1 240px; max-width: 340px; min-width: 200px; }
  .search input {
    width: 100%; height: 36px; padding: 0 34px 0 34px;
    border: 1px solid var(--line-strong); border-radius: var(--r-sm);
    background: var(--surface); font: inherit; font-size: 13px; color: var(--ink-900);
  }
  .search input::placeholder { color: var(--ink-400); }
  .search input:focus { outline: none; border-color: var(--ink-700); box-shadow: 0 0 0 3px rgba(20,23,28,.07); }
  .search .ic { position: absolute; left: 11px; top: 50%; transform: translateY(-50%); color: var(--ink-400); font-size: 13px; }
  .search .clr {
    position: absolute; right: 6px; top: 50%; transform: translateY(-50%);
    border: 0; background: transparent; cursor: pointer; color: var(--ink-400);
    font-size: 15px; line-height: 1; padding: 4px; display: none;
  }
  .search.has-val .clr { display: block; }

  .flabel { font-size: 11.5px; font-weight: 700; color: var(--ink-400); letter-spacing: .06em; text-transform: uppercase; }
  .chips { display: inline-flex; gap: 6px; flex-wrap: wrap; }
  .chip {
    border: 1px solid var(--line); background: var(--surface); color: var(--ink-700);
    font: inherit; font-size: 12px; font-weight: 600; padding: 5px 11px;
    border-radius: 999px; cursor: pointer; transition: all .14s; font-variant-numeric: tabular-nums;
  }
  .chip:hover { border-color: var(--ink-400); }
  .chip[aria-pressed="true"] { background: var(--ink-900); border-color: var(--ink-900); color: #fff; }
  .spacer { flex: 1 1 auto; }

  /* ============================================================
     Module sections
     ============================================================ */
  main { padding-bottom: 80px; }
  .module { padding-top: 60px; }

  /* 违规大类别标题卡 —— 贯穿整页的主锚点 */
  .module-banner {
    display: flex; align-items: center; gap: 26px; flex-wrap: wrap;
    background: var(--surface-dark); color: #fff;
    border-radius: var(--r-lg); padding: 26px 30px;
    box-shadow: 0 12px 30px rgba(20, 23, 28, .16);
    position: relative; overflow: hidden;
  }
  .module-banner::after {
    content: attr(data-letter);
    position: absolute; right: 26px; bottom: -42px;
    font-size: 170px; font-weight: 800; line-height: 1;
    color: rgba(255, 255, 255, .035); letter-spacing: -.06em; pointer-events: none;
  }
  .module-badge {
    width: 68px; height: 68px; flex: none; border-radius: 18px;
    background: var(--red); color: #fff;
    display: flex; flex-direction: column; align-items: center; justify-content: center;
    box-shadow: 0 6px 18px rgba(195, 43, 56, .34), inset 0 0 0 1px rgba(255, 255, 255, .16);
  }
  .module-badge .ltr { font-size: 32px; font-weight: 800; letter-spacing: -.04em; line-height: 1; }
  .module-badge .cap {
    font-size: 8.5px; font-weight: 800; letter-spacing: .18em;
    text-transform: uppercase; opacity: .75; margin-top: 3px;
  }
  .module-banner .mtxt { min-width: 0; flex: 1 1 400px; position: relative; z-index: 1; }
  .module-banner .mk {
    font-size: 11px; font-weight: 800; letter-spacing: .2em; text-transform: uppercase;
    color: #8F97A3;
  }
  .module-banner h2 {
    font-size: 27px; font-weight: 800; letter-spacing: -.03em; line-height: 1.22;
    margin-top: 5px; color: #fff;
  }
  .module-banner h2 .pfx { color: var(--red); margin-right: 2px; }
  .module-banner .desc {
    font-size: 13px; color: #9AA1AC; margin-top: 8px; max-width: 74ch; line-height: 1.68;
  }
  .module-banner .counts {
    display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
    margin-left: auto; position: relative; z-index: 1;
  }
  .tag-dark-red   { background: var(--red); color: #fff; }
  .tag-dark-amber { background: #A96410; color: #fff; }
  .tag-dark-slate { background: rgba(255, 255, 255, .13); color: #D5D9E0; }
  .tag-dark-red .dot, .tag-dark-amber .dot { background: rgba(255, 255, 255, .85); }

  @media (max-width: 720px) {
    .module-banner { padding: 22px; gap: 18px; }
    .module-banner h2 { font-size: 23px; }
    .module-banner::after { display: none; }
  }

  /* 跳转导航上的类别字母 */
  .jump a .jl {
    width: 18px; height: 18px; border-radius: 5px; flex: none;
    background: var(--ink-900); color: #fff;
    display: inline-grid; place-items: center;
    font-size: 10.5px; font-weight: 800;
  }

  /* clause group */
  .clause { margin-top: 32px; }
  .clause-head {
    display: flex; align-items: flex-start; gap: 16px; flex-wrap: wrap;
    padding: 16px 20px; background: var(--surface);
    border: 1px solid var(--line); border-radius: var(--r-md) var(--r-md) 0 0;
    border-bottom: 0;
  }
  .clause-code {
    flex: none; min-width: 52px; height: 30px; padding: 0 11px;
    border-radius: var(--r-sm); background: var(--ink-900); color: #fff;
    display: inline-flex; align-items: center; justify-content: center;
    font-size: 14px; font-weight: 800; letter-spacing: -.01em;
  }
  .clause-code.red { background: var(--red); }
  .clause-ttl { min-width: 0; flex: 1 1 320px; }
  .clause-ttl h3 { font-size: 15.5px; font-weight: 700; letter-spacing: -.012em; }
  .clause-ttl .src { font-size: 12px; color: var(--ink-500); margin-top: 3px; }
  .clause-actions { display: flex; align-items: center; gap: 8px; margin-left: auto; }

  .criteria {
    display: grid; grid-template-columns: 1fr 1fr; gap: 1px;
    background: var(--line); border: 1px solid var(--line); border-bottom: 0;
  }
  @media (max-width: 820px) { .criteria { grid-template-columns: 1fr; } }
  .crit { padding: 14px 20px; background: var(--surface-sunk); font-size: 12.5px; line-height: 1.6; }
  .crit .ct {
    font-size: 11px; font-weight: 800; letter-spacing: .08em; text-transform: uppercase;
    display: flex; align-items: center; gap: 6px; margin-bottom: 6px;
  }
  .crit.pass .ct { color: var(--green); }
  .crit.fail .ct { color: var(--red); }
  .crit p { color: var(--ink-700); }

  .sop-raw {
    border: 1px solid var(--line); border-bottom: 0; background: #FBFCFD;
    padding: 0 20px; max-height: 0; overflow: hidden; transition: max-height .25s ease, padding .25s ease;
  }
  .sop-raw.open { max-height: 1400px; overflow: auto; padding: 18px 20px; }
  .sop-raw pre {
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: 11.5px; line-height: 1.75; color: var(--ink-700);
    white-space: pre-wrap; word-break: break-word;
  }

  /* ============================================================
     Evidence cards
     ============================================================ */
  .card-grid {
    display: grid; grid-template-columns: repeat(auto-fill, minmax(372px, 1fr));
    gap: 1px; background: var(--line);
    border: 1px solid var(--line); border-radius: 0 0 var(--r-md) var(--r-md);
    overflow: hidden;
  }
  @media (max-width: 760px) { .card-grid { grid-template-columns: 1fr; } }

  .card {
    background: var(--surface); display: flex; flex-direction: column;
    transition: background .18s;
  }
  .card.is-dismissed { background: var(--surface-sunk); }
  .card.is-dismissed .thumb, .card.is-dismissed .card-body { opacity: .5; }

  /* thumbnail */
  .thumb {
    position: relative; aspect-ratio: 16 / 9; background: #10131A;
    overflow: hidden; cursor: pointer; display: block; width: 100%;
    border: 0; padding: 0;
  }
  .thumb img {
    width: 100%; height: 100%; object-fit: cover; display: block;
    transition: transform .4s cubic-bezier(.2,.7,.3,1), filter .25s;
  }
  .thumb:hover img { transform: scale(1.045); filter: brightness(1.06); }
  .thumb .fallback {
    position: absolute; inset: 0; display: none;
    flex-direction: column; align-items: center; justify-content: center; gap: 8px;
    background:
      radial-gradient(120% 90% at 50% 0%, #252B36 0%, #10131A 70%);
    color: #6E7686; font-size: 12px; text-align: center; padding: 16px;
  }
  .thumb.no-img img { display: none; }
  .thumb.no-img .fallback { display: flex; }
  .thumb .fallback .fb-ic { font-size: 22px; opacity: .55; }
  .thumb .fallback .fb-code { font-size: 13px; font-weight: 800; color: #A8B0BE; letter-spacing: .04em; }

  .thumb .scrim {
    position: absolute; inset: 0; pointer-events: none;
    background: linear-gradient(180deg, rgba(10,12,16,.42) 0%, rgba(10,12,16,0) 34%,
                rgba(10,12,16,0) 56%, rgba(10,12,16,.58) 100%);
  }
  .thumb .tl { position: absolute; top: 10px; left: 10px; display: flex; gap: 6px; flex-wrap: wrap; }
  .thumb .tr { position: absolute; top: 10px; right: 10px; }
  .thumb .bl { position: absolute; bottom: 10px; left: 10px; display: flex; gap: 6px; align-items: center; }
  .thumb .br { position: absolute; bottom: 10px; right: 10px; }
  .play {
    position: absolute; left: 50%; top: 50%; transform: translate(-50%,-50%) scale(.94);
    width: 54px; height: 54px; border-radius: 50%;
    background: rgba(255,255,255,.92); color: #14171C;
    display: grid; place-items: center; font-size: 17px; padding-left: 3px;
    box-shadow: 0 6px 20px rgba(0,0,0,.35); opacity: 0;
    transition: opacity .2s ease, transform .2s ease; pointer-events: none;
  }
  .thumb:hover .play, .thumb:focus-visible .play { opacity: 1; transform: translate(-50%,-50%) scale(1); }
  .thumb:focus-visible { outline: 3px solid var(--red); outline-offset: -3px; }

  /* body */
  .card-body { padding: 18px 20px 16px; display: flex; flex-direction: column; gap: 12px; flex: 1; }
  .card-meta { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; font-size: 11.5px; color: var(--ink-500); }
  .card-meta .code { font-weight: 800; color: var(--ink-900); font-size: 12px; letter-spacing: .02em; }
  .card-meta .bullet { color: var(--line-strong); }

  .card h4 {
    font-size: 15.5px; font-weight: 700; line-height: 1.45; letter-spacing: -.014em;
    color: var(--ink-900);
  }
  .evid { font-size: 13px; line-height: 1.72; color: var(--ink-700); }
  .evid.clamp { display: -webkit-box; -webkit-line-clamp: 4; -webkit-box-orient: vertical; overflow: hidden; }
  .evid .hl-s { font-weight: 700; color: var(--ink-900); box-shadow: inset 0 -.52em 0 #E8ECF3; }
  .evid .hl-t { font-weight: 700; color: var(--red-deep); font-variant-numeric: tabular-nums; }
  .more {
    align-self: flex-start; border: 0; background: transparent; padding: 0;
    font: inherit; font-size: 12px; font-weight: 700; color: var(--ink-500);
    cursor: pointer; text-decoration: underline; text-underline-offset: 3px;
  }
  .more:hover { color: var(--ink-900); }

  details.fold { border-top: 1px dashed var(--line); padding-top: 11px; }
  details.fold > summary {
    list-style: none; cursor: pointer; font-size: 12px; font-weight: 700; color: var(--ink-500);
    display: flex; align-items: center; gap: 7px; user-select: none;
  }
  details.fold > summary::-webkit-details-marker { display: none; }
  details.fold > summary::after { content: "▾"; font-size: 10px; margin-left: auto; transition: transform .2s; }
  details.fold[open] > summary::after { transform: rotate(180deg); }
  details.fold > summary:hover { color: var(--ink-900); }
  .fold-body { padding-top: 11px; display: flex; flex-direction: column; gap: 10px; }
  .fold-line { font-size: 12.5px; line-height: 1.65; color: var(--ink-700); }
  .fold-line .k { font-weight: 700; color: var(--ink-900); }
  .fold-line.bad .k { color: var(--red-deep); }

  .advice {
    font-size: 12.5px; line-height: 1.65; color: #6B4A12;
    background: var(--amber-soft); border: 1px solid var(--amber-line);
    border-radius: var(--r-sm); padding: 10px 12px;
  }
  .advice .k { font-weight: 700; color: var(--amber); }

  /* footer / decision */
  .card-foot { border-top: 1px solid var(--line); padding: 12px 20px 16px; background: var(--surface-sunk); }
  .card.is-dismissed .card-foot { background: #F5F6F8; }
  .dec-row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
  .dec {
    display: inline-flex; border: 1px solid var(--line-strong); border-radius: var(--r-sm);
    overflow: hidden; background: var(--surface);
  }
  .dec button {
    border: 0; background: transparent; font: inherit; font-size: 12px; font-weight: 600;
    color: var(--ink-700); padding: 7px 11px; cursor: pointer; transition: all .14s;
  }
  .dec button + button { border-left: 1px solid var(--line-strong); }
  .dec button:hover { background: var(--slate-soft); }

  /* 已处置：按钮置灰并改为完成态文案，仍可点击撤销 */
  .dec button[aria-pressed="true"] {
    background: #E3E6EB; color: var(--ink-400); font-weight: 700;
    cursor: default; box-shadow: inset 0 0 0 1px rgba(20,23,28,.06);
  }
  .dec button[aria-pressed="true"]:hover { background: #DBDFE6; color: var(--ink-500); }
  .dec.has-state button:not([aria-pressed="true"]) { opacity: .45; }
  .dec.has-state button:not([aria-pressed="true"]):hover { opacity: 1; }

  /* 卡片当前处置状态标记 */
  .state-flag {
    display: inline-flex; align-items: center; gap: 5px;
    font-size: 11.5px; font-weight: 700; padding: 1px 8px; border-radius: 999px;
  }
  .state-flag.c { background: var(--red-soft); color: var(--red-deep); }
  .state-flag.d { background: var(--green-soft); color: var(--green); }
  .state-flag.f { background: var(--amber-soft); color: var(--amber); }

  /* 顶部摘要区下载入口 */
  .verdict-dl {
    display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
    padding-top: 18px; border-top: 1px solid var(--line);
  }
  .verdict-dl .hint { font-size: 12px; color: var(--ink-400); }
  .note-row { display: flex; gap: 8px; margin-top: 10px; }
  .note-row input {
    flex: 1; height: 32px; padding: 0 10px; font: inherit; font-size: 12.5px;
    border: 1px solid var(--line); border-radius: var(--r-sm); background: var(--surface); color: var(--ink-900);
  }
  .note-row input::placeholder { color: var(--ink-400); }
  .note-row input:focus { outline: none; border-color: var(--ink-700); }

  .empty {
    margin-top: 32px; padding: 72px 32px; text-align: center;
    background: var(--surface); border: 1px dashed var(--line-strong); border-radius: var(--r-lg);
  }
  .empty .ic { font-size: 30px; opacity: .3; }
  .empty h3 { margin-top: 14px; font-size: 16px; font-weight: 700; }
  .empty p { margin-top: 6px; font-size: 13px; color: var(--ink-500); }

  /* ============================================================
     Appendix
     ============================================================ */
  .appendix { margin-top: 72px; }
  .ap-head { display: flex; align-items: center; gap: 14px; padding-bottom: 18px; }
  .ap-head h2 { font-size: 19px; font-weight: 800; letter-spacing: -.02em; }
  .ap-head .ln { flex: 1; height: 1px; background: var(--line-strong); }

  details.ap { background: var(--surface); border: 1px solid var(--line); border-radius: var(--r-md); margin-bottom: 14px; }
  details.ap > summary {
    list-style: none; cursor: pointer; padding: 18px 22px;
    display: flex; align-items: center; gap: 12px; user-select: none;
  }
  details.ap > summary::-webkit-details-marker { display: none; }
  details.ap > summary h3 { font-size: 14.5px; font-weight: 700; }
  details.ap > summary .hint { font-size: 12.5px; color: var(--ink-500); }
  details.ap > summary::after { content: "▾"; margin-left: auto; color: var(--ink-400); transition: transform .2s; }
  details.ap[open] > summary::after { transform: rotate(180deg); }
  .ap-body { border-top: 1px solid var(--line); overflow-x: auto; }

  table.dt { width: 100%; border-collapse: collapse; font-size: 12.5px; }
  table.dt th, table.dt td { padding: 12px 16px; text-align: left; vertical-align: top; border-bottom: 1px solid var(--line); }
  table.dt thead th {
    position: sticky; top: 0; background: var(--surface-sunk); z-index: 1;
    font-size: 11px; font-weight: 800; letter-spacing: .06em; text-transform: uppercase; color: var(--ink-500);
    white-space: nowrap;
  }
  table.dt tbody tr:hover { background: #FCFCFD; }
  table.dt tbody tr.hit { background: var(--red-soft); }
  table.dt tbody tr.hit:hover { background: #FBEBEC; }
  table.dt td.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
  table.dt .rid { font-weight: 800; white-space: nowrap; }
  table.dt tfoot td { padding: 12px 16px; font-weight: 800; background: var(--surface-sunk); font-variant-numeric: tabular-nums; }

  /* ============================================================
     Modal
     ============================================================ */
  .scrim-modal {
    position: fixed; inset: 0; z-index: 100; display: none;
    align-items: center; justify-content: center; padding: 28px;
    background: rgba(12,15,20,.6); backdrop-filter: blur(3px);
  }
  .scrim-modal.open { display: flex; }
  .modal {
    background: var(--surface); width: 100%; max-width: 1180px; max-height: 90vh;
    border-radius: var(--r-lg); overflow: hidden; box-shadow: var(--sh-3);
    display: flex; flex-direction: column;
  }
  .modal-head {
    background: var(--surface-dark); color: #fff; padding: 14px 20px;
    display: flex; align-items: center; gap: 14px; flex-wrap: wrap;
  }
  .modal-head .mt { min-width: 0; flex: 1; }
  .modal-head .mt .l1 { font-size: 11.5px; color: #969DA9; font-weight: 600; letter-spacing: .04em; }
  .modal-head .mt .l2 { font-size: 15px; font-weight: 700; letter-spacing: -.012em; margin-top: 2px; }
  .modal-head .ma { display: flex; align-items: center; gap: 8px; }
  .modal-body { display: grid; grid-template-columns: minmax(0, 1.5fr) minmax(320px, .9fr); overflow: hidden; flex: 1; }
  @media (max-width: 900px) { .modal-body { grid-template-columns: 1fr; overflow-y: auto; } }
  .vstage { background: #0A0C10; display: flex; flex-direction: column; min-height: 0; }
  .vstage .frame { flex: 1; min-height: 340px; }
  .vstage iframe { width: 100%; height: 100%; border: 0; display: block; }
  .vstage .cap {
    padding: 11px 18px; font-size: 11.5px; color: #7E8794;
    display: flex; justify-content: space-between; gap: 12px; flex-wrap: wrap;
    border-top: 1px solid #1C212A;
  }
  .vstage .cap b { color: #DDE2EA; font-variant-numeric: tabular-nums; }
  .mside {
    padding: 22px; background: var(--surface-sunk); border-left: 1px solid var(--line);
    overflow-y: auto; display: flex; flex-direction: column; gap: 16px;
  }
  .mside h5 { font-size: 11px; font-weight: 800; letter-spacing: .1em; text-transform: uppercase; color: var(--ink-400); }
  .mside .blk { background: var(--surface); border: 1px solid var(--line); border-radius: var(--r-md); padding: 14px 16px; }
  .mside .blk .tx { font-size: 13px; line-height: 1.7; color: var(--ink-700); margin-top: 8px; }
  .mside .foot-actions { margin-top: auto; display: flex; gap: 8px; flex-wrap: wrap; padding-top: 6px; }
  .mside .foot-actions .btn { flex: 1 1 auto; }

  .report { padding: 24px; overflow-y: auto; background: var(--surface); }
  .report pre {
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: 12px; line-height: 1.72; white-space: pre-wrap; word-break: break-word;
    color: var(--ink-900); background: var(--surface-sunk);
    border: 1px solid var(--line); border-radius: var(--r-md); padding: 22px;
  }

  .toast {
    position: fixed; left: 50%; bottom: 32px; transform: translate(-50%, 16px);
    background: var(--ink-900); color: #fff; padding: 11px 20px; border-radius: 999px;
    font-size: 13px; font-weight: 600; box-shadow: var(--sh-3); z-index: 200;
    opacity: 0; pointer-events: none; transition: opacity .22s ease, transform .22s ease;
  }
  .toast.show { opacity: 1; transform: translate(-50%, 0); }

  .sr {
    position: absolute; width: 1px; height: 1px; overflow: hidden;
    clip: rect(0 0 0 0); white-space: nowrap;
  }

  /* ============================================================
     证据切片访问性诊断条
     ============================================================ */
  .access-bar { padding-top: 20px; }
  .access-box {
    background: var(--amber-soft); border: 1px solid var(--amber-line);
    border-radius: var(--r-md); padding: 14px 18px;
    display: flex; align-items: flex-start; gap: 14px; flex-wrap: wrap;
  }
  .access-box .ai { font-size: 17px; line-height: 1.3; flex: none; }
  .access-box .atx { min-width: 0; flex: 1 1 420px; }
  .access-box .atx b { color: #7A4A0C; }
  .access-box .atx p { font-size: 12.5px; color: #6B4A12; line-height: 1.65; margin-top: 3px; }
  .access-box .aa { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-left: auto; }
  .access-box select {
    height: 30px; padding: 0 8px; font: inherit; font-size: 12px;
    border: 1px solid var(--amber-line); border-radius: var(--r-sm);
    background: #fff; color: var(--ink-700);
  }
  .access-help {
    flex: 1 1 100%; margin-top: 4px; padding-top: 12px;
    border-top: 1px dashed var(--amber-line);
    font-size: 12.5px; color: #6B4A12; line-height: 1.75;
  }
  .access-help ol { padding-left: 20px; }
  .access-help code {
    background: #fff; border: 1px solid var(--amber-line); border-radius: 4px;
    padding: 0 4px; font-size: 11.5px;
  }

  /* 弹窗内的权限提示 */
  .vnotice {
    padding: 10px 18px; background: #2A1E0A; color: #E3C99A;
    font-size: 12px; display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
    border-bottom: 1px solid #3A2B10;
  }
  .vnotice b { color: #F5D9A8; }
  .vnotice .spacer { flex: 1 1 auto; }
  .vnotice select {
    height: 26px; font: inherit; font-size: 11.5px; border-radius: 5px;
    border: 1px solid #4A381A; background: #19120A; color: #E3C99A; padding: 0 6px;
  }

  /* ============================================================
     Print — 只输出整改通知单正文
     ============================================================ */
  @media print {
    body { background: #fff; }
    .app-header, .batch-switcher-bar, .hero, .toolbar-shell, main, .toast, #modal-video { display: none !important; }
    #modal-report { position: static; display: block !important; padding: 0; background: #fff; backdrop-filter: none; }
    #modal-report .modal { max-width: none; max-height: none; box-shadow: none; border-radius: 0; }
    #modal-report .modal-head { background: #fff; color: #000; border-bottom: 2px solid #000; padding: 0 0 10px; }
    #modal-report .modal-head .ma { display: none; }
    #modal-report .report { padding: 0; }
    #modal-report .report pre { border: 0; background: #fff; padding: 0; font-size: 10.5px; }
  }
</style>
</head>
<body>

<header class="app-header">
  <div class="wrap">
    <div class="ident">
      <div class="logo">CH</div>
      <div>
        <h1 id="hd-brand-title">Store AI Video Audit · Supervisor Workbench</h1>
        <div class="sub">
          <b id="hd-store">1-Cantavil D2</b>
          <span class="sep">/</span>
          <span><span id="hd-audit-lbl">Audit</span> <b id="hd-audit">#39e851</b></span>
          <span class="sep">/</span>
          <span id="hd-scope">3 × 1080P Kitchen CCTV Clips</span>
          <span class="sep">/</span>
          <a id="hd-folder" href="#" target="_blank" rel="noopener">Store Folder ↗</a>
          <a id="hd-sheet" href="#" target="_blank" rel="noopener">Source Sheet ↗</a>
        </div>
      </div>
    </div>
    <div class="head-actions">
      <button class="btn btn-ghost-dark btn-sm" id="btn-lang-toggle" onclick="toggleLang()" title="Switch UI Language (EN / 中文)">🌐 EN | 中文</button>
      <button class="btn btn-ghost-dark btn-sm" id="btn-sync-sheet" onclick="syncFromSheet()" title="Sync latest team review decisions from Google Sheet">🔄 Sync Sheet</button>
      <button class="btn btn-ghost-dark" id="btn-batch-confirm" onclick="batchConfirm()">Confirm All AI Hits</button>
      <button class="btn btn-solid" id="btn-open-report" onclick="openReport()">Generate 48h Notice</button>
    </div>
  </div>
</header>

<div class="batch-switcher-bar" id="batch-switcher-bar" hidden>
  <div class="wrap" id="batch-switcher-inner">
    <span class="bs-label">🏪 Store Switcher:</span>
  </div>
</div>

<div class="access-bar" id="access-bar" hidden>
  <div class="wrap">
    <div class="access-box">
      <span class="ai">⚠</span>
      <div class="atx">
        <b id="access-title">Evidence video clips cannot be previewed</b>
        <p id="access-desc">—</p>
      </div>
      <div class="aa">
        <select id="authuser-sel" onchange="setAuthUser(this.value)" title="Switch Google account index">
          <option value="" id="opt-default-acct">Default Google Account</option>
          <option value="0">authuser=0</option>
          <option value="1">authuser=1</option>
          <option value="2">authuser=2</option>
          <option value="3">authuser=3</option>
        </select>
        <a class="btn btn-sm" id="access-folder" href="#" target="_blank" rel="noopener">Open Store Folder ↗</a>
        <button class="btn btn-sm" id="btn-access-help" onclick="toggleAccessHelp()">How to Fix</button>
      </div>
      <div class="access-help" id="access-help" hidden>
        <ol id="access-help-list">
          <li>Open the <b>Store Folder</b> above using a Google account that has access.</li>
          <li>Select the evidence clip files (or parent folder) → <b>Share</b> → set General Access to <code>Anyone with the link · Viewer</code>.</li>
          <li>Refresh this page to restore both thumbnail previews and embedded video playback.</li>
          <li>If the folder is shared with <b>another</b> signed-in Google account in your browser, select its <code>authuser</code> index on the left (`0` = primary browser account).</li>
        </ol>
      </div>
    </div>
  </div>
</div>

<section class="hero">
  <div class="wrap">
    <div id="job-status-banner" class="job-status-banner" hidden></div>
    <div class="hero-grid">

      <div class="panel verdict">
        <div class="verdict-kicker" id="v-kicker">AUDIT VERDICT SUMMARY · APAC Cloud Audit Parameters 2026 v1.0</div>

        <div class="verdict-main">
          <div class="figure">
            <span class="n red" id="v-red">—</span>
            <span class="unit" id="v-unit-red">Red-Line<br />Zero-Tolerance</span>
          </div>
          <div class="verdict-divider"></div>
          <div class="figure">
            <span class="n amber" id="v-normal">—</span>
            <span class="unit" id="v-unit-normal">Standard<br />SOP Deviations</span>
          </div>
          <div class="verdict-divider"></div>
          <div class="figure">
            <span class="n" id="v-clause">—</span>
            <span class="unit"><span id="v-unit-clause">SOP Clauses<br />Triggered</span> <span id="v-clause-total" class="mono"></span></span>
          </div>
        </div>

        <div class="verdict-note" id="v-note">—</div>

        <div class="modbars" id="v-modbars"></div>

        <div class="verdict-dl">
          <button class="btn btn-solid" id="btn-export-xlsx" onclick="exportXLSX()">⤓ Export Excel (.xlsx)</button>
          <button class="btn" id="btn-export-csv" onclick="exportCSV()">⤓ Export Review CSV</button>
          <span class="hint" id="dl-hint">—</span>
        </div>
      </div>

      <div class="panel prog">
        <div class="prog-top">
          <div class="ring" id="p-ring"><span id="p-pct">0%</span></div>
          <div>
            <h3 id="p-title">Supervisor Review Progress</h3>
            <p id="p-sub">Not started yet</p>
          </div>
        </div>
        <div class="prog-rows">
          <div class="prog-row">
            <span class="lb"><i class="dot dot-red"></i> <span id="p-lbl-c">Confirmed Violations</span></span>
            <span class="vv" id="p-c">0</span>
          </div>
          <div class="prog-row">
            <span class="lb"><i class="dot" style="background:var(--green)"></i> <span id="p-lbl-d">False Positives Dismissed</span></span>
            <span class="vv" id="p-d">0</span>
          </div>
          <div class="prog-row">
            <span class="lb"><i class="dot dot-amber"></i> <span id="p-lbl-f">Follow-Up Footage Needed</span></span>
            <span class="vv" id="p-f">0</span>
          </div>
          <div class="prog-row">
            <span class="lb"><i class="dot" style="background:var(--line-strong)"></i> <span id="p-lbl-p">Pending Review</span></span>
            <span class="vv" id="p-p">0</span>
          </div>
        </div>
        <button class="btn btn-sm" id="btn-reset-review" onclick="resetReview()" style="align-self:flex-start">Reset Local Reviews</button>
      </div>

    </div>
  </div>
</section>

<div class="toolbar-shell">
  <div class="wrap">
    <div class="toolbar">
      <div class="tb-row">
        <div class="seg" role="group" aria-label="Severity Filter">
          <button id="sev-ALL"      aria-pressed="true"  onclick="setFilter('sev','ALL')"><span id="lbl-sev-ALL">All Events</span> <span class="ct" id="ct-sev-ALL"></span></button>
          <button id="sev-RED_LINE" aria-pressed="false" class="is-red" onclick="setFilter('sev','RED_LINE')"><i class="dot dot-red"></i> <span id="lbl-sev-RED">Red Line</span> <span class="ct" id="ct-sev-RED_LINE"></span></button>
          <button id="sev-NORMAL"   aria-pressed="false" onclick="setFilter('sev','NORMAL')"><i class="dot dot-amber"></i> <span id="lbl-sev-NORMAL">Standard</span> <span class="ct" id="ct-sev-NORMAL"></span></button>
          <button id="sev-UNVERIFIED" aria-pressed="false" onclick="setFilter('sev','UNVERIFIED')"><span id="lbl-sev-UNVERIFIED">Unverified</span> <span class="ct" id="ct-sev-UNVERIFIED"></span></button>
        </div>

        <nav class="jump" id="jump-nav" aria-label="Module Navigation"></nav>

        <div class="spacer"></div>

        <div class="search" id="search-wrap">
          <span class="ic">⌕</span>
          <input id="q" type="search" placeholder="Search clause A2 / staff / phone / handwash / scoop…" oninput="onSearch()" />
          <button class="clr" onclick="clearSearch()" aria-label="Clear search">×</button>
        </div>
        <button class="btn btn-sm" id="btn-more" onclick="toggleMore()">More Filters</button>
      </div>

      <div class="tb-row" id="more-row" hidden>
        <span class="flabel" id="flbl-video">Source Video</span>
        <div class="chips" id="chips-video"></div>
        <span class="flabel" id="flbl-staff" style="margin-left:10px">Staff</span>
        <div class="chips" id="chips-staff"></div>
        <span class="flabel" id="flbl-review" style="margin-left:10px">Review Status</span>
        <div class="chips" id="chips-review"></div>
      </div>
    </div>
  </div>
</div>

<main>
  <div class="wrap">
    <div id="sections"></div>

    <section class="appendix">
      <div class="ap-head">
        <h2 id="ap-main-title">Appendix</h2>
        <span class="ln"></span>
        <span class="muted" id="ap-main-sub" style="font-size:12.5px">SOP Rule Catalog & Token Ledger</span>
      </div>

      <details class="ap">
        <summary>
          <h3 id="ap-dict-title">Full SOP Rule Catalog</h3>
          <span class="hint" id="ap-dict-hint"></span>
        </summary>
        <div class="ap-body" id="ap-dict"></div>
      </details>

      <details class="ap">
        <summary>
          <h3 id="ap-ledger-title">Video Token Consumption & Cost Ledger</h3>
          <span class="hint" id="ap-ledger-hint"></span>
        </summary>
        <div class="ap-body" id="ap-ledger"></div>
      </details>
    </section>
  </div>
</main>

<!-- Evidence Clip Player Modal -->
<div class="scrim-modal" id="modal-video" onclick="if(event.target===this) closeVideo()">
  <div class="modal" role="dialog" aria-modal="true" aria-labelledby="mv-title">
    <div class="modal-head">
      <div class="mt">
        <div class="l1" id="mv-kicker">—</div>
        <div class="l2" id="mv-title">—</div>
      </div>
      <div class="ma">
        <button class="btn btn-ghost-dark btn-sm" id="mv-prev" onclick="stepVideo(-1)">← Prev</button>
        <button class="btn btn-ghost-dark btn-sm" id="mv-next" onclick="stepVideo(1)">Next →</button>
        <a class="btn btn-ghost-dark btn-sm" id="mv-drive" href="#" target="_blank" rel="noopener">Open in Drive ↗</a>
        <button class="btn btn-ghost-dark btn-sm" id="mv-close" onclick="closeVideo()">Close ESC</button>
      </div>
    </div>
    <div class="modal-body">
      <div class="vstage">
        <div class="vnotice" id="mv-notice" hidden>
          <span id="mv-notice-txt">Seeing Google <b>"You need access"</b>? The clip isn't link-shared or your active browser account lacks access.</span>
          <span class="spacer"></span>
          <select id="mv-authuser" onchange="setAuthUser(this.value)" title="Switch Google account index">
            <option value="" id="mv-opt-default">Default Account</option>
            <option value="0">authuser=0</option>
            <option value="1">authuser=1</option>
            <option value="2">authuser=2</option>
            <option value="3">authuser=3</option>
          </select>
        </div>
        <div class="frame"><iframe id="mv-iframe" src="" allow="autoplay; encrypted-media" allowfullscreen title="Violation Evidence Clip"></iframe></div>
        <div class="cap">
          <span id="mv-cap-note">Evidence clip spans 2s before to 18s after the detected violation</span>
          <span id="mv-ts"></span>
        </div>
      </div>
      <div class="mside" id="mv-side"></div>
    </div>
  </div>
</div>

<!-- 48h Rectification Report Modal -->
<div class="scrim-modal" id="modal-report" onclick="if(event.target===this) closeReport()">
  <div class="modal" style="max-width:920px" role="dialog" aria-modal="true">
    <div class="modal-head">
      <div class="mt">
        <div class="l1" id="mr-kicker">Ready to paste into Google Chat / Feishu / WeCom</div>
        <div class="l2" id="mr-title">Store AI Cloud Audit — 48-Hour Rectification Notice</div>
      </div>
      <div class="ma">
        <button class="btn btn-solid btn-sm" id="mr-btn-copy" onclick="copyReport()">Copy Full Text</button>
        <button class="btn btn-ghost-dark btn-sm" id="mr-btn-print" onclick="printReport()">Print / PDF</button>
        <button class="btn btn-ghost-dark btn-sm" id="mr-btn-close" onclick="closeReport()">Close ESC</button>
      </div>
    </div>
    <div class="report"><pre id="mr-text"></pre></div>
  </div>
</div>

<div class="toast" id="toast" role="status" aria-live="polite"></div>

<script type="application/json" id="portal-data">
__PORTAL_JSON__
</script>

<script>
/* ==================================================================
   CHAGEE AI Audit Supervisor Workbench — Application Logic
   Bilingual (`EN | 中文`, default `en`) + Real-Time Team Sheet Sync
   ================================================================== */
const DATA = JSON.parse(document.getElementById("portal-data").textContent);
const F = DATA.findings || [];
const RULES = {};
(DATA.sop_rules || []).forEach(r => RULES[r.rule_id] = r);

const LANG_KEY = "chagee_portal_lang";
let lang = "en";
try {
  const qsLang = new URLSearchParams(window.location.search).get("lang");
  const savedLang = localStorage.getItem(LANG_KEY);
  if (qsLang === "zh" || qsLang === "en") lang = qsLang;
  else if (savedLang === "zh" || savedLang === "en") lang = savedLang;
} catch (e) {}

const isZh = () => lang === "zh";
const tr = (enStr, zhStr) => isZh() ? zhStr : enStr;

const fSubTitle = f => isZh() ? (f.sub_clause_title_zh || f.sub_clause_title) : (f.sub_clause_title_en || f.sub_clause_title);
const fPass = f => isZh() ? (f.specific_pass_zh || f.specific_pass) : (f.specific_pass_en || f.specific_pass);
const fFail = f => isZh() ? (f.sop_fail_criteria_zh || f.sop_fail_criteria) : (f.sop_fail_criteria_en || f.sop_fail_criteria);
const fAdvice = f => isZh() ? (f.rectification_advice_zh || f.rectification_advice) : (f.rectification_advice_en || f.rectification_advice);
const rTitle = r => isZh()
  ? (r.clean_subtitle_zh || r.name_zh || r.clean_subtitle || r.name || "")
  : (r.clean_subtitle_en || r.name_en || r.clean_subtitle || r.name || "");
const rPass = r => isZh() ? (r.pass_criteria_zh || r.pass_criteria || "—") : (r.pass_criteria_en || r.pass_criteria || "—");
const rFail = r => isZh() ? (r.fail_criteria_zh || r.fail_criteria || "—") : (r.fail_criteria_en || r.fail_criteria || "—");

const MODULE_META = {
  A: {
    order: 1, kicker: "MODULE A",
    title_en: "Handwashing & Personal Hygiene",
    title_zh: "洗手与手部卫生",
    desc_en: "Mandatory handwashing triggers, 5-step handwash verification, and disposable glove compliance (APAC Parameters 1.5.1–1.5.5 & 1.6).",
    desc_zh: "强制洗手触发场景、五步洗手法核对与一次性手套规范。对应 APAC 参数表 1.5.1–1.5.5 与 1.6，是污染链条的源头控制项。"
  },
  B: {
    order: 2, kicker: "MODULE B",
    title_en: "Ice Maker & Ice Handling",
    title_zh: "制冰机与冰块管理",
    desc_en: "Weekly ice maker teardown cleaning, sanitizer dwell lockout window, chemical & 3-color towel separation, and ice scoop hygiene (SOP 8 / SOP 21 & 3.1 / 4.1 / 4.5).",
    desc_zh: "制冰机周清洁流程、部件静置锁定期、化学品与三色毛巾专用，以及冰铲取冰与存放规范。对应 SOP 8 / SOP 21 与参数表 3.1 / 4.1 / 4.5。"
  },
  C: {
    order: 3, kicker: "MODULE C",
    title_en: "Red-Line Safety · Grooming · Tea Brewing",
    title_zh: "常态红线 · 仪容 · 泡茶设备",
    desc_en: "Zero-tolerance red-line scan: cross-contamination, cup handling, improper conduct, personal grooming & mobile phone use, and tea brewing / icing timeliness.",
    desc_zh: "每周必跑的零容忍红线扫描：交叉污染、杯具、不当行为、仪容着装与私人手机，以及泡茶机出茶入冰时效。"
  },
  D: {
    order: 4, kicker: "MODULE D",
    title_en: "Store Operations & Throughput (IPLH)",
    title_zh: "运营与人效 (IPLH)",
    desc_en: "Valid cup output counting, IPLH productivity verification, staffing allocation, and beverage handover quality checks.",
    desc_zh: "有效出杯计数、每工时出杯率核算、人力投放合理性与出品规范抽查。"
  }
};

function metaOf(code) {
  const m = MODULE_META[code];
  if (m) {
    return {
      order: m.order,
      kicker: m.kicker,
      title: isZh() ? m.title_zh : m.title_en,
      desc: isZh() ? m.desc_zh : m.desc_en
    };
  }
  return {
    order: 9,
    kicker: "MODULE " + code,
    title: tr("Spot-Check & Extended Clauses", "专项测试与扩展条款"),
    desc: tr("Targeted SOP spot-checks and custom extended clauses.", "自定义专项抽检与扩展 SOP 条款。")
  };
}

const STORE_KEY = "chagee_review_" + DATA.audit_id + "_v2";
const AUTH_KEY = "chagee_authuser";

/* Drive account index for multi-account browser sessions */
let authUser = null;
try { authUser = localStorage.getItem(AUTH_KEY); } catch (e) {}
if (authUser === "" || authUser === undefined) authUser = null;

const au = (url, sep) => authUser === null ? url : url + sep + "authuser=" + authUser;
const THUMB = id => id ? au("https://drive.google.com/thumbnail?id=" + id + "&sz=w640", "&") : "";
const PREVIEW = f => f.preview_url ? au(f.preview_url, f.preview_url.includes("?") ? "&" : "?") : "";

const badThumbs = new Set();

function noteThumbFail(img, id) {
  const box = img.closest(".thumb");
  if (box) box.classList.add("no-img");
  badThumbs.add(id);
  updateAccessBar();
}

function updateAccessBar() {
  const n = badThumbs.size, total = F.length;
  const bar = document.getElementById("access-bar");
  document.getElementById("access-folder").href = DATA.folder_url;
  if (!n) { bar.hidden = true; return; }
  bar.hidden = false;
  document.getElementById("access-title").textContent =
    n >= total
      ? tr("All " + total + " evidence clips cannot be previewed", "全部 " + total + " 个证据切片当前无法预览")
      : tr(n + " / " + total + " evidence clip thumbnails failed to load", n + " / " + total + " 个证据切片首帧加载失败");
  document.getElementById("access-desc").textContent =
    tr(
      "Google Drive returned 401/403 for these clips, blocking thumbnail and embedded playback. Audit findings, SOP comparison, review sync, and Excel/CSV exports remain unaffected.",
      "Google Drive 对这些文件返回了无权限（匿名预览 401），首帧缩略图与页内播放都会被拦截。违规判定、SOP 对照、复核与报表导出不受影响。"
    )
    + (authUser === null ? "" : tr(" (Active authuser=" + authUser + ")", "（当前已指定 authuser=" + authUser + "）"));
  document.getElementById("authuser-sel").value = authUser === null ? "" : authUser;
}

function toggleAccessHelp() {
  const h = document.getElementById("access-help");
  h.hidden = !h.hidden;
}

function setAuthUser(v) {
  authUser = (v === "" ? null : v);
  try {
    if (authUser === null) localStorage.removeItem(AUTH_KEY);
    else localStorage.setItem(AUTH_KEY, authUser);
  } catch (e) {}
  badThumbs.clear();
  document.getElementById("authuser-sel").value = v;
  document.getElementById("mv-authuser").value = v;
  updateAccessBar();
  renderSections();
  if (activeId != null && document.getElementById("modal-video").classList.contains("open")) {
    const f = F.find(x => x.id === activeId);
    if (f) document.getElementById("mv-iframe").src = PREVIEW(f);
  }
  toast(authUser === null
    ? tr("Switched back to default Google account", "已切回默认 Google 账号")
    : tr("Switched to authuser=" + authUser + ", reloading previews...", "已切换为 authuser=" + authUser + "，正在重载预览"));
}

let review = {};
try { review = JSON.parse(localStorage.getItem(STORE_KEY) || "{}") || {}; } catch (e) { review = {}; }

function hydrateReviewsFromFindings(findingsList) {
  (findingsList || []).forEach(f => {
    const cur = review[f.id] || {};
    const srvStatus = f.initial_review_status || "PENDING";
    const srvNote = f.initial_review_note || "";
    if (srvStatus !== "PENDING" || srvNote) {
      review[f.id] = {
        status: srvStatus !== "PENDING" ? srvStatus : (cur.status || "PENDING"),
        note: srvNote || cur.note || ""
      };
    }
  });
  try { localStorage.setItem(STORE_KEY, JSON.stringify(review)); } catch (e) {}
}
hydrateReviewsFromFindings(F);

let filters = { sev: "ALL", video: "ALL", staff: "ALL", rev: "ALL" };
let query = "";
let visible = [];
let activeId = null;
const expanded = new Set();

/* ---------- Utilities ---------- */
const esc = s => String(s ?? "")
  .replace(/&/g, "&amp;").replace(/</g, "&lt;")
  .replace(/>/g, "&gt;").replace(/"/g, "&quot;");

const stateOf = id => review[id] || { status: "PENDING", note: "" };
const staffKey = f => (f.staff_list && f.staff_list.length > 1) ? tr("Multiple Staff", "多人") : f.staff_tag;

function persist() {
  try { localStorage.setItem(STORE_KEY, JSON.stringify(review)); } catch (e) {}
}

function applyLangUI() {
  document.documentElement.lang = isZh() ? "zh-CN" : "en";
  const setTxt = (id, enVal, zhVal) => {
    const el = document.getElementById(id);
    if (el) el.textContent = isZh() ? zhVal : enVal;
  };
  const setHtml = (id, enVal, zhVal) => {
    const el = document.getElementById(id);
    if (el) el.innerHTML = isZh() ? zhVal : enVal;
  };
  setTxt("hd-brand-title", "Store AI Video Audit · Supervisor Workbench", "门店 AI 视频稽核 · 督导复核台");
  setTxt("hd-audit-lbl", "Audit", "稽核单");
  setTxt("hd-folder", "Store Folder ↗", "门店文件夹 ↗");
  setTxt("hd-sheet", "Source Sheet ↗", "源数据表 ↗");
  setTxt("btn-lang-toggle", isZh() ? "🌐 中文 | EN" : "🌐 EN | 中文", isZh() ? "🌐 中文 | EN" : "🌐 EN | 中文");
  setTxt("btn-sync-sheet", "🔄 Sync Sheet", "🔄 同步复核");
  setTxt("btn-batch-confirm", "Confirm All AI Hits", "一键确认实锤项");
  setTxt("btn-open-report", "Generate 48h Notice", "生成 48h 整改通知单");
  setTxt("opt-default-acct", "Default Google Account", "默认登录账号");
  setTxt("access-folder", "Open Store Folder ↗", "打开门店文件夹 ↗");
  setTxt("btn-access-help", "How to Fix", "修复方式");
  setHtml(
    "access-help-list",
    '<li>Open the <b>Store Folder</b> above using a Google account that has access.</li>'
    + '<li>Select the evidence clip files (or parent folder) → <b>Share</b> → set General Access to <code>Anyone with the link · Viewer</code>.</li>'
    + '<li>Refresh this page to restore both thumbnail previews and embedded video playback.</li>'
    + '<li>If the folder is shared with <b>another</b> signed-in Google account in your browser, select its <code>authuser</code> index on the left (`0` = primary browser account).</li>',
    '<li>用<b>有权限的那个 Google 账号</b>打开上方「门店文件夹」。</li>'
    + '<li>选中全部证据切片文件（或直接对父文件夹操作）→ <b>共享</b> → 常规访问权限改为 <code>知道链接的任何人 · 查看者</code>。</li>'
    + '<li>回到本页刷新。首帧缩略图与页内播放会同时恢复。</li>'
    + '<li>若文件已共享给你的<b>另一个</b> Google 账号，不必改权限，用左侧 <code>authuser</code> 下拉切换账号序号即可（0 为当前浏览器第一个登录账号）。</li>'
  );
  setTxt("v-kicker", "AUDIT VERDICT SUMMARY · APAC Cloud Audit Parameters 2026 v1.0", "稽核裁决摘要 · APAC Cloud Audit Parameters 2026 v1.0");
  setHtml("v-unit-red", "Red-Line<br />Zero-Tolerance", "项红线<br />零容忍违规");
  setHtml("v-unit-normal", "Standard<br />SOP Deviations", "项一般<br />操作偏差");
  setHtml("v-unit-clause", "SOP Clauses<br />Triggered", "条 SOP 条款<br />被命中");
  setTxt("btn-export-xlsx", "⤓ Export Excel (.xlsx)", "⤓ 下载统计明细（Excel）");
  setTxt("btn-export-csv", "⤓ Export Review CSV", "⤓ 复核明细 CSV");
  setTxt("p-title", "Supervisor Review Progress", "督导人工复核进度");
  setTxt("p-lbl-c", "Confirmed Violations", "已确认违规");
  setTxt("p-lbl-d", "False Positives Dismissed", "已排除误报");
  setTxt("p-lbl-f", "Follow-Up Footage Needed", "待调后续录像");
  setTxt("p-lbl-p", "Pending Review", "待复核");
  setTxt("btn-reset-review", "Reset Local Reviews", "清空本地复核记录");
  setTxt("lbl-sev-ALL", "All Events", "全部事件");
  setTxt("lbl-sev-RED", "Red Line", "红线");
  setTxt("lbl-sev-NORMAL", "Standard", "一般偏差");
  setTxt("lbl-sev-UNVERIFIED", "Unverified", "盲区待核");
  const qEl = document.getElementById("q");
  if (qEl) qEl.placeholder = tr("Search clause A2 / staff / phone / handwash / scoop…", "搜索条款 A2 / 员工 / 手机 / 洗手 / 冰铲…");
  const moreRow = document.getElementById("more-row");
  const btnMore = document.getElementById("btn-more");
  if (btnMore) btnMore.textContent = (moreRow && !moreRow.hidden) ? tr("Hide Filters", "收起筛选") : tr("More Filters", "更多筛选");
  setTxt("flbl-video", "Source Video", "监控原片");
  setTxt("flbl-staff", "Staff", "涉事人员");
  setTxt("flbl-review", "Review Status", "复核状态");
  setTxt("ap-main-title", "Appendix", "附录");
  setTxt("ap-main-sub", "SOP Rule Catalog & Token Ledger", "规则字典与算力账单");
  setTxt("ap-dict-title", "Full SOP Rule Catalog", "全量 SOP 规则字典");
  setTxt("ap-ledger-title", "Video Token Consumption & Cost Ledger", "视频 Token 消耗与算力账单");
  setTxt("mv-prev", "← Prev", "← 上一条");
  setTxt("mv-next", "Next →", "下一条 →");
  setTxt("mv-drive", "Open in Drive ↗", "Drive 原片 ↗");
  setTxt("mv-close", "Close ESC", "关闭 ESC");
  setHtml(
    "mv-notice-txt",
    'Seeing Google <b>"You need access"</b>? The clip isn\'t link-shared or your active browser account lacks access.',
    '播放器里显示 Google <b>「You need access」</b>？该切片未开启链接共享，或当前登录账号无权限。'
  );
  setTxt("mv-opt-default", "Default Account", "默认账号");
  setTxt("mv-cap-note", "Evidence clip spans 2s before to 18s after the detected violation", "证据切片覆盖违规发生前 2 秒至后 18 秒");
  setTxt("mr-kicker", "Ready to paste into Google Chat / Feishu / WeCom", "可直接粘贴至 Google Chat / 飞书 / 企微群");
  setTxt("mr-title", "Store AI Cloud Audit — 48-Hour Rectification Notice", "门店 AI 云稽核 48 小时整改通知单");
  setTxt("mr-btn-copy", "Copy Full Text", "复制全文");
  setTxt("mr-btn-print", "Print / PDF", "打印 / PDF");
  setTxt("mr-btn-close", "Close ESC", "关闭 ESC");
}

function toggleLang() {
  lang = isZh() ? "en" : "zh";
  try { localStorage.setItem(LANG_KEY, lang); } catch (e) {}
  applyLangUI();
  renderHero();
  renderProgress();
  buildChips();
  renderSections();
  renderAppendix();
  updateAccessBar();
  if (activeId != null && document.getElementById("modal-video").classList.contains("open")) {
    openVideo(activeId);
  }
  if (document.getElementById("modal-report").classList.contains("open")) {
    openReport();
  }
  toast(tr("Switched language to English", "已切换为中文界面"));
}

async function syncFromSheet() {
  const auth = DATA.auth || {};
  const qs = new URLSearchParams();
  if (auth.batch || DATA.batch_id) {
    qs.set("batch", auth.batch || DATA.batch_id || "");
    qs.set("store", auth.store || DATA.audit_id || "");
  } else {
    qs.set("job", auth.job || DATA.audit_id || "");
    if (auth.user || DATA.user_id) qs.set("user", auth.user || DATA.user_id || "");
  }
  if (auth.exp || DATA.exp) qs.set("exp", String(auth.exp || DATA.exp || 0));
  if (auth.sig || DATA.sig) qs.set("sig", auth.sig || DATA.sig || "");
  try {
    const resp = await fetch("/api/portal/data?" + qs.toString(), { cache: "no-store" });
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    const fresh = await resp.json();
    if (fresh && Array.isArray(fresh.findings)) {
      const freshByIncident = {};
      fresh.findings.forEach(ff => {
        if (ff.incident_id) freshByIncident[ff.incident_id] = ff;
      });
      F.forEach(f => {
        const ff = freshByIncident[f.incident_id] || fresh.findings.find(x => x.id === f.id);
        if (ff) {
          f.initial_review_status = ff.initial_review_status || "PENDING";
          f.initial_review_note = ff.initial_review_note || "";
          review[f.id] = {
            status: f.initial_review_status,
            note: f.initial_review_note
          };
        }
      });
      persist();
      renderHero();
      renderProgress();
      buildChips();
      renderSections();
      renderAppendix();
      toast(tr("🔄 Synced latest team review status from Google Sheet", "🔄 已从 Google Sheet 同步团队最新复核状态"));
      return;
    }
  } catch (e) {}
  toast(tr("Unable to reach server sync endpoint (local preview mode)", "当前处于离线/静态预览模式，仅使用本地复核状态"));
}

let noteDebounceTimers = {};
async function persistReviewsToSheet(updatesList) {
  if (!DATA.sheet_url || !updatesList || !updatesList.length) return;
  const auth = DATA.auth || {};
  try {
    const resp = await fetch("/api/portal/review", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        job: auth.job || DATA.job_id || DATA.audit_id || "",
        user: auth.user || DATA.user_id || "",
        batch: auth.batch || (DATA.batch_meta && DATA.batch_meta.batch_id) || "",
        store: auth.store || DATA.job_id || DATA.audit_id || "",
        exp: auth.exp || DATA.exp || 0,
        sig: auth.sig || DATA.sig || "",
        updates: updatesList
      })
    });
    if (resp.ok) {
      const res = await resp.json();
      if (res && res.persisted_to_sheet) {
        toast(tr("☁️ Synced review decision to Store Google Sheet", "☁️ 已实时同步回写至门店 Google Sheet 报告"));
      }
    }
  } catch (e) {
    /* Offline or static file preview mode: localStorage still persists */
  }
}

function setState(id, status, note, syncImmediate) {
  const cur = stateOf(id);
  const nextStatus = status === undefined ? cur.status : status;
  const nextNote = note === undefined ? cur.note : note;
  review[id] = { status: nextStatus, note: nextNote };
  persist();
  renderDlHint();

  const f = F.find(x => x.id === id);
  if (!f || !f.incident_id) return;
  const payloadItem = { incident_id: f.incident_id, status: nextStatus, note: nextNote };
  if (syncImmediate) {
    persistReviewsToSheet([payloadItem]);
  } else {
    clearTimeout(noteDebounceTimers[id]);
    noteDebounceTimers[id] = setTimeout(() => {
      const latest = stateOf(id);
      persistReviewsToSheet([{ incident_id: f.incident_id, status: latest.status, note: latest.note }]);
    }, 700);
  }
}

let toastTimer;
function toast(msg) {
  const t = document.getElementById("toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove("show"), 2600);
}

async function copyText(txt, okMsg) {
  let copied = false;
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(txt);
      copied = true;
    }
  } catch (e) {}

  if (!copied) {
    try {
      const ta = document.createElement("textarea");
      ta.value = txt;
      ta.setAttribute("readonly", "");
      ta.style.cssText = "position:fixed;top:0;left:0;width:2px;height:2px;padding:0;border:0;outline:none;box-shadow:none;background:transparent;opacity:0.01;z-index:-1;";
      document.body.appendChild(ta);
      ta.focus({ preventScroll: true });
      ta.select();
      ta.setSelectionRange(0, txt.length);
      copied = Boolean(document.execCommand && document.execCommand("copy"));
      ta.remove();
    } catch (e) {}
  }

  if (copied) {
    toast(okMsg);
    return;
  }

  const mrPre = document.getElementById("mr-text");
  const mrModal = document.getElementById("modal-report");
  if (mrPre && mrModal) {
    mrPre.textContent = txt;
    mrModal.classList.add("open");
    document.body.style.overflow = "hidden";
    try {
      const sel = window.getSelection();
      const range = document.createRange();
      range.selectNodeContents(mrPre);
      sel.removeAllRanges();
      sel.addRange(range);
    } catch (e) {}
  }
  toast(tr("Clipboard blocked by preview sandbox — text selected, press Ctrl+C / ⌘+C to copy", "当前预览沙盒限制自动剪贴板，已为您全选文本，请按 Ctrl+C / ⌘+C 复制"));
}

function hl(text) {
  let s = esc(text);
  s = s.replace(/((?:Staff|Employee|Partner|Worker|员工)\s*[A-Z0-9]+)/gi, '<span class="hl-s">$1</span>');
  s = s.replace(/(\d{2}:\d{2}(?::\d{2})?(?:\s*[-–]\s*\d{2}:\d{2}(?::\d{2})?)?)/g, '<span class="hl-t">$1</span>');
  return s;
}

/* ---------- Filters ---------- */
function matches(f) {
  if (filters.sev === "RED_LINE"   && f.severity !== "RED_LINE") return false;
  if (filters.sev === "NORMAL"     && f.severity !== "NORMAL") return false;
  if (filters.sev === "UNVERIFIED" && f.disposition !== "UNVERIFIED") return false;
  if (filters.video !== "ALL" && f.video_short !== filters.video) return false;
  if (filters.staff !== "ALL" && staffKey(f) !== filters.staff) return false;
  if (filters.rev   !== "ALL" && stateOf(f.id).status !== filters.rev) return false;
  if (query) {
    const hay = [f.code, f.rule_id, f.rule_name, f.rule_name_zh, f.sub_clause_title, f.sub_clause_title_zh,
                 f.description, f.staff_tag, f.video_display, f.mmss, f.osd_clock].join(" ").toLowerCase();
    if (!hay.includes(query)) return false;
  }
  return true;
}

/* ---------- Verdict Hero ---------- */
function renderHero() {
  document.getElementById("hd-store").textContent = DATA.store_name || tr("Store Audit", "门店稽核");
  document.getElementById("hd-audit").textContent = "#" + (DATA.audit_id || "");
  const folderLink = document.getElementById("hd-folder");
  const sheetLink = document.getElementById("hd-sheet");
  if (DATA.folder_url) { folderLink.href = DATA.folder_url; folderLink.hidden = false; }
  else { folderLink.hidden = true; }
  if (DATA.sheet_url) { sheetLink.href = DATA.sheet_url; sheetLink.hidden = false; }
  else { sheetLink.hidden = true; }

  const tk = DATA.token_ledger || [];
  const resTag = (tk[0] && tk[0].resolution && tk[0].resolution.includes("x"))
    ? (tk[0].resolution.split("x")[1] + "P ")
    : "";
  document.getElementById("hd-scope").textContent = isZh()
    ? (tk.length + " 段 " + resTag + "后厨监控")
    : (tk.length + " × " + resTag + "Kitchen CCTV Clips");

  /* Multi-Store Batch Switcher Bar */
  const batchStores = DATA.batch_stores || [];
  const batchBar = document.getElementById("batch-switcher-bar");
  const auth = DATA.auth || {};
  const batchId = auth.batch || (DATA.batch_meta && DATA.batch_meta.batch_id) || DATA.batch_id || "";
  if (batchStores.length > 0) {
    batchBar.hidden = false;
    const inner = document.getElementById("batch-switcher-inner");
    inner.innerHTML = '<span class="bs-label">' + tr("🏪 Store Switcher:", "🏪 批次门店切换：") + '</span>' + batchStores.map(st => {
      const isCur = Boolean(st.active) || st.job_id === DATA.audit_id;
      const stIcon = st.state === "DONE" ? "✅" : (st.state === "RUNNING" ? "⏳" : "⚠️");
      const vCount = st.violations_found !== undefined ? st.violations_found : (st.findings_count || 0);
      const href = st.switch_url || ("/portal?batch=" + encodeURIComponent(batchId)
        + "&store=" + encodeURIComponent(st.job_id)
        + "&exp=" + encodeURIComponent(auth.exp || DATA.exp || 0)
        + "&sig=" + encodeURIComponent(auth.sig || DATA.sig || ""));
      return '<a class="batch-pill' + (isCur ? ' active' : '') + '" href="' + esc(href) + '" onclick="return onBatchPillClick(event, \'' + esc(st.job_id) + '\')">'
        + stIcon + ' ' + esc(st.folder_name) + ' (' + vCount + tr("", "项") + ')</a>';
    }).join("");
  } else {
    batchBar.hidden = true;
  }

  /* Job Status Banner */
  const bannerEl = document.getElementById("job-status-banner");
  if (DATA.job_state === "RUNNING") {
    bannerEl.hidden = false;
    bannerEl.className = "job-status-banner running";
    bannerEl.textContent = tr(
      "⏳ Store videos are currently being audited in the background. Partial slice findings are shown below; refresh or click Sync Sheet for updates.",
      "⏳ 当前门店视频正在后台并发稽核中，已产出部分切片结果，刷新页面可查看最新进度。"
    );
  } else if (DATA.job_state === "FAILED" || DATA.job_state === "REJECTED") {
    bannerEl.hidden = false;
    bannerEl.className = "job-status-banner failed";
    bannerEl.textContent = tr("⚠️ Audit task did not complete (", "⚠️ 稽核任务未完成 (") + DATA.job_state + ")"
      + (DATA.error_message ? ": " + DATA.error_message : "");
  } else {
    bannerEl.hidden = true;
  }

  const red = F.filter(f => f.severity === "RED_LINE");
  const nor = F.filter(f => f.severity === "NORMAL");
  const hitClauses = [...new Set(F.map(f => f.rule_id))];

  document.getElementById("v-red").textContent = red.length;
  document.getElementById("v-normal").textContent = nor.length;
  document.getElementById("v-clause").textContent = hitClauses.length;
  document.getElementById("v-clause-total").textContent = isZh()
    ? ("/ 共 " + (DATA.sop_rules || []).length + " 条")
    : ("/ " + (DATA.sop_rules || []).length + " total");

  const noteEl = document.getElementById("v-note");
  if (F.length === 0) {
    noteEl.className = "verdict-note is-clean";
    noteEl.innerHTML = tr(
      "<b>✓ Zero SOP violations detected in this audit.</b> All inspected CCTV clips comply with CHAGEE APAC audit standards.",
      "<b>✓ 本次稽核未检出任何 SOP 违规事件</b>，全部监控原片均符合 CHAGEE APAC 稽核标准。"
    );
  } else if (red.length === 0) {
    noteEl.className = "verdict-note";
    const staffSet = [...new Set(F.flatMap(f => f.staff_list || [f.staff_tag]))];
    noteEl.innerHTML = isZh()
      ? ("本次稽核未触发红线零容忍违规，共检出 <b>" + nor.length + " 项一般操作偏差</b>，建议店长在晨会宣导纠偏。涉事人员：" + staffSet.map(esc).join(" / ") + "。")
      : ("No zero-tolerance red-line violations triggered. Detected <b>" + nor.length + " standard SOP deviation(s)</b> for morning briefing coaching. Involved staff: " + staffSet.map(esc).join(" / ") + ".");
  } else {
    noteEl.className = "verdict-note";
    const byClause = {};
    red.forEach(f => {
      const k = f.rule_id;
      byClause[k] = byClause[k] || { n: 0, label: fSubTitle(f).replace(/^[\d.\s/–-]+/, "") };
      byClause[k].n++;
    });
    const tops = Object.entries(byClause).sort((a, b) => b[1].n - a[1].n);
    const staffSet = [...new Set(red.flatMap(f => f.staff_list || [f.staff_tag]))];
    noteEl.innerHTML = isZh()
      ? ("<b>" + red.length + " 项红线违规须在 48 小时内闭环整改</b>，集中在 " + tops.length + " 条 SOP 条款："
        + tops.slice(0, 3).map(([rid, v]) => esc(rid) + " " + esc(v.label) + " <b>" + v.n + " 项</b>").join("、")
        + (tops.length > 3 ? " 等" : "")
        + "。涉事人员：" + staffSet.map(esc).join(" / ") + "。")
      : ("<b>" + red.length + " Red-Line violation(s) require mandatory 48-hour rectification</b> across " + tops.length + " SOP clause(s): "
        + tops.slice(0, 3).map(([rid, v]) => esc(rid) + " " + esc(v.label) + " (<b>" + v.n + "</b>)").join(", ")
        + (tops.length > 3 ? ", etc." : "")
        + ". Involved staff: " + staffSet.map(esc).join(" / ") + ".");
  }

  const mods = Object.keys(MODULE_META).sort((a, b) => MODULE_META[a].order - MODULE_META[b].order);
  const maxN = Math.max(...mods.map(m => F.filter(f => f.module_code === m).length), 1);
  document.getElementById("v-modbars").innerHTML = mods.map(m => {
    const list = F.filter(f => f.module_code === m);
    const r = list.filter(f => f.severity === "RED_LINE").length;
    const n = list.length - r;
    const scale = list.length / maxN * 100;
    const mMeta = metaOf(m);
    return '<div class="modbar">'
      + '<span class="nm"><b style="color:var(--red)">' + m + '</b> · ' + esc(mMeta.title) + '</span>'
      + '<span class="track" title="' + tr("Red Line " + r + " / Standard " + n, "红线 " + r + " / 一般 " + n) + '">'
      +   (list.length ? '<i class="fill-red" style="width:' + (r / list.length * scale) + '%"></i>'
                       + '<i class="fill-amber" style="width:' + (n / list.length * scale) + '%"></i>' : '')
      + '</span>'
      + '<span class="vl">' + (list.length || "—") + '</span>'
      + '</div>';
  }).join("");
}

function renderProgress() {
  let c = 0, d = 0, fu = 0;
  F.forEach(f => {
    const s = stateOf(f.id).status;
    if (s === "CONFIRMED_VIOLATION") c++;
    else if (s === "DISMISSED") d++;
    else if (s === "FOLLOWUP") fu++;
  });
  const total = F.length, done = c + d + fu, pct = total > 0 ? Math.round(done / total * 100) : 0;
  document.getElementById("p-pct").textContent = pct + "%";
  document.getElementById("p-ring").style.setProperty("--deg", (pct * 3.6) + "deg");
  document.getElementById("p-sub").textContent =
    done === 0
      ? tr("Not started · " + total + " pending review", "尚未开始复核 · 共 " + total + " 项待处置")
      : tr(done + " / " + total + " reviewed", done + " / " + total + " 项已处置");
  document.getElementById("p-c").textContent = c;
  document.getElementById("p-d").textContent = d;
  document.getElementById("p-f").textContent = fu;
  document.getElementById("p-p").textContent = total - done;
  renderDlHint();
}

/* ---------- Toolbar ---------- */
function renderToolbarCounts() {
  const base = f => {
    if (filters.video !== "ALL" && f.video_short !== filters.video) return false;
    if (filters.staff !== "ALL" && staffKey(f) !== filters.staff) return false;
    if (filters.rev !== "ALL" && stateOf(f.id).status !== filters.rev) return false;
    if (query) {
      const hay = [f.code, f.rule_id, f.rule_name, f.rule_name_zh, f.sub_clause_title, f.sub_clause_title_zh,
                   f.description, f.staff_tag, f.video_display, f.mmss, f.osd_clock].join(" ").toLowerCase();
      if (!hay.includes(query)) return false;
    }
    return true;
  };
  const pool = F.filter(base);
  document.getElementById("ct-sev-ALL").textContent = pool.length;
  document.getElementById("ct-sev-RED_LINE").textContent = pool.filter(f => f.severity === "RED_LINE").length;
  document.getElementById("ct-sev-NORMAL").textContent = pool.filter(f => f.severity === "NORMAL").length;
  document.getElementById("ct-sev-UNVERIFIED").textContent = pool.filter(f => f.disposition === "UNVERIFIED").length;

  ["ALL", "RED_LINE", "NORMAL", "UNVERIFIED"].forEach(k =>
    document.getElementById("sev-" + k).setAttribute("aria-pressed", String(filters.sev === k)));
}

function buildChips() {
  const vids = [];
  F.forEach(f => { if (!vids.some(v => v.k === f.video_short)) vids.push({ k: f.video_short, label: f.video_display }); });
  document.getElementById("chips-video").innerHTML =
    chipHtml("video", "ALL", tr("All Clips", "全部原片")) +
    vids.map(v => chipHtml("video", v.k,
      v.label.replace(/^Footage\s*/, "F") + " · " + F.filter(x => x.video_short === v.k).length)).join("");

  const staffs = [...new Set(F.map(staffKey))];
  document.getElementById("chips-staff").innerHTML =
    chipHtml("staff", "ALL", tr("All Staff", "全部人员")) +
    staffs.map(s => chipHtml("staff", s, s + " · " + F.filter(x => staffKey(x) === s).length)).join("");

  document.getElementById("chips-review").innerHTML =
    chipHtml("rev", "ALL", tr("All Statuses", "全部状态")) +
    chipHtml("rev", "PENDING", tr("Pending", "待复核")) +
    chipHtml("rev", "CONFIRMED_VIOLATION", tr("Confirmed", "已确认")) +
    chipHtml("rev", "DISMISSED", tr("False Positive", "已排除")) +
    chipHtml("rev", "FOLLOWUP", tr("Follow-Up", "待调录像"));
}

function chipHtml(key, val, label) {
  return '<button class="chip" data-k="' + key + '" data-v="' + esc(val) + '" aria-pressed="'
    + String(filters[key] === val) + '" onclick="setFilter(\'' + key + '\',\'' + val.replace(/'/g, "\\'") + '\')">'
    + esc(label) + '</button>';
}

function syncChips() {
  document.querySelectorAll(".chip[data-k]").forEach(b => {
    b.setAttribute("aria-pressed", String(filters[b.dataset.k] === b.dataset.v));
  });
}

function renderJump(groups) {
  document.getElementById("jump-nav").innerHTML = groups.map(g =>
    '<a href="#mod-' + g.code + '"><b class="jl">' + g.code + '</b>' + esc(metaOf(g.code).title)
    + ' <span class="n">' + g.items.length + '</span></a>').join("");
}

/* ---------- Review Decision Buttons ---------- */
function getDecLabels() {
  if (isZh()) {
    return {
      CONFIRMED_VIOLATION: { idle: "确认违规", done: "✓ 已确认违规", flag: "已确认违规" },
      DISMISSED:           { idle: "排除误报", done: "✓ 已排除误报", flag: "已排除误报" },
      FOLLOWUP:            { idle: "待调录像", done: "✓ 已标记待调", flag: "待调后续录像" }
    };
  }
  return {
    CONFIRMED_VIOLATION: { idle: "Confirm", done: "✓ Confirmed", flag: "Confirmed" },
    DISMISSED:           { idle: "False Positive", done: "✓ Dismissed", flag: "False Positive" },
    FOLLOWUP:            { idle: "Follow-Up", done: "✓ Follow-Up", flag: "Follow-Up" }
  };
}

function decBtn(id, cls, status, current) {
  const on = current === status;
  const L = getDecLabels()[status];
  return '<button class="' + cls + '" aria-pressed="' + on + '"'
    + ' title="' + (on ? L.done.replace("✓ ", "") + tr(" (click to undo)", "，点击可撤销") : L.idle) + '"'
    + ' onclick="decide(' + id + ',\'' + status + '\')">'
    + (on ? L.done : L.idle) + '</button>';
}

function stateFlag(status) {
  const labels = getDecLabels();
  if (status === "PENDING" || !labels[status]) return "";
  const cls = status === "CONFIRMED_VIOLATION" ? "c" : (status === "DISMISSED" ? "d" : "f");
  return '<span class="bullet">|</span><span class="state-flag ' + cls + '">'
    + labels[status].flag + '</span>';
}

/* ---------- Evidence Card ---------- */
function cardHtml(f) {
  const st = stateOf(f.id);
  const isRed = f.severity === "RED_LINE";
  const unv = f.disposition === "UNVERIFIED";
  const open = expanded.has(f.id);

  const hasFileId = Boolean(f.file_id);
  return '<article class="card ' + (st.status === "DISMISSED" ? "is-dismissed" : "") + '" id="card-' + f.id + '">'
    + '<button class="thumb' + (hasFileId ? '' : ' no-img') + '" onclick="openVideo(' + f.id + ')" aria-label="' + tr("Play evidence clip ", "播放 ") + esc(f.code) + '">'
    +   (hasFileId
          ? '<img src="' + esc(THUMB(f.file_id)) + '" alt="' + esc(f.code) + '" loading="lazy" '
            + 'referrerpolicy="no-referrer" onerror="noteThumbFail(this,' + f.id + ')" />'
          : '')
    +   '<div class="fallback"><span class="fb-ic">▣</span><span class="fb-code">' + esc(f.code) + ' · ' + esc(f.mmss) + '</span>'
    +     '<span>' + (hasFileId
            ? tr('Thumbnail blocked by Drive permissions<br />Click to view fix or play', '首帧不可用：Drive 无访问权限<br />点击查看修复方式')
            : tr('Click to play 20s evidence clip', '点击播放 20 秒证据视频切片')) + '</span></div>'
    +   '<div class="scrim"></div>'
    +   '<div class="tl">'
    +     (isRed ? '<span class="tag tag-solid-red">' + tr("RED LINE · 48h Fix", "红线 · 48h 必改") + '</span>'
                 : '<span class="tag tag-solid-amber">' + tr("Standard Deviation", "一般偏差") + '</span>')
    +     (unv ? '<span class="tag tag-solid-dark">' + tr("Unverified", "盲区待核") + '</span>' : '')
    +   '</div>'
    +   '<div class="tr"><span class="tag tag-solid-dark">' + esc(f.rule_id) + '</span></div>'
    +   '<div class="play">▶</div>'
    +   '<div class="bl"><span class="tag tag-solid-dark">' + tr("▶ 20s Evidence Clip", "▶ 20s 证据切片") + '</span></div>'
    +   '<div class="br"><span class="tag tag-solid-dark mono">' + esc(f.mmss)
    +     (f.osd_clock ? " · OSD " + esc(f.osd_clock) : "") + '</span></div>'
    + '</button>'

    + '<div class="card-body">'
    +   '<div class="card-meta">'
    +     '<span class="code">' + esc(f.code) + '</span>'
    +     '<span class="bullet">|</span><span>' + esc(f.staff_tag) + '</span>'
    +     '<span class="bullet">|</span><span>' + esc(f.video_display) + '</span>'
    +     '<span class="bullet">|</span><span class="mono">' + tr("Conf ", "置信度 ") + esc(f.confidence_pct) + '</span>'
    +     stateFlag(st.status)
    +   '</div>'
    +   '<h4>' + esc(fSubTitle(f)) + '</h4>'
    +   '<p class="evid ' + (open ? "" : "clamp") + '" id="evid-' + f.id + '">' + hl(f.description) + '</p>'
    +   '<button class="more" onclick="toggleEvid(' + f.id + ')">' + (open ? tr("Collapse evidence", "收起证据描述") : tr("Expand full evidence", "展开完整证据描述")) + '</button>'

    +   '<details class="fold">'
    +     '<summary>' + tr("SOP Criteria · ", "SOP 条款对照 · ") + esc(f.rule_id) + '</summary>'
    +     '<div class="fold-body">'
    +       '<div class="fold-line"><span class="k">' + tr("Pass Standard", "标准做法") + '</span><br />' + esc(fPass(f)) + '</div>'
    +       '<div class="fold-line bad"><span class="k">' + tr("Fail Red Line", "判定红线") + '</span><br />' + esc(fFail(f)) + '</div>'
    +       '<div class="fold-line muted" style="font-size:11.5px">' + esc(f.doc_ref) + '</div>'
    +     '</div>'
    +   '</details>'

    +   '<div class="advice"><span class="k">' + tr("Rectification Advice:", "整改建议") + '</span> ' + esc(fAdvice(f)) + '</div>'
    + '</div>'

    + '<div class="card-foot">'
    +   '<div class="dec-row">'
    +     '<div class="dec' + (st.status !== "PENDING" ? " has-state" : "") + '" role="group" aria-label="Review decision">'
    +       decBtn(f.id, "c", "CONFIRMED_VIOLATION", st.status)
    +       decBtn(f.id, "d", "DISMISSED", st.status)
    +       decBtn(f.id, "f", "FOLLOWUP", st.status)
    +     '</div>'
    +     '<button class="btn btn-sm" style="margin-left:auto" onclick="copyOne(' + f.id + ')">' + tr("Copy Notice", "复制单条通知") + '</button>'
    +   '</div>'
    +   '<div class="note-row">'
    +     '<input type="text" placeholder="' + tr("Supervisor review note / manager instruction (auto-saved to Sheet)", "督导复核批注 / 店长整改要求（自动保存）") + '" value="' + esc(st.note) + '" '
    +       'oninput="setState(' + f.id + ', undefined, this.value, false)" />'
    +   '</div>'
    + '</div>'
    + '</article>';
}

function toggleEvid(id) {
  const p = document.getElementById("evid-" + id);
  if (!p) return;
  const open = p.classList.toggle("clamp") === false;
  if (open) expanded.add(id); else expanded.delete(id);
  const btn = p.nextElementSibling;
  if (btn) btn.textContent = open ? tr("Collapse evidence", "收起证据描述") : tr("Expand full evidence", "展开完整证据描述");
}

/* ---------- Module & Clause Sections ---------- */
function groupData(list) {
  const allCodes = [...new Set([...Object.keys(MODULE_META), ...list.map(f => f.module_code || "C")])];
  const mods = allCodes.sort((a, b) => metaOf(a).order - metaOf(b).order);
  return mods.map(code => {
    const items = list.filter(f => (f.module_code || "C") === code);
    const clauseIds = [...new Set(items.map(f => f.rule_id))].sort();
    const clauses = clauseIds.map(rid => {
      const cItems = items.filter(f => f.rule_id === rid)
        .sort((a, b) => (b.severity === "RED_LINE") - (a.severity === "RED_LINE") || a.sec_in_clip - b.sec_in_clip);
      return { rid, items: cItems, hasRed: cItems.some(f => f.severity === "RED_LINE") };
    }).sort((a, b) => (b.hasRed - a.hasRed) || a.rid.localeCompare(b.rid));
    return { code, items, clauses };
  }).filter(g => g.items.length > 0);
}

function renderSections() {
  visible = F.filter(matches);
  renderToolbarCounts();
  syncChips();

  const groups = groupData(visible);
  renderJump(groups);
  const host = document.getElementById("sections");

  if (!groups.length) {
    host.innerHTML = '<div class="empty"><div class="ic">⌕</div><h3>' + tr("No findings match current filters", "当前筛选条件下没有事件") + '</h3>'
      + '<p>' + tr("Try relaxing the severity filter or clearing the search box.", "试着放宽严重等级，或清空搜索关键词。") + '</p>'
      + '<button class="btn" style="margin-top:16px" onclick="resetFilters()">' + tr("Reset All Filters", "重置全部筛选") + '</button></div>';
    return;
  }

  host.innerHTML = groups.map(g => {
    const m = metaOf(g.code);
    const red = g.items.filter(f => f.severity === "RED_LINE").length;
    const nor = g.items.length - red;

    return '<section class="module" id="mod-' + g.code + '">'
      + '<div class="module-banner" data-letter="' + g.code + '">'
      +   '<div class="module-badge" aria-hidden="true">'
      +     '<span class="ltr">' + g.code + '</span><span class="cap">' + tr("MOD", "类") + '</span>'
      +   '</div>'
      +   '<div class="mtxt">'
      +     '<div class="mk">' + m.kicker + ' · ' + tr("SOP CATEGORY", "违规大类") + '</div>'
      +     '<h2><span class="pfx">' + g.code + '</span> · ' + esc(m.title) + '</h2>'
      +     '<p class="desc">' + esc(m.desc) + '</p>'
      +   '</div>'
      +   '<div class="counts">'
      +     (red ? '<span class="tag tag-dark-red"><i class="dot"></i>' + tr("Red Line ", "红线 ") + red + '</span>' : '')
      +     (nor ? '<span class="tag tag-dark-amber"><i class="dot"></i>' + tr("Standard ", "一般 ") + nor + '</span>' : '')
      +     '<span class="tag tag-dark-slate">' + (isZh()
              ? ('共 ' + g.items.length + ' 项 / ' + g.clauses.length + ' 条条款')
              : (g.items.length + ' findings / ' + g.clauses.length + ' clauses')) + '</span>'
      +   '</div>'
      + '</div>'
      + g.clauses.map(c => clauseHtml(c)).join("")
      + '</section>';
  }).join("");
}

function clauseHtml(c) {
  const r = RULES[c.rid] || {};
  return '<div class="clause">'
    + '<div class="clause-head">'
    +   '<span class="clause-code ' + (c.hasRed ? "red" : "") + '">' + esc(c.rid) + '</span>'
    +   '<div class="clause-ttl">'
    +     '<h3>' + esc(rTitle(r) || c.rid) + '</h3>'
    +     '<div class="src">' + esc(r.doc_ref || "") + '</div>'
    +   '</div>'
    +   '<div class="clause-actions">'
    +     '<span class="tag ' + (c.hasRed ? "tag-red" : "tag-amber") + '">' + (isZh() ? ('本条检出 ' + c.items.length + ' 项') : (c.items.length + ' detected')) + '</span>'
    +     '<button class="btn btn-sm" onclick="toggleRaw(\'' + c.rid + '\', this)">' + tr("View Audit Logic", "核查细则原文") + '</button>'
    +   '</div>'
    + '</div>'
    + '<div class="criteria">'
    +   '<div class="crit pass"><div class="ct">' + tr("✓ PASS STANDARD", "✓ 合规基准") + '</div><p>' + esc(rPass(r)) + '</p></div>'
    +   '<div class="crit fail"><div class="ct">' + tr("✕ FAIL CRITERIA", "✕ 违规判定") + '</div><p>' + esc(rFail(r)) + '</p></div>'
    + '</div>'
    + '<div class="sop-raw" id="raw-' + esc(c.rid) + '"><pre>' + esc(r.check_instruction || tr("(No detailed check instructions attached)", "（该条款未附核查细则）")) + '</pre></div>'
    + '<div class="card-grid">' + c.items.map(cardHtml).join("") + '</div>'
    + '</div>';
}

function toggleRaw(rid, btn) {
  const el = document.getElementById("raw-" + rid);
  if (!el) return;
  const open = el.classList.toggle("open");
  btn.textContent = open ? tr("Hide Audit Logic", "收起细则原文") : tr("View Audit Logic", "核查细则原文");
}

/* ---------- Interactions ---------- */
function setFilter(key, val) {
  filters[key] = val;
  renderSections();
}
function resetFilters() {
  filters = { sev: "ALL", video: "ALL", staff: "ALL", rev: "ALL" };
  query = "";
  const i = document.getElementById("q");
  i.value = "";
  document.getElementById("search-wrap").classList.remove("has-val");
  renderSections();
}
function onSearch() {
  const i = document.getElementById("q");
  query = i.value.trim().toLowerCase();
  document.getElementById("search-wrap").classList.toggle("has-val", !!i.value);
  renderSections();
}
function clearSearch() {
  document.getElementById("q").value = "";
  onSearch();
}
function toggleMore() {
  const row = document.getElementById("more-row");
  const btn = document.getElementById("btn-more");
  row.hidden = !row.hidden;
  btn.textContent = row.hidden ? tr("More Filters", "更多筛选") : tr("Hide Filters", "收起筛选");
}

function decide(id, status) {
  const cur = stateOf(id).status;
  setState(id, cur === status ? "PENDING" : status, undefined, true);
  renderProgress();
  renderSections();
}

function batchConfirm() {
  const targets = F.filter(f => stateOf(f.id).status === "PENDING" && f.disposition === "CONFIRMED");
  if (!targets.length) { toast(tr("No pending AI-confirmed findings left to mark", "没有待处置的 AI 实锤项")); return; }
  const batchUpdates = [];
  targets.forEach(f => {
    const cur = stateOf(f.id);
    review[f.id] = { status: "CONFIRMED_VIOLATION", note: cur.note };
    if (f.incident_id) {
      batchUpdates.push({ incident_id: f.incident_id, status: "CONFIRMED_VIOLATION", note: cur.note });
    }
  });
  persist();
  renderProgress();
  renderSections();
  toast(tr("Marked " + targets.length + " AI-confirmed finding(s) as Confirmed Violation", "已将 " + targets.length + " 项 AI 实锤事件标记为确认违规"));
  if (batchUpdates.length) persistReviewsToSheet(batchUpdates);
}

function resetReview() {
  if (!Object.keys(review).length) { toast(tr("No local review records to reset", "本地没有复核记录")); return; }
  const batchUpdates = F.filter(f => f.incident_id).map(f => ({
    incident_id: f.incident_id,
    status: "PENDING",
    note: ""
  }));
  review = {};
  persist();
  renderProgress();
  renderSections();
  toast(tr("Cleared local review records", "已清空本地复核记录"));
  if (batchUpdates.length) persistReviewsToSheet(batchUpdates);
}

/* ---------- Evidence Clip Modal ---------- */
function openVideo(id) {
  const f = F.find(x => x.id === id);
  if (!f) return;
  activeId = id;
  const r = RULES[f.rule_id] || {};
  const st = stateOf(id);
  const isRed = f.severity === "RED_LINE";

  document.getElementById("mv-kicker").textContent =
    f.code + " · " + f.video_display + " · " + tr("Clause ", "条款 ") + f.rule_id;
  document.getElementById("mv-title").textContent = fSubTitle(f);
  document.getElementById("mv-drive").href = f.drive_url;
  document.getElementById("mv-iframe").src = PREVIEW(f);
  const nt = document.getElementById("mv-notice");
  nt.hidden = !(badThumbs.has(f.id) || badThumbs.size >= F.length);
  document.getElementById("mv-authuser").value = authUser === null ? "" : authUser;
  document.getElementById("mv-ts").textContent =
    tr("Clip ", "原片 ") + f.mmss + (f.osd_clock ? " | OSD " + f.osd_clock : "");

  const idx = visible.findIndex(x => x.id === id);
  document.getElementById("mv-prev").disabled = idx <= 0;
  document.getElementById("mv-next").disabled = idx < 0 || idx >= visible.length - 1;
  document.getElementById("mv-prev").style.opacity = idx <= 0 ? .4 : 1;
  document.getElementById("mv-next").style.opacity = (idx < 0 || idx >= visible.length - 1) ? .4 : 1;

  document.getElementById("mv-side").innerHTML =
      '<div style="display:flex;gap:8px;flex-wrap:wrap">'
    +   (isRed ? '<span class="tag tag-red"><i class="dot dot-red"></i>' + tr("Red Line · 48h Fix", "红线 · 48h 必改") + '</span>'
               : '<span class="tag tag-amber"><i class="dot dot-amber"></i>' + tr("Standard Deviation", "一般操作偏差") + '</span>')
    +   (f.disposition === "UNVERIFIED" ? '<span class="tag tag-slate">' + tr("Unverified", "盲区待核") + '</span>' : '')
    +   '<span class="tag tag-slate">' + esc(f.staff_tag) + '</span>'
    +   '<span class="tag tag-slate mono">' + tr("Conf ", "置信度 ") + esc(f.confidence_pct) + '</span>'
    + '</div>'
    + '<div><h5>' + tr("AI Evidence Observation", "AI 证据描述") + '</h5><div class="blk"><div class="tx" style="margin-top:0">' + hl(f.description) + '</div></div></div>'
    + '<div><h5>' + tr("SOP Clause Criteria", "SOP 条款对照") + '</h5><div class="blk">'
    +   '<div style="font-size:13px;font-weight:700">' + esc(f.rule_id) + ' · ' + esc(rTitle(r)) + '</div>'
    +   '<div class="tx"><b>' + tr("Pass Standard ·", "标准做法 ·") + '</b> ' + esc(fPass(f)) + '</div>'
    +   '<div class="tx" style="color:var(--red-deep)"><b>' + tr("Fail Red Line ·", "判定红线 ·") + '</b> ' + esc(fFail(f)) + '</div>'
    +   '<div class="tx muted" style="font-size:11.5px">' + esc(f.doc_ref) + '</div>'
    + '</div></div>'
    + '<div><h5>' + tr("Rectification Advice", "整改建议") + '</h5><div class="advice">' + esc(st.note || fAdvice(f)) + '</div></div>'
    + '<div class="foot-actions">'
    +   (st.status === "CONFIRMED_VIOLATION"
          ? '<button class="btn" disabled style="opacity:.5;cursor:default">' + tr("✓ Confirmed Violation", "✓ 已确认违规") + '</button>'
          : '<button class="btn btn-solid" onclick="decide(' + f.id + ',\'CONFIRMED_VIOLATION\');closeVideo();toast(\'' + tr("Confirmed violation ", "已确认违规 ") + f.code + '\')">' + tr("Confirm Violation", "确认违规") + '</button>')
    +   (st.status === "DISMISSED"
          ? '<button class="btn" disabled style="opacity:.5;cursor:default">' + tr("✓ Dismissed", "✓ 已排除误报") + '</button>'
          : '<button class="btn" onclick="decide(' + f.id + ',\'DISMISSED\');closeVideo();toast(\'' + tr("Dismissed false positive ", "已排除误报 ") + f.code + '\')">' + tr("False Positive", "排除误报") + '</button>')
    + '</div>';

  document.getElementById("modal-video").classList.add("open");
  document.body.style.overflow = "hidden";
}

function stepVideo(dir) {
  const idx = visible.findIndex(x => x.id === activeId);
  const next = idx + dir;
  if (idx < 0 || next < 0 || next >= visible.length) return;
  openVideo(visible[next].id);
}

function closeVideo() {
  document.getElementById("mv-iframe").src = "";
  document.getElementById("modal-video").classList.remove("open");
  document.body.style.overflow = "";
  if (activeId != null) {
    const card = document.getElementById("card-" + activeId);
    if (card) card.scrollIntoView({ block: "center", behavior: "smooth" });
  }
}

/* ---------- Rectification Notice ---------- */
function noticeOne(f) {
  const st = stateOf(f.id);
  if (isZh()) {
    return [
      "【门店 AI 稽核整改项 " + f.code + "】",
      "· 门店：" + DATA.store_name + "（稽核单 #" + DATA.audit_id + "）",
      "· 等级：" + (f.severity === "RED_LINE" ? "红线零容忍违规，须 48 小时内闭环" : "一般操作偏差，晨会宣导纠偏"),
      "· 时段：" + f.video_display + " @ " + f.mmss + (f.osd_clock ? "（OSD " + f.osd_clock + "）" : ""),
      "· 人员：" + f.staff_tag,
      "· 条款：" + f.rule_id + " " + fSubTitle(f) + "（" + f.doc_ref + "）",
      "· 事实：" + f.description,
      "· 标准：" + fPass(f),
      "· 要求：" + (st.note || fAdvice(f)),
      "· 证据：" + f.drive_url
    ].join("\n");
  }
  return [
    "【CHAGEE Store AI Audit - Rectification Item " + f.code + "】",
    "· Store: " + DATA.store_name + " (Audit #" + DATA.audit_id + ")",
    "· Severity: " + (f.severity === "RED_LINE" ? "RED LINE (Zero-Tolerance — Mandatory 48h Closure)" : "Standard SOP Deviation (Morning Briefing Coaching)"),
    "· Timestamp: " + f.video_display + " @ " + f.mmss + (f.osd_clock ? " (OSD " + f.osd_clock + ")" : ""),
    "· Staff: " + f.staff_tag,
    "· SOP Clause: " + f.rule_id + " " + fSubTitle(f) + " (" + f.doc_ref + ")",
    "· Observation: " + f.description,
    "· SOP Standard: " + fPass(f),
    "· Action Required: " + (st.note || fAdvice(f)),
    "· Evidence Clip: " + f.drive_url
  ].join("\n");
}

function copyOne(id) {
  const f = F.find(x => x.id === id);
  if (f) copyText(noticeOne(f), tr("Copied rectification notice for " + f.code, "已复制 " + f.code + " 整改通知"));
}

function openReport() {
  const active = F.filter(f => stateOf(f.id).status !== "DISMISSED");
  const red = active.filter(f => f.severity === "RED_LINE");
  const nor = active.filter(f => f.severity !== "RED_LINE");
  const dismissed = F.length - active.length;
  const pad = n => String(n).padStart(2, "0");
  const d = new Date();
  const stamp = d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate())
    + " " + pad(d.getHours()) + ":" + pad(d.getMinutes());

  const L = [];
  if (isZh()) {
    L.push("CHAGEE 霸王茶姬 · 门店 AI 云稽核 48 小时整改通知单");
    L.push("=".repeat(58));
    L.push("门店名称：" + DATA.store_name);
    L.push("稽核单号：#" + DATA.audit_id);
    L.push("稽核范围：" + DATA.token_ledger.length + " 段后厨监控原片");
    L.push("判定依据：CHAGEE APAC Cloud Audit Parameters 2026 v1.0");
    L.push("生成时间：" + stamp);
    L.push("整改汇总：共 " + active.length + " 项（红线 " + red.length + " 项 · 一般偏差 " + nor.length + " 项）"
      + (dismissed ? "；督导已排除误报 " + dismissed + " 项，不计入本单" : ""));
    L.push("");

    const blockZh = (title, list, note) => {
      L.push("-".repeat(58));
      L.push(title + "（共 " + list.length + " 项）" + note);
      L.push("-".repeat(58));
      if (!list.length) { L.push("（无）"); L.push(""); return; }
      list.forEach((f, i) => {
        const st = stateOf(f.id);
        L.push((i + 1) + ". [" + f.code + "] " + f.rule_id + " · " + fSubTitle(f));
        L.push("   时段：" + f.video_display + " @ " + f.mmss + (f.osd_clock ? "（OSD " + f.osd_clock + "）" : "")
          + "　人员：" + f.staff_tag + "　置信度：" + f.confidence_pct
          + (f.disposition === "UNVERIFIED" ? "　【盲区待核，需调取后续录像】" : ""));
        L.push("   事实：" + f.description);
        L.push("   标准：" + fPass(f));
        L.push("   要求：" + (st.note || fAdvice(f)));
        L.push("   证据：" + f.drive_url);
        L.push("");
      });
    };

    blockZh("一、红线零容忍违规", red, " · 48 小时内强制闭环并回传复训证明");
    blockZh("二、一般操作偏差", nor, " · 晨会宣导纠偏，一周内自查回传");

    L.push("=".repeat(58));
    L.push("店长签收：____________　　日期：____________");
  } else {
    L.push("CHAGEE · Store AI Cloud Audit — 48-Hour Rectification Notice");
    L.push("=".repeat(64));
    L.push("Store Name: " + DATA.store_name);
    L.push("Audit ID: #" + DATA.audit_id);
    L.push("Audit Scope: " + DATA.token_ledger.length + " Kitchen CCTV Clip(s)");
    L.push("SOP Baseline: CHAGEE APAC Cloud Audit Parameters 2026 v1.0");
    L.push("Generated At: " + stamp);
    L.push("Summary: " + active.length + " active item(s) (" + red.length + " Red Line · " + nor.length + " Standard Deviation)"
      + (dismissed ? "; " + dismissed + " false positive(s) dismissed by supervisor and excluded" : ""));
    L.push("");

    const blockEn = (title, list, note) => {
      L.push("-".repeat(64));
      L.push(title + " (" + list.length + " item(s))" + note);
      L.push("-".repeat(64));
      if (!list.length) { L.push("(None)"); L.push(""); return; }
      list.forEach((f, i) => {
        const st = stateOf(f.id);
        L.push((i + 1) + ". [" + f.code + "] " + f.rule_id + " · " + fSubTitle(f));
        L.push("   Time: " + f.video_display + " @ " + f.mmss + (f.osd_clock ? " (OSD " + f.osd_clock + ")" : "")
          + " | Staff: " + f.staff_tag + " | Confidence: " + f.confidence_pct
          + (f.disposition === "UNVERIFIED" ? " [UNVERIFIED — Check Follow-Up Footage]" : ""));
        L.push("   Observation: " + f.description);
        L.push("   SOP Standard: " + fPass(f));
        L.push("   Action Required: " + (st.note || fAdvice(f)));
        L.push("   Evidence Clip: " + f.drive_url);
        L.push("");
      });
    };

    blockEn("I. RED-LINE ZERO-TOLERANCE VIOLATIONS", red, " · Mandatory 48h Rectification & Retraining Proof");
    blockEn("II. STANDARD SOP DEVIATIONS", nor, " · Morning Briefing Coaching & 7-Day Self-Check");

    L.push("=".repeat(64));
    L.push("Store Manager Signature: ________________    Date: ____________");
  }

  document.getElementById("mr-text").textContent = L.join("\n");
  document.getElementById("modal-report").classList.add("open");
  document.body.style.overflow = "hidden";
}

function closeReport() {
  document.getElementById("modal-report").classList.remove("open");
  document.body.style.overflow = "";
}

function copyReport() {
  copyText(document.getElementById("mr-text").textContent, tr("Copied full 48h rectification notice", "已复制整改通知单全文"));
}

function printReport() {
  const txt = document.getElementById("mr-text").textContent || "";
  const isInIframe = (window.self !== window.top);
  if (!isInIframe) {
    window.print();
    return;
  }
  const docTitle = isZh()
    ? ("CHAGEE 48小时整改通知单 - " + esc(DATA.store_name || ""))
    : ("CHAGEE 48h Rectification Notice - " + esc(DATA.store_name || ""));
  const h1Title = isZh()
    ? "门店 AI 云稽核 48 小时整改通知单"
    : "CHAGEE Store AI Cloud Audit — 48-Hour Rectification Notice";
  const printHtml = '<!DOCTYPE html><html lang="' + (isZh() ? "zh-CN" : "en") + '"><head><meta charset="utf-8"><title>'
    + docTitle + '</title><style>'
    + 'body{font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;padding:32px;color:#0F1115;}'
    + 'h1{font-size:18px;margin:0 0 16px;padding-bottom:10px;border-bottom:2px solid #0F1115;}'
    + 'pre{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12px;line-height:1.75;white-space:pre-wrap;word-break:break-word;}'
    + '</style></head><body><h1>' + h1Title + '</h1><pre>' + esc(txt)
    + '</pre><script>window.onload=function(){setTimeout(function(){window.print();},150);};<\/script></body></html>';
  try {
    const w = window.open("", "_blank");
    if (w && w.document) {
      w.document.open();
      w.document.write(printHtml);
      w.document.close();
      return;
    }
  } catch (e) {}
  const blob = new Blob([printHtml], { type: "text/html;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = (isZh() ? "48h整改通知单_" : "48h_Rectification_Notice_") + (DATA.store_name || "Store") + "_" + (DATA.audit_id || "") + ".html";
  a.click();
  URL.revokeObjectURL(url);
  toast(tr("Direct print blocked by preview sandbox — downloaded printable notice HTML", "当前预览窗沙盒拦截直接打印，已为您下载独立可打印通知单"));
}

function onBatchPillClick(ev, jobId) {
  const stores = DATA.batch_stores || [];
  const target = stores.find(s => s.job_id === jobId);
  if (target && target.demo_payload) {
    ev.preventDefault();
    stores.forEach(s => { s.active = (s.job_id === jobId); });
    DATA.store_name = target.demo_payload.store_name;
    DATA.audit_id = target.demo_payload.audit_id;
    DATA.folder_url = target.demo_payload.folder_url || DATA.folder_url;
    DATA.sheet_url = target.demo_payload.sheet_url || DATA.sheet_url;
    DATA.findings = target.demo_payload.findings || [];
    DATA.token_ledger = target.demo_payload.token_ledger || [];
    F.length = 0;
    DATA.findings.forEach(x => F.push(x));
    resetFilters();
    renderHero();
    renderProgress();
    buildChips();
    renderSections();
    renderAppendix();
    toast(tr("Switched to store: ", "已切换至门店：") + DATA.store_name);
    return false;
  }
  return true;
}

/* ---------- CSV ---------- */
function exportCSV() {
  const head = isZh()
    ? ["事件编号", "稽核单号", "门店", "监控原片", "时间点", "OSD", "SOP条款", "细分违规项",
       "严重等级", "AI判定", "置信度", "证据描述", "SOP标准", "整改建议", "证据链接",
       "复核状态", "督导批注"]
    : ["Finding Code", "Audit ID", "Store", "Source Video", "Timestamp", "OSD Clock", "SOP Clause", "Sub-Clause Title",
       "Severity", "AI Disposition", "Confidence", "Observation", "SOP Pass Standard", "Rectification Advice", "Evidence Link",
       "Supervisor Review Status", "Supervisor Note"];
  const rows = [head];
  F.forEach(f => {
    const st = stateOf(f.id);
    rows.push([f.code, f.audit_id, DATA.store_name, f.video_filename, f.mmss, f.osd_clock,
               f.rule_id, fSubTitle(f), f.severity, f.disposition, f.confidence_pct,
               f.description, fPass(f), fAdvice(f), f.drive_url,
               st.status, st.note]);
  });
  const csv = "\uFEFF" + rows.map(r => r.map(c => '"' + String(c ?? "").replace(/"/g, '""') + '"').join(",")).join("\r\n");
  const url = URL.createObjectURL(new Blob([csv], { type: "text/csv;charset=utf-8;" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = (isZh() ? "AI稽核督导复核表_" : "AI_Audit_Review_") + DATA.store_name + "_" + DATA.audit_id + ".csv";
  a.click();
  URL.revokeObjectURL(url);
  toast(tr("Exported supervisor review CSV", "已导出督导复核表 CSV"));
}

/* ==================================================================
   Excel (.xlsx) Export — Zero-Dependency OOXML + Store ZIP
   ================================================================== */
const CRC_TABLE = (() => {
  const t = new Uint32Array(256);
  for (let i = 0; i < 256; i++) {
    let c = i;
    for (let k = 0; k < 8; k++) c = (c & 1) ? (0xEDB88320 ^ (c >>> 1)) : (c >>> 1);
    t[i] = c >>> 0;
  }
  return t;
})();

function crc32(buf) {
  let c = 0xFFFFFFFF;
  for (let i = 0; i < buf.length; i++) c = CRC_TABLE[(c ^ buf[i]) & 0xFF] ^ (c >>> 8);
  return (c ^ 0xFFFFFFFF) >>> 0;
}

function zipStore(files) {
  const enc = new TextEncoder();
  const parts = [], central = [];
  let offset = 0, cdSize = 0;

  files.forEach(f => {
    const name = enc.encode(f.name);
    const data = enc.encode(f.text);
    const crc = crc32(data);

    const lh = new Uint8Array(30 + name.length);
    const d1 = new DataView(lh.buffer);
    d1.setUint32(0, 0x04034B50, true);
    d1.setUint16(4, 20, true);
    d1.setUint16(6, 0x0800, true);
    d1.setUint16(8, 0, true);
    d1.setUint32(14, crc, true);
    d1.setUint32(18, data.length, true);
    d1.setUint32(22, data.length, true);
    d1.setUint16(26, name.length, true);
    lh.set(name, 30);
    parts.push(lh, data);

    const ch = new Uint8Array(46 + name.length);
    const d2 = new DataView(ch.buffer);
    d2.setUint32(0, 0x02014B50, true);
    d2.setUint16(4, 20, true);
    d2.setUint16(6, 20, true);
    d2.setUint16(8, 0x0800, true);
    d2.setUint16(10, 0, true);
    d2.setUint32(16, crc, true);
    d2.setUint32(20, data.length, true);
    d2.setUint32(24, data.length, true);
    d2.setUint16(28, name.length, true);
    d2.setUint32(42, offset, true);
    ch.set(name, 46);
    central.push(ch);
    cdSize += ch.length;
    offset += lh.length + data.length;
  });

  const eocd = new Uint8Array(22);
  const d3 = new DataView(eocd.buffer);
  d3.setUint32(0, 0x06054B50, true);
  d3.setUint16(8, files.length, true);
  d3.setUint16(10, files.length, true);
  d3.setUint32(12, cdSize, true);
  d3.setUint32(16, offset, true);

  return new Blob(parts.concat(central, [eocd]),
    { type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" });
}

const xe = s => String(s ?? "")
  .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
  .replace(/"/g, "&quot;").replace(/[\u0000-\u0008\u000B\u000C\u000E-\u001F]/g, "");

function colRef(n) {
  let s = "";
  while (n > 0) { const m = (n - 1) % 26; s = String.fromCharCode(65 + m) + s; n = (n - m - 1) / 26; }
  return s;
}

function sheetXml(rows, widths) {
  const body = rows.map((row, ri) => {
    const cells = row.map((v, ci) => {
      const ref = colRef(ci + 1) + (ri + 1);
      if (ri === 0) return '<c r="' + ref + '" s="1" t="inlineStr"><is><t>' + xe(v) + '</t></is></c>';
      if (typeof v === "number" && isFinite(v)) return '<c r="' + ref + '" s="2"><v>' + v + '</v></c>';
      if (v === null || v === undefined || v === "") return '<c r="' + ref + '" s="2"/>';
      return '<c r="' + ref + '" s="2" t="inlineStr"><is><t xml:space="preserve">' + xe(v) + '</t></is></c>';
    }).join("");
    return '<row r="' + (ri + 1) + '"' + (ri === 0 ? ' ht="26" customHeight="1"' : '') + '>' + cells + '</row>';
  }).join("");

  const cols = (widths || []).map((w, i) =>
    '<col min="' + (i + 1) + '" max="' + (i + 1) + '" width="' + w + '" customWidth="1"/>').join("");

  return '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    + '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
    + '<sheetViews><sheetView workbookViewId="0">'
    + '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
    + '</sheetView></sheetViews>'
    + '<sheetFormatPr defaultRowHeight="15"/>'
    + (cols ? '<cols>' + cols + '</cols>' : '')
    + '<sheetData>' + body + '</sheetData>'
    + '<autoFilter ref="A1:' + colRef(Math.max(1, rows[0] ? rows[0].length : 1)) + rows.length + '"/>'
    + '</worksheet>';
}

function buildXlsx(sheets, fileName) {
  const files = [];

  files.push({
    name: "[Content_Types].xml",
    text: '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
      + '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
      + '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
      + '<Default Extension="xml" ContentType="application/xml"/>'
      + '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
      + '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
      + sheets.map((s, i) => '<Override PartName="/xl/worksheets/sheet' + (i + 1)
          + '.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>').join("")
      + '</Types>'
  });

  files.push({
    name: "_rels/.rels",
    text: '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
      + '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
      + '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
      + '</Relationships>'
  });

  files.push({
    name: "xl/workbook.xml",
    text: '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
      + '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
      + ' xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
      + '<sheets>'
      + sheets.map((s, i) => '<sheet name="' + xe(s.name) + '" sheetId="' + (i + 1) + '" r:id="rId' + (i + 1) + '"/>').join("")
      + '</sheets></workbook>'
  });

  files.push({
    name: "xl/_rels/workbook.xml.rels",
    text: '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
      + '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
      + sheets.map((s, i) => '<Relationship Id="rId' + (i + 1)
          + '" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"'
          + ' Target="worksheets/sheet' + (i + 1) + '.xml"/>').join("")
      + '<Relationship Id="rId' + (sheets.length + 1)
      + '" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
      + '</Relationships>'
  });

  files.push({
    name: "xl/styles.xml",
    text: '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
      + '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
      + '<fonts count="2">'
      +   '<font><sz val="11"/><name val="Microsoft YaHei"/></font>'
      +   '<font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="Microsoft YaHei"/></font>'
      + '</fonts>'
      + '<fills count="3">'
      +   '<fill><patternFill patternType="none"/></fill>'
      +   '<fill><patternFill patternType="gray125"/></fill>'
      +   '<fill><patternFill patternType="solid"><fgColor rgb="FF14171C"/><bgColor indexed="64"/></patternFill></fill>'
      + '</fills>'
      + '<borders count="1"><border/></borders>'
      + '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
      + '<cellXfs count="3">'
      +   '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
      +   '<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1" applyAlignment="1">'
      +     '<alignment vertical="center" horizontal="left"/></xf>'
      +   '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1">'
      +     '<alignment vertical="top" wrapText="1"/></xf>'
      + '</cellXfs>'
      + '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
      + '</styleSheet>'
  });

  sheets.forEach((s, i) => files.push({
    name: "xl/worksheets/sheet" + (i + 1) + ".xml",
    text: sheetXml(s.rows, s.widths)
  }));

  const url = URL.createObjectURL(zipStore(files));
  const a = document.createElement("a");
  a.href = url;
  a.download = fileName;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/* ---------- Excel Workbook Assembly ---------- */
function statusLabel(s) {
  if (isZh()) {
    return s === "CONFIRMED_VIOLATION" ? "已确认违规"
      : s === "DISMISSED" ? "已排除误报"
      : s === "FOLLOWUP" ? "待调后续录像" : "待复核";
  }
  return s === "CONFIRMED_VIOLATION" ? "Confirmed Violation"
    : s === "DISMISSED" ? "False Positive"
    : s === "FOLLOWUP" ? "Follow-Up" : "Pending";
}

function exportXLSX() {
  const pad = n => String(n).padStart(2, "0");
  const d = new Date();
  const stamp = d.getFullYear() + pad(d.getMonth() + 1) + pad(d.getDate()) + "-" + pad(d.getHours()) + pad(d.getMinutes());
  const stampH = d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate())
    + " " + pad(d.getHours()) + ":" + pad(d.getMinutes());

  const cnt = s => F.filter(f => stateOf(f.id).status === s).length;
  const red = F.filter(f => f.severity === "RED_LINE");
  const nor = F.filter(f => f.severity === "NORMAL");
  const hitIds = [...new Set(F.map(f => f.rule_id))];
  const tk = DATA.token_ledger;
  const sum = k => tk.reduce((a, r) => a + r[k], 0);
  const done = cnt("CONFIRMED_VIOLATION") + cnt("DISMISSED") + cnt("FOLLOWUP");

  const overview = {
    name: tr("1_Overview", "① 稽核总览"),
    widths: [28, 62],
    rows: isZh() ? [
      ["指标项", "数值"],
      ["门店名称", DATA.store_name],
      ["稽核单号", "#" + DATA.audit_id],
      ["判定依据", "CHAGEE APAC Cloud Audit Parameters 2026 v1.0"],
      ["视觉引擎", tk[0] ? tk[0].model_version + "（" + tk[0].prompt_version + "）" : ""],
      ["报表导出时间", stampH],
      ["", ""],
      ["监控原片段数", tk.length],
      ["监控总时长（秒）", Math.round(sum("duration_sec"))],
      ["检出事件总数", F.length],
      ["红线零容忍违规", red.length],
      ["一般操作偏差", nor.length],
      ["盲区待核（UNVERIFIED）", F.filter(f => f.disposition === "UNVERIFIED").length],
      ["命中 SOP 条款数", hitIds.length],
      ["SOP 规则库总条数", DATA.sop_rules.length],
      ["涉事人员数", [...new Set(F.flatMap(f => f.staff_list || [f.staff_tag]))].length],
      ["", ""],
      ["督导已确认违规", cnt("CONFIRMED_VIOLATION")],
      ["督导已排除误报", cnt("DISMISSED")],
      ["待调后续录像", cnt("FOLLOWUP")],
      ["尚待复核", F.length - done],
      ["复核完成率", (F.length > 0 ? Math.round(done / F.length * 100) : 100) + "%"],
      ["", ""],
      ["算力 Token 合计", sum("total_tokens")],
      ["算力成本合计（USD）", Number(sum("cost_usd").toFixed(4))],
      ["处理总耗时（秒）", Math.round(sum("latency_sec"))]
    ] : [
      ["Metric", "Value"],
      ["Store Name", DATA.store_name],
      ["Audit ID", "#" + DATA.audit_id],
      ["SOP Baseline", "CHAGEE APAC Cloud Audit Parameters 2026 v1.0"],
      ["Vision Model", tk[0] ? tk[0].model_version + " (" + tk[0].prompt_version + ")" : ""],
      ["Exported At", stampH],
      ["", ""],
      ["Source Video Clips", tk.length],
      ["Total Video Duration (sec)", Math.round(sum("duration_sec"))],
      ["Total Findings Detected", F.length],
      ["Red-Line Zero-Tolerance Violations", red.length],
      ["Standard SOP Deviations", nor.length],
      ["Unverified Findings (UNVERIFIED)", F.filter(f => f.disposition === "UNVERIFIED").length],
      ["Triggered SOP Clauses", hitIds.length],
      ["Total SOP Clauses in Catalog", DATA.sop_rules.length],
      ["Involved Staff Count", [...new Set(F.flatMap(f => f.staff_list || [f.staff_tag]))].length],
      ["", ""],
      ["Confirmed Violations", cnt("CONFIRMED_VIOLATION")],
      ["Dismissed False Positives", cnt("DISMISSED")],
      ["Follow-Up Footage Needed", cnt("FOLLOWUP")],
      ["Pending Review", F.length - done],
      ["Review Completion Rate", (F.length > 0 ? Math.round(done / F.length * 100) : 100) + "%"],
      ["", ""],
      ["Total Tokens Consumed", sum("total_tokens")],
      ["Total Estimated Cost (USD)", Number(sum("cost_usd").toFixed(4))],
      ["Total Processing Time (sec)", Math.round(sum("latency_sec"))]
    ]
  };

  const detail = {
    name: tr("2_Findings", "② 违规明细"),
    widths: [9, 7, 11, 30, 10, 10, 9, 30, 46, 11, 11, 10, 72, 60, 50, 14, 30, 46],
    rows: [isZh() ? [
      "事件编号", "类别", "SOP 条款", "条款名称", "监控原片", "相对时间", "OSD 时钟",
      "细分违规项", "违规事实（AI 证据描述）", "严重等级", "AI 判定", "置信度",
      "SOP 标准做法", "SOP 判定红线", "整改建议", "督导复核状态", "督导批注", "证据切片链接"
    ] : [
      "Finding ID", "Module", "SOP Clause", "Clause Name", "Video Clip", "Timestamp", "OSD Clock",
      "Sub-Clause Title", "Observation (AI Evidence)", "Severity", "AI Disposition", "Confidence",
      "SOP Pass Standard", "SOP Fail Red Line", "Rectification Advice", "Review Status", "Supervisor Note", "Evidence Clip URL"
    ]].concat(F.map(f => {
      const st = stateOf(f.id);
      const r = RULES[f.rule_id] || {};
      return [
        f.code, f.module_code, f.rule_id, rTitle(r),
        f.video_short, f.mmss, f.osd_clock || "",
        fSubTitle(f), f.description,
        f.severity === "RED_LINE" ? tr("Red Line", "红线零容忍") : tr("Standard", "一般偏差"),
        f.disposition === "UNVERIFIED" ? tr("Unverified", "盲区待核") : tr("Confirmed", "实锤"),
        f.confidence_pct,
        fPass(f), fFail(f), fAdvice(f),
        statusLabel(st.status), st.note || "", f.drive_url
      ];
    }))
  };

  const byClause = {
    name: tr("3_By_Clause", "③ 按SOP条款统计"),
    widths: [10, 8, 44, 48, 11, 10, 10, 11, 11, 11, 11],
    rows: [isZh() ? [
      "SOP 条款", "类别", "条款名称", "手册出处", "检出项数",
      "红线", "一般", "已确认违规", "已排除误报", "待调录像", "待复核"
    ] : [
      "SOP Clause", "Module", "Clause Name", "Manual Reference", "Detected",
      "Red Line", "Standard", "Confirmed", "Dismissed", "Follow-Up", "Pending"
    ]].concat(DATA.sop_rules.map(r => {
      const list = F.filter(f => f.rule_id === r.rule_id);
      const by = s => list.filter(f => stateOf(f.id).status === s).length;
      return [
        r.rule_id, r.module_code, rTitle(r), r.doc_ref,
        list.length,
        list.filter(f => f.severity === "RED_LINE").length,
        list.filter(f => f.severity === "NORMAL").length,
        by("CONFIRMED_VIOLATION"), by("DISMISSED"), by("FOLLOWUP"), by("PENDING")
      ];
    }))
  };

  const byModule = {
    name: tr("4_By_Module", "④ 按违规大类统计"),
    widths: [8, 30, 11, 10, 10, 13, 26, 11, 11, 11],
    rows: [isZh() ? [
      "类别编号", "违规大类", "检出项数", "红线", "一般", "命中条款数", "命中条款",
      "已确认违规", "已排除误报", "待复核"
    ] : [
      "Module", "Category Name", "Detected", "Red Line", "Standard", "Clauses Hit", "Clause IDs",
      "Confirmed", "Dismissed", "Pending"
    ]].concat(Object.keys(MODULE_META)
      .sort((a, b) => MODULE_META[a].order - MODULE_META[b].order)
      .map(code => {
        const list = F.filter(f => f.module_code === code);
        const ids = [...new Set(list.map(f => f.rule_id))].sort();
        const by = s => list.filter(f => stateOf(f.id).status === s).length;
        return [
          code, metaOf(code).title, list.length,
          list.filter(f => f.severity === "RED_LINE").length,
          list.filter(f => f.severity === "NORMAL").length,
          ids.length, ids.join(" / ") || "—",
          by("CONFIRMED_VIOLATION"), by("DISMISSED"), by("PENDING")
        ];
      }))
  };

  const staffs = [...new Set(F.map(staffKey))];
  const byStaff = {
    name: tr("5_By_Staff", "⑤ 按涉事人员统计"),
    widths: [22, 11, 10, 10, 13, 30, 11, 11, 11],
    rows: [isZh() ? [
      "涉事人员", "事件数", "红线", "一般", "命中条款数", "命中条款",
      "已确认违规", "已排除误报", "待复核"
    ] : [
      "Staff", "Findings", "Red Line", "Standard", "Clauses Hit", "Clause IDs",
      "Confirmed", "Dismissed", "Pending"
    ]].concat(staffs.map(s => {
      const list = F.filter(f => staffKey(f) === s);
      const ids = [...new Set(list.map(f => f.rule_id))].sort();
      const by = x => list.filter(f => stateOf(f.id).status === x).length;
      return [
        s, list.length,
        list.filter(f => f.severity === "RED_LINE").length,
        list.filter(f => f.severity === "NORMAL").length,
        ids.length, ids.join(" / "),
        by("CONFIRMED_VIOLATION"), by("DISMISSED"), by("PENDING")
      ];
    }))
  };

  const byVideo = {
    name: tr("6_Token_Ledger", "⑥ 按原片与算力账单"),
    widths: [60, 12, 22, 11, 11, 10, 10, 13, 13, 13, 14, 13, 11],
    rows: [isZh() ? [
      "监控原片文件名", "原片编号", "时段", "时长(秒)", "分辨率", "事件数", "红线", "一般",
      "输入 Token", "思考 Token", "输出 Token", "合计 Token", "成本(USD)"
    ] : [
      "Video Filename", "Clip ID", "Segment", "Duration(s)", "Resolution", "Findings", "Red Line", "Standard",
      "Input Tokens", "Thinking Tokens", "Output Tokens", "Total Tokens", "Cost(USD)"
    ]].concat(tk.map(l => {
      const list = F.filter(f => f.video_short === l.video_short);
      const seg = list[0] ? (list[0].video_display.match(/\(([^)]+)\)/) || [, ""])[1] : "";
      return [
        l.video_filename, l.video_short, seg, Math.round(l.duration_sec), l.resolution,
        list.length,
        list.filter(f => f.severity === "RED_LINE").length,
        list.filter(f => f.severity === "NORMAL").length,
        l.prompt_tokens, l.thoughts_tokens, l.output_tokens, l.total_tokens,
        Number(l.cost_usd.toFixed(4))
      ];
    })).concat([[
      tr("Total", "合计"), "", "", Math.round(sum("duration_sec")), "", F.length, red.length, nor.length,
      sum("prompt_tokens"), sum("thoughts_tokens"), sum("output_tokens"), sum("total_tokens"),
      Number(sum("cost_usd").toFixed(4))
    ]])
  };

  buildXlsx([overview, detail, byClause, byModule, byStaff, byVideo],
    (isZh() ? "AI稽核统计报表_" : "AI_Audit_Report_") + DATA.store_name + "_" + DATA.audit_id + "_" + stamp + ".xlsx");

  toast(tr("Downloaded Excel report (6 sheets · " + F.length + " findings)", "已下载统计报表（6 个工作表 · " + F.length + " 条明细）"));
}

function renderDlHint() {
  const done = F.filter(f => stateOf(f.id).status !== "PENDING").length;
  document.getElementById("dl-hint").textContent = isZh()
    ? ("6 个工作表 · " + F.length + " 条违规明细 · 含当前复核状态（已处置 " + done + " 项）")
    : ("6 worksheets · " + F.length + " findings · includes live review status (" + done + " reviewed)");
}

/* ---------- Appendix ---------- */
function renderAppendix() {
  const hits = {};
  F.forEach(f => hits[f.rule_id] = (hits[f.rule_id] || 0) + 1);
  const hitN = Object.keys(hits).length;

  document.getElementById("ap-dict-hint").textContent = isZh()
    ? ("共 " + DATA.sop_rules.length + " 条，本次命中 " + hitN + " 条，未触发 " + (DATA.sop_rules.length - hitN) + " 条")
    : (DATA.sop_rules.length + " total rules · " + hitN + " triggered · " + (DATA.sop_rules.length - hitN) + " clear");

  document.getElementById("ap-dict").innerHTML =
      '<table class="dt"><thead><tr><th>' + tr("Clause", "条款") + '</th><th>' + tr("Rule Name & Reference", "规则名称与手册出处") + '</th><th>' + tr("Status", "本次结果") + '</th>'
    + '<th>' + tr("✓ Pass Standard", "✓ 合规标准") + '</th><th>' + tr("✕ Fail Criteria", "✕ 违规判定") + '</th></tr></thead><tbody>'
    + DATA.sop_rules.map(r => {
        const n = hits[r.rule_id] || 0;
        return '<tr class="' + (n ? "hit" : "") + '">'
          + '<td class="rid">' + esc(r.rule_id) + '</td>'
          + '<td><div style="font-weight:700">' + esc(rTitle(r)) + '</div>'
          +     '<div class="muted" style="font-size:11.5px;margin-top:2px">' + esc(r.doc_ref) + '</div></td>'
          + '<td>' + (n
              ? '<button class="chip" style="border-color:var(--red-line);background:var(--red-soft);color:var(--red-deep)" onclick="gotoRule(\'' + r.rule_id + '\')">' + (isZh() ? ('检出 ' + n + ' 项 →') : (n + ' hit(s) →')) + '</button>'
              : '<span class="tag tag-green">' + tr("All Clear", "未见违规") + '</span>') + '</td>'
          + '<td style="color:var(--green);max-width:360px">' + esc(rPass(r)) + '</td>'
          + '<td style="color:var(--red-deep);max-width:360px">' + esc(rFail(r)) + '</td>'
          + '</tr>';
      }).join("")
    + '</tbody></table>';

  const tk = DATA.token_ledger;
  const sum = k => tk.reduce((s, r) => s + r[k], 0);
  const cost = sum("cost_usd");
  document.getElementById("ap-ledger-hint").textContent = isZh()
    ? (tk.length + " 段原片合计 " + sum("total_tokens").toLocaleString() + " tokens · $" + cost.toFixed(4) + " USD")
    : (tk.length + " clip(s) · " + sum("total_tokens").toLocaleString() + " tokens · $" + cost.toFixed(4) + " USD");

  document.getElementById("ap-ledger").innerHTML =
      '<table class="dt"><thead><tr><th>' + tr("Source Video", "监控原片") + '</th><th>' + tr("Model", "模型") + '</th><th>' + tr("Duration", "时长") + '</th><th>' + tr("Resolution", "分辨率") + '</th>'
    + '<th>' + tr("Input", "输入") + '</th><th>' + tr("Thinking", "思考") + '</th><th>' + tr("Output", "输出") + '</th><th>' + tr("Total Tokens", "合计 Token") + '</th><th>' + tr("Cost USD", "成本 USD") + '</th><th>' + tr("Latency", "耗时") + '</th><th>' + tr("Findings", "检出") + '</th>'
    + '</tr></thead><tbody>'
    + tk.map(l => '<tr>'
        + '<td style="font-weight:600;max-width:300px;word-break:break-all">' + esc(l.video_filename) + '</td>'
        + '<td><code>' + esc(l.model_version) + '</code></td>'
        + '<td class="num">' + Math.round(l.duration_sec) + 's</td>'
        + '<td class="num">' + esc(l.resolution) + '</td>'
        + '<td class="num">' + l.prompt_tokens.toLocaleString() + '</td>'
        + '<td class="num">' + l.thoughts_tokens.toLocaleString() + '</td>'
        + '<td class="num">' + l.output_tokens.toLocaleString() + '</td>'
        + '<td class="num" style="font-weight:700">' + l.total_tokens.toLocaleString() + '</td>'
        + '<td class="num" style="font-weight:700;color:var(--red-deep)">$' + l.cost_usd.toFixed(4) + '</td>'
        + '<td class="num">' + l.latency_sec + 's</td>'
        + '<td class="num">' + l.flagged_events + tr("", " 项") + '</td>'
      + '</tr>').join("")
    + '</tbody><tfoot><tr><td colspan="4">' + (isZh()
        ? ('合计（平均 $' + (tk.length > 0 ? (cost / tk.length).toFixed(2) : '0.00') + ' / 5 分钟原片）')
        : ('Total (Avg $' + (tk.length > 0 ? (cost / tk.length).toFixed(2) : '0.00') + ' / 5-min clip)')) + '</td>'
    + '<td class="num">' + sum("prompt_tokens").toLocaleString() + '</td>'
    + '<td class="num">' + sum("thoughts_tokens").toLocaleString() + '</td>'
    + '<td class="num">' + sum("output_tokens").toLocaleString() + '</td>'
    + '<td class="num">' + sum("total_tokens").toLocaleString() + '</td>'
    + '<td class="num">$' + cost.toFixed(4) + '</td>'
    + '<td class="num">' + Math.round(sum("latency_sec")) + 's</td>'
    + '<td class="num">' + sum("flagged_events") + tr("", " 项") + '</td>'
    + '</tr></tfoot></table>';
}

function gotoRule(rid) {
  resetFilters();
  document.getElementById("q").value = rid;
  onSearch();
  const f = F.find(x => x.rule_id === rid);
  if (f) {
    const el = document.getElementById("card-" + f.id);
    if (el) el.scrollIntoView({ block: "center", behavior: "smooth" });
  }
}

/* ---------- Keyboard Shortcuts ---------- */
document.addEventListener("keydown", e => {
  const vOpen = document.getElementById("modal-video").classList.contains("open");
  const rOpen = document.getElementById("modal-report").classList.contains("open");
  if (e.key === "Escape") { if (vOpen) closeVideo(); if (rOpen) closeReport(); }
  if (vOpen && e.key === "ArrowLeft")  stepVideo(-1);
  if (vOpen && e.key === "ArrowRight") stepVideo(1);
});

/* ---------- Startup ---------- */
applyLangUI();
renderHero();
renderProgress();
buildChips();
renderSections();
renderAppendix();
updateAccessBar();
</script>
</body>
</html>
"""


def render_supervisor_portal_html(payload: Dict[str, Any]) -> str:
    """Renders the dynamic CHAGEE Supervisor Audit Workbench HTML (`REQ-015`)."""
    store_name = html.escape(str(payload.get("store_name") or "Store Audit"))
    audit_id = html.escape(str(payload.get("audit_id") or ""))
    portal_json = json.dumps(payload, ensure_ascii=False).replace("</", r"<\/")
    return (
        _SUPERVISOR_PORTAL_HTML_TEMPLATE
        .replace("__PORTAL_STORE_NAME__", store_name)
        .replace("__PORTAL_AUDIT_ID__", audit_id)
        .replace("__PORTAL_JSON__", portal_json)
    )
