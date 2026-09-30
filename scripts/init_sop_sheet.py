#!/usr/bin/env python3
"""Load the SOP Master Sheet snapshot (sop/master_sheet.json) into a Google Sheet.

The audit service reads its Layer 2 SOP rules at run time from the Google Sheet named by
Terraform variable `master_prompt_sheet_id`:
  * tab `Tab0_版本总控与回滚开关` holds the pointers (row `Active_Prompt_Version` = name of the
    rules tab to use, `Active_Model_Version`, `Fallback_Model_Version`);
  * the rules tab holds one SOP rule per row (9 columns, header in row 1).
A Sheet is a Drive file, not a GCP resource, so Terraform cannot create it. Run this once per
environment.

Recommended (no extra OAuth scopes needed):
  1. In the browser, create an empty Google Sheet (owned by you or by the bot account).
  2. Share it with the bot account (`workspace_impersonate_user`) as Viewer (Editor if you want
     the service to append newly seen model names to Tab0).
  3. Run, with credentials that can edit that Sheet:
       gcloud auth application-default login \\
         --scopes=https://www.googleapis.com/auth/spreadsheets,https://www.googleapis.com/auth/cloud-platform
       python3 scripts/init_sop_sheet.py --sheet-id <SHEET_ID_OR_URL>
  4. Put the Sheet ID into `<env>.tfvars` -> `master_prompt_sheet_id`, then deploy.

The target Sheet must not already contain tabs with the same titles unless --overwrite is given
(then those tabs are cleared and rewritten). Other tabs are left untouched. Values are written
with valueInputOption=RAW so rule text that starts with '=' or '+' is never parsed as a formula.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

DEFAULT_SNAPSHOT = Path(__file__).resolve().parent.parent / "sop" / "master_sheet.json"
TAB0_TITLE = "Tab0_版本总控与回滚开关"
_SHEET_URL_RE = re.compile(r"/spreadsheets/d/([A-Za-z0-9_-]+)")


def extract_sheet_id(value: str) -> str:
    """Accept a bare Sheet ID or a docs.google.com/spreadsheets URL; reject Drive folder links."""
    value = (value or "").strip()
    match = _SHEET_URL_RE.search(value)
    if match:
        return match.group(1)
    if "/" in value or not re.fullmatch(r"[A-Za-z0-9_-]{20,}", value):
        raise ValueError(f"not a Google Sheet ID or Sheet URL: {value!r}")
    return value


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sheet-id", required=True, help="Target Google Sheet ID or URL (create it in the browser first)")
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT, help=f"default: {DEFAULT_SNAPSHOT}")
    parser.add_argument("--overwrite", action="store_true", help="Clear and rewrite tabs whose titles already exist")
    parser.add_argument("--dry-run", action="store_true", help="Validate the snapshot only; do not call the API")
    args = parser.parse_args(argv)

    tabs = load_snapshot(args.snapshot)
    sheet_id = extract_sheet_id(args.sheet_id)
    print(f"snapshot OK: {[(t['title'], len(t['values']) - 1) for t in tabs]} (rows excl. header)")
    if args.dry_run:
        return 0

    import google.auth
    from googleapiclient.discovery import build

    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/spreadsheets"])
    service = build("sheets", "v4", credentials=creds, cache_discovery=False)
    write_tabs(service, sheet_id, tabs, args.overwrite)
    verify(service, sheet_id, tabs)
    print(f"done: https://docs.google.com/spreadsheets/d/{sheet_id}/edit")
    print("next: share it with the bot account and set master_prompt_sheet_id in <env>.tfvars")
    return 0


if __name__ == "__main__":
    sys.exit(main())
