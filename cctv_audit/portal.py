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

import hashlib
import hmac
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

INITIAL_REVIEW_STATUS: str = "待复核"

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
    "CONFIRMED_VIOLATION": "✅ 确认违规 (CONFIRMED)",
    "DISMISSED": "❌ 误报排除 (FALSE_POSITIVE)",
    "FOLLOWUP": "⚠️ 待调取后续录像 (FOLLOWUP)",
    "PENDING": INITIAL_REVIEW_STATUS,
}

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
        return False, "缺少访问签名参数（sig / exp），请从 Google Chat 通知卡片重新点击进入。"
    if not batch_id and not job_id:
        return False, "缺少任务标识（job 或 batch 参数）。"
    now_ts = int(now if now is not None else time.time())
    if int(exp) < now_ts:
        return False, "该督导复核工作台链接已过期（有效期 30 天），请从最新通知或报告进入。"
    expected = compute_portal_signature(
        job_id=job_id,
        user_id=user_id,
        batch_id=batch_id,
        exp=int(exp),
    )
    cleaned_sig = sig.strip().rstrip(")*>]'\" ").lower()
    if not hmac.compare_digest(expected, cleaned_sig):
        return False, "访问签名校验未通过（HMAC 签名不匹配）。"
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
    """Maps Google Sheet Column J text back to the portal's 4 state codes."""
    text = (raw_status or "").strip()
    upper = text.upper()
    if not text or "PENDING_HUMAN_REVIEW" in upper or "待人工复核" in text:
        return "PENDING"
    if "FALSE_POSITIVE" in upper or "DISMISSED" in upper or "误报" in text or "排除" in text:
        return "DISMISSED"
    if "FOLLOWUP" in upper or "后续录像" in text or "待调" in text:
        return "FOLLOWUP"
    if "CONFIRMED" in upper or "确认违规" in text:
        return "CONFIRMED_VIOLATION"
    return "PENDING"


def map_portal_code_to_sheet_status(portal_code: str) -> str:
    """Maps a portal decision code (`CONFIRMED_VIOLATION` / `DISMISSED` / `FOLLOWUP` / `PENDING`) to Sheet Column J."""
    code = (portal_code or "").strip().upper()
    return PORTAL_STATUS_TO_SHEET_LABEL.get(code, INITIAL_REVIEW_STATUS)


_DEFAULT_DOC_REF_BY_PREFIX: Dict[str, str] = {
    "A1": "APAC Cloud Audit Parameters 2026 v1.0 · 参数表 1.5.5",
    "A2": "APAC Cloud Audit Parameters 2026 v1.0 · 参数表 1.5.3 / 1.5.5",
    "A3": "APAC Cloud Audit Parameters 2026 v1.0 · 参数表 1.5.1–1.5.4",
    "A4": "APAC Cloud Audit Parameters 2026 v1.0 · 参数表 1.6",
    "A5": "APAC Cloud Audit Parameters 2026 v1.0 · 参数表 1.5.2 (2h 周期)",
    "B0": "CHAGEE APAC 官方 SOP 8 (Manitowoc) & SOP 21 (Langtuo)",
    "B1-1": "CHAGEE APAC 官方 SOP 21 · Langtuo 制冰机周清 15 步",
    "B1-2": "CHAGEE APAC 官方 SOP 8 · Manitowoc 制冰机周清 13 步",
    "B2": "APAC Cloud Audit Parameters 2026 v1.0 & SOP 8 / SOP 21 · 静置锁定期",
    "B3": "APAC Cloud Audit Parameters 2026 v1.0 · 0 Tolerance 化学品红线",
    "B4": "APAC Cloud Audit Parameters 2026 v1.0 · 参数表 3.1 三色毛巾规范",
    "B5": "APAC Cloud Audit Parameters 2026 v1.0 · 参数表 4.1 / 4.5 冰铲规范",
    "C1": "APAC Cloud Audit Parameters 2026 v1.0 · 0 Tolerance 交叉污染",
    "C2": "APAC Cloud Audit Parameters 2026 v1.0 · 0 Tolerance 杯具使用",
    "C3": "APAC Cloud Audit Parameters 2026 v1.0 · 0 Tolerance 不当行为",
    "C4": "APAC Cloud Audit Parameters 2026 v1.0 · 参数表 1.1–1.4 仪容与手机",
    "C5": "APAC Cloud Audit Parameters 2026 v1.0 · 参数表 4.3 / 4.4 垃圾与地面",
    "C6": "APAC Cloud Audit Parameters 2026 v1.0 · 参数表 6.x / 7.x / 8.x",
    "C7": "APAC Cloud Audit Parameters 2026 v1.0 · 参数表 9.x 顾客服务",
    "C8": "APAC Cloud Audit Parameters 2026 v1.0 · 参数表 6.2 自动泡茶机",
}

_DEFAULT_RECTIFICATION_BY_RULE: Dict[str, str] = {
    "A1": "【48h 整改】组织涉事员工复训强制洗手触发场景（触碰头面部/口罩/手机/垃圾桶/清洁工具后 5 分钟内必须完成五步洗手）。",
    "A2": "【48h 红线整改】严禁污染后未洗手直接戴手套或接触食品接触器具；店长现场督导考核「污染触发→五步洗手→换新手套」全流程。",
    "A3": "要求涉事员工严格执行标准五步洗手（先流水湿手→取皂液→纯泡沫对搓满 20 秒→冲净→抽取擦手纸彻底擦干），严禁戴手套冲水假洗手或干手按皂液。",
    "A4": "要求员工接触食品或即食原料全程规范佩戴一次性手套，洗手后及手套污染后立即更换新手套。",
    "A5": "落实门店在岗每 2 小时定时洗手提醒机制。",
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
}


def load_sop_catalog(extra_rules: Optional[Sequence[Any]] = None) -> List[Dict[str, str]]:
    """Loads the built-in 24-rule SOP catalog from `sop/master_sheet.json` or `DEFAULT_CHAGEE_V25_RULES`,
    merging any custom/spot-check rules from `Tab4_专项测试表` or `extra_rules` by `rule_id`.
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
                            rules_by_id[rid] = {
                                "rule_id": rid,
                                "name": str(row[1]).strip(),
                                "category": str(row[2]).strip(),
                                "severity": str(row[3]).strip() or "NORMAL",
                                "doc_ref": _DEFAULT_DOC_REF_BY_PREFIX.get(
                                    rid, "APAC Cloud Audit Parameters 2026 v1.0"
                                ),
                                "check_instruction": str(row[6]).strip(),
                                "pass_criteria": str(row[7]).strip(),
                                "fail_criteria": str(row[8]).strip(),
                            }
                    break
            for tab in tabs:
                if str(tab.get("title") or "").strip() == TAB4_SPOT_CHECK_TITLE:
                    values = tab.get("values") or []
                    for row in values[1:]:
                        if len(row) >= 9 and str(row[0]).strip():
                            rid = str(row[0]).strip()
                            if rid not in rules_by_id:
                                rules_by_id[rid] = {
                                    "rule_id": rid,
                                    "name": str(row[1]).strip(),
                                    "category": str(row[2]).strip(),
                                    "severity": str(row[3]).strip() or "NORMAL",
                                    "doc_ref": _DEFAULT_DOC_REF_BY_PREFIX.get(
                                        rid, f"{TAB4_SPOT_CHECK_TITLE} · 专项抽检与新 SOP 测试"
                                    ),
                                    "check_instruction": str(row[6]).strip(),
                                    "pass_criteria": str(row[7]).strip(),
                                    "fail_criteria": str(row[8]).strip(),
                                }
        except Exception as exc:
            logger.warning("Failed to read sop/master_sheet.json: %s", exc)
    if not rules_by_id:
        for item in DEFAULT_CHAGEE_V25_RULES:
            rid = str(item.rule_id).strip()
            rules_by_id[rid] = {
                "rule_id": rid,
                "name": str(item.name).strip(),
                "category": str(item.category).strip(),
                "severity": str(item.severity).strip() or "NORMAL",
                "doc_ref": _DEFAULT_DOC_REF_BY_PREFIX.get(
                    rid, "APAC Cloud Audit Parameters 2026 v1.0"
                ),
                "check_instruction": str(item.check_instruction).strip(),
                "pass_criteria": str(item.pass_criteria).strip(),
                "fail_criteria": str(item.fail_criteria).strip(),
            }
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
        rules_by_id[rid] = {
            "rule_id": rid,
            "name": name or f"{rid} · 专项测试规则",
            "category": category or "专项测试",
            "severity": sev_val,
            "doc_ref": _DEFAULT_DOC_REF_BY_PREFIX.get(
                rid, f"{TAB4_SPOT_CHECK_TITLE} · 专项抽检与新 SOP 测试"
            ),
            "check_instruction": check_inst,
            "pass_criteria": pass_crit,
            "fail_criteria": fail_crit,
        }
    return list(rules_by_id.values())


_STAFF_RE = re.compile(r"员工[A-Z0-9]+")
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
    found = sorted(set(_STAFF_RE.findall(description or "")))
    if not found:
        if any(w in (description or "") for w in ("全员", "在岗员工", "多名员工", "两人", "多人")):
            return "全员/多人", ["多人"]
        return "在岗员工", ["在岗员工"]
    if len(found) > 1:
        return "、".join(found) + " (多人)", found
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


def _module_code_and_name(rule_id: str) -> Tuple[str, str]:
    prefix = (rule_id or "C")[:1].upper()
    if prefix == "A":
        return "A", "【A】洗手与手部卫生"
    if prefix == "B":
        return "B", "【B】制冰机与冰块管理"
    if prefix == "D":
        return "D", "【D】运营与人效模块"
    return "C", "【C】常态红线、仪容与泡茶设备"


def _infer_sub_clause_title(rule_id: str, rule_name: str, description: str) -> str:
    desc = description or ""
    if rule_id == "C4":
        if "手机" in desc:
            return "1.4 在岗行为红线：工作区使用私人手机"
        if "围裙" in desc:
            return "1.2 工装围裙规范：围裙穿戴或存放不合规"
        if "口罩" in desc:
            return "1.1 个人仪容着装：口罩未规范遮挡口鼻"
        if "帽" in desc or "发网" in desc or "头发" in desc:
            return "1.1 个人仪容着装：未佩戴工作帽及发网"
        if "倚靠" in desc:
            return "1.4 在岗行为红线：等客时倚靠台面"
    if rule_id == "A2":
        return "1.5.3 / 1.5.5 强制洗手红线：污染后未洗手直接戴手套或接触食品器具"
    if rule_id == "A3":
        return "1.5.1–1.5.4 洗手五步法核对：湿手/皂液/搓洗≥20s/冲净/擦手纸擦干"
    if rule_id == "B2":
        return "SOP 8 / SOP 21 静置锁定期红线：消毒液静置时长不足或侧面边缘漏擦"
    if rule_id == "B3":
        return "0 Tolerance 化学品红线：使用黄色瓶身多功能清洁剂或混用化学品"
    if rule_id == "B4":
        return "3.1 三色毛巾规范：未从专用消毒桶取用干净红边毛巾或三色混用"
    if rule_id == "B5":
        return "4.1 / 4.5 冰铲卫生规范：冰铲未放回专用收纳盒或取冰不规范"
    return rule_name or f"SOP 条款 {rule_id}"


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
        severity = str(raw_row[5] if len(raw_row) > 5 else "NORMAL").strip().upper() or "NORMAL"
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
        mod_code, mod_name = _module_code_and_name(rule_id)
        sop_info = sop_by_id.get(rule_id, {})
        rule_name = sop_info.get("name") or f"{rule_id} · SOP 稽核条款"
        pass_crit = sop_info.get("pass_criteria") or "严格按照 CHAGEE APAC 官方 SOP 标准执行。"
        fail_crit = sop_info.get("fail_criteria") or "违反 CHAGEE APAC 官方 SOP 标准。"
        doc_ref = sop_info.get("doc_ref") or _DEFAULT_DOC_REF_BY_PREFIX.get(
            rule_id, "APAC Cloud Audit Parameters 2026 v1.0"
        )
        rect_advice = _DEFAULT_RECTIFICATION_BY_RULE.get(
            rule_id, "请店长对照 20 秒证据视频组织当班员工复盘纠偏，并在晨会宣导。"
        )
        sub_clause = _infer_sub_clause_title(rule_id, rule_name, desc)

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
                "module_name": mod_name,
                "rule_name": rule_name,
                "rule_subtitle": sub_clause,
                "doc_ref": doc_ref,
                "sub_clause_title": sub_clause,
                "specific_pass": pass_crit,
                "sop_pass_criteria": pass_crit,
                "sop_fail_criteria": fail_crit,
                "rectification_advice": rect_advice,
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
        v_file = str(r[1] if len(r) > 1 else "").strip()
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
) -> Dict[str, Any]:
    """Loads and validates the full JSON payload for `/portal` and `GET /api/portal/data`."""
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
            raise LookupError(f"未找到定时巡检批次 `{batch_id}` 的状态记录。")
        batch_rec = ScheduledBatchRecord.model_validate_json(raw_batch)
        sorted_tasks = sorted(
            batch_rec.store_tasks.values(),
            key=lambda x: (x.folder_name, x.job_id),
        )
        selected_task = None
        if store_job_id:
            selected_task = batch_rec.store_tasks.get(store_job_id)
        if selected_task is None and sorted_tasks:
            done_tasks = [t for t in sorted_tasks if t.state.upper() == "DONE"]
            selected_task = done_tasks[0] if done_tasks else sorted_tasks[0]

        for t in sorted_tasks:
            live_job = await job_store.get(t.user_id, t.job_id)
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
            "rule_name": batch_rec.rule_name,
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
            raise LookupError(f"未找到稽核单号 `{job_id}` 的任务记录。")

    if target_job is None:
        raise LookupError("未找到可展示的门店稽核任务。")

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
    if not spot_tab and str(getattr(target_job, "active_prompt_version", "")).startswith("Tab4_专项测试表"):
        spot_tab = "Tab4_专项测试表"
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
            else f"门店文件夹 ({target_job.folder_id[:10]})"
        )
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
) -> Dict[str, Any]:
    """Verifies HMAC signature and writes supervisor review decisions + notes to Sheet `Tab 1` Columns J & K."""
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
    if batch_id:
        from .scheduled_auditor import ScheduledBatchRecord

        b_key = state_store.batch_key(batch_id)
        raw_batch, _ = state_store.read_text_with_generation(b_key)
        if raw_batch is None:
            raise LookupError(f"未找到定时巡检批次 `{batch_id}`。")
        batch_rec = ScheduledBatchRecord.model_validate_json(raw_batch)
        eff_store_job_id = store_job_id or next(iter(batch_rec.store_tasks.keys()), "")
        task = batch_rec.store_tasks.get(eff_store_job_id)
        if task is None:
            raise LookupError(f"批次 `{batch_id}` 中未找到门店任务 `{eff_store_job_id}`。")
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
        raise LookupError("未找到对应的稽核任务记录。")
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

    updated_count = await gateway.update_report_human_reviews(
        target_job.folder_id,
        target_job.report_sheet_url,
        normalized_updates,
    )
    return {
        "ok": True,
        "updated_count": updated_count,
        "persisted_to_sheet": True,
        "job_id": target_job.job_id,
        "report_sheet_url": target_job.report_sheet_url,
    }


def render_supervisor_portal_html(payload: Dict[str, Any]) -> str:
    """Renders the dynamic CHAGEE Supervisor Audit Workbench HTML (`REQ-015`)."""
    store_name = str(payload.get("store_name") or "门店稽核")
    audit_id = str(payload.get("audit_id") or "")
    portal_json = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>CHAGEE 霸王茶姬 · 门店 AI 视频稽核督导工作台 ({store_name} | #{audit_id})</title>
  <meta name="description" content="CHAGEE 门店 CCTV AI 稽核督导复核工作台，集成 20 秒违规证据切片、24 条官方 SOP 条款原文对照、Google Sheet 实时双向同步与门店 48 小时整改通知单生成。" />
  <style>
    :root {{
      --bg-canvas: #F6F7F9;
      --bg-surface: #FFFFFF;
      --bg-subtle: #F1F4F8;
      --bg-sop: #F8FAFC;
      --text-primary: #111827;
      --text-secondary: #374151;
      --text-muted: #6B7280;
      --border-light: #E5E7EB;
      --border-strong: #D1D5DB;
      --brand-crimson: #B91C1C;
      --brand-crimson-bg: #FEF2F2;
      --brand-crimson-border: #FECACA;
      --amber-text: #B45309;
      --amber-bg: #FFFBEB;
      --amber-border: #FDE68A;
      --emerald-text: #047857;
      --emerald-bg: #ECFDF5;
      --emerald-border: #A7F3D0;
      --blue-text: #1D4ED8;
      --blue-bg: #EFF6FF;
      --blue-border: #BFDBFE;
      --purple-text: #6D28D9;
      --purple-bg: #F5F3FF;
      --purple-border: #DDD6FE;
      --shadow-xs: 0 1px 2px rgba(15, 23, 42, 0.04);
      --shadow-sm: 0 2px 8px rgba(15, 23, 42, 0.06);
      --shadow-md: 0 8px 24px rgba(15, 23, 42, 0.10);
      --radius-sm: 6px;
      --radius-md: 10px;
      --radius-lg: 14px;
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", "Noto Sans CJK SC", sans-serif;
      background-color: var(--bg-canvas);
      color: var(--text-primary);
      line-height: 1.55;
      font-size: 14px;
      -webkit-font-smoothing: antialiased;
    }}
    .top-header {{
      background: #111827;
      color: #F9FAFB;
      padding: 16px 24px;
      border-bottom: 3px solid #DC2626;
      position: sticky;
      top: 0;
      z-index: 40;
      box-shadow: 0 4px 12px rgba(0, 0, 0, 0.12);
    }}
    .top-header-inner {{
      max-width: 1440px;
      margin: 0 auto;
      display: flex;
      flex-wrap: wrap;
      justify-content: space-between;
      align-items: center;
      gap: 14px;
    }}
    .brand-title-group {{ display: flex; align-items: center; gap: 14px; flex-wrap: wrap; }}
    .brand-badge {{
      background: #DC2626;
      color: #FFFFFF;
      font-weight: 800;
      font-size: 12px;
      letter-spacing: 0.06em;
      padding: 5px 10px;
      border-radius: 6px;
      text-transform: uppercase;
      white-space: nowrap;
    }}
    .top-header h1 {{ font-size: 18px; font-weight: 700; color: #FFFFFF; letter-spacing: -0.01em; }}
    .store-meta-pills {{
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 8px;
      margin-top: 6px;
      font-size: 12px;
      color: #D1D5DB;
    }}
    .meta-pill {{
      background: rgba(255, 255, 255, 0.10);
      padding: 2px 9px;
      border-radius: 999px;
      font-variant-numeric: tabular-nums;
      display: inline-flex;
      align-items: center;
      gap: 4px;
    }}
    .meta-pill a {{ color: #93C5FD; text-decoration: none; }}
    .meta-pill a:hover {{ text-decoration: underline; }}
    .header-actions {{ display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }}
    .btn {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 6px;
      padding: 7px 13px;
      border-radius: var(--radius-sm);
      font-size: 13px;
      font-weight: 600;
      cursor: pointer;
      transition: all 0.15s ease;
      border: 1px solid transparent;
      text-decoration: none;
      white-space: nowrap;
      font-family: inherit;
    }}
    .btn-primary {{ background: #DC2626; color: #FFFFFF; border-color: #B91C1C; }}
    .btn-primary:hover {{ background: #B91C1C; }}
    .btn-outline-light {{
      background: rgba(255, 255, 255, 0.08);
      color: #F3F4F6;
      border-color: rgba(255, 255, 255, 0.2);
    }}
    .btn-outline-light:hover {{ background: rgba(255, 255, 255, 0.16); }}
    .btn-secondary {{
      background: var(--bg-surface);
      color: var(--text-secondary);
      border-color: var(--border-strong);
    }}
    .btn-secondary:hover {{ background: var(--bg-subtle); color: var(--text-primary); }}
    .batch-switcher-bar {{
      background: #1E293B;
      border-bottom: 1px solid #334155;
      padding: 10px 24px;
    }}
    .batch-switcher-inner {{
      max-width: 1440px;
      margin: 0 auto;
      display: flex;
      align-items: center;
      flex-wrap: wrap;
      gap: 8px;
    }}
    .batch-store-tab {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 5px 12px;
      border-radius: 999px;
      font-size: 12.5px;
      font-weight: 600;
      text-decoration: none;
      color: #E2E8F0;
      background: rgba(255, 255, 255, 0.08);
      border: 1px solid rgba(255, 255, 255, 0.14);
      transition: all 0.15s ease;
    }}
    .batch-store-tab:hover {{ background: rgba(255, 255, 255, 0.16); }}
    .batch-store-tab.active {{
      background: #DC2626;
      color: #FFFFFF;
      border-color: #F87171;
      box-shadow: 0 2px 8px rgba(220, 38, 38, 0.35);
    }}
    .main-container {{ max-width: 1440px; margin: 0 auto; padding: 20px 24px 60px; }}
    .status-banner {{
      padding: 14px 18px;
      border-radius: var(--radius-md);
      margin-bottom: 18px;
      font-size: 13.5px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-wrap: wrap;
      gap: 10px;
    }}
    .status-banner.running {{ background: #EFF6FF; border: 1px solid #BFDBFE; color: #1E40AF; }}
    .status-banner.failed {{ background: #FEF2F2; border: 1px solid #FECACA; color: #991B1B; }}
    .kpi-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 14px;
      margin-bottom: 18px;
    }}
    .kpi-card {{
      background: var(--bg-surface);
      border: 1px solid var(--border-light);
      border-radius: var(--radius-md);
      padding: 14px 16px;
      box-shadow: var(--shadow-xs);
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      position: relative;
      overflow: hidden;
    }}
    .kpi-card::before {{
      content: "";
      position: absolute;
      top: 0;
      left: 0;
      width: 4px;
      height: 100%;
      background: #6B7280;
    }}
    .kpi-card.kpi-red::before {{ background: #DC2626; }}
    .kpi-card.kpi-amber::before {{ background: #D97706; }}
    .kpi-card.kpi-blue::before {{ background: #2563EB; }}
    .kpi-card.kpi-emerald::before {{ background: #059669; }}
    .kpi-label {{
      font-size: 12px;
      font-weight: 600;
      color: var(--text-muted);
      display: flex;
      align-items: center;
      justify-content: space-between;
    }}
    .kpi-value-row {{ display: flex; align-items: baseline; gap: 8px; margin: 6px 0 4px; }}
    .kpi-value {{ font-size: 26px; font-weight: 800; font-variant-numeric: tabular-nums; letter-spacing: -0.02em; }}
    .kpi-sub {{ font-size: 12px; color: var(--text-muted); line-height: 1.4; }}
    .progress-bar-track {{
      width: 100%;
      height: 6px;
      background: #E5E7EB;
      border-radius: 999px;
      overflow: hidden;
      margin-top: 6px;
      display: flex;
    }}
    .progress-fill-confirmed {{ background: #DC2626; height: 100%; transition: width 0.25s ease; }}
    .progress-fill-dismissed {{ background: #10B981; height: 100%; transition: width 0.25s ease; }}
    .progress-fill-followup {{ background: #F59E0B; height: 100%; transition: width 0.25s ease; }}
    .control-panel {{
      background: var(--bg-surface);
      border: 1px solid var(--border-light);
      border-radius: var(--radius-md);
      padding: 14px 18px;
      margin-bottom: 20px;
      box-shadow: var(--shadow-xs);
    }}
    .view-tabs-row {{
      display: flex;
      flex-wrap: wrap;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      padding-bottom: 12px;
      border-bottom: 1px solid var(--border-light);
      margin-bottom: 12px;
    }}
    .view-tabs {{
      display: inline-flex;
      background: var(--bg-subtle);
      padding: 4px;
      border-radius: 8px;
      gap: 4px;
      flex-wrap: wrap;
    }}
    .view-tab-btn {{
      border: none;
      background: transparent;
      color: var(--text-secondary);
      font-size: 13px;
      font-weight: 600;
      padding: 7px 14px;
      border-radius: 6px;
      cursor: pointer;
      transition: all 0.15s ease;
      font-family: inherit;
    }}
    .view-tab-btn.active {{
      background: var(--bg-surface);
      color: var(--brand-crimson);
      box-shadow: var(--shadow-xs);
    }}
    .search-box {{ position: relative; min-width: 270px; flex: 1; max-width: 380px; }}
    .search-box input {{
      width: 100%;
      padding: 7px 12px 7px 32px;
      border: 1px solid var(--border-strong);
      border-radius: var(--radius-sm);
      font-size: 13px;
      outline: none;
      font-family: inherit;
    }}
    .search-box input:focus {{
      border-color: var(--brand-crimson);
      box-shadow: 0 0 0 2px rgba(220, 38, 38, 0.12);
    }}
    .search-icon {{
      position: absolute;
      left: 10px;
      top: 50%;
      transform: translateY(-50%);
      color: var(--text-muted);
      font-size: 13px;
    }}
    .filter-rows {{ display: flex; flex-direction: column; gap: 8px; }}
    .filter-group {{ display: flex; align-items: center; flex-wrap: wrap; gap: 6px; }}
    .filter-label {{ font-size: 12px; font-weight: 700; color: var(--text-muted); min-width: 72px; }}
    .chip {{
      border: 1px solid var(--border-light);
      background: var(--bg-subtle);
      color: var(--text-secondary);
      padding: 4px 10px;
      border-radius: 999px;
      font-size: 12px;
      font-weight: 500;
      cursor: pointer;
      transition: all 0.12s ease;
      font-family: inherit;
      font-variant-numeric: tabular-nums;
    }}
    .chip:hover {{ border-color: var(--border-strong); background: #E5E7EB; }}
    .chip.active {{ background: #111827; color: #FFFFFF; border-color: #111827; font-weight: 600; }}
    .chip.chip-red.active {{ background: #DC2626; border-color: #DC2626; }}
    .module-section {{ margin-bottom: 28px; }}
    .module-header-banner {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      flex-wrap: wrap;
      gap: 10px;
      background: linear-gradient(90deg, #1E293B 0%, #334155 100%);
      color: #FFFFFF;
      padding: 12px 18px;
      border-radius: var(--radius-md);
      margin-bottom: 14px;
    }}
    .module-header-banner h2 {{ font-size: 16px; font-weight: 700; display: flex; align-items: center; gap: 10px; }}
    .sop-rule-block {{
      background: var(--bg-surface);
      border: 1px solid var(--border-strong);
      border-radius: var(--radius-lg);
      margin-bottom: 20px;
      overflow: hidden;
      box-shadow: var(--shadow-sm);
    }}
    .sop-standard-header {{ background: #F8FAFC; border-bottom: 1px solid var(--border-light); padding: 16px 20px; }}
    .sop-title-bar {{
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      flex-wrap: wrap;
      gap: 12px;
      margin-bottom: 12px;
    }}
    .sop-code-badge {{
      background: #111827;
      color: #FFFFFF;
      font-weight: 800;
      font-size: 13px;
      padding: 4px 10px;
      border-radius: 6px;
      font-variant-numeric: tabular-nums;
    }}
    .sop-code-badge.is-redline {{ background: #B91C1C; }}
    .sop-doc-tag {{
      font-size: 12px;
      color: var(--blue-text);
      background: var(--blue-bg);
      border: 1px solid var(--blue-border);
      padding: 2px 8px;
      border-radius: 4px;
      font-weight: 600;
    }}
    .sop-criteria-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 12px; }}
    .criteria-box {{ padding: 10px 14px; border-radius: var(--radius-sm); font-size: 13px; line-height: 1.5; }}
    .criteria-pass {{ background: var(--emerald-bg); border: 1px solid var(--emerald-border); color: #065F46; }}
    .criteria-fail {{ background: var(--brand-crimson-bg); border: 1px solid var(--brand-crimson-border); color: #991B1B; }}
    .criteria-box-title {{ font-weight: 700; font-size: 12px; margin-bottom: 4px; display: flex; align-items: center; gap: 5px; }}
    .sop-full-drawer {{
      display: none;
      margin-top: 12px;
      padding: 14px 16px;
      background: #FFFFFF;
      border: 1px dashed var(--border-strong);
      border-radius: var(--radius-sm);
      font-size: 13px;
      color: var(--text-secondary);
      white-space: pre-wrap;
      line-height: 1.65;
    }}
    .sop-full-drawer.open {{ display: block; }}
    .cards-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(420px, 1fr));
      gap: 16px;
      padding: 18px 20px;
      background: #FCFDFE;
    }}
    @media (max-width: 920px) {{ .cards-grid {{ grid-template-columns: 1fr; }} }}
    .violation-card {{
      background: var(--bg-surface);
      border: 1px solid var(--border-light);
      border-top: 4px solid #D97706;
      border-radius: var(--radius-md);
      padding: 16px;
      box-shadow: var(--shadow-xs);
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      gap: 12px;
      transition: transform 0.15s ease, box-shadow 0.15s ease;
      position: relative;
    }}
    .violation-card:hover {{ box-shadow: var(--shadow-md); }}
    .violation-card.sev-RED_LINE {{ border-top-color: #DC2626; }}
    .violation-card.status-CONFIRMED_VIOLATION {{ background: #FFFDFD; border-color: #FCA5A5; }}
    .violation-card.status-DISMISSED {{ opacity: 0.68; background: #F9FAFB; border-top-color: #10B981; }}
    .card-top-bar {{ display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 6px; }}
    .badge-group {{ display: flex; align-items: center; flex-wrap: wrap; gap: 6px; }}
    .card-num {{
      font-weight: 800;
      font-size: 12px;
      color: #374151;
      background: #F3F4F6;
      padding: 2px 7px;
      border-radius: 4px;
      font-variant-numeric: tabular-nums;
    }}
    .sev-pill {{
      font-size: 12px;
      font-weight: 700;
      padding: 2px 8px;
      border-radius: 4px;
      display: inline-flex;
      align-items: center;
      gap: 4px;
    }}
    .sev-pill.RED_LINE {{ background: var(--brand-crimson-bg); color: var(--brand-crimson); border: 1px solid var(--brand-crimson-border); }}
    .sev-pill.NORMAL {{ background: var(--amber-bg); color: var(--amber-text); border: 1px solid var(--amber-border); }}
    .disp-pill {{ font-size: 12px; font-weight: 600; padding: 2px 7px; border-radius: 4px; font-variant-numeric: tabular-nums; }}
    .disp-pill.CONFIRMED {{ background: #F1F5F9; color: #334155; }}
    .disp-pill.UNVERIFIED, .disp-pill.SUSPECTED {{ background: var(--purple-bg); color: var(--purple-text); border: 1px solid var(--purple-border); }}
    .staff-pill {{
      font-size: 12px;
      font-weight: 700;
      background: var(--blue-bg);
      color: var(--blue-text);
      border: 1px solid var(--blue-border);
      padding: 2px 8px;
      border-radius: 4px;
    }}
    .video-time-bar {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      flex-wrap: wrap;
      gap: 6px;
      background: var(--bg-subtle);
      padding: 6px 10px;
      border-radius: 6px;
      font-size: 12px;
      color: var(--text-secondary);
      font-variant-numeric: tabular-nums;
    }}
    .evidence-desc {{ font-size: 14px; color: var(--text-primary); line-height: 1.6; padding: 2px 0; }}
    .hl-staff {{ font-weight: 700; color: #1D4ED8; background: #EFF6FF; padding: 0 4px; border-radius: 3px; }}
    .hl-time {{ font-weight: 700; color: #7C2D12; background: #FFEDD5; padding: 0 4px; border-radius: 3px; font-variant-numeric: tabular-nums; }}
    .card-sop-box {{
      background: var(--bg-sop);
      border: 1px solid var(--border-light);
      border-left: 3px solid #3B82F6;
      border-radius: var(--radius-sm);
      padding: 10px 12px;
      font-size: 12.5px;
      display: flex;
      flex-direction: column;
      gap: 6px;
    }}
    .violation-card.sev-RED_LINE .card-sop-box {{ border-left-color: #DC2626; }}
    .card-sop-header {{ display: flex; justify-content: space-between; align-items: center; gap: 6px; flex-wrap: wrap; }}
    .card-sop-clause-title {{ font-weight: 700; color: var(--text-primary); font-size: 13px; }}
    .card-sop-row {{ color: var(--text-secondary); line-height: 1.48; }}
    .card-sop-row strong {{ color: var(--text-primary); }}
    .rectification-tip {{
      background: #FFFBEB;
      border: 1px solid #FDE68A;
      color: #92400E;
      padding: 7px 10px;
      border-radius: 6px;
      font-size: 12px;
      line-height: 1.45;
    }}
    .card-actions-footer {{ border-top: 1px solid var(--border-light); padding-top: 10px; display: flex; flex-direction: column; gap: 8px; }}
    .video-and-decision-row {{ display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px; }}
    .btn-video-play {{
      background: #1E293B;
      color: #FFFFFF;
      padding: 5px 11px;
      border-radius: 6px;
      font-size: 12px;
      font-weight: 600;
      border: none;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      gap: 5px;
    }}
    .btn-video-play:hover {{ background: #0F172A; }}
    .btn-drive-link {{ font-size: 12px; color: var(--blue-text); text-decoration: none; font-weight: 600; padding: 4px 6px; }}
    .btn-drive-link:hover {{ text-decoration: underline; }}
    .decision-btns {{ display: inline-flex; gap: 5px; flex-wrap: wrap; }}
    .dec-btn {{
      font-size: 12px;
      font-weight: 600;
      padding: 4px 9px;
      border-radius: 5px;
      border: 1px solid var(--border-strong);
      background: var(--bg-surface);
      color: var(--text-secondary);
      cursor: pointer;
      font-family: inherit;
    }}
    .dec-btn:hover {{ background: var(--bg-subtle); }}
    .dec-btn.active-confirm {{ background: #DC2626; color: #FFFFFF; border-color: #DC2626; }}
    .dec-btn.active-dismiss {{ background: #059669; color: #FFFFFF; border-color: #059669; }}
    .dec-btn.active-followup {{ background: #D97706; color: #FFFFFF; border-color: #D97706; }}
    .supervisor-note-row {{ display: flex; gap: 6px; align-items: center; }}
    .supervisor-note-input {{
      flex: 1;
      padding: 5px 9px;
      font-size: 12px;
      border: 1px solid var(--border-light);
      border-radius: 5px;
      background: #F9FAFB;
      color: var(--text-primary);
      font-family: inherit;
    }}
    .supervisor-note-input:focus {{ outline: none; border-color: #2563EB; background: #FFFFFF; }}
    .timeline-track-box {{ padding: 14px 20px; background: #F8FAFC; border-bottom: 1px solid var(--border-light); }}
    .timeline-bar {{ position: relative; height: 10px; background: #E2E8F0; border-radius: 999px; margin: 18px 8px 10px; }}
    .timeline-pin {{
      position: absolute;
      top: 50%;
      transform: translate(-50%, -50%);
      width: 16px;
      height: 16px;
      border-radius: 50%;
      border: 2px solid #FFFFFF;
      cursor: pointer;
      box-shadow: 0 1px 4px rgba(0,0,0,0.25);
      transition: transform 0.15s ease;
    }}
    .timeline-pin:hover {{ transform: translate(-50%, -50%) scale(1.3); z-index: 5; }}
    .timeline-pin.RED_LINE {{ background: #DC2626; }}
    .timeline-pin.NORMAL {{ background: #F59E0B; }}
    .video-drawer-backdrop {{
      display: none;
      position: fixed;
      inset: 0;
      background: rgba(15, 23, 42, 0.55);
      z-index: 100;
      align-items: center;
      justify-content: center;
      padding: 20px;
    }}
    .video-drawer-backdrop.open {{ display: flex; }}
    .video-modal {{
      background: var(--bg-surface);
      width: 100%;
      max-width: 1060px;
      border-radius: var(--radius-lg);
      overflow: hidden;
      box-shadow: 0 20px 50px rgba(0, 0, 0, 0.3);
      display: flex;
      flex-direction: column;
      max-height: 92vh;
    }}
    .video-modal-header {{
      background: #111827;
      color: #FFFFFF;
      padding: 14px 20px;
      display: flex;
      justify-content: space-between;
      align-items: center;
    }}
    .video-modal-body {{ display: grid; grid-template-columns: 1.35fr 1fr; gap: 0; overflow-y: auto; }}
    @media (max-width: 860px) {{ .video-modal-body {{ grid-template-columns: 1fr; }} }}
    .video-iframe-wrap {{ background: #000000; min-height: 380px; display: flex; flex-direction: column; justify-content: center; }}
    .video-iframe-wrap iframe {{ width: 100%; height: 400px; border: none; }}
    .video-modal-sidebar {{
      padding: 18px 20px;
      display: flex;
      flex-direction: column;
      gap: 12px;
      background: #F8FAFC;
      border-left: 1px solid var(--border-light);
    }}
    .data-table {{ width: 100%; border-collapse: collapse; font-size: 13px; background: var(--bg-surface); }}
    .data-table th, .data-table td {{ padding: 10px 14px; border-bottom: 1px solid var(--border-light); text-align: left; vertical-align: top; }}
    .data-table th {{ background: #F8FAFC; font-weight: 700; color: var(--text-secondary); font-size: 12px; }}
    .toast {{
      position: fixed;
      bottom: 24px;
      right: 24px;
      background: #111827;
      color: #FFFFFF;
      padding: 10px 16px;
      border-radius: 8px;
      font-size: 13px;
      font-weight: 600;
      box-shadow: var(--shadow-md);
      z-index: 200;
      opacity: 0;
      pointer-events: none;
      transition: opacity 0.2s ease;
    }}
    .toast.show {{ opacity: 1; }}
  </style>
</head>
<body>

  <header class="top-header">
    <div class="top-header-inner">
      <div>
        <div class="brand-title-group">
          <span class="brand-badge">CHAGEE · AI 云稽核督导台</span>
          <h1 id="header-title">门店后厨视频稽核复核与 SOP 整改工作台</h1>
        </div>
        <div class="store-meta-pills" id="header-meta-pills"></div>
      </div>
      <div class="header-actions">
        <button class="btn btn-outline-light" id="btn-batch-confirm" onclick="batchConfirmAllConfirmed()">✅ 一键确认全部实锤违规</button>
        <button class="btn btn-outline-light" onclick="exportReviewedCSV()">📥 导出复核 CSV</button>
        <button class="btn btn-primary" onclick="openRectificationModal()">📋 生成《门店 48h 整改通知单》</button>
      </div>
    </div>
  </header>

  <nav class="batch-switcher-bar" id="batch-switcher-bar" style="display:none;">
    <div class="batch-switcher-inner" id="batch-switcher-inner"></div>
  </nav>

  <main class="main-container">
    <div id="job-status-banner"></div>

    <section class="kpi-grid">
      <div class="kpi-card kpi-red">
        <div class="kpi-label">
          <span>🔴 红线零容忍违规 (RED_LINE)</span>
          <span style="color:#DC2626;font-weight:700;">48h 必改</span>
        </div>
        <div class="kpi-value-row">
          <span class="kpi-value" style="color:#DC2626;" id="kpi-red-count">0</span>
          <span style="font-size:13px;color:var(--text-muted);" id="kpi-total-suffix">/ 0 项总事件</span>
        </div>
        <div class="kpi-sub" id="kpi-red-sub">红线事件须于 48 小时内完成闭环整改并回传复训证明</div>
      </div>

      <div class="kpi-card kpi-amber">
        <div class="kpi-label">
          <span>🟡 一般操作偏差 (NORMAL)</span>
          <span style="color:#D97706;font-weight:700;">晨会宣导纠偏</span>
        </div>
        <div class="kpi-value-row">
          <span class="kpi-value" style="color:#D97706;" id="kpi-normal-count">0</span>
          <span style="font-size:13px;color:var(--text-muted);">项常规偏差</span>
        </div>
        <div class="kpi-sub" id="kpi-normal-sub">日常仪容、操作规范与设备清洁细节偏差</div>
      </div>

      <div class="kpi-card kpi-blue">
        <div class="kpi-label">
          <span>📐 违反 SOP 条款分布</span>
          <span style="color:#2563EB;font-weight:700;" id="kpi-sop-badge">共命中 0 条 SOP</span>
        </div>
        <div class="kpi-value-row">
          <span class="kpi-value" style="color:#1E40AF;" id="kpi-sop-count">0 <span style="font-size:15px;font-weight:600;color:var(--text-muted);">/ 24 条</span></span>
        </div>
        <div class="kpi-sub" id="kpi-sop-sub">【A】洗手卫生 · 【B】制冰机管理 · 【C】仪容与泡茶机</div>
      </div>

      <div class="kpi-card kpi-emerald">
        <div class="kpi-label">
          <span>👨‍💼 督导人工复核进度（实时同步 Sheet）</span>
          <span id="kpi-review-pct" style="color:#059669;font-weight:700;">0%</span>
        </div>
        <div class="kpi-value-row">
          <span class="kpi-value" id="kpi-reviewed-count">0 <span style="font-size:15px;font-weight:600;color:var(--text-muted);">/ 0 已复核</span></span>
        </div>
        <div class="kpi-sub" id="kpi-review-breakdown">确认违规 0 · 排除误报 0 · 待调录像 0</div>
        <div class="progress-bar-track">
          <div class="progress-fill-confirmed" id="prog-confirm" style="width:0%"></div>
          <div class="progress-fill-dismissed" id="prog-dismiss" style="width:0%"></div>
          <div class="progress-fill-followup" id="prog-followup" style="width:0%"></div>
        </div>
      </div>
    </section>

    <section class="control-panel">
      <div class="view-tabs-row">
        <div class="view-tabs" role="tablist">
          <button class="view-tab-btn active" id="tab-sop" onclick="setViewMode('sop')">🗂️ 按违反的 SOP 模块与条款分组（带 SOP 原文）</button>
          <button class="view-tab-btn" id="tab-video" onclick="setViewMode('video')">🎬 按监控原片与时间轴顺序巡检</button>
          <button class="view-tab-btn" id="tab-dict" onclick="setViewMode('dict')">📚 全量 SOP 标准字典 & Token 账单</button>
        </div>
        <div class="search-box">
          <span class="search-icon">🔍</span>
          <input type="text" id="search-input" placeholder="搜索员工(员工A/B)、动作(手机/洗手/冰铲)、SOP条款(A2/C4)..." oninput="renderActiveView()" />
        </div>
      </div>

      <div class="filter-rows" id="filter-bar-container">
        <div class="filter-group" id="filter-severity-group"></div>
        <div class="filter-group" id="filter-module-group"></div>
        <div class="filter-group" id="filter-video-staff-group"></div>
        <div class="filter-group">
          <span class="filter-label">复核状态：</span>
          <button class="chip active" data-filter="review" data-val="ALL" onclick="setFilter('review','ALL')">全部状态</button>
          <button class="chip" data-filter="review" data-val="PENDING" onclick="setFilter('review','PENDING')">⏳ 待督导复核</button>
          <button class="chip" data-filter="review" data-val="CONFIRMED_VIOLATION" onclick="setFilter('review','CONFIRMED_VIOLATION')">✅ 已确认违规</button>
          <button class="chip" data-filter="review" data-val="DISMISSED" onclick="setFilter('review','DISMISSED')">❌ 已排除误报</button>
          <button class="chip" data-filter="review" data-val="FOLLOWUP" onclick="setFilter('review','FOLLOWUP')">⚠️ 待调后续录像</button>
        </div>
      </div>
    </section>

    <div id="view-content"></div>
  </main>

  <div class="video-drawer-backdrop" id="video-modal-backdrop" onclick="if(event.target===this) closeVideoModal()">
    <div class="video-modal">
      <div class="video-modal-header">
        <div id="modal-title" style="font-weight:700;font-size:15px;">🎬 20 秒违规证据视频复核</div>
        <div style="display:flex;gap:10px;align-items:center;">
          <a id="modal-drive-link" href="#" target="_blank" rel="noopener" class="btn btn-outline-light" style="padding:4px 10px;font-size:12px;">↗ 在新窗口打开原视频</a>
          <button class="btn btn-outline-light" style="padding:4px 10px;font-size:12px;" onclick="closeVideoModal()">✕ 关闭 (ESC)</button>
        </div>
      </div>
      <div class="video-modal-body">
        <div class="video-iframe-wrap">
          <iframe id="modal-video-iframe" src="" allow="autoplay; encrypted-media" allowfullscreen></iframe>
          <div style="padding:10px 16px;background:#0F172A;color:#94A3B8;font-size:12px;display:flex;justify-content:space-between;align-items:center;">
            <span>💡 提示：系统已自动截取违规发生前后约 20 秒高清 H.264 证据视频片段</span>
            <span id="modal-ts-label" style="color:#F8FAFC;font-weight:600;"></span>
          </div>
        </div>
        <div class="video-modal-sidebar" id="modal-sidebar-content"></div>
      </div>
    </div>
  </div>

  <div class="video-drawer-backdrop" id="rect-modal-backdrop" onclick="if(event.target===this) closeRectificationModal()">
    <div class="video-modal" style="max-width:860px;">
      <div class="video-modal-header">
        <div id="rect-modal-title" style="font-weight:700;font-size:15px;">📋 《CHAGEE 门店 AI 云稽核 48 小时整改通知单》</div>
        <div style="display:flex;gap:8px;">
          <button class="btn btn-primary" style="padding:5px 12px;font-size:12px;" onclick="copyRectificationReport()">📋 一键复制全文（发飞书/企微/群聊）</button>
          <button class="btn btn-outline-light" style="padding:5px 10px;font-size:12px;" onclick="window.print()">🖨️ 打印 / 存为 PDF</button>
          <button class="btn btn-outline-light" style="padding:5px 10px;font-size:12px;" onclick="closeRectificationModal()">✕ 关闭</button>
        </div>
      </div>
      <div style="padding:20px;overflow-y:auto;max-height:78vh;background:#FFFFFF;">
        <pre id="rect-report-text" style="white-space:pre-wrap;font-family:inherit;font-size:13px;line-height:1.65;background:#F8FAFC;padding:18px;border:1px solid var(--border-strong);border-radius:8px;color:var(--text-primary);"></pre>
      </div>
    </div>
  </div>

  <div class="toast" id="toast-msg">已保存</div>

  <script>
    const PORTAL_DATA = {portal_json};

    const STORAGE_KEY = "chagee_audit_review_state_" + PORTAL_DATA.audit_id + "_v2";
    let reviewState = {{}};
    try {{
      const saved = localStorage.getItem(STORAGE_KEY);
      if (saved) reviewState = JSON.parse(saved);
    }} catch (e) {{}}

    // Seed initial review state from Google Sheet Columns J & K (Sheet outranks empty local state)
    PORTAL_DATA.findings.forEach(f => {{
      const existing = reviewState[f.id] || {{ status: "PENDING", note: "" }};
      const sheetStatus = f.initial_review_status || "PENDING";
      const sheetNote = f.initial_review_note || "";
      reviewState[f.id] = {{
        status: sheetStatus !== "PENDING" ? sheetStatus : existing.status,
        note: sheetNote ? sheetNote : existing.note
      }};
    }});

    let currentView = "sop";
    let activeFilters = {{
      severity: "ALL",
      module: "ALL",
      video: "ALL",
      staff: "ALL",
      review: "ALL"
    }};
    let noteTimers = {{}};

    const sopMap = {{}};
    (PORTAL_DATA.sop_rules || []).forEach(r => {{ sopMap[r.rule_id] = r; }});

    function escapeHtml(str) {{
      return String(str || "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
    }}

    function showToast(msg) {{
      const t = document.getElementById("toast-msg");
      t.textContent = msg;
      t.classList.add("show");
      setTimeout(() => t.classList.remove("show"), 2600);
    }}

    function getFindingState(id) {{
      return reviewState[id] || {{ status: "PENDING", note: "" }};
    }}

    async function persistReviewsToSheet(updatesList) {{
      if (!PORTAL_DATA.sheet_url || !updatesList || updatesList.length === 0) return;
      try {{
        const auth = PORTAL_DATA.auth || {{}};
        const resp = await fetch("/api/portal/review", {{
          method: "POST",
          headers: {{ "Content-Type": "application/json" }},
          body: JSON.stringify({{
            job: auth.job || "",
            user: auth.user || "",
            batch: auth.batch || "",
            store: auth.store || PORTAL_DATA.audit_id,
            exp: auth.exp || 0,
            sig: auth.sig || "",
            updates: updatesList
          }})
        }});
        if (resp.ok) {{
          const data = await resp.json();
          if (data.persisted_to_sheet) {{
            showToast(`💾 已实时同步回写至 Google Sheet (${{data.updated_count}} 项)`);
          }}
        }}
      }} catch (err) {{
        console.warn("Failed to sync review to Sheet:", err);
      }}
    }}

    function saveFindingState(id, status, note, syncNow) {{
      const cur = getFindingState(id);
      const nextStatus = status !== undefined ? status : cur.status;
      const nextNote = note !== undefined ? note : cur.note;
      reviewState[id] = {{ status: nextStatus, note: nextNote }};
      try {{
        localStorage.setItem(STORAGE_KEY, JSON.stringify(reviewState));
      }} catch (e) {{}}
      updateKpiProgress();

      const f = PORTAL_DATA.findings.find(x => x.id === id);
      if (!f || !f.incident_id) return;
      const payload = [{{ incident_id: f.incident_id, status: nextStatus, note: nextNote }}];
      if (syncNow) {{
        persistReviewsToSheet(payload);
      }} else {{
        if (noteTimers[id]) clearTimeout(noteTimers[id]);
        noteTimers[id] = setTimeout(() => persistReviewsToSheet(payload), 700);
      }}
    }}

    function initHeaderAndFilters() {{
      document.getElementById("header-title").textContent =
        `门店后厨视频稽核复核与 SOP 整改工作台 — ${{PORTAL_DATA.store_name}}`;
      document.getElementById("rect-modal-title").textContent =
        `📋 《CHAGEE 门店 AI 云稽核 48 小时整改通知单》— ${{PORTAL_DATA.store_name}}`;

      const pills = [];
      if (PORTAL_DATA.batch_meta) {{
        pills.push(`<span class="meta-pill">🗓️ 巡检批次：<strong>${{escapeHtml(PORTAL_DATA.batch_meta.batch_id)}}</strong> (${{escapeHtml(PORTAL_DATA.batch_meta.rule_name)}})</span>`);
      }}
      pills.push(`<span class="meta-pill">📌 门店稽核单号：<strong>#${{escapeHtml(PORTAL_DATA.audit_id)}}</strong></span>`);
      if (PORTAL_DATA.folder_url) {{
        pills.push(`<span class="meta-pill">🏪 门店目录：<a href="${{escapeHtml(PORTAL_DATA.folder_url)}}" target="_blank" rel="noopener">${{escapeHtml(PORTAL_DATA.store_name)}} ↗</a></span>`);
      }}
      if (PORTAL_DATA.sheet_url) {{
        pills.push(`<span class="meta-pill">📊 源数据表：<a href="${{escapeHtml(PORTAL_DATA.sheet_url)}}" target="_blank" rel="noopener">AI稽核报告与Token账单 ↗</a></span>`);
      }}
      pills.push(`<span class="meta-pill">📐 适用标准：<strong>${{escapeHtml(PORTAL_DATA.active_prompt_version)}}</strong></span>`);
      pills.push(`<span class="meta-pill">🤖 视觉引擎：<strong>${{escapeHtml(PORTAL_DATA.active_model_version)}}</strong></span>`);
      document.getElementById("header-meta-pills").innerHTML = pills.join("");

      // Batch Store Switcher ([P1-2])
      if (PORTAL_DATA.batch_stores && PORTAL_DATA.batch_stores.length > 0) {{
        const bar = document.getElementById("batch-switcher-bar");
        const inner = document.getElementById("batch-switcher-inner");
        bar.style.display = "block";
        inner.innerHTML = `<span style="color:#94A3B8;font-size:12px;font-weight:700;">🏪 批次门店切换 (${{PORTAL_DATA.batch_stores.length}} 家)：</span>` +
          PORTAL_DATA.batch_stores.map(st => {{
            const icon = st.state === "DONE" ? "🟢" : (st.state === "RUNNING" ? "⏳" : "❌");
            const badge = st.state === "DONE" ? `${{st.violations_found}}项` : st.state;
            return `<a href="${{escapeHtml(st.switch_url)}}" class="batch-store-tab ${{st.active ? 'active' : ''}}">${{icon}} ${{escapeHtml(st.folder_name)}} (${{badge}})</a>`;
          }}).join("");
      }}

      // Status banner for RUNNING or FAILED stores
      const bannerEl = document.getElementById("job-status-banner");
      if (PORTAL_DATA.job_state === "RUNNING") {{
        bannerEl.innerHTML = `
          <div class="status-banner running">
            <div>⏳ <strong>该门店视频正在后台 AI 稽核中</strong>：已完成 <strong>${{PORTAL_DATA.completed_segments_count}} / ${{PORTAL_DATA.planned_segments_count}}</strong> 个视频分片（下方展示已完成分片的实时结果，全部完成后将自动生成完整 Google Sheet 报告）。</div>
            <button class="btn btn-secondary" onclick="window.location.reload()">🔄 刷新最新进度</button>
          </div>
        `;
      }} else if (PORTAL_DATA.job_state === "FAILED" || PORTAL_DATA.job_state === "REJECTED") {{
        bannerEl.innerHTML = `
          <div class="status-banner failed">
            <div>❌ <strong>该门店任务未完成 (${{escapeHtml(PORTAL_DATA.job_state)}})</strong>：${{escapeHtml(PORTAL_DATA.error_message || '预检未通过或执行中断')}}</div>
          </div>
        `;
      }} else {{
        bannerEl.innerHTML = "";
      }}

      // KPI static counts
      const findings = PORTAL_DATA.findings || [];
      const redCount = findings.filter(f => f.severity === "RED_LINE").length;
      const normalCount = findings.filter(f => f.severity !== "RED_LINE").length;
      const unverifiedCount = findings.filter(f => f.disposition === "UNVERIFIED").length;
      const confirmedDispCount = findings.filter(f => f.disposition === "CONFIRMED").length;
      const uniqueRules = [...new Set(findings.map(f => f.rule_id))];
      const countA = findings.filter(f => f.module_code === "A").length;
      const countB = findings.filter(f => f.module_code === "B").length;
      const countC = findings.filter(f => f.module_code === "C").length;

      document.getElementById("kpi-red-count").textContent = String(redCount);
      document.getElementById("kpi-total-suffix").textContent = `/ ${{findings.length}} 项总事件`;
      document.getElementById("kpi-normal-count").textContent = String(normalCount);
      document.getElementById("kpi-sop-badge").textContent = `共命中 ${{uniqueRules.length}} 条 SOP`;
      document.getElementById("kpi-sop-count").innerHTML =
        `${{uniqueRules.length}} <span style="font-size:15px;font-weight:600;color:var(--text-muted);">/ ${{(PORTAL_DATA.sop_rules || []).length || 24}} 条</span>`;
      document.getElementById("kpi-sop-sub").textContent =
        `【A】洗手与手部卫生 ${{countA}} 项 · 【B】制冰机与冰铲 ${{countB}} 项 · 【C】仪容手机与泡茶机 ${{countC}} 项`;
      document.getElementById("btn-batch-confirm").textContent =
        `✅ 一键确认 ${{confirmedDispCount}} 项实锤违规`;

      // Dynamic filter chips
      document.getElementById("filter-severity-group").innerHTML = `
        <span class="filter-label">严重等级：</span>
        <button class="chip active" data-filter="severity" data-val="ALL" onclick="setFilter('severity','ALL')">全部事件 (${{findings.length}})</button>
        <button class="chip chip-red" data-filter="severity" data-val="RED_LINE" onclick="setFilter('severity','RED_LINE')">🔴 红线必改 RED_LINE (${{redCount}})</button>
        <button class="chip" data-filter="severity" data-val="NORMAL" onclick="setFilter('severity','NORMAL')">🟡 一般偏差 NORMAL (${{normalCount}})</button>
        <button class="chip" data-filter="severity" data-val="UNVERIFIED" onclick="setFilter('severity','UNVERIFIED')">⚠️ 视线盲区待核 (${{unverifiedCount}})</button>
      `;

      document.getElementById("filter-module-group").innerHTML = `
        <span class="filter-label">SOP 模块：</span>
        <button class="chip active" data-filter="module" data-val="ALL" onclick="setFilter('module','ALL')">全部模块 (${{findings.length}})</button>
        <button class="chip" data-filter="module" data-val="A" onclick="setFilter('module','A')">【A】洗手与手部卫生 (${{countA}})</button>
        <button class="chip" data-filter="module" data-val="B" onclick="setFilter('module','B')">【B】制冰机与冰块管理 (${{countB}})</button>
        <button class="chip" data-filter="module" data-val="C" onclick="setFilter('module','C')">【C】仪容、手机与泡茶设备 (${{countC}})</button>
      `;

      const videoKeys = [...new Set(findings.map(f => f.video_short))];
      const videoBtns = videoKeys.map(vk => {{
        const vf = findings.filter(f => f.video_short === vk);
        return `<button class="chip" data-filter="video" data-val="${{escapeHtml(vk)}}" onclick="setFilter('video','${{escapeHtml(vk)}}')">📹 ${{escapeHtml(vk)}} (${{vf.length}})</button>`;
      }}).join("");

      const staffSet = new Set();
      findings.forEach(f => (f.staff_list || []).forEach(s => staffSet.add(s)));
      const staffBtns = [...staffSet].map(st => {{
        const sf = findings.filter(f => (f.staff_list || []).includes(st));
        return `<button class="chip" data-filter="staff" data-val="${{escapeHtml(st)}}" onclick="setFilter('staff','${{escapeHtml(st)}}')">👤 ${{escapeHtml(st)}} (${{sf.length}})</button>`;
      }}).join("");

      document.getElementById("filter-video-staff-group").innerHTML = `
        <span class="filter-label">监控原片：</span>
        <button class="chip active" data-filter="video" data-val="ALL" onclick="setFilter('video','ALL')">全部原片 (${{videoKeys.length}}段)</button>
        ${{videoBtns}}
        <span style="margin-left:12px;color:var(--border-strong);">|</span>
        <span class="filter-label" style="min-width:auto;margin-left:8px;">涉事人员：</span>
        <button class="chip active" data-filter="staff" data-val="ALL" onclick="setFilter('staff','ALL')">全部人员</button>
        ${{staffBtns}}
      `;
    }}

    function highlightDescription(desc) {{
      let safe = escapeHtml(desc);
      safe = safe.replace(/(员工[A-Z0-9]+)/g, '<span class="hl-staff">$1</span>');
      safe = safe.replace(/(\\b\\d{{2}}:\\d{{2}}(?::\\d{{2}})?(?:-\\d{{2}}:\\d{{2}}(?::\\d{{2}})?)?\\b)/g, '<span class="hl-time">$1</span>');
      return safe;
    }}

    function updateKpiProgress() {{
      let confirmed = 0, dismissed = 0, followup = 0;
      const findings = PORTAL_DATA.findings || [];
      findings.forEach(f => {{
        const st = getFindingState(f.id).status;
        if (st === "CONFIRMED_VIOLATION") confirmed++;
        else if (st === "DISMISSED") dismissed++;
        else if (st === "FOLLOWUP") followup++;
      }});
      const total = findings.length;
      const reviewed = confirmed + dismissed + followup;
      const pct = total > 0 ? Math.round((reviewed / total) * 100) : 0;

      document.getElementById("kpi-review-pct").textContent = pct + "%";
      document.getElementById("kpi-reviewed-count").innerHTML =
        `${{reviewed}} <span style="font-size:15px;font-weight:600;color:var(--text-muted);">/ ${{total}} 已复核</span>`;
      document.getElementById("kpi-review-breakdown").textContent =
        `已确认违规 ${{confirmed}} · 已排除误报 ${{dismissed}} · 待调录像 ${{followup}}`;

      document.getElementById("prog-confirm").style.width = (total > 0 ? (confirmed / total) * 100 : 0) + "%";
      document.getElementById("prog-dismiss").style.width = (total > 0 ? (dismissed / total) * 100 : 0) + "%";
      document.getElementById("prog-followup").style.width = (total > 0 ? (followup / total) * 100 : 0) + "%";
    }}

    function setViewMode(mode) {{
      currentView = mode;
      ["sop", "video", "dict"].forEach(m => {{
        document.getElementById("tab-" + m).classList.toggle("active", m === mode);
      }});
      document.getElementById("filter-bar-container").style.display = (mode === "dict") ? "none" : "flex";
      renderActiveView();
    }}

    function setFilter(key, val) {{
      activeFilters[key] = val;
      document.querySelectorAll(`.chip[data-filter="${{key}}"]`).forEach(btn => {{
        btn.classList.toggle("active", btn.getAttribute("data-val") === val);
      }});
      renderActiveView();
    }}

    function filterFindings() {{
      const q = (document.getElementById("search-input").value || "").trim().toLowerCase();
      return (PORTAL_DATA.findings || []).filter(f => {{
        if (activeFilters.severity === "RED_LINE" && f.severity !== "RED_LINE") return false;
        if (activeFilters.severity === "NORMAL" && f.severity !== "NORMAL") return false;
        if (activeFilters.severity === "UNVERIFIED" && f.disposition !== "UNVERIFIED") return false;
        if (activeFilters.module !== "ALL" && f.module_code !== activeFilters.module) return false;
        if (activeFilters.video !== "ALL" && f.video_short !== activeFilters.video) return false;
        if (activeFilters.staff !== "ALL" && !(f.staff_list || []).includes(activeFilters.staff)) return false;
        const st = getFindingState(f.id).status;
        if (activeFilters.review !== "ALL" && st !== activeFilters.review) return false;
        if (q) {{
          const hay = [
            f.code, f.rule_id, f.rule_name, f.sub_clause_title,
            f.description, f.staff_tag, f.video_display, f.mmss, f.osd_clock
          ].join(" ").toLowerCase();
          if (!hay.includes(q)) return false;
        }}
        return true;
      }});
    }}

    function renderViolationCard(f) {{
      const st = getFindingState(f.id);
      const isRed = f.severity === "RED_LINE";
      const sevLabel = isRed ? "🔴 红线零容忍 · 48h 必改" : "🟡 一般操作偏差";
      const dispLabel = f.disposition === "UNVERIFIED"
        ? `⚠️ UNVERIFIED 盲区待核 (${{f.confidence_pct}})`
        : `CONFIRMED 实锤 (${{f.confidence_pct}})`;

      return `
        <article class="violation-card sev-${{f.severity}} status-${{st.status}}" id="finding-card-${{f.id}}">
          <div style="display:flex;flex-direction:column;gap:10px;">
            <div class="card-top-bar">
              <div class="badge-group">
                <span class="card-num">${{f.code}}</span>
                <span class="sev-pill ${{f.severity}}">${{sevLabel}}</span>
                <span class="disp-pill ${{f.disposition}}">${{dispLabel}}</span>
              </div>
              <span class="staff-pill">👤 ${{escapeHtml(f.staff_tag)}}</span>
            </div>

            <div class="video-time-bar">
              <span>📹 <strong>${{escapeHtml(f.video_display)}}</strong></span>
              <span>⏱️ 相对时间 <strong>${{escapeHtml(f.mmss)}}</strong>${{f.osd_clock ? ` · 🕒 监控OSD <strong>${{escapeHtml(f.osd_clock)}}</strong>` : ""}}</span>
            </div>

            <div class="evidence-desc">
              ${{highlightDescription(f.description)}}
            </div>

            <div class="card-sop-box">
              <div class="card-sop-header">
                <span class="card-sop-clause-title">📌 违反 SOP：${{escapeHtml(f.rule_id)}} · ${{escapeHtml(f.sub_clause_title)}}</span>
                <span style="font-size:11.5px;color:var(--text-muted);">${{escapeHtml(f.doc_ref)}}</span>
              </div>
              <div class="card-sop-row">
                <strong>✅ SOP 标准做法：</strong>${{escapeHtml(f.specific_pass)}}
              </div>
              <div class="card-sop-row" style="color:#991B1B;">
                <strong>🚫 SOP 判定红线：</strong>${{escapeHtml(f.sop_fail_criteria)}}
              </div>
            </div>

            <div class="rectification-tip">
              <strong>💡 督导整改建议：</strong>${{escapeHtml(f.rectification_advice)}}
            </div>
          </div>

          <div class="card-actions-footer">
            <div class="video-and-decision-row">
              <div style="display:flex;align-items:center;gap:6px;">
                ${{f.preview_url ? `<button class="btn-video-play" onclick="openVideoModal(${{f.id}})">▶ 页内播放 20s 证据切片</button>` : ""}}
                ${{f.drive_url ? `<a class="btn-drive-link" href="${{escapeHtml(f.drive_url)}}" target="_blank" rel="noopener">↗ 打开原视频</a>` : ""}}
              </div>
              <div class="decision-btns">
                <button class="dec-btn ${{st.status === 'CONFIRMED_VIOLATION' ? 'active-confirm' : ''}}" onclick="setDecision(${{f.id}}, 'CONFIRMED_VIOLATION')">✅ 确认违规</button>
                <button class="dec-btn ${{st.status === 'DISMISSED' ? 'active-dismiss' : ''}}" onclick="setDecision(${{f.id}}, 'DISMISSED')">❌ 排除误报</button>
                <button class="dec-btn ${{st.status === 'FOLLOWUP' ? 'active-followup' : ''}}" onclick="setDecision(${{f.id}}, 'FOLLOWUP')">⚠️ 待调录像</button>
              </div>
            </div>
            <div class="supervisor-note-row">
              <input type="text" class="supervisor-note-input" placeholder="添加督导复核批注 / 店长整改要求（自动同步写回 Google Sheet 第 K 列）..." value="${{escapeHtml(st.note)}}" oninput="saveFindingState(${{f.id}}, undefined, this.value, false)" />
              <button class="dec-btn" title="复制该条整改通知发给店长" onclick="copySingleFindingNotice(${{f.id}})">📋 复制单条通知</button>
            </div>
          </div>
        </article>
      `;
    }}

    function toggleSopDrawer(ruleId) {{
      const el = document.getElementById("sop-drawer-" + ruleId);
      if (el) el.classList.toggle("open");
    }}

    function renderSopGroupedView(filtered) {{
      if (filtered.length === 0) {{
        return `<div style="background:#FFF;padding:40px;text-align:center;border-radius:12px;color:var(--text-muted);">当前筛选条件下没有匹配的稽核事件。</div>`;
      }}
      const modulesOrder = ["A", "B", "C", "D"];
      const moduleTitles = {{
        "A": "【A】洗手与手部卫生模块（APAC 参数表 1.5 / 1.6 · 强制洗手三要素与五步法）",
        "B": "【B】制冰机周清洁与冰块管理模块（SOP 8 / SOP 21 & 参数表 3.1 / 4.1 / 4.5）",
        "C": "【C】常态零容忍红线、员工仪容与泡茶机规范（参数表 1.1–1.4 / 6.2）",
        "D": "【D】运营与人效模块（IPLH 出杯效率与出品规范）"
      }};
      let html = "";
      modulesOrder.forEach(mCode => {{
        const modFindings = filtered.filter(f => f.module_code === mCode);
        if (modFindings.length === 0) return;
        const redCount = modFindings.filter(f => f.severity === "RED_LINE").length;
        html += `
          <section class="module-section">
            <div class="module-header-banner">
              <h2><span>${{moduleTitles[mCode] || mCode}}</span></h2>
              <div style="display:flex;gap:8px;font-size:12.5px;">
                <span style="background:rgba(255,255,255,0.15);padding:3px 10px;border-radius:999px;">共检出 <strong>${{modFindings.length}}</strong> 项</span>
                ${{redCount > 0 ? `<span style="background:#DC2626;padding:3px 10px;border-radius:999px;font-weight:700;">🔴 红线 ${{redCount}} 项</span>` : ""}}
              </div>
            </div>
        `;
        const ruleIds = [...new Set(modFindings.map(f => f.rule_id))].sort();
        ruleIds.forEach(rid => {{
          const ruleFindings = modFindings.filter(f => f.rule_id === rid);
          const sop = sopMap[rid] || {{}};
          const hasRed = ruleFindings.some(f => f.severity === "RED_LINE");
          html += `
            <div class="sop-rule-block">
              <div class="sop-standard-header">
                <div class="sop-title-bar">
                  <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;">
                    <span class="sop-code-badge ${{hasRed ? 'is-redline' : ''}}">SOP 条款 ${{escapeHtml(rid)}}（共 ${{ruleFindings.length}} 项违规）</span>
                    <h3 style="font-size:15px;font-weight:700;color:var(--text-primary);">${{escapeHtml(sop.name || rid)}}</h3>
                    <span class="sop-doc-tag">📖 ${{escapeHtml(sop.doc_ref || "")}}</span>
                  </div>
                  <button class="btn btn-secondary" style="padding:4px 10px;font-size:12px;" onclick="toggleSopDrawer('${{escapeHtml(rid)}}')">
                    📑 展开 / 收起完整 SOP 核查细则原文
                  </button>
                </div>
                <div class="sop-criteria-grid">
                  <div class="criteria-box criteria-pass">
                    <div class="criteria-box-title">✅ 官方 SOP 合规基准 (Pass Criteria)</div>
                    <div>${{escapeHtml(sop.pass_criteria || ruleFindings[0].sop_pass_criteria || "")}}</div>
                  </div>
                  <div class="criteria-box criteria-fail">
                    <div class="criteria-box-title">🚫 官方 SOP 违规判定红线 (Fail Criteria)</div>
                    <div>${{escapeHtml(sop.fail_criteria || ruleFindings[0].sop_fail_criteria || "")}}</div>
                  </div>
                </div>
                <div class="sop-full-drawer" id="sop-drawer-${{escapeHtml(rid)}}"><strong>【${{escapeHtml(sop.name || rid)}} · 完整核查指令与判定细则】</strong>\n\n${{escapeHtml(sop.check_instruction || "")}}</div>
              </div>
              <div class="cards-grid">
                ${{ruleFindings.map(f => renderViolationCard(f)).join("")}}
              </div>
            </div>
          `;
        }});
        html += `</section>`;
      }});
      return html;
    }}

    function renderVideoTimelineView(filtered) {{
      if (filtered.length === 0) {{
        return `<div style="background:#FFF;padding:40px;text-align:center;border-radius:12px;color:var(--text-muted);">当前筛选条件下没有匹配的稽核事件。</div>`;
      }}
      const videos = [...new Set(filtered.map(f => f.video_short))];
      let html = "";
      videos.forEach(vKey => {{
        const vFindings = filtered
          .filter(f => f.video_short === vKey)
          .sort((a, b) => a.sec_in_clip - b.sec_in_clip);
        if (vFindings.length === 0) return;
        const ledger = (PORTAL_DATA.token_ledger || []).find(l => l.video_short === vKey) || {{}};
        const vDisplay = vFindings[0].video_display;
        const maxDur = Math.max(300, ledger.duration_sec || 300, ...vFindings.map(f => f.sec_in_clip + 15));
        const pinsHtml = vFindings.map(f => {{
          const leftPct = Math.min(98, Math.max(2, (f.sec_in_clip / maxDur) * 100));
          return `<div class="timeline-pin ${{f.severity}}" style="left:${{leftPct}}%;" title="${{f.code}} [${{f.mmss}}] ${{f.rule_id}}: ${{escapeHtml(f.sub_clause_title)}}" onclick="document.getElementById('finding-card-${{f.id}}').scrollIntoView({{behavior:'smooth', block:'center'}})"></div>`;
        }}).join("");

        html += `
          <div class="sop-rule-block">
            <div class="sop-standard-header">
              <div class="sop-title-bar" style="margin-bottom:4px;">
                <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;">
                  <span class="sop-code-badge">📹 ${{escapeHtml(vDisplay)}}</span>
                  <span style="font-size:12.5px;color:var(--text-muted);">原片文件名：${{escapeHtml(vFindings[0].video_filename)}}</span>
                </div>
                <div style="font-size:12px;color:var(--text-secondary);font-variant-numeric:tabular-nums;">
                  分辨率：<strong>${{escapeHtml(ledger.resolution || '1920x1080')}}</strong> · 时长：<strong>${{ledger.duration_sec || 300}}s</strong> · 检出事件：<strong>${{vFindings.length}} 项</strong> · 单片成本：<strong>$${{(ledger.cost_usd || 0).toFixed(4)}} USD</strong>
                </div>
              </div>
            </div>
            <div class="timeline-track-box">
              <div style="display:flex;justify-content:space-between;font-size:12px;color:var(--text-muted);font-weight:600;">
                <span>00:00 (起点)</span>
                <span>👆 点击时间轴上的圆点可快速定位对应违规卡片（🔴 红线 · 🟡 一般偏差）</span>
                <span>${{Math.floor(maxDur/60)}}:${{String(Math.round(maxDur%60)).padStart(2,'0')}} (终点)</span>
              </div>
              <div class="timeline-bar">${{pinsHtml}}</div>
            </div>
            <div class="cards-grid">
              ${{vFindings.map(f => renderViolationCard(f)).join("")}}
            </div>
          </div>
        `;
      }});
      return html;
    }}

    function renderSopDictionaryAndLedgerView() {{
      const triggeredCounts = {{}};
      (PORTAL_DATA.findings || []).forEach(f => {{
        triggeredCounts[f.rule_id] = (triggeredCounts[f.rule_id] || 0) + 1;
      }});
      const rowsHtml = (PORTAL_DATA.sop_rules || []).map(r => {{
        const cnt = triggeredCounts[r.rule_id] || 0;
        const badge = cnt > 0
          ? `<span class="sev-pill ${{r.severity}}" style="cursor:pointer;" onclick="setFilter('severity','ALL');setFilter('module','ALL');document.getElementById('search-input').value='${{escapeHtml(r.rule_id)}}';setViewMode('sop');">🚨 本次检出 ${{cnt}} 项 (点击查看)</span>`
          : `<span style="font-size:12px;color:#059669;background:#ECFDF5;padding:2px 8px;border-radius:4px;font-weight:600;">✅ 本次未见违规 (0)</span>`;
        return `
          <tr>
            <td style="font-weight:800;white-space:nowrap;">${{escapeHtml(r.rule_id)}}</td>
            <td>
              <div style="font-weight:700;color:var(--text-primary);">${{escapeHtml(r.name)}}</div>
              <div style="font-size:11.5px;color:var(--blue-text);margin-top:2px;">${{escapeHtml(r.doc_ref)}}</div>
            </td>
            <td style="white-space:nowrap;">${{badge}}</td>
            <td style="color:#065F46;font-size:12.5px;">${{escapeHtml(r.pass_criteria)}}</td>
            <td style="color:#991B1B;font-size:12.5px;">${{escapeHtml(r.fail_criteria)}}</td>
          </tr>
        `;
      }}).join("");

      const ledger = PORTAL_DATA.token_ledger || [];
      const totalTokens = ledger.reduce((s, r) => s + (r.total_tokens || 0), 0);
      const totalCost = ledger.reduce((s, r) => s + (r.cost_usd || 0), 0).toFixed(4);
      const ledgerRowsHtml = ledger.map(l => `
        <tr>
          <td style="font-weight:700;">${{escapeHtml(l.video_filename)}}</td>
          <td>${{l.duration_sec}}s</td>
          <td>${{escapeHtml(l.resolution)}}</td>
          <td><code>${{escapeHtml(l.model_version)}}</code></td>
          <td>${{(l.prompt_tokens || 0).toLocaleString()}}</td>
          <td>${{(l.thoughts_tokens || 0).toLocaleString()}}</td>
          <td>${{(l.output_tokens || 0).toLocaleString()}}</td>
          <td style="font-weight:700;">${{(l.total_tokens || 0).toLocaleString()}}</td>
          <td style="font-weight:700;color:#B91C1C;">$${{(l.cost_usd || 0).toFixed(4)}}</td>
          <td>${{l.latency_sec}}s</td>
          <td><strong>${{l.flagged_events}} 项</strong></td>
        </tr>
      `).join("");

      return `
        <div class="sop-rule-block" style="margin-bottom:24px;">
          <div class="sop-standard-header">
            <h3 style="font-size:16px;font-weight:700;">📚 CHAGEE APAC 全量 SOP 稽核规则对照字典</h3>
            <p style="font-size:12.5px;color:var(--text-muted);margin-top:4px;">对应《APAC Cloud Audit Parameters 2026 v1.0》、《SOP 8 Manitowoc Ice Maker》与《SOP 21 Langtuo Ice Maker》</p>
          </div>
          <div style="overflow-x:auto;">
            <table class="data-table">
              <thead>
                <tr>
                  <th>条款编号</th>
                  <th>SOP 规则名称与手册出处</th>
                  <th>本店本次稽核结果</th>
                  <th>✅ 官方 SOP 合规标准 (Pass Criteria)</th>
                  <th>🚫 官方 SOP 违规判定红线 (Fail Criteria)</th>
                </tr>
              </thead>
              <tbody>${{rowsHtml}}</tbody>
            </table>
          </div>
        </div>

        <div class="sop-rule-block">
          <div class="sop-standard-header">
            <h3 style="font-size:16px;font-weight:700;">💳 本次视频 Token 消耗与算力账单明细 (数据源：Sheet Tab 2)</h3>
            <p style="font-size:12.5px;color:var(--text-muted);margin-top:4px;">共 ${{ledger.length}} 个视频分片合计消耗 <strong>${{totalTokens.toLocaleString()}} Tokens</strong>，总计按量成本 <strong>$${{totalCost}} USD</strong></p>
          </div>
          <div style="overflow-x:auto;">
            <table class="data-table" style="font-variant-numeric:tabular-nums;">
              <thead>
                <tr>
                  <th>监控原片 (Video_Filename)</th>
                  <th>时长</th>
                  <th>分辨率</th>
                  <th>调用模型</th>
                  <th>输入 Token</th>
                  <th>思考 Token</th>
                  <th>输出 Token</th>
                  <th>总 Token</th>
                  <th>成本 (USD)</th>
                  <th>处理耗时</th>
                  <th>检出事件</th>
                </tr>
              </thead>
              <tbody>${{ledgerRowsHtml}}</tbody>
            </table>
          </div>
        </div>
      `;
    }}

    function renderActiveView() {{
      const container = document.getElementById("view-content");
      if (currentView === "dict") {{
        container.innerHTML = renderSopDictionaryAndLedgerView();
        return;
      }}
      const filtered = filterFindings();
      if (currentView === "sop") {{
        container.innerHTML = renderSopGroupedView(filtered);
      }} else {{
        container.innerHTML = renderVideoTimelineView(filtered);
      }}
    }}

    function setDecision(id, status) {{
      const cur = getFindingState(id).status;
      const next = (cur === status) ? "PENDING" : status;
      saveFindingState(id, next, undefined, true);
      renderActiveView();
    }}

    function batchConfirmAllConfirmed() {{
      const updates = [];
      (PORTAL_DATA.findings || []).forEach(f => {{
        const cur = getFindingState(f.id);
        if (cur.status === "PENDING" && f.disposition === "CONFIRMED") {{
          reviewState[f.id] = {{ status: "CONFIRMED_VIOLATION", note: cur.note }};
          if (f.incident_id) {{
            updates.push({{ incident_id: f.incident_id, status: "CONFIRMED_VIOLATION", note: cur.note }});
          }}
        }}
      }});
      try {{ localStorage.setItem(STORAGE_KEY, JSON.stringify(reviewState)); }} catch (e) {{}}
      updateKpiProgress();
      renderActiveView();
      if (updates.length > 0) {{
        persistReviewsToSheet(updates);
      }} else {{
        showToast("✅ 所有 AI 实锤事件均已复核");
      }}
    }}

    function openVideoModal(findingId) {{
      const f = (PORTAL_DATA.findings || []).find(x => x.id === findingId);
      if (!f) return;
      const sop = sopMap[f.rule_id] || {{}};
      document.getElementById("modal-title").textContent =
        `${{f.code}} · ${{f.video_short}} (${{f.mmss}}) — 违反 SOP ${{f.rule_id}}：${{f.sub_clause_title}}`;
      document.getElementById("modal-drive-link").href = f.drive_url;
      document.getElementById("modal-video-iframe").src = f.preview_url;
      document.getElementById("modal-ts-label").textContent =
        `原片时间点：${{f.mmss}}${{f.osd_clock ? ' | 监控 OSD 时钟：' + f.osd_clock : ''}}`;

      document.getElementById("modal-sidebar-content").innerHTML = `
        <div>
          <span class="sev-pill ${{f.severity}}">${{f.severity === 'RED_LINE' ? '🔴 红线零容忍 · 48h 必改' : '🟡 一般操作偏差'}}</span>
          <span class="staff-pill" style="margin-left:6px;">👤 ${{escapeHtml(f.staff_tag)}}</span>
        </div>
        <div style="font-size:13.5px;line-height:1.6;background:#FFFFFF;padding:12px;border-radius:8px;border:1px solid var(--border-light);">
          <strong>👁️ AI 稽核证据描述：</strong><br/>
          ${{highlightDescription(f.description)}}
        </div>
        <div class="card-sop-box" style="background:#FFFFFF;">
          <div class="card-sop-clause-title">📌 违反 SOP 条款：${{escapeHtml(f.rule_id)}} · ${{escapeHtml(sop.name || '')}}</div>
          <div style="font-size:11.5px;color:var(--blue-text);">${{escapeHtml(f.doc_ref)}}</div>
          <div class="card-sop-row" style="margin-top:4px;"><strong>✅ SOP 合规要求：</strong>${{escapeHtml(f.specific_pass)}}</div>
          <div class="card-sop-row" style="color:#991B1B;"><strong>🚫 SOP 判定红线：</strong>${{escapeHtml(f.sop_fail_criteria)}}</div>
        </div>
        <div class="rectification-tip">
          <strong>💡 建议店长整改动作：</strong>${{escapeHtml(f.rectification_advice)}}
        </div>
        <div style="margin-top:auto;display:flex;gap:8px;flex-wrap:wrap;">
          <button class="btn btn-primary" style="flex:1;" onclick="setDecision(${{f.id}}, 'CONFIRMED_VIOLATION');closeVideoModal();">✅ 确认违规并关闭</button>
          <button class="btn btn-secondary" onclick="setDecision(${{f.id}}, 'DISMISSED');closeVideoModal();">❌ 排除误报</button>
        </div>
      `;
      document.getElementById("video-modal-backdrop").classList.add("open");
    }}

    function closeVideoModal() {{
      document.getElementById("modal-video-iframe").src = "";
      document.getElementById("video-modal-backdrop").classList.remove("open");
    }}

    function buildSingleNoticeText(f) {{
      const st = getFindingState(f.id);
      return [
        `【CHAGEE 门店 AI 云稽核整改项 ${{f.code}}】`,
        `• 门店名称：${{PORTAL_DATA.store_name}} (稽核单 #${{PORTAL_DATA.audit_id}})`,
        `• 严重等级：${{f.severity === 'RED_LINE' ? '🔴 红线零容忍违规（须 48 小时内闭环整改）' : '🟡 一般操作偏差'}}`,
        `• 监控时段：${{f.video_display}} @ ${{f.mmss}}${{f.osd_clock ? ' (OSD ' + f.osd_clock + ')' : ''}}`,
        `• 涉事人员：${{f.staff_tag}}`,
        `• 违规事实：${{f.description}}`,
        `• 违反 SOP 条款：【${{f.rule_id}}】${{f.sub_clause_title}} (${{f.doc_ref}})`,
        `• SOP 合规标准：${{f.specific_pass}}`,
        `• 整改要求：${{st.note ? st.note : f.rectification_advice}}`,
        `• 20秒证据视频：${{f.drive_url}}`
      ].join("\\n");
    }}

    function copySingleFindingNotice(id) {{
      const f = (PORTAL_DATA.findings || []).find(x => x.id === id);
      if (!f) return;
      navigator.clipboard.writeText(buildSingleNoticeText(f));
      showToast(`📋 已复制 ${{f.code}} 门店整改通知，可直接粘贴发送店长`);
    }}

    function openRectificationModal() {{
      const activeList = (PORTAL_DATA.findings || []).filter(f => getFindingState(f.id).status !== "DISMISSED");
      const redList = activeList.filter(f => f.severity === "RED_LINE");
      const normalList = activeList.filter(f => f.severity !== "RED_LINE");
      const lines = [
        `====================================================================`,
        `📋 CHAGEE 霸王茶姬 · 门店远程视频稽核整改通知单`,
        `====================================================================`,
        `门店名称：${{PORTAL_DATA.store_name}}`,
        `稽核单号：#${{PORTAL_DATA.audit_id}}`,
        `判定依据：${{PORTAL_DATA.active_prompt_version}}`,
        `汇总结果：共需整改 ${{activeList.length}} 项（其中 🔴 红线零容忍违规 ${{redList.length}} 项，🟡 一般操作偏差 ${{normalList.length}} 项）`,
        `整改时限：🔴 红线违规项须于收到通知后 48 小时内完成整改并回传复训证明`,
        ``,
        `--------------------------------------------------------------------`,
        `一、🔴 红线零容忍违规项（共 ${{redList.length}} 项 · 48 小时内强制闭环）`,
        `--------------------------------------------------------------------`
      ];
      redList.forEach((f, idx) => {{
        const st = getFindingState(f.id);
        lines.push(
          `${{idx + 1}}. [${{f.code}}] 【违反 SOP ${{f.rule_id}} · ${{f.sub_clause_title}}】`,
          `   - 发生时间：${{f.video_display}} 相对时间 ${{f.mmss}}${{f.osd_clock ? ' (监控时间 ' + f.osd_clock + ')' : ''}} | 涉事人员：${{f.staff_tag}}`,
          `   - 违规事实：${{f.description}}`,
          `   - SOP 原文要求：${{f.specific_pass}}`,
          `   - 整改指令：${{st.note ? st.note : f.rectification_advice}}`,
          `   - 20s 证据视频：${{f.drive_url}}`,
          ``
        );
      }});
      lines.push(
        `--------------------------------------------------------------------`,
        `二、🟡 一般操作偏差项（共 ${{normalList.length}} 项 · 晨会宣导纠偏）`,
        `--------------------------------------------------------------------`
      );
      normalList.forEach((f, idx) => {{
        const st = getFindingState(f.id);
        lines.push(
          `${{idx + 1}}. [${{f.code}}] 【违反 SOP ${{f.rule_id}} · ${{f.sub_clause_title}}】`,
          `   - 发生时间：${{f.video_display}} 相对时间 ${{f.mmss}} | 涉事人员：${{f.staff_tag}}`,
          `   - 违规事实：${{f.description}}`,
          `   - SOP 原文要求：${{f.specific_pass}}`,
          `   - 整改指令：${{st.note ? st.note : f.rectification_advice}}`,
          `   - 20s 证据视频：${{f.drive_url}}`,
          ``
        );
      }});
      document.getElementById("rect-report-text").textContent = lines.join("\\n");
      document.getElementById("rect-modal-backdrop").classList.add("open");
    }}

    function closeRectificationModal() {{
      document.getElementById("rect-modal-backdrop").classList.remove("open");
    }}

    function copyRectificationReport() {{
      const txt = document.getElementById("rect-report-text").textContent;
      navigator.clipboard.writeText(txt);
      showToast("📋 已复制整份《门店 48 小时整改通知单》全文");
    }}

    function exportReviewedCSV() {{
      const headers = [
        "事件编号", "复合主键(Incident_ID)", "稽核单号", "门店名称", "监控原片", "违规时间点", "SOP条款",
        "SOP细分违规项", "严重等级", "AI判定类型", "AI置信度", "AI稽核证据描述",
        "SOP合规标准原文", "20秒证据视频链接", "督导复核状态", "督导整改批注"
      ];
      const rows = [headers];
      (PORTAL_DATA.findings || []).forEach(f => {{
        const st = getFindingState(f.id);
        rows.push([
          f.code, f.incident_id, f.audit_id, PORTAL_DATA.store_name, f.video_filename, f.timestamp_raw,
          f.rule_id, f.sub_clause_title, f.severity, f.disposition, f.confidence,
          f.description, f.specific_pass, f.drive_url, st.status, st.note || f.rectification_advice
        ]);
      }});
      const csvContent = "\\uFEFF" + rows.map(r => r.map(c => `"${{String(c ?? '').replace(/"/g, '""')}}"`).join(",")).join("\\n");
      const blob = new Blob([csvContent], {{ type: "text/csv;charset=utf-8;" }});
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `CHAGEE_AI稽核督导复核表_${{PORTAL_DATA.store_name}}_${{PORTAL_DATA.audit_id}}.csv`;
      a.click();
      URL.revokeObjectURL(url);
      showToast("📥 已导出督导复核 CSV 文件");
    }}

    document.addEventListener("keydown", e => {{
      if (e.key === "Escape") {{
        closeVideoModal();
        closeRectificationModal();
      }}
    }});

    initHeaderAndFilters();
    updateKpiProgress();
    renderActiveView();
  </script>
</body>
</html>"""
