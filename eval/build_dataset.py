"""Freeze the customer's Manual Audit Result into a versioned golden dataset.

Input
  * ``--manual-json``: the raw 2-D array of the ``Manual Audit Result`` sheet
    (``gsheets readonly read <id> 'Sheet1!A1:F40' --json``). Row 0 is the header.
  * ``--job``: one or more GCS job JSONs of an audit run. Their
    ``preflight_report.videos`` list is the authoritative Drive listing
    (filename -> file_id) of the four validation folders.

  * ``--config``: OPTIONAL build config JSON for this label sheet (``eval/data/golden_v1.build.json``
    reproduces golden_v1). Keys, all optional: ``source_sheet_id``, ``expected_rows``,
    ``dev_groups`` (list of ``[focus, outlet_name]``; every other row is "holdout"),
    ``text_compound_parts`` (``{sheet_row: [part description, ...]}`` for one-timestamp rows that
    hold several violations), ``item_fields`` (``{item_id: {"sop_category": ..., "temporal_mode":
    "POINT"|"WINDOW", "stable_baseline": true, "drive_folder_id": ...}}`` copied into the output).
    ``--source-sheet-id`` / ``--expected-rows`` / ``--dev-group "focus|outlet"`` override it.

Output
  * ``--out``: JSONL, one line per labelled finding (schema: ``eval/data/README.md``).

Rules
  * Merged cells (Focus / Outlet / Outlet Name / Audit Clause) are forward-filled.
  * Every labelled video filename must exist verbatim in the Drive listing;
    otherwise the build fails. There is no fuzzy matching.
  * No label content (row counts, splits, compound rows, sheet IDs) is coded here: it comes from the
    config / flags, so a rebuilt customer label sheet needs a new config, not a code change.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import re
import sys
from typing import Any

# A sheet row holding two independent violations is scored per part (both caught = 1.0, one caught =
# 0.5). Two-timestamp rows are split automatically; rows with one timestamp but two distinct issues
# come from the build config's ``text_compound_parts`` (sheet row -> the customer's words per part).
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
    raw: list[list[str]],
    filename_index: dict[str, str],
    *,
    dev_groups: set[tuple[str, str]] | frozenset[tuple[str, str]] = frozenset(),
    text_compound_parts: dict[int, list[str]] | None = None,
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
        compound = (text_compound_parts or {}).get(sheet_row)
        if compound:
            parts = [
                Part(f"{item_id}{chr(97 + k)}", desc, times)
                for k, desc in enumerate(compound)
            ]
        elif len(times) > 1:
            parts = [
                Part(f"{item_id}{chr(97 + k)}", finding, [t])
                for k, t in enumerate(times)
            ]
        else:
            parts = [Part(item_id, finding, times)]
        split = "dev" if (focus, outlet_name) in dev_groups else "holdout"
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
    ap.add_argument("--config", default=None, help="build config JSON (see module docstring)")
    ap.add_argument("--source-sheet-id", default=None, help="label sheet ID recorded in every line")
    ap.add_argument("--expected-rows", type=int, default=None, help="fail unless exactly N labelled rows")
    ap.add_argument("--dev-group", action="append", default=None, metavar="FOCUS|OUTLET",
                    help="(focus, outlet) pair that goes to the dev split; repeatable")
    args = ap.parse_args(argv)
    cfg: dict[str, Any] = {}
    if args.config:
        with open(args.config, encoding="utf-8") as fh:
            cfg = json.load(fh)
    source_sheet_id = args.source_sheet_id or cfg.get("source_sheet_id") or ""
    expected_rows = args.expected_rows if args.expected_rows is not None else cfg.get("expected_rows")
    dev_pairs = [g.split("|", 1) for g in args.dev_group] if args.dev_group else cfg.get("dev_groups") or []
    dev_groups = {(str(f).strip(), str(o).strip()) for f, o in dev_pairs}
    compound = {int(k): list(v) for k, v in (cfg.get("text_compound_parts") or {}).items()}
    item_fields: dict[str, dict[str, Any]] = cfg.get("item_fields") or {}

    with open(args.manual_json, encoding="utf-8") as fh:
        raw_text = fh.read()
    raw = json.loads(raw_text)
    jobs = []
    for path in args.job:
        with open(path, encoding="utf-8") as fh:
            jobs.append(json.load(fh))
    items = build_items(raw, build_filename_index(jobs), dev_groups=dev_groups,
                        text_compound_parts=compound)
    if expected_rows is not None and len(items) != int(expected_rows):
        print(f"expected {expected_rows} rows, got {len(items)}", file=sys.stderr)
        return 1
    unknown = sorted(set(item_fields) - {i.item_id for i in items})
    if unknown:
        print(f"item_fields for unknown item_id(s): {unknown}", file=sys.stderr)
        return 1
    source_sha = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
    with open(args.out, "w", encoding="utf-8") as fh:
        for it in items:
            rec = dataclasses.asdict(it)
            rec.update(item_fields.get(it.item_id) or {})
            rec["source_sheet_id"] = source_sheet_id
            rec["source_sha256"] = source_sha
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    dev = sum(1 for i in items if i.split == "dev")
    print(f"wrote {len(items)} items ({dev} dev / {len(items) - dev} holdout) -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
