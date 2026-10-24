#!/usr/bin/env python3
"""Load the SOP Master Sheet snapshot (sop/master_sheet.json) into a Google Sheet, or export .xlsx.

The audit service reads its Layer 2 SOP rules at run time from the Google Sheet named by
Terraform variable `master_prompt_sheet_id`:
  * tab `Tab0_版本总控与回滚开关` holds the pointers (row `Active_Prompt_Version` = name of the
    rules tab to use, `Active_Model_Version`, `Fallback_Model_Version`);
  * the rules tab holds one SOP rule per row (9 columns, header in row 1).
A Sheet is a Drive file, not a GCP resource, so Terraform cannot create it. Run this once per
environment using either:

Option A — Browser import (zero OAuth / zero CLI setup):
  1. In Google Sheets, create a blank spreadsheet -> File -> Import -> Upload `sop/master_sheet.xlsx`
     -> choose "Replace spreadsheet" -> Import data.
  2. Share the Sheet with the bot account (`workspace_impersonate_user`) as Viewer (or Editor).
  3. Put the Sheet ID into `<env>.tfvars` -> `master_prompt_sheet_id`.

Option C — Zero-GWS (no Google Workspace): keep the SOP workbook in a customer GCS bucket as `.xlsx`
   (Object Versioning is enabled on the bucket when permitted, so every re-upload is archived):
     gcloud auth application-default login
     python3 scripts/init_sop_sheet.py --gcs-uri gs://<bucket>/sop/master_sheet.xlsx --tfvars <env>.tfvars
   then set `master_prompt_sheet_id = "gs://<bucket>/sop/master_sheet.xlsx"` in `<env>.tfvars`.
   Later edits: download the file, change rules / ENABLE-DISABLE / Tab0 pointers in Excel or WPS,
   save as .xlsx and upload it to the same path; the service picks it up within 60s, no redeploy.

Option B — CLI via Service Account impersonation (uses standard `gcloud auth application-default login`
without `--scopes=spreadsheets`, avoiding Google's OAuth block on the default gcloud client ID):
  1. Run `bootstrap` first (so `<name_prefix>-worker@<project_id>.iam.gserviceaccount.com` exists).
  2. Create an empty Google Sheet and share it as **Editor** with `<name_prefix>-worker@<project_id>.iam.gserviceaccount.com`
     (or with `workspace_impersonate_user` if Domain-Wide Delegation is already active).
  3. Run:
       gcloud auth application-default login
       python3 scripts/init_sop_sheet.py --sheet-id <SHEET_ID_OR_URL> --tfvars <env>.tfvars
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import zipfile
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape as xml_escape

DEFAULT_SNAPSHOT = Path(__file__).resolve().parent.parent / "sop" / "master_sheet.json"
DEFAULT_XLSX = Path(__file__).resolve().parent.parent / "sop" / "master_sheet.xlsx"
TAB0_TITLE = "Tab0_Version_Control"
LEGACY_TAB0_TITLE = "Tab0_版本总控与回滚开关"
LEGACY_TAB_RENAME_MAP: dict[str, str] = {
    "Tab0_版本总控与回滚开关": "Tab0_Version_Control",
    "Prompt_v2.5_V10全量17条标准版": "Prompt_v2.5_Standard",
    "Prompt_v2.5_归档基准": "Prompt_v2.5_Standard",
    "Tab2_定时巡检配置表": "Tab2_Scheduled_Jobs",
    "Tab3_黄金集评测工作台": "Tab3_Golden_Eval",
    "Tab4_专项测试表": "Tab4_Spot_Check",
}
_SHEET_URL_RE = re.compile(r"/spreadsheets/d/([A-Za-z0-9_-]+)")
_TFVAR_LINE_RE = re.compile(r'^\s*([a-zA-Z0-9_]+)\s*=\s*"([^"]*)"')
_SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
_DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"
_USERINFO_SCOPE = "https://www.googleapis.com/auth/userinfo.profile"
_DWD_SCOPES = (_DRIVE_SCOPE, _SHEETS_SCOPE)
_ALL_RECOMMENDED_DWD_SCOPES = f"{_DRIVE_SCOPE},{_SHEETS_SCOPE},{_USERINFO_SCOPE}"
_CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
DEFAULT_SHEET_TITLE = "CHAGEE_SOP_and_Model_Control_Tower"


def build_master_sheet_payload() -> dict[str, Any]:
    """Builds the canonical English Master SOP Sheet payload from `cctv_audit.prompt_manager`."""
    _import_cctv_audit()
    from cctv_audit.prompt_manager import (
        DEFAULT_CHAGEE_V25_RULES,
        DEFAULT_PROMPT_TAB_TITLE,
        DEFAULT_TAB3_SAMPLE_ROW,
        DEFAULT_TAB4_SAMPLE_ROW,
        EVAL_WORKBENCH_HEADERS,
        SCHEDULED_RULE_HEADERS,
        SHEET_RULE_HEADER,
        TAB0_TITLE as PM_TAB0_TITLE,
        TAB2_SCHEDULED_TITLE,
        TAB3_EVAL_TITLE,
        TAB4_SPOT_CHECK_TITLE,
        rules_to_sheet_rows,
    )

    standard_rows = rules_to_sheet_rows(list(DEFAULT_CHAGEE_V25_RULES))
    return {
        "_comment": (
            "Snapshot of the SOP Master Sheet (Layer 2 rules + Tab0 version pointers). "
            "Load into a Google Sheet with scripts/init_sop_sheet.py. "
            "Tab0 row 'Active_Prompt_Version' names the rules tab the service reads at run time."
        ),
        "source_snapshot_date": "2026-10-21",
        "tabs": [
            {
                "title": PM_TAB0_TITLE,
                "values": [
                    ["Config_Key", "Active_Value", "Description", "Candidates"],
                    [
                        "Active_Prompt_Version",
                        DEFAULT_PROMPT_TAB_TITLE,
                        "Active production SOP rule tab name (0.1s switch & rollback)",
                        f"{DEFAULT_PROMPT_TAB_TITLE} | Prompt_v2.6_r01 | Prompt_v2.6_r02",
                    ],
                    [
                        "Active_Model_Version",
                        "gemini-3.8-flash",
                        "Active production Vertex AI model version (AUTO_LATEST_FLASH prohibited)",
                        "gemini-3.8-flash | gemini-2.5-flash",
                    ],
                    [
                        "Fallback_Model_Version",
                        "gemini-3.8-flash",
                        "Governed fallback model version when primary model probe fails",
                        "gemini-3.8-flash",
                    ],
                ],
            },
            {
                "title": DEFAULT_PROMPT_TAB_TITLE,
                "values": [row[:] for row in standard_rows],
            },
            {
                "title": "Prompt_v2.6_r01",
                "values": [row[:] for row in standard_rows],
            },
            {
                "title": "Prompt_v2.6_r02",
                "values": [row[:] for row in standard_rows],
            },
            {
                "title": TAB2_SCHEDULED_TITLE,
                "values": [
                    list(SCHEDULED_RULE_HEADERS),
                    [
                        "SCHED-EXAMPLE-DRIVE",
                        "[Example] Daily Regional Automated Inspection (fill in root folder link and set Enabled to ENABLED to activate)",
                        "https://drive.google.com/drive/folders/1ExampleRootFolderIdReplaceMe0000",
                        "18",
                        "00",
                        "1",
                        "auditor@example.com",
                        "PAUSED",
                        "Default example (not enabled)",
                    ],
                    [
                        "SCHED-EXAMPLE-GCS",
                        "[Example] Zero-GWS Bucket Daily Automated Inspection (fill in gs:// root prefix and set Enabled to ENABLED to activate)",
                        "gs://example-workspace-bucket/stores",
                        "19",
                        "00",
                        "1",
                        "auditor@example.com",
                        "PAUSED",
                        "Default example (not enabled)",
                    ],
                ],
            },
            {
                "title": TAB3_EVAL_TITLE,
                "values": [
                    list(EVAL_WORKBENCH_HEADERS),
                    list(DEFAULT_TAB3_SAMPLE_ROW),
                ],
            },
            {
                "title": TAB4_SPOT_CHECK_TITLE,
                "values": [
                    list(SHEET_RULE_HEADER),
                    list(DEFAULT_TAB4_SAMPLE_ROW),
                ],
            },
        ],
    }


def write_local_artifacts(
    json_path: Path = DEFAULT_SNAPSHOT,
    xlsx_path: Path = DEFAULT_XLSX,
) -> dict[str, Any]:
    """Writes both `sop/master_sheet.json` and `sop/master_sheet.xlsx` locally from `build_master_sheet_payload()`."""
    payload = build_master_sheet_payload()
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1) + "\n",
        encoding="utf-8",
    )
    export_xlsx(payload["tabs"], xlsx_path)
    return payload


def extract_sheet_id(value: str) -> str:
    """Accept a bare Sheet ID or a docs.google.com/spreadsheets URL; reject Drive folder links."""
    value = (value or "").strip()
    match = _SHEET_URL_RE.search(value)
    if match:
        return match.group(1)
    if "/" in value or not re.fullmatch(r"[A-Za-z0-9_-]{20,}", value):
        raise ValueError(f"not a Google Sheet ID or Sheet URL: {value!r}")
    return value


def parse_tfvars(path: Path) -> dict[str, str]:
    """Extract simple `key = "value"` string assignments from a `.tfvars` file."""
    result: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        match = _TFVAR_LINE_RE.match(line)
        if match:
            result[match.group(1)] = match.group(2).strip()
    return result


def resolve_impersonation_targets(
    tfvars_path: Path | None,
    service_account: str,
    impersonate_user: str,
) -> tuple[str, str]:
    """Return `(worker_sa_email, workspace_impersonate_user)` from flags, `--tfvars`, or env."""
    sa = (service_account or os.environ.get("WORKSPACE_DWD_SERVICE_ACCOUNT", "")).strip()
    user = (impersonate_user or os.environ.get("WORKSPACE_IMPERSONATE_USER", "")).strip()
    if tfvars_path is not None:
        tfv = parse_tfvars(tfvars_path)
        if not sa:
            proj = tfv.get("project_id", "")
            prefix = tfv.get("name_prefix", "")
            if proj and prefix and "<" not in proj and "<" not in prefix:
                sa = f"{prefix}-worker@{proj}.iam.gserviceaccount.com"
        if not user:
            cand = tfv.get("workspace_impersonate_user", "")
            if cand and "<" not in cand:
                user = cand
    return sa, user


def load_snapshot(path: Path) -> list[dict[str, Any]]:
    """Read and validate the snapshot; returns the list of {title, values} tabs."""
    doc = json.loads(path.read_text(encoding="utf-8"))
    tabs = doc.get("tabs")
    if not isinstance(tabs, list) or not tabs:
        raise ValueError(f"{path}: no 'tabs' list")
    titles = [t.get("title") for t in tabs]
    tab0_title = next((t for t in (TAB0_TITLE, LEGACY_TAB0_TITLE) if t in titles), None)
    if tab0_title is None:
        raise ValueError(f"{path}: missing pointer tab {TAB0_TITLE!r}")
    if len(set(titles)) != len(titles):
        raise ValueError(f"{path}: duplicate tab titles")
    tab0 = next(t for t in tabs if t["title"] == tab0_title)
    pointers = {row[0]: row[1] for row in tab0["values"][1:] if len(row) >= 2}
    active = pointers.get("Active_Prompt_Version", "")
    if active not in titles:
        raise ValueError(
            f"{path}: Tab0 Active_Prompt_Version={active!r} does not name a tab in the snapshot"
        )
    for tab in tabs:
        values = tab.get("values")
        if not isinstance(values, list) or not values or not all(isinstance(r, list) for r in values):
            raise ValueError(f"{path}: tab {tab.get('title')!r} has no row data")
        if tab["title"] in ("Tab3_Golden_Eval", "Tab3_黄金集评测工作台"):
            if len(values[0]) not in (9, 10):
                raise ValueError(
                    f"{path}: eval workbench tab {tab['title']!r} must have 9 or 10 columns, got {len(values[0])}"
                )
        elif tab["title"] not in (TAB0_TITLE, LEGACY_TAB0_TITLE) and len(values[0]) != 9:
            raise ValueError(f"{path}: rules tab {tab['title']!r} must have 9 columns, got {len(values[0])}")
    return tabs


def _col_letter(col_idx_zero_based: int) -> str:
    """Convert 0-based column index to Excel column letters (0 -> A, 25 -> Z, 26 -> AA)."""
    n = col_idx_zero_based + 1
    chars: list[str] = []
    while n > 0:
        n, rem = divmod(n - 1, 26)
        chars.append(chr(ord("A") + rem))
    return "".join(reversed(chars))


def export_xlsx(tabs: list[dict[str, Any]], out_path: Path) -> None:
    """Write a standards-compliant multi-sheet `.xlsx` file using inline strings (`t="inlineStr"`).

    Using `inlineStr` guarantees every cell is imported by Google Sheets as literal text (never
    interpreted as a formula even if a cell starts with `=` or `+`), with zero third-party deps.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(build_xlsx_bytes(tabs))


def build_xlsx_bytes(tabs: list[dict[str, Any]]) -> bytes:
    """In-memory bytes of the `.xlsx` written by `export_xlsx`."""
    sheet_overrides = "\n".join(
        f'  <Override PartName="/xl/worksheets/sheet{i}.xml" '
        f'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        for i in range(1, len(tabs) + 1)
    )
    content_types_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">\n'
        '  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>\n'
        '  <Default Extension="xml" ContentType="application/xml"/>\n'
        '  <Override PartName="/xl/workbook.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>\n'
        f"{sheet_overrides}\n"
        "</Types>\n"
    )
    root_rels_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
        '  <Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="xl/workbook.xml"/>\n'
        "</Relationships>\n"
    )
    sheets_entries = "\n".join(
        f'    <sheet name="{xml_escape(str(t["title"]), {"\"": "&quot;"})}" sheetId="{i}" r:id="rId{i}"/>'
        for i, t in enumerate(tabs, start=1)
    )
    workbook_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">\n'
        "  <sheets>\n"
        f"{sheets_entries}\n"
        "  </sheets>\n"
        "</workbook>\n"
    )
    wb_rels_entries = "\n".join(
        f'  <Relationship Id="rId{i}" '
        f'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        f'Target="worksheets/sheet{i}.xml"/>'
        for i in range(1, len(tabs) + 1)
    )
    workbook_rels_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
        f"{wb_rels_entries}\n"
        "</Relationships>\n"
    )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types_xml)
        zf.writestr("_rels/.rels", root_rels_xml)
        zf.writestr("xl/workbook.xml", workbook_xml)
        zf.writestr("xl/_rels/workbook.xml.rels", workbook_rels_xml)
        for i, tab in enumerate(tabs, start=1):
            row_xml_parts: list[str] = []
            for r_idx, row in enumerate(tab["values"], start=1):
                cell_parts: list[str] = []
                for c_idx, val in enumerate(row):
                    ref = f"{_col_letter(c_idx)}{r_idx}"
                    text = xml_escape(str(val))
                    cell_parts.append(
                        f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{text}</t></is></c>'
                    )
                row_xml_parts.append(f'    <row r="{r_idx}">{"".join(cell_parts)}</row>')
            sheet_xml = (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">\n'
                "  <sheetData>\n"
                + "\n".join(row_xml_parts)
                + "\n  </sheetData>\n"
                "</worksheet>\n"
            )
            zf.writestr(f"xl/worksheets/sheet{i}.xml", sheet_xml)
    return buf.getvalue()


def _a1_quote(title: str) -> str:
    return "'" + title.replace("'", "''") + "'"


def write_tabs(service: Any, sheet_id: str, tabs: list[dict[str, Any]], overwrite: bool) -> None:
    meta = service.spreadsheets().get(spreadsheetId=sheet_id, fields="sheets.properties").execute()
    existing = {s["properties"]["title"]: s["properties"]["sheetId"] for s in meta.get("sheets", [])}

    if overwrite:
        rename_requests: list[dict[str, Any]] = []
        for old_title, new_title in LEGACY_TAB_RENAME_MAP.items():
            if old_title in existing and new_title not in existing:
                sheet_gid = existing.pop(old_title)
                existing[new_title] = sheet_gid
                rename_requests.append(
                    {
                        "updateSheetProperties": {
                            "properties": {"sheetId": sheet_gid, "title": new_title},
                            "fields": "title",
                        }
                    }
                )
        if rename_requests:
            service.spreadsheets().batchUpdate(
                spreadsheetId=sheet_id, body={"requests": rename_requests}
            ).execute()

    clashes = [t["title"] for t in tabs if t["title"] in existing]
    if clashes and not overwrite:
        raise SystemExit(
            f"Sheet {sheet_id} already has tab(s) {clashes}. Re-run with --overwrite to replace them."
        )

    requests: list[dict[str, Any]] = [
        {"addSheet": {"properties": {"title": t["title"]}}} for t in tabs if t["title"] not in existing
    ]
    if requests:
        service.spreadsheets().batchUpdate(spreadsheetId=sheet_id, body={"requests": requests}).execute()
    for title in clashes:
        service.spreadsheets().values().clear(
            spreadsheetId=sheet_id, range=_a1_quote(title), body={}
        ).execute()

    service.spreadsheets().values().batchUpdate(
        spreadsheetId=sheet_id,
        body={
            "valueInputOption": "RAW",
            "data": [{"range": f"{_a1_quote(t['title'])}!A1", "values": t["values"]} for t in tabs],
        },
    ).execute()

    # A brand-new spreadsheet starts with an empty "Sheet1"; drop it so Tab0 is the first tab.
    placeholder = [
        sid for title, sid in existing.items()
        if title in ("Sheet1", "工作表1") and title not in {t["title"] for t in tabs}
    ]
    if placeholder and len(existing) == 1:
        service.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id, body={"requests": [{"deleteSheet": {"sheetId": placeholder[0]}}]}
        ).execute()

    # Apply native Google Sheets dropdown & formula validations across Tab0, Prompt_*, and Tab2.
    _import_cctv_audit()
    from cctv_audit.prompt_manager import (
        build_master_sheet_validation_requests,
        model_dropdown_options,
    )

    tab0_values = next((t["values"] for t in tabs if t["title"] in (TAB0_TITLE, LEGACY_TAB0_TITLE)), [])
    updated_meta = service.spreadsheets().get(spreadsheetId=sheet_id, fields="sheets.properties").execute()
    val_requests = build_master_sheet_validation_requests(
        updated_meta, model_versions=model_dropdown_options(tab0_values)
    )
    if val_requests:
        service.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id, body={"requests": val_requests}
        ).execute()


def verify(service: Any, sheet_id: str, tabs: list[dict[str, Any]]) -> None:
    """Read every tab back and compare cell by cell (trailing empty cells ignored)."""
    for tab in tabs:
        got = service.spreadsheets().values().get(
            spreadsheetId=sheet_id, range=_a1_quote(tab["title"]), valueRenderOption="UNFORMATTED_VALUE"
        ).execute().get("values", [])
        want = [[str(c) for c in row] for row in tab["values"]]
        norm = lambda rows: [[str(c) for c in r][: max((i + 1 for i, c in enumerate(r) if str(c) != ""), default=0)] for r in rows]  # noqa: E731
        if norm(got) != norm(want):
            raise SystemExit(f"read-back mismatch in tab {tab['title']!r}")


def _normalize_rows(rows: list[list[Any]]) -> list[list[str]]:
    """Cells as text with trailing empty cells / rows dropped (what any spreadsheet round-trip keeps)."""
    out = [[str(c) for c in r][: max((i + 1 for i, c in enumerate(r) if str(c) != ""), default=0)] for r in rows]
    while out and not out[-1]:
        out.pop()
    return out


def _import_cctv_audit() -> None:
    root = str(Path(__file__).resolve().parent.parent)
    if root not in sys.path:
        sys.path.insert(0, root)


def resolve_gcs_sop_uri(value: str) -> str:
    """`gs://bucket/<obj>.xlsx` from a gs:// URI or Cloud Console / storage URL; '' if not a GCS reference."""
    _import_cctv_audit()
    from cctv_audit.gcs_uri import normalize_gcs_target, split_generation

    target = normalize_gcs_target(value or "")
    if target is None:
        return ""
    obj = split_generation(target)[0][len("gs://"):].partition("/")[2]
    if not obj.lower().endswith(".xlsx"):
        raise ValueError(f"--gcs-uri must name a .xlsx object such as gs://<bucket>/sop/master_sheet.xlsx, got {value!r}")
    return target


def make_gcs_gateway(service_account: str = "") -> Any:
    """GcsStorageGateway on ADC, or on `service_account` impersonated with the cloud-platform scope."""
    _import_cctv_audit()
    from cctv_audit.gcs_gateway import GcsStorageGateway

    gw = GcsStorageGateway()
    if service_account:
        from google.auth import impersonated_credentials

        source_creds = _get_source_cloud_platform_credentials()
        gw._creds = impersonated_credentials.Credentials(
            source_credentials=source_creds,
            target_principal=service_account,
            target_scopes=[_CLOUD_PLATFORM_SCOPE],
            lifetime=3600,
        )
    return gw


def _get_source_cloud_platform_credentials() -> Any:
    """Return cloud-platform source credentials from GOOGLE_OAUTH_ACCESS_TOKEN, ADC, or gcloud CLI."""
    import subprocess
    import google.auth
    from google.auth.transport.requests import Request
    from google.oauth2 import credentials as oauth2_credentials

    env_token = os.environ.get("GOOGLE_OAUTH_ACCESS_TOKEN", "").strip()
    if env_token:
        return oauth2_credentials.Credentials(token=env_token)

    try:
        creds, _ = google.auth.default(scopes=[_CLOUD_PLATFORM_SCOPE])
        strip = getattr(creds, "with_quota_project", None)
        if callable(strip):
            creds = strip(None)
        if not creds.valid:
            creds.refresh(Request())
        return creds
    except Exception:
        res = subprocess.run(
            ["gcloud", "auth", "print-access-token"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        token = (res.stdout or "").strip()
        if res.returncode == 0 and token:
            return oauth2_credentials.Credentials(token=token)
        raise


def enable_versioning(gw: Any, gcs_uri: str) -> bool:
    """Best-effort Object Versioning on the SOP bucket so every re-upload keeps the previous version."""
    _import_cctv_audit()
    from cctv_audit.gcs_gateway import parse_gcs_uri

    bucket, _ = parse_gcs_uri(gcs_uri)
    status = gw.enable_bucket_versioning(bucket)
    if status < 400:
        print(f"object versioning: enabled on gs://{bucket}")
        return True
    print(
        f"note: could not enable object versioning on gs://{bucket} (HTTP {status}; needs storage.buckets.update). "
        f"Ask a bucket admin to run: gcloud storage buckets update gs://{bucket} --versioning",
        file=sys.stderr,
    )
    return False


def upload_to_gcs(gw: Any, gcs_uri: str, tabs: list[dict[str, Any]]) -> tuple[str, str]:
    """Uploads the snapshot as `.xlsx`, reads that generation back and verifies every tab and cell.

    Returns (Console URL, uploaded object generation or '').
    """
    _import_cctv_audit()
    from cctv_audit.gcs_gateway import XLSX_CONTENT_TYPE, read_xlsx_sheets

    console_url, generation = gw.upload_object(gcs_uri, build_xlsx_bytes(tabs), XLSX_CONTENT_TYPE)
    got = read_xlsx_sheets(gw.download_object_bytes(f"{gcs_uri}#{generation}" if generation else gcs_uri))
    for tab in tabs:
        if tab["title"] not in got:
            raise SystemExit(f"read-back from {gcs_uri}: tab {tab['title']!r} missing")
        if _normalize_rows(got[tab["title"]]) != _normalize_rows(tab["values"]):
            raise SystemExit(f"read-back mismatch in tab {tab['title']!r} of {gcs_uri}")
    return console_url, generation


def build_sheets_credentials(
    service_account: str = "",
    impersonate_user: str = "",
    scopes: tuple[str, ...] = (_SHEETS_SCOPE,),
    strict_dwd: bool = False,
) -> Any:
    """Build Sheets/Drive API credentials.

    When `service_account` is given, uses standard `cloud-platform` ADC (or `gcloud auth print-access-token`,
    avoiding Google's OAuth block on `--scopes=spreadsheets`) to impersonate `service_account` via IAM
    Credentials API. If `impersonate_user` is also set, attempts Domain-Wide Delegation
    (`subject=impersonate_user`) first and falls back to direct service-account impersonation if DWD is
    not yet active (unless `strict_dwd=True`).
    """
    import google.auth
    from google.auth import impersonated_credentials
    from google.auth.transport.requests import Request

    if not service_account:
        creds, _ = google.auth.default(scopes=list(scopes))
        return creds

    source_creds = _get_source_cloud_platform_credentials()
    if impersonate_user:
        dwd_creds = impersonated_credentials.Credentials(
            source_credentials=source_creds,
            target_principal=service_account,
            target_scopes=list(scopes),
            subject=impersonate_user,
            lifetime=3600,
        )
        try:
            dwd_creds.refresh(Request())
            return dwd_creds
        except Exception as exc:
            if strict_dwd:
                raise
            print(
                f"note: DWD refresh as {impersonate_user} not ready ({exc}); "
                f"falling back to direct service account {service_account} (share the Sheet with {service_account} as Editor).",
                file=sys.stderr,
            )

    sa_creds = impersonated_credentials.Credentials(
        source_credentials=source_creds,
        target_principal=service_account,
        target_scopes=list(scopes),
        lifetime=3600,
    )
    sa_creds.refresh(Request())
    return sa_creds


def _lookup_sa_unique_id(service_account: str) -> str:
    """Best-effort lookup of the numeric OAuth2 Client ID (`uniqueId`) of `service_account`."""
    if not service_account or "@" not in service_account:
        return "<workspace_dwd_client_id>"
    project_id = service_account.split("@", 1)[1].split(".iam.gserviceaccount.com", 1)[0]
    try:
        import urllib.request

        source_creds = _get_source_cloud_platform_credentials()
        url = f"https://iam.googleapis.com/v1/projects/{project_id}/serviceAccounts/{service_account}"
        req = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {source_creds.token}"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            uid = str(data.get("uniqueId", "")).strip()
            if uid:
                return uid
    except Exception:
        pass
    return "<workspace_dwd_client_id>"


def check_dwd_status(service_account: str, impersonate_user: str) -> dict[str, Any]:
    """Verify keyless Domain-Wide Delegation for `(service_account, impersonate_user)` against Drive & Sheets.

    Returns a summary dict with `client_id`, `service_account`, `impersonate_user`, `user_email`,
    `storage_limit_bytes`, `storage_usage_bytes` on success, or raises `RuntimeError` with an
    operator-actionable remediation guide on failure.
    """
    if not service_account:
        raise RuntimeError(
            "Missing worker service account (`--service-account` or `project_id` + `name_prefix` in `--tfvars`)."
        )
    if not impersonate_user:
        raise RuntimeError(
            "Missing `workspace_impersonate_user` (`--impersonate-user` or `workspace_impersonate_user` in `--tfvars`)."
        )
    client_id = _lookup_sa_unique_id(service_account)
    try:
        creds = build_sheets_credentials(
            service_account=service_account,
            impersonate_user=impersonate_user,
            scopes=_DWD_SCOPES,
            strict_dwd=True,
        )
        from googleapiclient.discovery import build

        drive_svc = build("drive", "v3", credentials=creds, cache_discovery=False)
        about = drive_svc.about().get(fields="user(emailAddress,displayName),storageQuota").execute()
    except Exception as exc:
        raw = str(exc)
        if "unauthorized_client" in raw:
            raise RuntimeError(
                f"❌ Domain-Wide Delegation (DWD) is NOT yet active for {service_account} (Client ID: {client_id}).\n"
                "Remediation (Workspace Super Admin in https://admin.google.com ):\n"
                "  1. Go to Security -> Access and data control -> API controls -> Manage Domain Wide Delegation -> Add new\n"
                f"  2. Client ID:    {client_id}\n"
                f"  3. OAuth Scopes: {_ALL_RECOMMENDED_DWD_SCOPES}\n"
                f"  (Raw error: {raw})"
            ) from exc
        if "invalid_grant" in raw:
            raise RuntimeError(
                f"❌ Impersonated Workspace bot user {impersonate_user!r} cannot be used (`invalid_grant`).\n"
                "Remediation:\n"
                f"  1. Verify that user {impersonate_user!r} exists in https://admin.google.com -> Directory -> Users, is not suspended,\n"
                "     and has an active Google Workspace license with Google Drive storage.\n"
                "  2. Or update `workspace_impersonate_user` in `<env>.tfvars` to the actual bot email you created.\n"
                f"  (Raw error: {raw})"
            ) from exc
        if "signBlob" in raw or "signJwt" in raw or "PERMISSION_DENIED" in raw:
            raise RuntimeError(
                f"❌ Caller lacks `roles/iam.serviceAccountTokenCreator` on {service_account} to sign DWD JWTs.\n"
                "Remediation (Project Owner):\n"
                f"  gcloud iam service-accounts add-iam-policy-binding {service_account} \\\n"
                '    --member="user:$(gcloud config get-value account)" \\\n'
                '    --role="roles/iam.serviceAccountTokenCreator"\n'
                f"  (Raw error: {raw})"
            ) from exc
        raise RuntimeError(f"❌ DWD verification failed for {impersonate_user} via {service_account}: {raw}") from exc

    quota = about.get("storageQuota") or {}
    limit_bytes = int(quota.get("limit", 0) or 0)
    usage_bytes = int(quota.get("usage", 0) or 0)
    user_info = about.get("user") or {}
    if quota.get("limit") is not None and limit_bytes <= 0:
        raise RuntimeError(
            f"❌ DWD token exchange succeeded for {impersonate_user}, but Drive storage quota is 0 bytes.\n"
            f"Remediation: Assign a paid Google Workspace license with Drive storage to {impersonate_user} in admin.google.com."
        )
    return {
        "client_id": client_id,
        "service_account": service_account,
        "impersonate_user": impersonate_user,
        "user_email": user_info.get("emailAddress", impersonate_user),
        "display_name": user_info.get("displayName", ""),
        "storage_limit_bytes": limit_bytes,
        "storage_usage_bytes": usage_bytes,
    }


def create_master_spreadsheet(
    creds: Any,
    title: str = DEFAULT_SHEET_TITLE,
    share_with: list[str] | None = None,
) -> str:
    """Create a brand-new Google Sheet in the authenticated identity's Drive and optionally share it."""
    from googleapiclient.discovery import build

    sheets_svc = build("sheets", "v4", credentials=creds, cache_discovery=False)
    created = sheets_svc.spreadsheets().create(
        body={"properties": {"title": title}},
        fields="spreadsheetId",
    ).execute()
    sheet_id = str(created["spreadsheetId"]).strip()

    if share_with:
        drive_svc = build("drive", "v3", credentials=creds, cache_discovery=False)
        for raw_target in share_with:
            target = (raw_target or "").strip()
            if not target:
                continue
            if target.startswith("domain:"):
                perm_body = {
                    "type": "domain",
                    "role": "writer",
                    "domain": target.split(":", 1)[1].strip(),
                }
            elif target.startswith("group:"):
                perm_body = {
                    "type": "group",
                    "role": "writer",
                    "emailAddress": target.split(":", 1)[1].strip(),
                }
            else:
                email = target.split(":", 1)[1].strip() if target.startswith("user:") else target
                perm_body = {
                    "type": "user",
                    "role": "writer",
                    "emailAddress": email,
                }
            drive_svc.permissions().create(
                fileId=sheet_id,
                body=perm_body,
                sendNotificationEmail=False,
                supportsAllDrives=True,
            ).execute()
            print(f"shared SOP Sheet {sheet_id} with {target} (writer)")
    return sheet_id


def update_tfvars_sheet_id(tfvars_path: Path, sheet_id: str, impersonate_user: str = "") -> None:
    """Update `master_prompt_sheet_id` (and optionally `workspace_impersonate_user`) in-place in `tfvars_path`."""
    text = tfvars_path.read_text(encoding="utf-8")
    updated, count = re.subn(
        r'^(\s*master_prompt_sheet_id\s*=\s*)"[^"]*"',
        rf'\g<1>"{sheet_id}"',
        text,
        count=1,
        flags=re.MULTILINE,
    )
    if count == 0:
        updated = updated.rstrip() + f'\nmaster_prompt_sheet_id = "{sheet_id}"\n'
    if impersonate_user:
        updated, user_count = re.subn(
            r'^(\s*workspace_impersonate_user\s*=\s*)"[^"]*"',
            rf'\g<1>"{impersonate_user}"',
            updated,
            count=1,
            flags=re.MULTILINE,
        )
        if user_count == 0:
            updated = updated.rstrip() + f'\nworkspace_impersonate_user = "{impersonate_user}"\n'
    tfvars_path.write_text(updated, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sheet-id", default="", help="Target Google Sheet ID or URL (create it in the browser first)")
    parser.add_argument("--gcs-uri", default="", help="Zero-GWS: upload the snapshot as .xlsx to gs://<bucket>/<path>.xlsx (or a Console URL of it) instead of a Google Sheet")
    parser.add_argument(
        "--enable-versioning",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="With --gcs-uri: best-effort enable Object Versioning on the bucket (default on; --no-enable-versioning to skip)",
    )
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT, help=f"default: {DEFAULT_SNAPSHOT}")
    parser.add_argument("--export-xlsx", type=Path, default=None, help="Export snapshot to a multi-tab .xlsx file for browser File -> Import (no OAuth needed)")
    parser.add_argument("--local-only", action="store_true", help="Regenerate sop/master_sheet.json and sop/master_sheet.xlsx locally from prompt_manager constants without calling Google APIs")
    parser.add_argument("--tfvars", type=Path, default=None, help="Optional <env>.tfvars to auto-derive worker service account and workspace_impersonate_user")
    parser.add_argument("--service-account", default="", help="Worker service account email to impersonate via IAM Credentials API (uses plain cloud-platform ADC)")
    parser.add_argument("--impersonate-user", default="", help="Optional Workspace bot email for DWD subject")
    parser.add_argument("--check-dwd", action="store_true", help="Verify that Domain-Wide Delegation (DWD) is active and the bot user has Google Drive storage quota")
    parser.add_argument("--create-sheet", action="store_true", help="Automatically create a new Google Sheet in the bot user's Google Drive via DWD and populate all SOP tabs")
    parser.add_argument("--sheet-title", default=DEFAULT_SHEET_TITLE, help=f"Title for --create-sheet (default: {DEFAULT_SHEET_TITLE!r})")
    parser.add_argument("--share-with", action="append", default=[], help="Email, group:<email>, or domain:<domain> to share the newly created Sheet with as Editor (repeatable)")
    parser.add_argument("--update-tfvars", action="store_true", help="Write the created/target Sheet ID (and --impersonate-user if passed) into --tfvars master_prompt_sheet_id")
    parser.add_argument("--overwrite", action="store_true", help="Clear and rewrite tabs whose titles already exist")
    parser.add_argument("--dry-run", action="store_true", help="Validate the snapshot only; do not call the API")
    args = parser.parse_args(argv)

    if args.local_only:
        xlsx_target = args.export_xlsx or DEFAULT_XLSX
        payload = write_local_artifacts(args.snapshot, xlsx_target)
        tabs = payload["tabs"]
        print(f"local artifacts written: {args.snapshot}, {xlsx_target} ({[(t['title'], len(t['values']) - 1) for t in tabs]})")
        return 0

    tabs = load_snapshot(args.snapshot)
    print(f"snapshot OK: {[(t['title'], len(t['values']) - 1) for t in tabs]} (rows excl. header)")

    if args.check_dwd:
        sa_email, bot_user = resolve_impersonation_targets(args.tfvars, args.service_account, args.impersonate_user)
        try:
            dwd_info = check_dwd_status(sa_email, bot_user)
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        limit_gb = dwd_info["storage_limit_bytes"] / (1024 ** 3) if dwd_info["storage_limit_bytes"] else 0.0
        usage_mb = dwd_info["storage_usage_bytes"] / (1024 ** 2) if dwd_info["storage_usage_bytes"] else 0.0
        print(
            f"✅ DWD PASSED: {dwd_info['service_account']} (Client ID: {dwd_info['client_id']}) "
            f"-> {dwd_info['user_email']} (Drive quota: {usage_mb:.1f} MB used / {limit_gb:.1f} GB limit)"
        )
        if not args.create_sheet and not args.sheet_id and not args.gcs_uri and args.export_xlsx is None:
            return 0

    gcs_uri = resolve_gcs_sop_uri(args.gcs_uri) if args.gcs_uri else resolve_gcs_sop_uri(args.sheet_id)
    if args.gcs_uri and not gcs_uri:
        parser.error(f"--gcs-uri is not a gs:// URI or Cloud Storage URL: {args.gcs_uri!r}")

    if args.export_xlsx is not None:
        export_xlsx(tabs, args.export_xlsx)
        print(f"exported xlsx: {args.export_xlsx}")
        if not args.sheet_id and not gcs_uri and not args.create_sheet:
            return 0

    if args.dry_run:
        if args.sheet_id and not gcs_uri:
            extract_sheet_id(args.sheet_id)
        return 0

    if gcs_uri:
        live_uri = gcs_uri.split("#", 1)[0]  # always upload the live object, never a pinned generation
        sa_email, _ = resolve_impersonation_targets(args.tfvars, args.service_account, "")
        gw = make_gcs_gateway(sa_email)
        if args.enable_versioning:
            enable_versioning(gw, live_uri)
        console_url, generation = upload_to_gcs(gw, live_uri, tabs)
        print(f"done: {live_uri} (read-back verified)")
        print(f"console: {console_url}")
        print(f"live URI (tracks the latest version): {live_uri}")
        print("  roll back anytime: Cloud Console -> bucket -> master_sheet.xlsx -> Version history -> Restore")
        if generation:
            print(f"pinned URI (this exact version, immutable): {live_uri}#{generation}")
        print(f'next: set master_prompt_sheet_id = "{live_uri}" in <env>.tfvars and grant the worker service account '
              "roles/storage.objectViewer (or objectAdmin) on the bucket; later edits only need re-uploading the .xlsx")
        return 0

    sa_email, bot_user = resolve_impersonation_targets(args.tfvars, args.service_account, args.impersonate_user)

    from googleapiclient.discovery import build

    if args.create_sheet and not args.sheet_id:
        # Check if --tfvars already has a valid Google Sheet ID (not empty and not gs://)
        existing_sheet_in_tfvars = ""
        if args.tfvars is not None and args.tfvars.exists():
            raw_tfv_sheet = parse_tfvars(args.tfvars).get("master_prompt_sheet_id", "").strip()
            if raw_tfv_sheet and not resolve_gcs_sop_uri(raw_tfv_sheet):
                try:
                    existing_sheet_in_tfvars = extract_sheet_id(raw_tfv_sheet)
                except ValueError:
                    existing_sheet_in_tfvars = ""
        if existing_sheet_in_tfvars:
            sheet_id = existing_sheet_in_tfvars
            print(f"using existing Google Sheet ID from {args.tfvars}: {sheet_id}")
            creds = build_sheets_credentials(
                service_account=sa_email,
                impersonate_user=bot_user,
                scopes=_DWD_SCOPES,
            )
            overwrite_tabs = True
        else:
            if not bot_user:
                parser.error("--create-sheet requires workspace_impersonate_user (via --tfvars or --impersonate-user)")
            creds = build_sheets_credentials(
                service_account=sa_email,
                impersonate_user=bot_user,
                scopes=_DWD_SCOPES,
                strict_dwd=True,
            )
            sheet_id = create_master_spreadsheet(
                creds,
                title=args.sheet_title,
                share_with=args.share_with,
            )
            print(f"created new SOP Master Google Sheet in {bot_user}'s Drive: {sheet_id}")
            overwrite_tabs = True
    else:
        if not args.sheet_id:
            parser.error("either --sheet-id, --create-sheet, --check-dwd, or --export-xlsx (or --dry-run / --local-only) is required")
        sheet_id = extract_sheet_id(args.sheet_id)
        creds = build_sheets_credentials(service_account=sa_email, impersonate_user=bot_user)
        overwrite_tabs = args.overwrite

    service = build("sheets", "v4", credentials=creds, cache_discovery=False)
    write_tabs(service, sheet_id, tabs, overwrite_tabs)
    verify(service, sheet_id, tabs)
    if args.update_tfvars and args.tfvars is not None:
        update_tfvars_sheet_id(args.tfvars, sheet_id, impersonate_user=args.impersonate_user)
        print(f"updated {args.tfvars}: master_prompt_sheet_id = \"{sheet_id}\"")
    print(f"done: https://docs.google.com/spreadsheets/d/{sheet_id}/edit")
    if not (args.update_tfvars and args.tfvars is not None):
        print("next: share it with the bot account and set master_prompt_sheet_id in <env>.tfvars")
    return 0


if __name__ == "__main__":
    sys.exit(main())
