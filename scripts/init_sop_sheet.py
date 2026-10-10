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
TAB0_TITLE = "Tab0_版本总控与回滚开关"
_SHEET_URL_RE = re.compile(r"/spreadsheets/d/([A-Za-z0-9_-]+)")
_TFVAR_LINE_RE = re.compile(r'^\s*([a-zA-Z0-9_]+)\s*=\s*"([^"]*)"')
_SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
_CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"


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
    if TAB0_TITLE not in titles:
        raise ValueError(f"{path}: missing pointer tab {TAB0_TITLE!r}")
    if len(set(titles)) != len(titles):
        raise ValueError(f"{path}: duplicate tab titles")
    tab0 = next(t for t in tabs if t["title"] == TAB0_TITLE)
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
        if tab["title"] != TAB0_TITLE and len(values[0]) != 9:
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
        import google.auth
        from google.auth import impersonated_credentials

        source_creds, _ = google.auth.default(scopes=[_CLOUD_PLATFORM_SCOPE])
        gw._creds = impersonated_credentials.Credentials(
            source_credentials=source_creds,
            target_principal=service_account,
            target_scopes=[_CLOUD_PLATFORM_SCOPE],
            lifetime=3600,
        )
    return gw


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
) -> Any:
    """Build Sheets API credentials.

    When `service_account` is given, uses standard `cloud-platform` ADC (from plain
    `gcloud auth application-default login`, avoiding Google's OAuth block on `--scopes=spreadsheets`)
    to impersonate `service_account` via IAM Credentials API. If `impersonate_user` is also set,
    attempts Domain-Wide Delegation (`subject=impersonate_user`) first and falls back to direct
    service-account impersonation if DWD is not yet active.
    """
    import google.auth
    from google.auth import impersonated_credentials
    from google.auth.transport.requests import Request

    if not service_account:
        creds, _ = google.auth.default(scopes=[_SHEETS_SCOPE])
        return creds

    source_creds, _ = google.auth.default(scopes=[_CLOUD_PLATFORM_SCOPE])
    if impersonate_user:
        dwd_creds = impersonated_credentials.Credentials(
            source_credentials=source_creds,
            target_principal=service_account,
            target_scopes=[_SHEETS_SCOPE],
            subject=impersonate_user,
            lifetime=3600,
        )
        try:
            dwd_creds.refresh(Request())
            return dwd_creds
        except Exception as exc:
            print(
                f"note: DWD refresh as {impersonate_user} not ready ({exc}); "
                f"falling back to direct service account {service_account} (share the Sheet with {service_account} as Editor).",
                file=sys.stderr,
            )

    sa_creds = impersonated_credentials.Credentials(
        source_credentials=source_creds,
        target_principal=service_account,
        target_scopes=[_SHEETS_SCOPE],
        lifetime=3600,
    )
    sa_creds.refresh(Request())
    return sa_creds


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
    parser.add_argument("--tfvars", type=Path, default=None, help="Optional <env>.tfvars to auto-derive worker service account and workspace_impersonate_user")
    parser.add_argument("--service-account", default="", help="Worker service account email to impersonate via IAM Credentials API (uses plain cloud-platform ADC)")
    parser.add_argument("--impersonate-user", default="", help="Optional Workspace bot email for DWD subject")
    parser.add_argument("--overwrite", action="store_true", help="Clear and rewrite tabs whose titles already exist")
    parser.add_argument("--dry-run", action="store_true", help="Validate the snapshot only; do not call the API")
    args = parser.parse_args(argv)

    tabs = load_snapshot(args.snapshot)
    print(f"snapshot OK: {[(t['title'], len(t['values']) - 1) for t in tabs]} (rows excl. header)")

    gcs_uri = resolve_gcs_sop_uri(args.gcs_uri) if args.gcs_uri else resolve_gcs_sop_uri(args.sheet_id)
    if args.gcs_uri and not gcs_uri:
        parser.error(f"--gcs-uri is not a gs:// URI or Cloud Storage URL: {args.gcs_uri!r}")

    if args.export_xlsx is not None:
        export_xlsx(tabs, args.export_xlsx)
        print(f"exported xlsx: {args.export_xlsx}")
        if not args.sheet_id and not gcs_uri:
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
        print("  roll back anytime: Cloud Console -> bucket -> master_sheet.xlsx -> Version history (版本历史记录) -> Restore (恢复)")
        if generation:
            print(f"pinned URI (this exact version, immutable): {live_uri}#{generation}")
        print(f'next: set master_prompt_sheet_id = "{live_uri}" in <env>.tfvars and grant the worker service account '
              "roles/storage.objectViewer (or objectAdmin) on the bucket; later edits only need re-uploading the .xlsx")
        return 0

    if not args.sheet_id:
        parser.error("either --sheet-id or --export-xlsx (or --dry-run) is required")

    sheet_id = extract_sheet_id(args.sheet_id)
    sa_email, bot_user = resolve_impersonation_targets(args.tfvars, args.service_account, args.impersonate_user)

    from googleapiclient.discovery import build

    creds = build_sheets_credentials(service_account=sa_email, impersonate_user=bot_user)
    service = build("sheets", "v4", credentials=creds, cache_discovery=False)
    write_tabs(service, sheet_id, tabs, args.overwrite)
    verify(service, sheet_id, tabs)
    print(f"done: https://docs.google.com/spreadsheets/d/{sheet_id}/edit")
    print("next: share it with the bot account and set master_prompt_sheet_id in <env>.tfvars")
    return 0


if __name__ == "__main__":
    sys.exit(main())
