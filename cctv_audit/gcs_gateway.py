"""Zero-GWS GCS fallback storage (`gcs_gateway.py`, Round 69).

Supervisors without Google Workspace can send `gs://bucket/prefix` (or a Cloud Console Storage URL)
instead of a Google Drive folder link. `RoutingStorageGateway` dispatches every storage call by the
target ID: `gs://...` goes to `GcsStorageGateway`, everything else to the unchanged
`GoogleWorkspaceGateway`. The GCS gateway mirrors the Drive gateway's async interface:

* reads source videos from the prefix (direct children only, natural-sorted),
* writes 20s evidence MP4s under `<prefix>/📁 违规证据切片_Evidence/`,
* writes the dual-tab report as a styled `.xlsx` (stdlib `zipfile` + SpreadsheetML; no openpyxl).

Guardrail: no path inside the service's own staging bucket is ever accepted (videos, reports,
evidence or SOP workbooks). It holds job checkpoints / eval artefacts (`jobs/`, `eval/`, `smoke/`,
`agent_platform_eval/`) and has a 30-day auto-delete lifecycle rule.

SOP workbooks may be pinned to one Object Versioning generation (`gs://b/sop/master_sheet.xlsx#<gen>`).
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import posixpath
import re
import shutil
import subprocess
import threading
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Dict, List, Optional, Sequence, Tuple, Union
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape as _xml_escape

from .config import config
from .gcp import WorkspaceAccessError
from .gcs_uri import GCS_SCHEME, is_gcs_target, normalize_gcs_target, split_generation
from .video_ingestor import VideoMetadataItem, natural_video_sort_key

logger = logging.getLogger("cctv_audit.gcs_gateway")

# Internal prefixes of the staging bucket; the whole bucket is refused (30-day auto-delete).
INTERNAL_STATE_PREFIXES: Tuple[str, ...] = ("jobs", "eval", "smoke", "agent_platform_eval")
REPORT_SHEET_TITLE = "📊 AI_Audit_Report_and_Token_Ledger"
LEGACY_REPORT_SHEET_TITLES = ("📊 AI_Audit_Report_and_Token_Ledger", "📊 AI稽核报告与Token账单")
EVIDENCE_SUBFOLDER_NAME = "📁 Evidence_Clips"
LEGACY_EVIDENCE_SUBFOLDER_NAMES = ("📁 Evidence_Clips", "Evidence_Clips", "📁 违规证据切片_Evidence", "📁 违规证据切片")
EVIDENCE_DIR_MARKER = "Evidence_Clips"
WRITE_PROBE_OBJECT = ".cctv_audit_write_probe"
XLSX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
TAB1_TITLE = "Tab1_Violation_Review"
LEGACY_TAB1_TITLES = ("Tab1_Violation_Review", "违规事件 3 秒复核台")
TAB2_TITLE = "Tab2_Token_Ledger"
LEGACY_TAB2_TITLES = ("Tab2_Token_Ledger", "本次视频 Token 消耗与耗时账单")
REVIEW_STATUS_OPTIONS = ["PENDING", "✅ CONFIRMED", "❌ FALSE_POSITIVE", "⚠️ FOLLOWUP"]
_VIDEO_EXTS = (".mp4", ".mov", ".m4v", ".avi", ".mkv", ".ts")
_DOWNLOAD_CHUNK = 4 * 1024 * 1024
_STORAGE_API = "https://storage.googleapis.com/storage/v1"
_UPLOAD_API = "https://storage.googleapis.com/upload/storage/v1"
_CONSOLE = "https://console.cloud.google.com/storage/browser"
_META_W, _META_H, _META_DUR = "cctv_width", "cctv_height", "cctv_duration_sec"


# --------------------------------------------------------------------------------------------
# Identifier helpers (pure functions; safe to import anywhere above gcp.py in the import graph)
# --------------------------------------------------------------------------------------------


def parse_gcs_uri_with_generation(uri: str) -> Tuple[str, str, Optional[str]]:
    """(bucket, object_or_prefix, generation or None); `#<digits>` / `?generation=` pin one object version."""
    try:
        canonical = normalize_gcs_target(uri)
    except ValueError as exc:
        raise WorkspaceAccessError(str(exc)) from exc
    if canonical is None:
        raise WorkspaceAccessError(f"Not a valid GCS URI (expected `gs://bucket/prefix`): `{uri}`")
    base, generation = split_generation(canonical)
    bucket, _, prefix = base[len(GCS_SCHEME):].partition("/")
    return bucket, prefix.strip("/"), (generation or None)


def parse_gcs_uri(uri: str) -> Tuple[str, str]:
    """(bucket, object_or_prefix) for a GCS URI / Console URL (generation dropped); raises WorkspaceAccessError."""
    bucket, prefix, _ = parse_gcs_uri_with_generation(uri)
    return bucket, prefix


def _quote_path(path: str) -> str:
    return urllib.parse.quote(path, safe="/")


def build_gcs_console_object_url(uri: str) -> str:
    bucket, obj = parse_gcs_uri(uri)
    return f"{_CONSOLE}/_details/{bucket}/{_quote_path(obj)}"


def build_gcs_console_folder_url(uri: str) -> str:
    bucket, prefix = parse_gcs_uri(uri)
    return f"{_CONSOLE}/{bucket}/{_quote_path(prefix)}/" if prefix else f"{_CONSOLE}/{bucket}"


def build_source_video_url(file_id: str) -> str:
    """Clickable link to a source video: Console object page for `gs://`, Drive viewer otherwise."""
    if not file_id:
        return ""
    if is_gcs_target(file_id):
        return build_gcs_console_object_url(file_id)
    return f"https://drive.google.com/file/d/{file_id}/view"


def safe_storage_id_slug(file_id: str) -> str:
    """Drive IDs unchanged; `gs://` IDs -> `<stem[:40]>_<sha1[:10]>` (safe in object names / dirs)."""
    if not is_gcs_target(file_id):
        return file_id
    stem = PurePosixPath(file_id[len(GCS_SCHEME):].rstrip("/")).stem
    clean = re.sub(r"[^A-Za-z0-9_-]+", "_", stem).strip("_")[:40] or "gcs"
    return f"{clean}_{hashlib.sha1(file_id.encode('utf-8')).hexdigest()[:10]}"


def _staging_bucket_name() -> str:
    return (config.staging_bucket or "").strip().replace(GCS_SCHEME, "").strip("/").lower()


def _assert_not_internal_state_prefix(bucket: str, prefix: str) -> None:
    """Rejects ANY path in the service's staging bucket or internal `config/` prefix.

    The staging bucket holds job state (`jobs/`, `eval/`, `smoke/`, ...) and has a 30-day
    `age = 30 -> Delete` lifecycle rule, so customer files placed there would silently vanish.
    """
    norm_prefix = (prefix or "").strip("/")
    if norm_prefix == "config" or norm_prefix.startswith("config/"):
        shown = f"gs://{bucket}/{norm_prefix}"
        raise WorkspaceAccessError(
            f"`{shown}` is inside the audit service's internal configuration directory (`config/`) "
            "and cannot store store videos, audit reports, or SOP workbooks: please use a store prefix (e.g. `gs://<customer-bucket>/stores/store_name/`)"
        )
    staging = _staging_bucket_name()
    if not staging or bucket.lower() != staging:
        return
    shown = f"gs://{bucket}/{prefix}".rstrip("/")
    raise WorkspaceAccessError(
        f"`{shown}` is inside the audit service's internal staging bucket (used for job state and temporary slices, auto-deleted after 30 days) "
        "and cannot store store videos, audit reports, or SOP workbooks: please use your own customer bucket (e.g. "
        "`gs://<customer-bucket>/stores/store_name/`, `gs://<customer-bucket>/sop/master_sheet.xlsx`) and resend"
    )


def _object_parent(obj: str) -> str:
    return posixpath.dirname(obj.strip("/"))


# --------------------------------------------------------------------------------------------
# Report tab values (column parity with GoogleWorkspaceGateway.create_dual_tab_report_sheet)
# --------------------------------------------------------------------------------------------

# Must match the data column order written by gcs_gateway.build_tab1_values / ViolationSheetRow
# (A..K). Readers address columns by position (J = review status, K = review note), never by label.
TAB1_HEADERS = [
    "Audit ID",
    "Video Filename",
    "Violation Timestamp (MM:SS)",
    "SOP Rule ID",
    "AI Disposition",
    "Severity",
    "AI Confidence",
    "Violation Description & Timestamped Trajectory",
    "Evidence Clip Link (Click to Play)",
    "Supervisor Review Status (Dropdown)",
    "Supervisor Review Note",
]
TAB2_HEADERS = [
    "Store/Folder Name",
    "Video Filename",
    "Video Duration (sec)",
    "Media Processing Mode",
    "Input Tokens",
    "Thinking Tokens",
    "Output Tokens",
    "Total Tokens",
    "Estimated Cost (USD)",
    "Inference Time (sec)",
    "Violations Found",
    "Audit Completion Time (UTC)",
    "Audited Prompt Version",
    "Audited Model Version",
]
TAB1_WIDTHS = [22, 28, 16, 14, 16, 12, 10, 60, 48, 28, 36]
TAB2_WIDTHS = [22, 36, 28, 12, 12, 22, 30, 12, 12, 12, 12, 12, 14, 12, 30]
TAB1_STATUS_COL = 4  # Disposition
TAB1_LINK_COL = 8  # Evidence Clip Link

STATUS_RED, STATUS_YELLOW, STATUS_GREEN = "🔴 VIOLATION", "🟡 SUSPECTED", "🟢 COMPLIANT"
_DISPOSITION_STATUS = {
    "CONFIRMED": STATUS_RED,
    "SUSPECTED": STATUS_YELLOW,
    "UNVERIFIED": STATUS_YELLOW,
    "OUT_OF_SCOPE": STATUS_GREEN,
    "COMPLIANT": STATUS_GREEN,
}


def disposition_status_label(value: Any) -> str:
    """Traffic-light label for a Tab-1 status cell ('' when unknown)."""
    text = str(value or "").strip()
    for label in (STATUS_RED, STATUS_YELLOW, STATUS_GREEN):
        if text.startswith(label[:1]) or label in text:
            return label
    return _DISPOSITION_STATUS.get(text.upper(), "")


def build_incident_composite_key(
    audit_id: str,
    video_filename: str,
    timestamp_in_clip: str,
    rule_id: str,
    occ_idx: int = 0,
) -> str:
    """Deterministic composite key for a Tab-1 row (`{audit_id}::{video}::{ts}::{rule_id}::{occ_idx}`)."""
    return (
        f"{str(audit_id or '').strip()}::"
        f"{str(video_filename or '').strip()}::"
        f"{str(timestamp_in_clip or '').strip()}::"
        f"{str(rule_id or '').strip()}::"
        f"{int(occ_idx)}"
    )


def match_and_apply_tab1_reviews(
    raw_rows: Sequence[Sequence[Any]],
    updates: Sequence[Dict[str, Any]],
) -> Tuple[List[List[Any]], List[Tuple[int, str, str]]]:
    """Matches `updates` against `Tab 1` data rows (excluding header) by composite key.

    Returns `(updated_rows, patches)` where each patch is `(sheet_row_1based, new_status, new_note)`.
    """
    padded_rows: List[List[Any]] = []
    base_key_counts: Dict[str, int] = {}
    full_key_to_idx: Dict[str, int] = {}
    base_key_to_indices: Dict[str, List[int]] = {}

    for idx, r in enumerate(raw_rows):
        row = [str(c) for c in list(r)[:11]] + [""] * max(0, 11 - len(r))
        try:
            row[6] = float(row[6] or 0.0)
        except (TypeError, ValueError):
            row[6] = 0.0
        padded_rows.append(row)
        base_key = (
            f"{row[0].strip()}::{row[1].strip()}::{row[2].strip()}::{row[3].strip()}"
        )
        occ_idx = base_key_counts.get(base_key, 0)
        base_key_counts[base_key] = occ_idx + 1
        full_key = f"{base_key}::{occ_idx}"
        full_key_to_idx[full_key] = idx
        base_key_to_indices.setdefault(base_key, []).append(idx)

    patches_by_idx: Dict[int, Tuple[str, str]] = {}
    for u in updates:
        inc_id = str(u.get("incident_id") or "").strip()
        if not inc_id and u.get("audit_id") and u.get("rule_id"):
            inc_id = build_incident_composite_key(
                str(u.get("audit_id") or ""),
                str(u.get("video_filename") or ""),
                str(u.get("timestamp_in_clip") or ""),
                str(u.get("rule_id") or ""),
                int(u.get("occ_idx") or 0),
            )
        if not inc_id:
            continue
        target_idx = full_key_to_idx.get(inc_id)
        if target_idx is None:
            parts = inc_id.split("::")
            if len(parts) == 4:
                candidates = base_key_to_indices.get(inc_id) or []
                if candidates:
                    target_idx = candidates[0]
        if target_idx is None:
            continue
        row = padded_rows[target_idx]
        if "human_review_status" in u and u["human_review_status"] is not None:
            row[9] = str(u["human_review_status"])
        if "human_review_note" in u and u["human_review_note"] is not None:
            row[10] = str(u["human_review_note"])
        patches_by_idx[target_idx] = (str(row[9]), str(row[10]))

    patches = [
        (idx + 2, status_val, note_val)
        for idx, (status_val, note_val) in sorted(patches_by_idx.items())
    ]
    return padded_rows, patches


def build_tab1_values(tab1_rows: Sequence[Any]) -> List[List[Any]]:
    values: List[List[Any]] = [list(TAB1_HEADERS)]
    for r in tab1_rows:
        values.append(
            [
                str(getattr(r, "audit_id", "")),
                str(getattr(r, "video_filename", "")),
                str(getattr(r, "timestamp_in_clip", "")),
                str(getattr(r, "rule_id", "")),
                str(getattr(r, "disposition", "")),
                str(getattr(r, "severity", "")),
                float(getattr(r, "confidence", 0.0)),
                str(getattr(r, "evidence_description", "")),
                str(getattr(r, "evidence_drive_url", "")),
                str(getattr(r, "human_review_status", "") or "PENDING"),
                str(getattr(r, "human_review_note", "") or ""),
            ]
        )
    return values


def build_tab2_values(tab2_rows: Sequence[Any]) -> List[List[Any]]:
    values: List[List[Any]] = [list(TAB2_HEADERS)]
    for r in tab2_rows:
        values.append(
            [
                str(getattr(r, "audit_id", "")),
                str(getattr(r, "auditor_folder_id", "")),
                str(getattr(r, "video_filename", "")),
                float(getattr(r, "video_duration_sec", 0.0)),
                str(getattr(r, "resolution", "")),
                str(getattr(r, "model_version_used", "")),
                str(getattr(r, "prompt_version_used", "")),
                int(getattr(r, "prompt_token_count", 0)),
                int(getattr(r, "thoughts_token_count", 0)),
                int(getattr(r, "candidates_token_count", 0)),
                int(getattr(r, "total_token_count", 0)),
                float(getattr(r, "estimated_cost_usd", 0.0)),
                int(getattr(r, "e2e_latency_ms", 0)),
                int(getattr(r, "flagged_events_count", 0)),
                str(getattr(r, "fallback_warning", "") or ""),
            ]
        )
    return values


# --------------------------------------------------------------------------------------------
# Minimal styled XLSX writer / reader (stdlib only)
# --------------------------------------------------------------------------------------------

HEADER_FILL = "1C2D42"
STATUS_FILLS = {STATUS_RED: "FCE8E6", STATUS_YELLOW: "FEF7E0", STATUS_GREEN: "E6F4EA"}
_XF_DEFAULT, _XF_HEADER, _XF_LINK, _XF_RED, _XF_YELLOW, _XF_GREEN = range(6)
_STATUS_XF = {STATUS_RED: _XF_RED, STATUS_YELLOW: _XF_YELLOW, STATUS_GREEN: _XF_GREEN}
_ILLEGAL_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f]")
_NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_NS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_NS_PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_REL_HYPERLINK = f"{_NS_REL}/hyperlink"


def _col_letter(idx: int) -> str:
    letters = ""
    n = idx + 1
    while n:
        n, rem = divmod(n - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def _x(text: Any) -> str:
    return _xml_escape(_ILLEGAL_XML.sub("", str(text)), {'"': "&quot;"})


def _styles_xml() -> str:
    fills = "".join(
        f'<fill><patternFill patternType="solid"><fgColor rgb="FF{c}"/><bgColor indexed="64"/>'
        "</patternFill></fill>"
        for c in (HEADER_FILL, STATUS_FILLS[STATUS_RED], STATUS_FILLS[STATUS_YELLOW], STATUS_FILLS[STATUS_GREEN])
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<styleSheet xmlns="{_NS_MAIN}">'
        '<fonts count="3">'
        '<font><sz val="11"/><name val="Calibri"/></font>'
        '<font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="Calibri"/></font>'
        '<font><u/><sz val="11"/><color rgb="FF1155CC"/><name val="Calibri"/></font>'
        "</fonts>"
        '<fills count="6"><fill><patternFill patternType="none"/></fill>'
        f'<fill><patternFill patternType="gray125"/></fill>{fills}</fills>'
        '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        '<cellXfs count="6">'
        '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
        '<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1" '
        'applyAlignment="1"><alignment vertical="center" wrapText="1"/></xf>'
        '<xf numFmtId="0" fontId="2" fillId="0" borderId="0" xfId="0" applyFont="1"/>'
        '<xf numFmtId="0" fontId="0" fillId="3" borderId="0" xfId="0" applyFill="1"/>'
        '<xf numFmtId="0" fontId="0" fillId="4" borderId="0" xfId="0" applyFill="1"/>'
        '<xf numFmtId="0" fontId="0" fillId="5" borderId="0" xfId="0" applyFill="1"/>'
        "</cellXfs>"
        '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
        "</styleSheet>"
    )


def _sheet_xml(
    rows: List[List[Any]],
    widths: Sequence[int],
    *,
    selected: bool,
    status_col: Optional[int],
    link_cols: Sequence[int],
) -> Tuple[str, List[str]]:
    """(worksheet XML, external hyperlink targets in rId order)."""
    links: List[Tuple[str, str]] = []
    out_rows: List[str] = []
    for r_idx, row in enumerate(rows):
        cells: List[str] = []
        for c_idx, val in enumerate(row):
            ref = f"{_col_letter(c_idx)}{r_idx + 1}"
            style = _XF_DEFAULT
            if r_idx == 0:
                style = _XF_HEADER
            elif c_idx in link_cols and str(val).lower().startswith("https://"):
                style = _XF_LINK
                links.append((ref, str(val)))
            elif status_col is not None and c_idx == status_col:
                style = _STATUS_XF.get(disposition_status_label(val), _XF_DEFAULT)
            s_attr = f' s="{style}"' if style else ""
            if isinstance(val, bool) or not isinstance(val, (int, float)):
                cells.append(
                    f'<c r="{ref}" t="inlineStr"{s_attr}><is><t xml:space="preserve">{_x(val)}</t></is></c>'
                )
            else:
                cells.append(f'<c r="{ref}"{s_attr}><v>{val!r}</v></c>')
        out_rows.append(f'<row r="{r_idx + 1}">{"".join(cells)}</row>')
    ncols = max((len(r) for r in rows), default=1)
    cols = "".join(
        f'<col min="{i + 1}" max="{i + 1}" width="{w}" customWidth="1"/>' for i, w in enumerate(widths)
    )
    hyperlinks = (
        "<hyperlinks>"
        + "".join(f'<hyperlink ref="{ref}" r:id="rId{i + 1}"/>' for i, (ref, _) in enumerate(links))
        + "</hyperlinks>"
        if links
        else ""
    )
    tab_attr = ' tabSelected="1"' if selected else ""
    cols_xml = f"<cols>{cols}</cols>" if cols else ""
    xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<worksheet xmlns="{_NS_MAIN}" xmlns:r="{_NS_REL}">'
        f'<dimension ref="A1:{_col_letter(ncols - 1)}{max(1, len(rows))}"/>'
        f'<sheetViews><sheetView{tab_attr} workbookViewId="0">'
        '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
        '<selection pane="bottomLeft" activeCell="A2" sqref="A2"/></sheetView></sheetViews>'
        '<sheetFormatPr defaultRowHeight="15"/>'
        f"{cols_xml}"
        f'<sheetData>{"".join(out_rows)}</sheetData>{hyperlinks}'
        "</worksheet>"
    )
    return xml, [target for _, target in links]


def build_xlsx_bytes(sheets: Sequence[Dict[str, Any]]) -> bytes:
    """Workbook bytes from `[{name, rows, widths, status_col, link_cols}]` (row 0 = header)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        overrides = "".join(
            f'<Override PartName="/xl/worksheets/sheet{i + 1}.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            for i in range(len(sheets))
        )
        zf.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/styles.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
            f"{overrides}</Types>",
        )
        zf.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<Relationships xmlns="{_NS_PKG_REL}">'
            f'<Relationship Id="rId1" Type="{_NS_REL}/officeDocument" Target="xl/workbook.xml"/>'
            "</Relationships>",
        )
        sheet_entries = "".join(
            f'<sheet name="{_x(s["name"])}" sheetId="{i + 1}" r:id="rId{i + 1}"/>'
            for i, s in enumerate(sheets)
        )
        zf.writestr(
            "xl/workbook.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<workbook xmlns="{_NS_MAIN}" xmlns:r="{_NS_REL}">'
            f"<sheets>{sheet_entries}</sheets></workbook>",
        )
        wb_rels = "".join(
            f'<Relationship Id="rId{i + 1}" Type="{_NS_REL}/worksheet" Target="worksheets/sheet{i + 1}.xml"/>'
            for i in range(len(sheets))
        )
        wb_rels += (
            f'<Relationship Id="rId{len(sheets) + 1}" Type="{_NS_REL}/styles" Target="styles.xml"/>'
        )
        zf.writestr(
            "xl/_rels/workbook.xml.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<Relationships xmlns="{_NS_PKG_REL}">{wb_rels}</Relationships>',
        )
        zf.writestr("xl/styles.xml", _styles_xml())
        for i, s in enumerate(sheets):
            xml, links = _sheet_xml(
                s["rows"],
                s.get("widths") or [],
                selected=(i == 0),
                status_col=s.get("status_col"),
                link_cols=s.get("link_cols") or (),
            )
            zf.writestr(f"xl/worksheets/sheet{i + 1}.xml", xml)
            if links:
                rels = "".join(
                    f'<Relationship Id="rId{j + 1}" Type="{_REL_HYPERLINK}" Target="{_x(t)}" '
                    'TargetMode="External"/>'
                    for j, t in enumerate(links)
                )
                zf.writestr(
                    f"xl/worksheets/_rels/sheet{i + 1}.xml.rels",
                    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                    f'<Relationships xmlns="{_NS_PKG_REL}">{rels}</Relationships>',
                )
    return buf.getvalue()


def build_dual_tab_xlsx_bytes(tab1_rows: Sequence[Any], tab2_rows: Sequence[Any]) -> bytes:
    return build_xlsx_bytes(
        [
            {
                "name": TAB1_TITLE,
                "rows": build_tab1_values(tab1_rows),
                "widths": TAB1_WIDTHS,
                "status_col": TAB1_STATUS_COL,
                "link_cols": (TAB1_LINK_COL,),
            },
            {"name": TAB2_TITLE, "rows": build_tab2_values(tab2_rows), "widths": TAB2_WIDTHS},
        ]
    )


_CELL_REF_RE = re.compile(r"^([A-Za-z]+)(\d*)$")
# Hard limits for customer-supplied SOP workbooks (zip-bomb / oversized-object guard).
SOP_MAX_BYTES = 20 * 1024 * 1024
_XLSX_MAX_MEMBER_BYTES = 20 * 1024 * 1024
_XLSX_MAX_TOTAL_UNCOMPRESSED = 40 * 1024 * 1024


def _safe_xml(xml_bytes: bytes) -> ET.Element:
    """ET.fromstring after refusing DTDs, so entity-expansion (Billion Laughs) input never parses."""
    if b"<!doctype" in xml_bytes[:4096].lower() or b"<!entity" in xml_bytes.lower():
        raise ValueError("Refusing XML with DOCTYPE/ENTITY declarations")
    return ET.fromstring(xml_bytes)


def _col_index(letters: str) -> int:
    """`A` -> 0, `D` -> 3, `AA` -> 26."""
    idx = 0
    for ch in letters.upper():
        idx = idx * 26 + (ord(ch) - 64)
    return idx - 1


def _rich_text(node: ET.Element) -> str:
    """Text of an `<si>` / `<is>` node: direct `<t>` plus rich-text runs `<r><t>` (phonetic `<rPh>` skipped)."""
    parts: List[str] = []
    for child in node:
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "t":
            parts.append(child.text or "")
        elif tag == "r":
            parts.extend(t.text or "" for t in child.iter(f"{{{_NS_MAIN}}}t"))
    return "".join(parts)


def _zip_part(target: str) -> str:
    """Workbook-rels `Target` -> zip member: `worksheets/s.xml`, `xl/worksheets/s.xml`, `/xl/worksheets/s.xml`."""
    t = target.replace("\\", "/")
    if t.startswith("/"):
        return t.lstrip("/")
    if t.startswith("xl/"):
        return t
    return posixpath.normpath(posixpath.join("xl", t))


def read_xlsx_sheets(data: bytes) -> Dict[str, List[List[str]]]:
    """{sheet name: rows of cell text} for `.xlsx` written by this module, Excel, WPS or LibreOffice.

    Handles inline strings, shared strings (`xl/sharedStrings.xml`, `t="s"`), booleans (`t="b"` ->
    `TRUE`/`FALSE`), and sparse rows/cells: Excel omits empty cells and rows, so cells are placed by
    their `r="D2"` reference and missing cells / rows are padded with `""` / `[]`.
    """
    if len(data) > SOP_MAX_BYTES:
        raise ValueError(f"xlsx is {len(data)} bytes, above the {SOP_MAX_BYTES // (1024 * 1024)} MiB limit")
    ns = {"m": _NS_MAIN, "r": _NS_REL, "p": _NS_PKG_REL}
    out: Dict[str, List[List[str]]] = {}
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        infos = zf.infolist()
        if any(i.file_size > _XLSX_MAX_MEMBER_BYTES for i in infos) or (
            sum(i.file_size for i in infos) > _XLSX_MAX_TOTAL_UNCOMPRESSED
        ):
            raise ValueError("xlsx uncompressed size exceeds the 20 MiB per-part / 40 MiB total limit")
        names = set(zf.namelist())

        def _xml(name: str) -> ET.Element:
            return _safe_xml(zf.read(name))

        shared: List[str] = []
        if "xl/sharedStrings.xml" in names:
            sst = _xml("xl/sharedStrings.xml")
            shared = [_rich_text(si) for si in sst.findall("m:si", ns)]
        wb = _xml("xl/workbook.xml")
        rels = _xml("xl/_rels/workbook.xml.rels")
        targets = {r.get("Id"): str(r.get("Target") or "") for r in rels.findall("p:Relationship", ns)}
        for sheet in wb.findall("m:sheets/m:sheet", ns):
            rid = sheet.get(f"{{{_NS_REL}}}id")
            ws = _xml(_zip_part(targets[rid]))
            rows: List[List[str]] = []
            for row in ws.findall("m:sheetData/m:row", ns):
                r_attr = row.get("r")
                if r_attr and r_attr.isdigit():
                    while len(rows) < int(r_attr) - 1:
                        rows.append([])
                vals: List[str] = []
                for c in row.findall("m:c", ns):
                    m = _CELL_REF_RE.match(c.get("r") or "")
                    if m:
                        col = _col_index(m.group(1))
                        if col > len(vals):
                            vals.extend([""] * (col - len(vals)))
                    t = c.get("t")
                    v = c.find("m:v", ns)
                    raw = v.text if v is not None and v.text is not None else ""
                    if t == "inlineStr":
                        is_node = c.find("m:is", ns)
                        text = _rich_text(is_node) if is_node is not None else ""
                    elif t == "s":
                        text = shared[int(raw)] if raw.strip().isdigit() and int(raw) < len(shared) else ""
                    elif t == "b":
                        text = "TRUE" if raw.strip().lower() in ("1", "true") else "FALSE"
                    else:
                        text = raw
                    vals.append(text)
                rows.append(vals)
            out[str(sheet.get("name"))] = rows
    return out


SOP_TAB0_TITLE = "Tab0_Version_Control"
LEGACY_SOP_TAB0_TITLES = ("Tab0_Version_Control", "Tab0_Control_and_Rollback", "Tab0_版本总控与回滚开关")


def load_gcs_sop_tabs(raw: bytes, source_uri: str) -> Dict[str, List[List[str]]]:
    """{tab title: rows} from a Master SOP workbook: `.json` snapshot (`{"tabs": [{title, values}]}`) or `.xlsx`.

    Raises ValueError (with the source URI) when the bytes are not a readable workbook.
    """
    if len(raw) > SOP_MAX_BYTES:
        raise ValueError(f"File size {len(raw)} bytes exceeds the {SOP_MAX_BYTES // (1024 * 1024)} MiB limit")
    try:
        if split_generation(source_uri)[0].lower().endswith(".json"):
            doc = json.loads(raw.decode("utf-8-sig"))
            tabs = doc.get("tabs") if isinstance(doc, dict) else None
            if not isinstance(tabs, list):
                raise ValueError("Missing `tabs` list")
            return {
                str(t.get("title")): [[str(c) for c in row] for row in (t.get("values") or [])]
                for t in tabs
                if isinstance(t, dict) and t.get("title")
            }
        return read_xlsx_sheets(raw)
    except (ValueError, KeyError, zipfile.BadZipFile, ET.ParseError, UnicodeDecodeError) as exc:
        raise ValueError(f"Failed to parse SOP configuration file `{source_uri}`: {exc}") from exc


# --------------------------------------------------------------------------------------------
# GCS gateway
# --------------------------------------------------------------------------------------------

Body = Union[None, bytes, BinaryIO]


class GcsStorageGateway:
    """GCS implementation of the Drive/Sheets gateway interface (ADC identity, JSON API over urllib)."""

    def __init__(self) -> None:
        self._creds: Any = None
        self._creds_lock = threading.Lock()

    # ---- transport (overridable in tests) ---------------------------------------------------

    def _get_access_token(self) -> str:
        import google.auth
        import google.auth.transport.requests

        with self._creds_lock:
            if self._creds is None:
                self._creds, _ = google.auth.default(
                    scopes=["https://www.googleapis.com/auth/devstorage.read_write"]
                )
            if not getattr(self._creds, "valid", False):
                self._creds.refresh(google.auth.transport.requests.Request())
            return str(self._creds.token)

    def _principal(self) -> str:
        return str(getattr(self._creds, "service_account_email", "") or "Worker service account")

    def _request(
        self,
        method: str,
        url: str,
        *,
        body: Body = None,
        content_type: str = "",
        content_length: Optional[int] = None,
        timeout: float = 60.0,
    ) -> Tuple[int, bytes]:
        """(HTTP status, response body). HTTP errors are returned, not raised; I/O errors raise."""
        req = urllib.request.Request(url, data=body, method=method)
        req.add_header("Authorization", f"Bearer {self._get_access_token()}")
        if content_type:
            req.add_header("Content-Type", content_type)
        if content_length is not None:
            req.add_header("Content-Length", str(content_length))
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return int(resp.status), resp.read()
        except urllib.error.HTTPError as exc:
            return int(exc.code), exc.read() or b""

    def _download(self, url: str, dest_path: Path) -> int:
        """Streams `url` into `dest_path` in 4 MiB chunks; returns the HTTP status."""
        req = urllib.request.Request(url, method="GET")
        req.add_header("Authorization", f"Bearer {self._get_access_token()}")
        try:
            with urllib.request.urlopen(req, timeout=600.0) as resp, open(dest_path, "wb") as fh:
                while True:
                    chunk = resp.read(_DOWNLOAD_CHUNK)
                    if not chunk:
                        break
                    fh.write(chunk)
                return int(resp.status)
        except urllib.error.HTTPError as exc:
            return int(exc.code)

    def _ffprobe_gcs_stream(self, bucket: str, obj: str) -> Tuple[int, int, float]:
        """(width, height, duration) by ffprobe-ing the authenticated media URL; zeros on failure."""
        ffprobe = shutil.which("ffprobe")
        if not ffprobe:
            return 0, 0, 0.0
        try:
            proc = subprocess.run(
                [
                    ffprobe,
                    "-v",
                    "error",
                    "-headers",
                    f"Authorization: Bearer {self._get_access_token()}\r\n",
                    "-print_format",
                    "json",
                    "-show_format",
                    "-show_streams",
                    self._media_url(bucket, obj),
                ],
                capture_output=True,
                text=True,
                timeout=15.0,
                check=False,
            )
            if proc.returncode != 0:
                return 0, 0, 0.0
            info = json.loads(proc.stdout or "{}")
            vstreams = [s for s in info.get("streams", []) if s.get("codec_type") == "video"]
            if not vstreams:
                return 0, 0, 0.0
            vs = vstreams[0]
            dur = float(info.get("format", {}).get("duration") or vs.get("duration") or 0.0)
            return int(vs.get("width") or 0), int(vs.get("height") or 0), dur
        except Exception as exc:
            logger.debug("ffprobe on gs://%s/%s failed: %s", bucket, obj, exc)
            return 0, 0, 0.0

    # ---- URL builders / error mapping -------------------------------------------------------

    @staticmethod
    def _object_url(bucket: str, obj: str) -> str:
        return f"{_STORAGE_API}/b/{urllib.parse.quote(bucket, safe='')}/o/{urllib.parse.quote(obj, safe='')}"

    def _media_url(self, bucket: str, obj: str) -> str:
        return self._object_url(bucket, obj) + "?alt=media"

    @staticmethod
    def _upload_url(bucket: str, obj: str) -> str:
        return (
            f"{_UPLOAD_API}/b/{urllib.parse.quote(bucket, safe='')}/o"
            f"?uploadType=media&name={urllib.parse.quote(obj, safe='')}"
        )

    def _check(self, status: int, body: bytes, bucket: str, path: str, action: str) -> None:
        if status < 400:
            return
        shown = f"gs://{bucket}/{path}".rstrip("/")
        who = self._principal()
        if status in (401, 403):
            raise WorkspaceAccessError(
                f"Runtime identity `{who}` lacks permission to {action} `{shown}` (HTTP {status}): please grant it "
                f"`roles/storage.objectAdmin` on bucket `gs://{bucket}` (`roles/storage.objectViewer` is required to read videos; "
                "`roles/storage.objectAdmin` is required to write Excel reports and evidence clips)"
            )
        if status == 404:
            raise WorkspaceAccessError(
                f"`{shown}` not found (HTTP 404): please verify the bucket name and prefix path, and ensure `{who}` has access"
            )
        detail = body[:300].decode("utf-8", "replace")
        raise RuntimeError(f"GCS {action} on `{shown}` failed (HTTP {status}): {detail}")

    # ---- gateway interface ------------------------------------------------------------------

    async def probe_write_access(self, folder_id: str) -> str:
        """Proves the identity can list, create and delete objects under the prefix; returns `gs://…`."""
        bucket, prefix = parse_gcs_uri(folder_id)
        _assert_not_internal_state_prefix(bucket, prefix)
        display = f"gs://{bucket}/{prefix}".rstrip("/")

        def _probe() -> str:
            list_url = (
                f"{_STORAGE_API}/b/{urllib.parse.quote(bucket, safe='')}/o?maxResults=1"
                f"&prefix={urllib.parse.quote(prefix + '/' if prefix else '', safe='')}"
            )
            status, body = self._request("GET", list_url, timeout=15.0)
            self._check(status, body, bucket, prefix, "read")
            probe_obj = f"{prefix}/{WRITE_PROBE_OBJECT}" if prefix else WRITE_PROBE_OBJECT
            status, body = self._request(
                "POST", self._upload_url(bucket, probe_obj), body=b"ok", content_type="text/plain", timeout=15.0
            )
            self._check(status, body, bucket, prefix, "write")
            status, _ = self._request("DELETE", self._object_url(bucket, probe_obj), timeout=15.0)
            if status >= 400 and status != 404:
                logger.warning("GCS write probe gs://%s/%s left behind (HTTP %s)", bucket, probe_obj, status)
            return display

        return await asyncio.to_thread(_probe)

    def _object_path_gen(self, gcs_uri: str) -> Tuple[str, str, Optional[str]]:
        bucket, obj, generation = parse_gcs_uri_with_generation(gcs_uri)
        _assert_not_internal_state_prefix(bucket, obj)
        if not obj:
            raise WorkspaceAccessError(
                "GCS SOP configuration path must point to a specific .xlsx or .json file object (e.g. gs://bucket/sop/master_sheet.xlsx)"
            )
        return bucket, obj, generation

    def _object_path(self, gcs_uri: str) -> Tuple[str, str]:
        bucket, obj, _ = self._object_path_gen(gcs_uri)
        return bucket, obj

    def download_object_bytes(self, gcs_uri: str, max_bytes: int = SOP_MAX_BYTES) -> bytes:
        """Bytes of one GCS object, at most `max_bytes` (blocking; wrap in `asyncio.to_thread`).

        The object's `size` is checked from metadata before downloading, and the body length again
        after, so an oversized (or swapped-in) object is refused instead of filling memory.
        """
        bucket, obj, generation = self._object_path_gen(gcs_uri)
        gen_q = f"&generation={generation}" if generation else ""
        shown = f"gs://{bucket}/{obj}" + (f"#{generation}" if generation else "")
        too_big = WorkspaceAccessError(
            f"SOP configuration file `{shown}` exceeds the {max_bytes // (1024 * 1024)} MiB limit: please keep only Tab0 and rule tabs and re-upload"
        )
        status, body = self._request(
            "GET", self._object_url(bucket, obj) + "?fields=size,generation" + gen_q, timeout=15.0
        )
        if status < 400:
            try:
                size = int(json.loads(body.decode("utf-8") or "{}").get("size") or 0)
            except (ValueError, AttributeError):
                size = 0
            if size > max_bytes:
                raise too_big
            status, body = self._request("GET", self._media_url(bucket, obj) + gen_q, timeout=30.0)
        if status in (401, 403, 404):
            who = self._principal()
            if status == 404 and generation:
                raise WorkspaceAccessError(
                    f"Historical version `{shown}` of SOP configuration file not found (HTTP 404): that generation does not exist or was deleted. "
                    "Please check 'Version history' in Cloud Console or remove `#<generation>` to use the live version"
                )
            if status == 404:
                raise WorkspaceAccessError(
                    f"SOP configuration file `{shown}` not found (HTTP 404): please run "
                    f"`python3 scripts/init_sop_sheet.py --gcs-uri {shown} --tfvars <env>.tfvars` to upload it first, "
                    f"or verify the path and ensure `{who}` has `roles/storage.objectViewer`"
                )
            raise WorkspaceAccessError(
                f"Runtime identity `{who}` lacks permission to read SOP configuration file `{shown}` (HTTP {status}): please grant it "
                f"`roles/storage.objectViewer` (or `roles/storage.objectAdmin`) on bucket `gs://{bucket}`"
            )
        self._check(status, body, bucket, obj, "read")
        if len(body) > max_bytes:
            raise too_big
        return body

    def upload_object_bytes(self, gcs_uri: str, data: bytes, content_type: str) -> str:
        """Uploads `data` to one GCS object (blocking); returns its Cloud Console URL."""
        return self.upload_object(gcs_uri, data, content_type)[0]

    def upload_object(self, gcs_uri: str, data: bytes, content_type: str) -> Tuple[str, str]:
        """Uploads `data` (blocking); returns (Console URL, new object generation or '').

        With Object Versioning enabled the previous content stays archived under its old generation.
        """
        bucket, obj = self._object_path(gcs_uri)
        status, body = self._request(
            "POST",
            self._upload_url(bucket, obj),
            body=data,
            content_type=content_type,
            content_length=len(data),
            timeout=60.0,
        )
        self._check(status, body, bucket, obj, "write")
        try:
            generation = str(json.loads(body.decode("utf-8") or "{}").get("generation") or "")
        except (ValueError, AttributeError):
            generation = ""
        if generation:
            logger.info("Uploaded gs://%s/%s generation %s", bucket, obj, generation)
        return build_gcs_console_object_url(f"gs://{bucket}/{obj}"), generation

    def enable_bucket_versioning(self, bucket: str) -> int:
        """Best-effort `versioning.enabled = true` on `bucket`; returns the HTTP status (403 = no buckets.update)."""
        status, _ = self._request(
            "PATCH",
            f"{_STORAGE_API}/b/{urllib.parse.quote(bucket, safe='')}?fields=versioning",
            body=json.dumps({"versioning": {"enabled": True}}).encode(),
            content_type="application/json",
            timeout=15.0,
        )
        return status

    async def check_sheet_readable(self, sheet_id: str) -> str:
        """'' when no SOP workbook is configured or the GCS `.xlsx` / `.json` has a readable Tab 0.

        Raises WorkspaceAccessError (an operator-fixable setup problem, same contract as the Drive
        gateway) when the object is missing, not readable by the identity, not a workbook, or has no
        `Tab0_Control_and_Rollback` (or legacy `Tab0_版本总控与回滚开关`) -- otherwise every audit would silently fall back to the built-in rules.
        """
        if not (sheet_id or "").strip():
            return ""

        def _check() -> str:
            raw = self.download_object_bytes(sheet_id)
            try:
                tabs = load_gcs_sop_tabs(raw, sheet_id)
            except ValueError as exc:
                raise WorkspaceAccessError(
                    f"{exc}: please save as `.xlsx` in Excel / WPS (or use `.json` exported by `init_sop_sheet.py`) and re-upload"
                ) from exc
            if not any(t in tabs for t in LEGACY_SOP_TAB0_TITLES):
                raise WorkspaceAccessError(
                    f"SOP configuration file `{sheet_id}` is missing control tab '{SOP_TAB0_TITLE}' (found tabs: {', '.join(tabs) or 'none'}): "
                    "do not rename that tab; you can re-run `scripts/init_sop_sheet.py --gcs-uri ...` to generate the standard template"
                )
            return ""

        return await asyncio.to_thread(_check)

    def _object_dims(self, bucket: str, item: Dict[str, Any]) -> Tuple[int, int, float]:
        meta = item.get("metadata") or {}
        try:
            w, h, d = int(meta.get(_META_W) or 0), int(meta.get(_META_H) or 0), float(meta.get(_META_DUR) or 0)
        except (TypeError, ValueError):
            w, h, d = 0, 0, 0.0
        if w > 0 and h > 0 and d > 0:
            return w, h, d
        w, h, d = self._ffprobe_gcs_stream(bucket, item["name"])
        if w > 0 and h > 0 and d > 0:
            # Best-effort cache so the next preflight skips ffprobe (needs objectAdmin; viewer-only is fine).
            patch = json.dumps({"metadata": {_META_W: str(w), _META_H: str(h), _META_DUR: f"{d:.3f}"}}).encode()
            try:
                status, _ = self._request(
                    "PATCH",
                    self._object_url(bucket, item["name"]),
                    body=patch,
                    content_type="application/json",
                    timeout=10.0,
                )
                if status >= 400:
                    logger.debug("metadata cache PATCH on gs://%s/%s -> HTTP %s", bucket, item["name"], status)
            except Exception as exc:
                logger.debug("metadata cache PATCH failed: %s", exc)
        return w, h, d

    @staticmethod
    def _is_video_object(item: Dict[str, Any]) -> bool:
        name = str(item.get("name") or "")
        base = posixpath.basename(name)
        if not base or name.endswith("/") or base == WRITE_PROBE_OBJECT:
            return False
        if (
            any(ev in name for ev in LEGACY_EVIDENCE_SUBFOLDER_NAMES)
            or "Evidence_Clips" in name
            or "违规证据切片" in name
        ):
            return False
        ctype = str(item.get("contentType") or "").lower()
        return ctype.startswith("video/") or base.lower().endswith(_VIDEO_EXTS)

    async def list_videos_in_folder(self, folder_id: str) -> List[VideoMetadataItem]:
        """Alias for `list_folder_videos`."""
        return await self.list_folder_videos(folder_id)

    async def list_folder_videos(self, folder_id: str) -> List[VideoMetadataItem]:
        bucket, prefix = parse_gcs_uri(folder_id)
        _assert_not_internal_state_prefix(bucket, prefix)

        def _fetch() -> List[VideoMetadataItem]:
            raw: List[Dict[str, Any]] = []
            page_token = ""
            while True:
                url = (
                    f"{_STORAGE_API}/b/{urllib.parse.quote(bucket, safe='')}/o?delimiter=%2F"
                    f"&prefix={urllib.parse.quote(prefix + '/' if prefix else '', safe='')}"
                    "&fields=nextPageToken,items(name,size,contentType,metadata)"
                )
                if page_token:
                    url += f"&pageToken={urllib.parse.quote(page_token, safe='')}"
                status, body = self._request("GET", url, timeout=15.0)
                self._check(status, body, bucket, prefix, "read")
                page = json.loads(body.decode("utf-8") or "{}")
                raw.extend(i for i in page.get("items", []) if self._is_video_object(i))
                page_token = str(page.get("nextPageToken") or "")
                if not page_token:
                    break
            raw.sort(key=lambda i: str(i["name"]))
            if len(raw) > 1:
                from concurrent.futures import ThreadPoolExecutor

                with ThreadPoolExecutor(max_workers=min(4, len(raw))) as pool:
                    dims = list(pool.map(lambda i: self._object_dims(bucket, i), raw))
            else:
                dims = [self._object_dims(bucket, i) for i in raw]
            items = [
                VideoMetadataItem(
                    file_id=f"gs://{bucket}/{i['name']}",
                    filename=posixpath.basename(i["name"]),
                    width=w,
                    height=h,
                    duration_sec=d,
                    mime_type=str(i.get("contentType") or "video/mp4"),
                    size_bytes=int(i.get("size") or 0),
                )
                for i, (w, h, d) in zip(raw, dims)
            ]
            items.sort(key=lambda v: natural_video_sort_key(v.filename))
            return items

        return await asyncio.to_thread(_fetch)

    async def download_video_to_path(self, file_id: str, dest_path: Path) -> Path:
        bucket, obj = parse_gcs_uri(file_id)
        _assert_not_internal_state_prefix(bucket, _object_parent(obj))
        tmp_path = dest_path.with_name(dest_path.name + ".tmp")

        def _dl() -> Path:
            status = self._download(self._media_url(bucket, obj), tmp_path)
            if status >= 400:
                tmp_path.unlink(missing_ok=True)
                self._check(status, b"", bucket, obj, "download")
            tmp_path.rename(dest_path)
            return dest_path

        return await asyncio.to_thread(_dl)

    async def ensure_subfolder(self, parent_folder_id: str, name: str) -> str:
        """GCS has no folders: the subfolder is just the `<prefix>/<name>` object-name prefix."""
        bucket, prefix = parse_gcs_uri(parent_folder_id)
        _assert_not_internal_state_prefix(bucket, prefix)
        sub = f"{prefix}/{name.strip().strip('/')}" if prefix else name.strip().strip("/")
        return f"gs://{bucket}/{sub}"

    async def upload_evidence_mp4(self, subfolder_id: str, local_path: Path) -> str:
        bucket, prefix = parse_gcs_uri(subfolder_id)
        _assert_not_internal_state_prefix(bucket, prefix)
        obj = f"{prefix}/{local_path.name}" if prefix else local_path.name
        uri = f"gs://{bucket}/{obj}"

        def _up() -> str:
            status, _ = self._request("GET", self._object_url(bucket, obj) + "?fields=name", timeout=15.0)
            if status == 200:  # a resumed job already uploaded this clip
                return build_gcs_console_object_url(uri)
            size = local_path.stat().st_size
            with local_path.open("rb") as fh:
                status, body = self._request(
                    "POST",
                    self._upload_url(bucket, obj),
                    body=fh,
                    content_type="video/mp4",
                    content_length=size,
                    timeout=120.0,
                )
            if status in (401, 403, 404):
                self._check(status, body, bucket, obj, "write")
            if status >= 400:
                logger.warning("Failed to upload evidence clip %s to %s: HTTP %s", local_path.name, uri, status)
                return ""
            return build_gcs_console_object_url(uri)

        return await asyncio.to_thread(_up)

    async def create_dual_tab_report_sheet(
        self,
        parent_folder_id: str,
        title: str,
        tab1_rows: List,
        tab2_rows: List,
        *,
        reuse_suffix: str = "",
        audited_rule_ids: Optional[Sequence[str]] = None,
    ) -> Tuple[str, str]:
        """Writes the dual-tab `.xlsx` report into the prefix, preserving historical rows from
        previously audited videos in the same folder (Method 1: Same-Sheet Smart Append).
        When `audited_rule_ids` is non-empty (`Tab4_Spot_Check` spot-check), preserves existing rows
        for other SOP rules on the same video.
        """
        bucket, prefix = parse_gcs_uri(parent_folder_id)
        _assert_not_internal_state_prefix(bucket, prefix)
        fname = re.sub(r'[\\/:*?"<>|]+', "_", title) + reuse_suffix + ".xlsx"
        obj = f"{prefix}/{fname}" if prefix else fname
        uri = f"gs://{bucket}/{obj}"
        candidate_objs = [obj]
        for leg_title in LEGACY_REPORT_SHEET_TITLES:
            leg_fname = re.sub(r'[\\/:*?"<>|]+', "_", leg_title) + reuse_suffix + ".xlsx"
            leg_obj = f"{prefix}/{leg_fname}" if prefix else leg_fname
            if leg_obj not in candidate_objs:
                candidate_objs.append(leg_obj)
        audited_video_filenames = {
            str(getattr(r, "video_filename", "")).strip()
            for r in (*tab1_rows, *tab2_rows)
            if str(getattr(r, "video_filename", "")).strip()
        }
        scoped_rules = {
            str(r).strip().upper()
            for r in (audited_rule_ids or [])
            if str(r).strip()
        }
        current_prompt_versions = {
            str(getattr(r, "prompt_version_used", "")).strip()
            for r in tab2_rows
            if str(getattr(r, "prompt_version_used", "")).strip()
        }

        def _write() -> Tuple[str, str]:
            kept_tab1_rows: List[List[Any]] = []
            kept_tab2_rows: List[List[Any]] = []
            if audited_video_filenames:
                try:
                    ex_body = b""
                    for cand_obj in candidate_objs:
                        ex_status, cand_body = self._request("GET", self._media_url(bucket, cand_obj), timeout=20.0)
                        if ex_status == 200 and cand_body:
                            ex_body = cand_body
                            break
                    if ex_body:
                        ex_sheets = read_xlsx_sheets(ex_body)
                        ex_t1_rows = next(
                            (ex_sheets[t] for t in LEGACY_TAB1_TITLES if t in ex_sheets),
                            ex_sheets.get(TAB1_TITLE) or [],
                        )
                        for raw_row in ex_t1_rows[1:]:
                            row_video = str(raw_row[1]).strip() if len(raw_row) > 1 else ""
                            row_rule = str(raw_row[3]).strip().upper() if len(raw_row) > 3 else ""
                            if row_video and (
                                row_video not in audited_video_filenames
                                or (bool(scoped_rules) and row_rule not in scoped_rules)
                            ):
                                padded = [str(c) for c in raw_row[:11]] + [""] * max(0, 11 - len(raw_row))
                                try:
                                    padded[6] = float(padded[6] or 0.0)
                                except (TypeError, ValueError):
                                    padded[6] = 0.0
                                if not str(padded[9]).strip():
                                    padded[9] = "PENDING"
                                kept_tab1_rows.append(padded)
                        ex_t2_rows = next(
                            (ex_sheets[t] for t in LEGACY_TAB2_TITLES if t in ex_sheets),
                            ex_sheets.get(TAB2_TITLE) or [],
                        )
                        for raw_row in ex_t2_rows[1:]:
                            row_video2 = str(raw_row[2]).strip() if len(raw_row) > 2 else ""
                            row_prompt2 = str(raw_row[6]).strip() if len(raw_row) > 6 else ""
                            if row_video2 and (
                                row_video2 not in audited_video_filenames
                                or (
                                    bool(scoped_rules)
                                    and bool(current_prompt_versions)
                                    and row_prompt2 not in current_prompt_versions
                                )
                            ):
                                padded2: List[Any] = [str(c) for c in raw_row[:15]] + [""] * max(
                                    0, 15 - len(raw_row)
                                )
                                for f_idx in (3, 11):
                                    try:
                                        padded2[f_idx] = float(padded2[f_idx] or 0.0)
                                    except (TypeError, ValueError):
                                        padded2[f_idx] = 0.0
                                for i_idx in (7, 8, 9, 10, 12, 13):
                                    try:
                                        padded2[i_idx] = int(float(padded2[i_idx] or 0))
                                    except (TypeError, ValueError):
                                        padded2[i_idx] = 0
                                kept_tab2_rows.append(padded2)
                except Exception as exc:
                    logger.warning(
                        "Non-fatal warning reading existing GCS report %s for smart append: %s",
                        uri,
                        exc,
                    )

            if kept_tab1_rows or kept_tab2_rows:
                t1_vals = [list(TAB1_HEADERS), *kept_tab1_rows, *build_tab1_values(tab1_rows)[1:]]
                t2_vals = [list(TAB2_HEADERS), *kept_tab2_rows, *build_tab2_values(tab2_rows)[1:]]
                payload = build_xlsx_bytes(
                    [
                        {
                            "name": TAB1_TITLE,
                            "rows": t1_vals,
                            "widths": TAB1_WIDTHS,
                            "status_col": TAB1_STATUS_COL,
                            "link_cols": (TAB1_LINK_COL,),
                        },
                        {"name": TAB2_TITLE, "rows": t2_vals, "widths": TAB2_WIDTHS},
                    ]
                )
            else:
                payload = build_dual_tab_xlsx_bytes(tab1_rows, tab2_rows)

            status, body = self._request(
                "POST",
                self._upload_url(bucket, obj),
                body=payload,
                content_type=XLSX_CONTENT_TYPE,
                content_length=len(payload),
                timeout=60.0,
            )
            self._check(status, body, bucket, obj, "write")
            return uri, build_gcs_console_object_url(uri)

        return await asyncio.to_thread(_write)

    def _resolve_report_object(self, parent_folder_id: str, sheet_id_or_url: str) -> Tuple[str, str]:
        if sheet_id_or_url and is_gcs_target(sheet_id_or_url):
            return parse_gcs_uri(sheet_id_or_url)
        bucket, prefix = parse_gcs_uri(parent_folder_id)
        _assert_not_internal_state_prefix(bucket, prefix)
        fname = f"{REPORT_SHEET_TITLE}.xlsx"
        obj = f"{prefix}/{fname}" if prefix else fname
        return bucket, obj

    async def read_report_sheet_data(
        self,
        parent_folder_id: str,
        sheet_id_or_url: str = "",
    ) -> Tuple[List[List[str]], List[List[str]]]:
        """Reads `(tab1_rows_without_header, tab2_rows_without_header)` from a GCS `.xlsx` report."""
        bucket, obj = self._resolve_report_object(parent_folder_id, sheet_id_or_url)

        def _read() -> Tuple[List[List[str]], List[List[str]]]:
            status, body = self._request("GET", self._media_url(bucket, obj), timeout=20.0)
            if (status == 404 or not body) and not (sheet_id_or_url and is_gcs_target(sheet_id_or_url)):
                _, prefix = parse_gcs_uri(parent_folder_id)
                for leg_title in LEGACY_REPORT_SHEET_TITLES:
                    leg_obj = f"{prefix}/{leg_title}.xlsx" if prefix else f"{leg_title}.xlsx"
                    if leg_obj != obj:
                        leg_status, leg_body = self._request("GET", self._media_url(bucket, leg_obj), timeout=20.0)
                        if leg_status == 200 and leg_body:
                            status, body = leg_status, leg_body
                            break
            if status == 404 or not body:
                return [], []
            self._check(status, body, bucket, obj, "read")
            sheets = read_xlsx_sheets(body)
            raw_t1 = next((sheets[t] for t in LEGACY_TAB1_TITLES if t in sheets), sheets.get(TAB1_TITLE) or [])
            raw_t2 = next((sheets[t] for t in LEGACY_TAB2_TITLES if t in sheets), sheets.get(TAB2_TITLE) or [])
            t1 = [
                [str(c) for c in r[:11]] + [""] * max(0, 11 - len(r))
                for r in raw_t1[1:]
                if any(str(c).strip() for c in r)
            ]
            t2 = [
                [str(c) for c in r[:15]] + [""] * max(0, 15 - len(r))
                for r in raw_t2[1:]
                if any(str(c).strip() for c in r)
            ]
            return t1, t2

        return await asyncio.to_thread(_read)

    async def update_report_human_reviews(
        self,
        parent_folder_id: str,
        sheet_id_or_url: str,
        updates: Sequence[Dict[str, Any]],
    ) -> int:
        """Applies human review status & note updates to `Tab 1` of a GCS `.xlsx` report by composite key."""
        if not updates:
            return 0
        bucket, obj = self._resolve_report_object(parent_folder_id, sheet_id_or_url)

        def _update() -> int:
            target_obj = obj
            status, body = self._request("GET", self._media_url(bucket, target_obj), timeout=20.0)
            if (status == 404 or not body) and not (sheet_id_or_url and is_gcs_target(sheet_id_or_url)):
                _, prefix = parse_gcs_uri(parent_folder_id)
                for leg_title in LEGACY_REPORT_SHEET_TITLES:
                    leg_obj = f"{prefix}/{leg_title}.xlsx" if prefix else f"{leg_title}.xlsx"
                    if leg_obj != obj:
                        leg_status, leg_body = self._request("GET", self._media_url(bucket, leg_obj), timeout=20.0)
                        if leg_status == 200 and leg_body:
                            status, body, target_obj = leg_status, leg_body, leg_obj
                            break
            self._check(status, body, bucket, target_obj, "read")
            ex_sheets = read_xlsx_sheets(body)
            raw_t1 = next((ex_sheets[t] for t in LEGACY_TAB1_TITLES if t in ex_sheets), ex_sheets.get(TAB1_TITLE) or [])[1:]
            raw_t2 = next((ex_sheets[t] for t in LEGACY_TAB2_TITLES if t in ex_sheets), ex_sheets.get(TAB2_TITLE) or [])[1:]
            updated_t1, patches = match_and_apply_tab1_reviews(raw_t1, updates)
            if not patches:
                return 0
            t2_rows: List[List[Any]] = []
            for r in raw_t2:
                padded2: List[Any] = [str(c) for c in r[:15]] + [""] * max(0, 15 - len(r))
                for f_idx in (3, 11):
                    try:
                        padded2[f_idx] = float(padded2[f_idx] or 0.0)
                    except (TypeError, ValueError):
                        padded2[f_idx] = 0.0
                for i_idx in (7, 8, 9, 10, 12, 13):
                    try:
                        padded2[i_idx] = int(float(padded2[i_idx] or 0))
                    except (TypeError, ValueError):
                        padded2[i_idx] = 0
                t2_rows.append(padded2)
            payload = build_xlsx_bytes(
                [
                    {
                        "name": TAB1_TITLE,
                        "rows": [list(TAB1_HEADERS), *updated_t1],
                        "widths": TAB1_WIDTHS,
                        "status_col": TAB1_STATUS_COL,
                        "link_cols": (TAB1_LINK_COL,),
                    },
                    {"name": TAB2_TITLE, "rows": [list(TAB2_HEADERS), *t2_rows], "widths": TAB2_WIDTHS},
                ]
            )
            up_status, up_body = self._request(
                "POST",
                self._upload_url(bucket, target_obj),
                body=payload,
                content_type=XLSX_CONTENT_TYPE,
                content_length=len(payload),
                timeout=60.0,
            )
            self._check(up_status, up_body, bucket, target_obj, "write")
            return len(patches)

        return await asyncio.to_thread(_update)


class RoutingStorageGateway:
    """Dispatches each storage call by target ID: `gs://…` -> GCS, anything else -> Google Workspace."""

    def __init__(self, drive_gateway: Any = None, gcs_gateway: Any = None) -> None:
        self._drive_gateway = drive_gateway
        self._gcs_gateway = gcs_gateway

    def _drive(self) -> Any:
        if self._drive_gateway is None:
            from .gcp import GoogleWorkspaceGateway

            self._drive_gateway = GoogleWorkspaceGateway()
        return self._drive_gateway

    def _gcs(self) -> Any:
        if self._gcs_gateway is None:
            self._gcs_gateway = GcsStorageGateway()
        return self._gcs_gateway

    def _for(self, target_id: str) -> Any:
        return self._gcs() if is_gcs_target(target_id) else self._drive()

    async def probe_write_access(self, folder_id: str) -> str:
        return await self._for(folder_id).probe_write_access(folder_id)

    async def check_sheet_readable(self, sheet_id: str) -> str:
        if not (sheet_id or "").strip():
            return ""
        if is_gcs_target(sheet_id):
            return await self._gcs().check_sheet_readable(sheet_id)
        return await self._drive().check_sheet_readable(sheet_id)

    async def list_folder_videos(self, folder_id: str) -> List[VideoMetadataItem]:
        return await self._for(folder_id).list_folder_videos(folder_id)

    async def download_video_to_path(self, file_id: str, dest_path: Path) -> Path:
        return await self._for(file_id).download_video_to_path(file_id, dest_path)

    async def ensure_subfolder(self, parent_folder_id: str, name: str) -> str:
        return await self._for(parent_folder_id).ensure_subfolder(parent_folder_id, name)

    async def upload_evidence_mp4(self, subfolder_id: str, local_path: Path) -> str:
        return await self._for(subfolder_id).upload_evidence_mp4(subfolder_id, local_path)

    async def create_dual_tab_report_sheet(
        self,
        parent_folder_id: str,
        title: str,
        tab1_rows: List,
        tab2_rows: List,
        *,
        reuse_suffix: str = "",
        audited_rule_ids: Optional[Sequence[str]] = None,
    ) -> Tuple[str, str]:
        return await self._for(parent_folder_id).create_dual_tab_report_sheet(
            parent_folder_id,
            title,
            tab1_rows,
            tab2_rows,
            reuse_suffix=reuse_suffix,
            audited_rule_ids=audited_rule_ids,
        )

    async def read_report_sheet_data(
        self,
        parent_folder_id: str,
        sheet_id_or_url: str = "",
    ) -> Tuple[List[List[str]], List[List[str]]]:
        target = sheet_id_or_url if is_gcs_target(sheet_id_or_url) else parent_folder_id
        return await self._for(target).read_report_sheet_data(parent_folder_id, sheet_id_or_url)

    async def update_report_human_reviews(
        self,
        parent_folder_id: str,
        sheet_id_or_url: str,
        updates: Sequence[Dict[str, Any]],
    ) -> int:
        target = sheet_id_or_url if is_gcs_target(sheet_id_or_url) else parent_folder_id
        return await self._for(target).update_report_human_reviews(
            parent_folder_id, sheet_id_or_url, updates
        )

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._drive(), name)
