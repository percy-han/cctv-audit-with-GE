"""Freeze the customer's Manual Audit Result into a versioned golden dataset.

Input
  * ``--manual-json``: the raw 2-D array of the ``Manual Audit Result`` sheet
    (``gsheets readonly read <id> 'Sheet1!A1:F40' --json``). Row 0 is the header.
  * ``--job``: one or more GCS job JSONs of an audit run. Their
    ``preflight_report.videos`` list is the authoritative Drive listing
    (filename -> file_id) of the four validation folders.

Output
  * ``--out``: JSONL, one line per labelled finding (19 lines expected).

Rules
  * Merged cells (Focus / Outlet / Outlet Name / Audit Clause) are forward-filled.
  * Every labelled video filename must exist verbatim in the Drive listing;
    otherwise the build fails. There is no fuzzy matching.
  * Split (decided by the user on 2026-09-28): the "dev" (open-book) set is
    Handwashing x Cantavil D2 plus Ice Maker x Bau Cat; everything else is
    "holdout".
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import re
import sys
from typing import Any

SOURCE_SHEET_ID = "<manual-audit-sheet-id>"  # ID of your manual audit result Sheet (recorded for provenance)
EXPECTED_ROWS = 19
DEV_GROUPS = {
    ("Handwashing Monitoring", "Cantavil D2"),
    ("Ice Maker Weekly Cleaning", "Bau Cat"),
}
# A sheet row holding two independent violations is scored per part
# (both caught = 1.0, one caught = 0.5). Two-timestamp rows are split
# automatically; rows with one timestamp but two distinct issues are listed
# here by sheet row number, with the customer's own words for each part.
TEXT_COMPOUND_PARTS: dict[int, list[str]] = {
    18: [
        "After spray sanitiser at the white cover, do not wait for 5 minutes "
        "contact time before cleaning and installing back.",
        "Dot not wipe the sides of white cover thoroughly",
    ],
}
_OSD_RE = re.compile(r"(?<!\d)(\d{2})(\d{2})(\d{2})(?!\d)")


@dataclasses.dataclass
class Part:
    part_id: str
    description: str
    osd_times: list[str]


@dataclasses.dataclass
class GoldenItem:
    item_id: str
    sheet_row: int
    focus: str
    outlet_no: str
    outlet_name: str
    audit_clause: str
    finding_verbatim: str
    video_filenames: list[str]
    video_file_ids: list[str]
    parts: list[Part]
    split: str


def parse_osd_times(text: str) -> list[str]:
    """Return every 6-digit HHMMSS token in ``text`` as ``HH:MM:SS``."""
    out = []
    for hh, mm, ss in _OSD_RE.findall(text):
        if int(hh) < 24 and int(mm) < 60 and int(ss) < 60:
            out.append(f"{hh}:{mm}:{ss}")
    return out


def _cell(row: list[str], i: int) -> str:
    return row[i].strip() if i < len(row) and row[i] is not None else ""


def build_filename_index(jobs: list[dict[str, Any]]) -> dict[str, str]:
    index: dict[str, str] = {}
    for job in jobs:
        for v in (job.get("preflight_report") or {}).get("videos") or []:
            name, fid = v.get("filename"), v.get("file_id")
            if not name or not fid:
                continue
            if name in index and index[name] != fid:
                raise ValueError(f"filename {name!r} maps to two Drive IDs")
            index[name] = fid
    return index


def build_items(
    raw: list[list[str]], filename_index: dict[str, str]
) -> list[GoldenItem]:
    if not raw or _cell(raw[0], 4) != "Findings":
        raise ValueError("unexpected header row in manual audit sheet")
    items: list[GoldenItem] = []
    carry = ["", "", "", ""]
    missing: list[str] = []
    for offset, row in enumerate(raw[1:]):
        sheet_row = offset + 2
        finding = _cell(row, 4)
        if not finding:
            continue
        for i in range(4):
            if _cell(row, i):
                carry[i] = _cell(row, i)
        focus, outlet_no, outlet_name, clause = carry
        files = [f.strip() for f in _cell(row, 5).split(",") if f.strip()]
        if not files:
            raise ValueError(f"sheet row {sheet_row}: no video filename")
        ids = []
        for f in files:
            if f not in filename_index:
                missing.append(f"row {sheet_row}: {f}")
            ids.append(filename_index.get(f, ""))
        times = parse_osd_times(finding)
        item_id = f"R{sheet_row:02d}"
        if sheet_row in TEXT_COMPOUND_PARTS:
            parts = [
                Part(f"{item_id}{chr(97 + k)}", desc, times)
                for k, desc in enumerate(TEXT_COMPOUND_PARTS[sheet_row])
            ]
        elif len(times) > 1:
            parts = [
                Part(f"{item_id}{chr(97 + k)}", finding, [t])
                for k, t in enumerate(times)
            ]
        else:
            parts = [Part(item_id, finding, times)]
        split = "dev" if (focus, outlet_name) in DEV_GROUPS else "holdout"
        items.append(
            GoldenItem(
                item_id=item_id,
                sheet_row=sheet_row,
                focus=focus,
                outlet_no=outlet_no,
                outlet_name=outlet_name,
                audit_clause=clause,
                finding_verbatim=finding,
                video_filenames=files,
                video_file_ids=ids,
                parts=parts,
                split=split,
            )
        )
    if missing:
        raise ValueError(
            "labelled video(s) not found in the Drive listing:\n  "
            + "\n  ".join(missing)
        )
    return items


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--manual-json", required=True)
    ap.add_argument("--job", action="append", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--source-sheet-id", default=SOURCE_SHEET_ID,
                    help="ID of the manual audit result Sheet, recorded in every row for provenance")
    args = ap.parse_args(argv)

    with open(args.manual_json, encoding="utf-8") as fh:
        raw_text = fh.read()
    raw = json.loads(raw_text)
    jobs = []
    for path in args.job:
        with open(path, encoding="utf-8") as fh:
            jobs.append(json.load(fh))
    items = build_items(raw, build_filename_index(jobs))
    if len(items) != EXPECTED_ROWS:
        print(f"expected {EXPECTED_ROWS} rows, got {len(items)}", file=sys.stderr)
        return 1
    source_sha = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
    with open(args.out, "w", encoding="utf-8") as fh:
        for it in items:
            rec = dataclasses.asdict(it)
            rec["source_sheet_id"] = args.source_sheet_id
            rec["source_sha256"] = source_sha
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    dev = sum(1 for i in items if i.split == "dev")
    print(f"wrote {len(items)} items ({dev} dev / {len(items) - dev} holdout) -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
