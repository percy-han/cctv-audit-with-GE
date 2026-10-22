#!/usr/bin/env python3
"""Golden set straight from the customer's label Sheet + an ``Eval_Config`` (or legacy ``测评配置``) tab.

The customer keeps maintaining their own "Manual Audit Result" label tab (never modified by code). One
extra tab, ``Eval_Config``, in the same spreadsheet says how to turn it into a golden set: golden name,
label range, the Drive folders holding the clips, the dev split, compound rows and optional per-item
settings. Each eval run reads both tabs live, lists every video in the configured folders (including
unlabelled clips, which count for alert density) and produces exactly the in-memory golden JSONL +
manifest that ``eval/golden_set.py`` consumes, so ``golden_version`` is computed the same way as for a
file. A config tab rendered from ``golden_v1.build.json`` + ``golden_v1.manifest.json`` over the
unchanged label tab reproduces the file's ruler version (``golden_v1@9426fe9b52``, scheme ruler-v2).

Config tab layout (see eval/data/README.md for a full example). Column A holds section markers
``[Basic Settings]`` / ``[Video Folders]`` / ``[Dev Split Groups]`` / ``[Compound Item Splits]`` /
``[Per-Item Settings]`` (legacy Chinese markers ``[基本设置]`` etc. are also accepted); the row after a
marker is that section's header row (checked); blank rows and rows whose column A starts with ``#``
are ignored.

CLI (run as the Workspace identity, e.g. in Cloud Build as the worker SA)::

    python -m eval.golden_sheet check --sheet-id <ID>
    python -m eval.golden_sheet init-config --sheet-id <ID> --from-build-config <x.build.json> \\
        --manifest <x.manifest.json> [--label-range 'Sheet1!A1:F40'] [--overwrite] [--dry-run]
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any, Callable, Mapping, Sequence

THIS_DIR = Path(__file__).resolve().parent
CODE_ROOT = THIS_DIR.parent
if str(CODE_ROOT) not in sys.path:
    sys.path.insert(0, str(CODE_ROOT))

from eval.build_dataset import GoldenBuildError, build_golden_records, records_to_jsonl  # noqa: E402
from eval.golden_set import VERSION_SCHEME, GoldenSet, GoldenSetError, build_golden  # noqa: E402

CONFIG_TAB_TITLE = "Eval_Config"
CONFIG_TAB = CONFIG_TAB_TITLE
LEGACY_CONFIG_TAB_TITLES = ("Eval_Config", "测评配置")
SEC_BASIC, SEC_FOLDERS, SEC_DEV, SEC_COMPOUND, SEC_ITEMS = (
    "Basic Settings",
    "Video Folders",
    "Dev Split Groups",
    "Compound Item Splits",
    "Per-Item Settings",
)
_SECTION_ALIASES = {
    SEC_BASIC: SEC_BASIC,
    "1. Basic Info": SEC_BASIC,
    "基本设置": SEC_BASIC,
    "一、基础信息": SEC_BASIC,
    SEC_FOLDERS: SEC_FOLDERS,
    "2. Folder -> Store Mapping": SEC_FOLDERS,
    "视频文件夹": SEC_FOLDERS,
    "二、目录名 -> 门店映射": SEC_FOLDERS,
    SEC_DEV: SEC_DEV,
    "3. Filename -> Clip Mapping": SEC_DEV,
    "开发集分组": SEC_DEV,
    "三、文件名 -> 切片映射": SEC_DEV,
    SEC_COMPOUND: SEC_COMPOUND,
    "5. Sub-Part Splits": SEC_COMPOUND,
    "复合题拆分": SEC_COMPOUND,
    "五、复合题子项拆分": SEC_COMPOUND,
    SEC_ITEMS: SEC_ITEMS,
    "4. Temporal Mode Overrides": SEC_ITEMS,
    "6. Excluded Rows": SEC_ITEMS,
    "逐题设置": SEC_ITEMS,
    "四、时间窗口模式覆盖": SEC_ITEMS,
    "六、排除行号": SEC_ITEMS,
}
HEADERS = {
    SEC_BASIC: ["Field", "Value"],
    SEC_FOLDERS: ["Group ID", "Store", "Audit Focus", "Drive Folder ID", "Cache Job IDs (optional, comma-separated)"],
    SEC_DEV: ["Audit Focus", "Store"],
    SEC_COMPOUND: ["Sheet Row", "Split Description 1", "Split Description 2"],  # more description columns allowed
    SEC_ITEMS: [
        "Item ID",
        "SOP Category (optional)",
        "Temporal Mode (POINT/WINDOW, optional)",
        "Stable Baseline (Yes/No, optional)",
    ],
}
LEGACY_HEADERS = {
    SEC_BASIC: ["项目", "值"],
    SEC_FOLDERS: ["分组ID", "门店", "稽核重点", "Drive 文件夹 ID", "缓存任务 ID（可空，逗号分隔）"],
    SEC_DEV: ["稽核重点", "门店"],
    SEC_COMPOUND: ["表格行号", "拆分描述 1", "拆分描述 2"],
    SEC_ITEMS: ["题号", "SOP 大类（可空）", "时间判定模式（POINT/WINDOW，可空）", "稳定基线题（是/否，可空）"],
}
KEY_NAME, KEY_RANGE, KEY_EXPECTED, KEY_NOTE, KEY_SOURCE = (
    "Golden Set Name",
    "Label Range",
    "Expected Item Count",
    "Notes",
    "Source Sheet ID",
  )
BASIC_KEYS = (KEY_NAME, KEY_RANGE, KEY_EXPECTED, KEY_NOTE, KEY_SOURCE)
_BASIC_KEY_ALIASES = {
    KEY_NAME: KEY_NAME,
    "golden_version": KEY_NAME,
    "黄金集名称": KEY_NAME,
    "黄金集版本号": KEY_NAME,
    KEY_RANGE: KEY_RANGE,
    "label_range": KEY_RANGE,
    "标注页范围": KEY_RANGE,
    KEY_EXPECTED: KEY_EXPECTED,
    "expected_rows": KEY_EXPECTED,
    "期望题数": KEY_EXPECTED,
    KEY_NOTE: KEY_NOTE,
    "source_description": KEY_NOTE,
    "备注": KEY_NOTE,
    "黄金集来源说明": KEY_NOTE,
    KEY_SOURCE: KEY_SOURCE,
    "source_sheet_id": KEY_SOURCE,
    "来源表 ID": KEY_SOURCE,
}
YES, NO = {"是", "yes", "y", "true", "1"}, {"否", "no", "n", "false", "0", ""}
_DRIVE_ID = re.compile(r"^[A-Za-z0-9_-]{10,}$")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")
_RANGE = re.compile(r"^(?:'([^']+)'|([^'!]+))!([A-Z]+)(\d+):([A-Z]+)(\d*)$")
VIDEO_QUERY = ("'{folder}' in parents and trashed=false and "
               "(mimeType contains 'video/mp4' or mimeType contains 'video/quicktime' or mimeType contains 'video/')")


class GoldenSheetError(GoldenSetError):
    """The label Sheet / config tab / Drive folders cannot produce a golden set (names the cell)."""


# --------------------------------------------------------------------------------------- pure helpers


def _resolve_config_tab_title(existing_titles: Sequence[str]) -> str:
    """Returns the config tab title present in ``existing_titles`` (preferring ``Eval_Config``, falling
    back to legacy ``测评配置``), or ``CONFIG_TAB_TITLE`` if neither exists yet."""
    for candidate in LEGACY_CONFIG_TAB_TITLES:
        if candidate in existing_titles:
            return candidate
    return CONFIG_TAB_TITLE


def _col_index(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def _col_letter(idx: int) -> str:
    out, idx = "", idx + 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        out = chr(65 + rem) + out
    return out


def range_width(a1: str) -> int:
    m = _RANGE.match(a1.strip())
    if not m:
        raise GoldenSheetError(f"Invalid label range format {a1!r}; expected e.g. Sheet1!A1:F40")
    return _col_index(m.group(5)) - _col_index(m.group(3)) + 1


def range_tab(a1: str) -> str:
    m = _RANGE.match(a1.strip())
    return (m.group(1) or m.group(2)) if m else ""


def pad_rows(values: Sequence[Sequence[Any]], width: int) -> list[list[str]]:
    """The Sheets API drops trailing empty cells; restore a fixed-width 2-D array of strings."""
    return [[("" if c is None else str(c)) for c in row] + [""] * (width - len(row)) for row in values]


def label_values_sha256(values: list[list[str]]) -> str:
    """Provenance only: sha256 of the label range (canonical compact JSON of the padded 2-D array),
    recorded as ``source_sha256`` in every golden line. It is excluded from ``golden_version``
    (eval/golden_set.PROVENANCE_ITEM_FIELDS), so it does not need to match any earlier export format."""
    return hashlib.sha256(json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


@dataclass
class FolderConfig:
    group_id: str
    outlet: str
    focus: str
    folder_id: str
    cache_job_ids: list[str]
    row: int


@dataclass
class ItemSetting:
    item_id: str
    sop_category: str
    temporal_mode: str
    stable_baseline: bool
    row: int


@dataclass
class GoldenSheetConfig:
    name: str
    label_range: str
    expected_rows: int | None = None
    note: str = ""
    source_sheet_id: str = ""
    folders: list[FolderConfig] = field(default_factory=list)
    dev_groups: list[tuple[str, str]] = field(default_factory=list)
    compound: dict[int, list[str]] = field(default_factory=dict)
    items: list[ItemSetting] = field(default_factory=list)


def _cell(row: Sequence[Any], i: int) -> str:
    return str(row[i]).strip() if i < len(row) and row[i] is not None else ""


def parse_config(rows: Sequence[Sequence[Any]], tab: str = CONFIG_TAB) -> GoldenSheetConfig:
    """Parses the ``Eval_Config`` (or legacy ``测评配置``) tab; collects every problem and raises one GoldenSheetError naming the cells."""
    errors: list[str] = []
    sections: dict[str, list[tuple[int, list[str]]]] = {}
    current = ""
    expect_header = False
    for r, raw_row in enumerate(rows, 1):
        row = [_cell(raw_row, i) for i in range(len(raw_row))]
        if not any(row) or row[0].startswith("#"):
            continue
        marker = re.fullmatch(r"\[(.+)\]", row[0])
        if marker:
            raw_sec = marker.group(1).strip()
            current = _SECTION_ALIASES.get(raw_sec, "")
            if not current:
                errors.append(
                    f"{tab}!A{r}: Unknown section [{raw_sec}], available: {', '.join(f'[{k}]' for k in HEADERS)}"
                )
            elif current in sections:
                errors.append(f"{tab}!A{r}: Duplicate section [{raw_sec}]")
            else:
                sections[current] = []
                expect_header = True
            continue
        if not current:
            errors.append(f"{tab}!A{r}: Row is outside any section (sections start with a marker like [{SEC_BASIC}])")
            continue
        if expect_header:
            want = HEADERS[current]
            legacy_want = LEGACY_HEADERS[current]
            n = 2 if current == SEC_COMPOUND else len(want)
            if [c for c in row[:n]] != want[:n] and [c for c in row[:n]] != legacy_want[:n]:
                errors.append(
                    f"{tab}!A{r}: [{current}] header should be {' | '.join(want)}, got {' | '.join(row[:len(want)])}"
                )
            expect_header = False
            continue
        sections[current].append((r, row))

    cfg = GoldenSheetConfig(name="", label_range="")
    for sec in (SEC_BASIC, SEC_FOLDERS):
        if sec not in sections:
            errors.append(f"{tab}: Missing required section [{sec}]")
    basic: dict[str, tuple[int, str]] = {}
    for r, row in sections.get(SEC_BASIC, []):
        raw_key, val = _cell(row, 0), _cell(row, 1)
        key = _BASIC_KEY_ALIASES.get(raw_key, "")
        if not key:
            errors.append(f"{tab}!A{r}: [{SEC_BASIC}] Unknown field {raw_key!r}, available: {', '.join(BASIC_KEYS)}")
        elif key in basic:
            errors.append(f"{tab}!A{r}: [{SEC_BASIC}] Duplicate field {raw_key}")
        else:
            basic[key] = (r, val)
    name_r, cfg.name = basic.get(KEY_NAME, (0, ""))
    if not cfg.name:
        errors.append(f"{tab}: [{SEC_BASIC}] Missing {KEY_NAME} (e.g. golden_v2)")
    elif not _NAME.match(cfg.name):
        errors.append(
            f"{tab}!B{name_r}: {KEY_NAME} may only contain letters, digits, dots, underscores, and hyphens (got {cfg.name!r})"
        )
    range_r, cfg.label_range = basic.get(KEY_RANGE, (0, ""))
    if not cfg.label_range:
        errors.append(f"{tab}: [{SEC_BASIC}] Missing {KEY_RANGE} (e.g. Sheet1!A1:F40)")
    elif not _RANGE.match(cfg.label_range):
        errors.append(f"{tab}!B{range_r}: Invalid {KEY_RANGE} {cfg.label_range!r}; expected e.g. Sheet1!A1:F40")
    elif range_tab(cfg.label_range) in ({tab} | set(LEGACY_CONFIG_TAB_TITLES)):
        errors.append(f"{tab}!B{range_r}: {KEY_RANGE} cannot point to the config tab itself")
    exp_r, exp = basic.get(KEY_EXPECTED, (0, ""))
    if exp:
        if exp.isdigit() and int(exp) > 0:
            cfg.expected_rows = int(exp)
        else:
            errors.append(f"{tab}!B{exp_r}: {KEY_EXPECTED} must be a positive integer, got {exp!r}")
    cfg.note = basic.get(KEY_NOTE, (0, ""))[1]
    src_r, cfg.source_sheet_id = basic.get(KEY_SOURCE, (0, ""))
    if cfg.source_sheet_id and not _DRIVE_ID.match(cfg.source_sheet_id):
        errors.append(f"{tab}!B{src_r}: {KEY_SOURCE} must be a spreadsheet ID (not a URL)")

    seen_groups: set[str] = set()
    for r, row in sections.get(SEC_FOLDERS, []):
        gid, outlet, focus, fid, cache = (_cell(row, i) for i in range(5))
        missing = [h for h, v in zip(HEADERS[SEC_FOLDERS][:4], (gid, outlet, focus, fid)) if not v]
        if missing:
            errors.append(f"{tab}!A{r}: [{SEC_FOLDERS}] Missing {', '.join(missing)}")
            continue
        if gid in seen_groups:
            errors.append(f"{tab}!A{r}: [{SEC_FOLDERS}] Duplicate Group ID {gid}")
        seen_groups.add(gid)
        if not _DRIVE_ID.match(fid):
            errors.append(f"{tab}!D{r}: Invalid Drive Folder ID {fid!r} (expected the ID segment after /folders/)")
        cfg.folders.append(FolderConfig(gid, outlet, focus, fid,
                                        [c.strip() for c in cache.split(",") if c.strip()], r))
    if SEC_FOLDERS in sections and not cfg.folders:
        errors.append(f"{tab}: [{SEC_FOLDERS}] At least one folder row is required")

    for r, row in sections.get(SEC_DEV, []):
        focus, outlet = _cell(row, 0), _cell(row, 1)
        if not focus or not outlet:
            errors.append(f"{tab}!A{r}: [{SEC_DEV}] Both Audit Focus and Store are required")
        else:
            cfg.dev_groups.append((focus, outlet))

    for r, row in sections.get(SEC_COMPOUND, []):
        num, descs = _cell(row, 0), [c for c in row[1:] if c]
        if not num.isdigit():
            errors.append(f"{tab}!A{r}: [{SEC_COMPOUND}] Sheet Row must be an integer, got {num!r}")
        elif len(descs) < 2:
            errors.append(f"{tab}!B{r}: [{SEC_COMPOUND}] Row {num} must split into at least 2 descriptions")
        elif int(num) in cfg.compound:
            errors.append(f"{tab}!A{r}: [{SEC_COMPOUND}] Duplicate Sheet Row {num}")
        else:
            cfg.compound[int(num)] = descs

    seen_items: set[str] = set()
    for r, row in sections.get(SEC_ITEMS, []):
        iid, cat, mode, stable = (_cell(row, i) for i in range(4))
        if not iid:
            errors.append(f"{tab}!A{r}: [{SEC_ITEMS}] Missing Item ID")
            continue
        if iid in seen_items:
            errors.append(f"{tab}!A{r}: [{SEC_ITEMS}] Duplicate Item ID {iid}")
        seen_items.add(iid)
        mode_u = mode.upper()
        if mode_u not in ("", "POINT", "WINDOW"):
            errors.append(f"{tab}!C{r}: Temporal Mode must be POINT, WINDOW, or blank, got {mode!r}")
        if stable.lower() not in YES | NO:
            errors.append(f"{tab}!D{r}: Stable Baseline must be Yes / No or blank, got {stable!r}")
        cfg.items.append(ItemSetting(iid, cat, mode_u, stable.lower() in YES, r))

    if errors:
        raise GoldenSheetError("Eval config tab has errors:\n  - " + "\n  - ".join(errors))
    return cfg


def render_config_rows(cfg: GoldenSheetConfig) -> list[list[str]]:
    """The ``Eval_Config`` tab content for ``cfg`` (inverse of ``parse_config``)."""
    rows: list[list[str]] = [
        ["# Eval Config: the evaluation harness reads this tab to convert the Manual Audit Result sheet into a golden set. Editing the label tab or this tab automatically generates a new golden version on the next eval run."],
        ["# Format reference: eval/data/README.md. Blank rows and rows starting with # are ignored."],
        [],
        [f"[{SEC_BASIC}]"], HEADERS[SEC_BASIC],
        [KEY_NAME, cfg.name], [KEY_RANGE, cfg.label_range],
    ]
    if cfg.expected_rows is not None:
        rows.append([KEY_EXPECTED, str(cfg.expected_rows)])
    if cfg.source_sheet_id:
        rows.append([KEY_SOURCE, cfg.source_sheet_id])
    if cfg.note:
        rows.append([KEY_NOTE, cfg.note])
    rows += [[], [f"[{SEC_FOLDERS}]"], HEADERS[SEC_FOLDERS]]
    rows += [[f.group_id, f.outlet, f.focus, f.folder_id, ",".join(f.cache_job_ids)] for f in cfg.folders]
    rows += [[], [f"[{SEC_DEV}]"], HEADERS[SEC_DEV]] + [[f, o] for f, o in cfg.dev_groups]
    width = max([len(v) for v in cfg.compound.values()] + [2])
    rows += [[], [f"[{SEC_COMPOUND}]"], ["Sheet Row"] + [f"Split Description {i + 1}" for i in range(width)]]
    rows += [[str(k)] + v for k, v in sorted(cfg.compound.items())]
    rows += [[], [f"[{SEC_ITEMS}]"], HEADERS[SEC_ITEMS]]
    rows += [[it.item_id, it.sop_category, it.temporal_mode, "Yes" if it.stable_baseline else "No"] for it in cfg.items]
    return rows


def config_from_files(build_cfg: Mapping[str, Any], manifest: Mapping[str, Any], *, name: str,
                      label_range: str, sheet_id: str) -> GoldenSheetConfig:
    """Renders a build config + manifest (the file-based golden definition) into a config-tab model."""
    if build_cfg.get("item_fields"):
        raise GoldenSheetError(f"Move item_fields from build config into [{SEC_ITEMS}] (included in manifest and golden version)")
    folders = []
    for i, f in enumerate(manifest.get("folders") or [], 1):
        label = str(f.get("label") or "")
        if " | " not in label:
            raise GoldenSheetError(f"manifest folder {f.get('group_id')!r} label {label!r} is not formatted as 'Store | Audit Focus'")
        outlet, focus = label.split(" | ", 1)
        folders.append(FolderConfig(str(f["group_id"]), outlet, focus, str(f["folder_id"]),
                                    [str(x) for x in f.get("cache_job_ids") or []], i))
    overrides: dict[str, dict[str, Any]] = dict(manifest.get("item_overrides") or {})
    stable = [str(x) for x in manifest.get("stable_baseline_items") or []]
    order = list(overrides) + [s for s in stable if s not in overrides]
    items = [ItemSetting(iid, str((overrides.get(iid) or {}).get("sop_category") or ""),
                         str((overrides.get(iid) or {}).get("temporal_mode") or "").upper(),
                         iid in stable or bool((overrides.get(iid) or {}).get("stable_baseline")), 0)
             for iid in order]
    src = str(build_cfg.get("source_sheet_id") or "")
    return GoldenSheetConfig(
        name=name, label_range=label_range, expected_rows=build_cfg.get("expected_rows"),
        note=str(manifest.get("_comment") or ""), source_sheet_id=src if src and src != sheet_id else "",
        folders=folders, dev_groups=[(str(f), str(o)) for f, o in build_cfg.get("dev_groups") or []],
        compound={int(k): list(v) for k, v in (build_cfg.get("text_compound_parts") or {}).items()},
        items=items)


def build_manifest(cfg: GoldenSheetConfig, listings: Mapping[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """Manifest dict (same shape as ``<name>.manifest.json``) from the config + Drive listings."""
    manifest: dict[str, Any] = {}
    if cfg.note:
        manifest["_comment"] = cfg.note
    stable = [it.item_id for it in cfg.items if it.stable_baseline]
    if stable:
        manifest["stable_baseline_items"] = stable
    overrides = {}
    for it in cfg.items:
        ov = {k: v for k, v in (("sop_category", it.sop_category), ("temporal_mode", it.temporal_mode)) if v}
        if ov:
            overrides[it.item_id] = ov
    if overrides:
        manifest["item_overrides"] = overrides
    manifest["folders"] = [
        {"group_id": f.group_id, "folder_id": f.folder_id, "label": f"{f.outlet} | {f.focus}",
         "cache_job_ids": list(f.cache_job_ids), "videos": listings[f.folder_id]}
        for f in cfg.folders
    ]
    return manifest


# --------------------------------------------------------------------------------------- API layer


def list_folder_videos(drive: Any, folder_id: str) -> list[dict[str, Any]]:
    """Every video in a Drive folder, as manifest ``videos`` entries, naturally sorted by filename.

    Mirrors the production preflight listing (``GoogleWorkspaceGateway.list_folder_videos``): metadata
    from Drive's ``videoMediaMetadata`` (duration = durationMillis / 1000) and ``size_bytes`` 0, so the
    same folder yields the same manifest the r00 baseline recorded. Missing metadata is left out (the
    runner probes the clip at first ingest)."""
    from cctv_audit.video_ingestor import natural_video_sort_key

    files: list[dict[str, Any]] = []
    token = None
    while True:
        page = drive.files().list(
            q=VIDEO_QUERY.format(folder=folder_id), fields="nextPageToken, files(id, name, mimeType, videoMediaMetadata)",
            pageSize=1000, pageToken=token, supportsAllDrives=True, includeItemsFromAllDrives=True,
        ).execute(num_retries=3)
        files += page.get("files", [])
        token = page.get("nextPageToken")
        if not token:
            break
    out = []
    for f in files:
        if f.get("mimeType") == "application/vnd.google-apps.folder":
            continue
        meta = f.get("videoMediaMetadata") or {}
        v: dict[str, Any] = {"file_id": f["id"], "filename": f["name"]}
        dur = float(meta.get("durationMillis", 0) or 0) / 1000.0
        w, h = int(meta.get("width", 0) or 0), int(meta.get("height", 0) or 0)
        if dur > 0:
            v["duration_sec"] = dur
        if w > 0 and h > 0:
            v["width"], v["height"] = w, h
        v["size_bytes"] = 0
        out.append(v)
    out.sort(key=lambda v: natural_video_sort_key(v["filename"]))
    return out


def _http_hint(exc: BaseException, what: str) -> str:
    status = getattr(getattr(exc, "resp", None), "status", None)
    if status in (403, 404):
        return f"Cannot open {what} (HTTP {status}): please share it with Viewer or higher access to the evaluation Workspace identity"
    return f"Failed to read {what}: {type(exc).__name__}: {exc}"


_FOOTAGE_PREFIX_RE = re.compile(r"(?i)^\s*footage\s*(\d+(?:\s*[&,]\s*\d+)*)\s*[:：]")
_LEADING_NUM_PREFIX_RE = re.compile(r"^\s*\d+\s*[\.\-]\s*(.+?)\s*$")


def _clean_hierarchy_segment(name: str) -> str:
    """Strips leading numbering like ``1. Handwashing Monitoring`` -> ``Handwashing Monitoring`` or
    ``1-Bau Cat`` -> ``Bau Cat``."""
    m = _LEADING_NUM_PREFIX_RE.match((name or "").strip())
    return m.group(1).strip() if m else (name or "").strip()


def _infer_chagee_sop_category(clause: str, focus: str) -> str:
    """Maps a human label row's Audit Clause + Focus to the canonical 3-bucket Chagee SOP category."""
    cl = (clause or "").strip().lower()
    fc = (focus or "").strip().lower()
    if "tea maker" in cl or "personal belonging" in cl or "tea bar" in cl or "6.2" in cl or "1.4" in cl:
        return "C_TeaBar_Hygiene"
    if "ice maker" in cl or "equipment cleaning" in cl or "ice maker" in fc:
        return "B_IceMaker"
    if "handwashing" in cl or "cross contamination" in cl or "handwashing" in fc:
        return "A_Handwashing"
    return ""


def _infer_single_ts_compound_parts(finding: str) -> list[str] | None:
    """Splits a single-timestamp finding text containing two independent violation sentences (e.g. R18 in golden_v1)
    into two scoring parts, while keeping multi-stage causal chains (e.g. Cross Contamination) as a single part."""
    from eval.build_dataset import parse_osd_times

    if "cross contamination" in (finding or "").lower():
        return None
    times = parse_osd_times(finding)
    if len(times) != 1:
        return None
    body = re.sub(
        r"(?i)^\s*footage\s*\d+(?:\s*[&,]\s*\d+)*\s*[:：]\s*\d{6}\s*",
        "",
        finding.strip(),
    )
    parts = [p.strip() for p in re.split(r"\.\s+(?=[A-Z])", body) if p.strip()]
    neg_re = re.compile(r"(?i)\b(do\s+not|dot\s+not|not\s+|did\s+not|without\s+|fail|less\s+than)\b")
    if len(parts) >= 2 and all(neg_re.search(p) for p in parts):
        restored: list[str] = []
        for idx, p in enumerate(parts):
            if idx < len(parts) - 1 and not p.endswith("."):
                restored.append(p + ".")
            else:
                restored.append(p)
        return restored
    return None


def discover_golden_video_folders(
    drive: Any,
    root_folder_id: str,
    *,
    known_focuses: set[str] | None = None,
    known_outlets: set[str] | None = None,
) -> tuple[list[FolderConfig], dict[str, list[dict[str, Any]]]]:
    """Recursively discovers all leaf video folders under ``root_folder_id`` (up to depth 4) and lists
    their videos. Skips ``📁 Violation_Evidence_Clips`` / ``📁 违规证据切片`` evidence subfolders."""
    focuses_norm = {f.lower(): f for f in (known_focuses or set()) if f}
    outlets_norm = {o.lower(): o for o in (known_outlets or set()) if o}

    def _list_subfolders(parent_id: str) -> list[dict[str, str]]:
        subfolders: list[dict[str, str]] = []
        token = None
        q = (
            f"'{parent_id}' in parents and trashed=false and "
            "mimeType='application/vnd.google-apps.folder'"
        )
        while True:
            page = (
                drive.files()
                .list(
                    q=q,
                    fields="nextPageToken, files(id, name, mimeType)",
                    pageSize=200,
                    pageToken=token,
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                )
                .execute(num_retries=3)
            )
            for item in page.get("files", []) or []:
                mtype = str(item.get("mimeType") or "").strip()
                if mtype and mtype != "application/vnd.google-apps.folder":
                    continue
                nm = str(item.get("name") or "").strip()
                if nm.startswith("📁") or "违规证据切片" in nm or "Violation_Evidence_Clips" in nm:
                    continue
                subfolders.append({"id": str(item["id"]), "name": nm})
            token = page.get("nextPageToken")
            if not token:
                break
        subfolders.sort(key=lambda x: x["name"])
        return subfolders

    try:
        root_meta = (
            drive.files()
            .get(fileId=root_folder_id, fields="id, name", supportsAllDrives=True)
            .execute(num_retries=3)
        )
    except Exception as exc:  # noqa: BLE001
        raise GoldenSheetError(_http_hint(exc, f"Golden videos root folder {root_folder_id}")) from exc

    root_name = str(root_meta.get("name") or root_folder_id).strip()
    queue: list[tuple[str, list[str]]] = [(root_folder_id, [root_name])]
    discovered_folders: list[FolderConfig] = []
    listings: dict[str, list[dict[str, Any]]] = {}

    visited: set[str] = set()
    while queue:
        curr_id, path_names = queue.pop(0)
        if curr_id in visited or len(path_names) > 5:
            continue
        visited.add(curr_id)

        try:
            vids = list_folder_videos(drive, curr_id)
        except Exception as exc:  # noqa: BLE001
            raise GoldenSheetError(_http_hint(exc, f"Golden videos subfolder {curr_id}")) from exc

        if vids:
            cleaned_ancestors = [_clean_hierarchy_segment(seg) for seg in path_names]
            matched_focus = ""
            matched_outlet = ""
            for seg in cleaned_ancestors:
                seg_l = seg.lower()
                if not matched_focus:
                    for kf_l, kf_orig in focuses_norm.items():
                        if kf_l == seg_l or kf_l in seg_l or seg_l in kf_l:
                            matched_focus = kf_orig
                            break
                if not matched_outlet:
                    for ko_l, ko_orig in outlets_norm.items():
                        if ko_l == seg_l or ko_l in seg_l or seg_l in ko_l:
                            matched_outlet = ko_orig
                            break
            if not matched_outlet and cleaned_ancestors:
                matched_outlet = cleaned_ancestors[-1]
            if not matched_focus and len(cleaned_ancestors) >= 2:
                matched_focus = cleaned_ancestors[-2]
            elif not matched_focus:
                matched_focus = matched_outlet

            gid = hashlib.sha1(f"{matched_outlet}|{matched_focus}|{curr_id}".encode("utf-8")).hexdigest()[:6]
            discovered_folders.append(
                FolderConfig(
                    group_id=gid,
                    outlet=matched_outlet,
                    focus=matched_focus,
                    folder_id=curr_id,
                    cache_job_ids=[],
                    row=len(discovered_folders) + 1,
                )
            )
            listings[curr_id] = vids

        try:
            children = _list_subfolders(curr_id)
        except Exception:
            children = []
        for child in children:
            queue.append((child["id"], path_names + [child["name"]]))

    discovered_folders.sort(key=lambda f: (f.outlet, f.focus, f.folder_id))
    return discovered_folders, listings


def _resolve_missing_filenames_in_rows(
    values: list[list[str]],
    folders: Sequence[FolderConfig],
    listings: Mapping[str, list[dict[str, Any]]],
) -> list[list[str]]:
    """Fills in Column F (``Video Filename``) when a human label sheet (such as ``CHAGEE_GolderSet``)
    has only 5 columns (`Focus`, `Outlet`, `Outlet Name`, `Audit Clause`, `Findings`) by matching
    `(Focus, Outlet Name)` + `Footage N` prefix against the discovered folder listings."""
    folder_by_pair: dict[tuple[str, str], str] = {
        (f.focus.lower(), f.outlet.lower()): f.folder_id for f in folders
    }
    carry = ["", "", "", ""]
    out_rows: list[list[str]] = []
    for idx, row in enumerate(values):
        padded = list(row) + [""] * max(0, 6 - len(row))
        if idx == 0:
            if not padded[5].strip():
                padded[5] = "Video Filename"
            out_rows.append(padded[:6])
            continue
        finding = padded[4].strip()
        if not finding:
            out_rows.append(padded[:6])
            continue
        for c_i in range(4):
            if padded[c_i].strip():
                carry[c_i] = padded[c_i].strip()
        focus, _, outlet_name, _ = carry
        if not padded[5].strip():
            fid = folder_by_pair.get((focus.lower(), outlet_name.lower()), "")
            vids = listings.get(fid, []) if fid else []
            m = _FOOTAGE_PREFIX_RE.match(finding)
            if m and vids:
                nums = [int(n.strip()) for n in re.split(r"[&,]", m.group(1)) if n.strip().isdigit()]
                matched_names: list[str] = []
                for num in nums:
                    pat = re.compile(rf"(?i)^footage\s*0*{num}(?!\d)")
                    cand = next((v["filename"] for v in vids if pat.search(v["filename"])), "")
                    if cand:
                        matched_names.append(cand)
                if matched_names and len(matched_names) == len(nums):
                    padded[5] = ",".join(matched_names)
        out_rows.append(padded[:6])
    return out_rows


def _load_golden_from_sheet_and_folder(
    sheet_id: str,
    golden_videos_folder_id: str,
    *,
    sheets: Any,
    drive: Any,
) -> GoldenSet:
    """Builds a ``GoldenSet`` from a Human Labels Google Sheet (``sheet_id``, which does NOT need an
    ``Eval_Config`` tab) and a dedicated ``golden_videos_folder_id`` in Google Drive."""
    try:
        meta = (
            drive.files()
            .get(fileId=sheet_id, fields="name, modifiedTime, version", supportsAllDrives=True)
            .execute(num_retries=3)
        )
        ss_meta = (
            sheets.spreadsheets()
            .get(spreadsheetId=sheet_id, fields="sheets.properties.title")
            .execute(num_retries=3)
        )
    except Exception as exc:  # noqa: BLE001
        raise GoldenSheetError(_http_hint(exc, f"Human labels sheet {sheet_id}")) from exc

    tab_titles = [
        str((s.get("properties") or {}).get("title") or "")
        for s in (ss_meta.get("sheets") or [])
    ]
    optional_cfg: GoldenSheetConfig | None = None
    active_config_tab = _resolve_config_tab_title(tab_titles)
    if active_config_tab in tab_titles:
        try:
            config_rows = (
                sheets.spreadsheets()
                .values()
                .get(spreadsheetId=sheet_id, range=f"'{active_config_tab}'")
                .execute(num_retries=3)
                .get("values", [])
            )
            optional_cfg = parse_config(config_rows, tab=active_config_tab)
        except Exception:
            optional_cfg = None

    label_range = optional_cfg.label_range if optional_cfg and optional_cfg.label_range else ""
    label_values: list[list[Any]] = []
    if label_range:
        try:
            label_values = (
                sheets.spreadsheets()
                .values()
                .get(spreadsheetId=sheet_id, range=label_range)
                .execute(num_retries=3)
                .get("values", [])
            )
        except Exception:
            label_values = []

    if not label_values:
        for t_title in tab_titles:
            if t_title in LEGACY_CONFIG_TAB_TITLES:
                continue
            try:
                cand_vals = (
                    sheets.spreadsheets()
                    .values()
                    .get(spreadsheetId=sheet_id, range=f"'{t_title}'!A1:F200")
                    .execute(num_retries=3)
                    .get("values", [])
                )
            except Exception:
                continue
            if cand_vals and len(cand_vals[0]) >= 5 and str(cand_vals[0][4]).strip() == "Findings":
                label_range = f"{t_title}!A1:F200"
                label_values = cand_vals
                break

    if not label_values:
        raise GoldenSheetError(
            f"No label worksheet containing 'Findings' in Column E was found in human labels sheet {sheet_id}; please verify the header row"
        )

    raw_padded = pad_rows(label_values, 6)
    # Remove any completely empty separator rows inside the label table so sheet_row numbers stay contiguous
    cleaned_rows: list[list[str]] = [raw_padded[0]]
    known_focuses: set[str] = set()
    known_outlets: set[str] = set()
    carry_fo = ["", ""]
    for r in raw_padded[1:]:
        if not any(c.strip() for c in r):
            continue
        if r[0].strip():
            carry_fo[0] = r[0].strip()
        if r[2].strip():
            carry_fo[1] = r[2].strip()
        if carry_fo[0]:
            known_focuses.add(carry_fo[0])
        if carry_fo[1]:
            known_outlets.add(carry_fo[1])
        cleaned_rows.append(r)

    discovered_folders, listings = discover_golden_video_folders(
        drive,
        golden_videos_folder_id,
        known_focuses=known_focuses,
        known_outlets=known_outlets,
    )
    if not discovered_folders:
        raise GoldenSheetError(
            f"No video subfolders found under golden videos folder {golden_videos_folder_id}; please verify Golden_Videos_Folder_URL"
        )

    # Preserve group_id and cache_job_ids from optional_cfg if matching (outlet, focus) exists
    if optional_cfg and optional_cfg.folders:
        cfg_by_pair = {(f.outlet.lower(), f.focus.lower()): f for f in optional_cfg.folders}
        merged_folders: list[FolderConfig] = []
        for df in discovered_folders:
            prev_f = cfg_by_pair.get((df.outlet.lower(), df.focus.lower()))
            if prev_f is not None:
                merged_folders.append(
                    FolderConfig(
                        group_id=prev_f.group_id,
                        outlet=prev_f.outlet,
                        focus=prev_f.focus,
                        folder_id=df.folder_id,
                        cache_job_ids=list(prev_f.cache_job_ids),
                        row=df.row,
                    )
                )
            else:
                merged_folders.append(df)
        discovered_folders = merged_folders

    resolved_values = _resolve_missing_filenames_in_rows(cleaned_rows, discovered_folders, listings)
    filename_index: dict[str, str] = {}
    problems: list[str] = []
    for f in discovered_folders:
        for v in listings.get(f.folder_id, []):
            prev = filename_index.setdefault(v["filename"], v["file_id"])
            if prev != v["file_id"]:
                problems.append(
                    f"Video filename {v['filename']!r} appears more than once in the golden videos folders ({prev} / {v['file_id']}); labels cannot be uniquely mapped"
                )
    if problems:
        raise GoldenSheetError("Golden videos folder errors:\n  - " + "\n  - ".join(problems))

    dev_groups = (
        list(optional_cfg.dev_groups)
        if optional_cfg and optional_cfg.dev_groups
        else [
            ("Handwashing Monitoring", "Cantavil D2"),
            ("Ice Maker Weekly Cleaning", "Bau Cat"),
        ]
    )
    compound: dict[int, list[str]] = (
        dict(optional_cfg.compound) if optional_cfg and optional_cfg.compound else {}
    )
    if not compound:
        for offset, r in enumerate(resolved_values[1:]):
            s_row = offset + 2
            inferred_parts = _infer_single_ts_compound_parts(r[4])
            if inferred_parts:
                compound[s_row] = inferred_parts

    try:
        records = build_golden_records(
            resolved_values,
            filename_index,
            source_sheet_id=(optional_cfg.source_sheet_id if optional_cfg else "") or sheet_id,
            source_sha256=label_values_sha256(resolved_values),
            dev_groups=set(dev_groups),
            text_compound_parts=compound,
            expected_rows=optional_cfg.expected_rows if optional_cfg else None,
        )
    except (GoldenBuildError, ValueError) as exc:
        msg = str(exc)
        if "not found in the Drive listing" in msg:
            msg = (
                "Labelled video filename(s) not found in the golden videos folders (must match verbatim, including spaces and parentheses):\n  "
                + msg.split(":", 1)[1].strip().replace("\n  ", "\n  ")
            )
        raise GoldenSheetError(f"Human label sheet {label_range} cannot be converted into a golden set: {msg}") from exc

    if optional_cfg and optional_cfg.items:
        item_settings = list(optional_cfg.items)
    else:
        default_stable = {"R08", "R09", "R12", "R13", "R14", "R19"}
        item_settings = []
        for idx_r, rec in enumerate(records, 1):
            iid = str(rec["item_id"])
            cat = _infer_chagee_sop_category(str(rec.get("audit_clause") or ""), str(rec.get("focus") or ""))
            item_settings.append(
                ItemSetting(
                    item_id=iid,
                    sop_category=cat,
                    temporal_mode="",
                    stable_baseline=iid in default_stable,
                    row=idx_r,
                )
            )

    synth_cfg = GoldenSheetConfig(
        name=(optional_cfg.name if optional_cfg and optional_cfg.name else "golden_v1"),
        label_range=label_range,
        expected_rows=len(records),
        note=(optional_cfg.note if optional_cfg else ""),
        source_sheet_id=sheet_id,
        folders=discovered_folders,
        dev_groups=dev_groups,
        compound=compound,
        items=item_settings,
    )
    manifest = build_manifest(synth_cfg, listings)
    origin = {
        "type": "google_sheet",
        "sheet_id": sheet_id,
        "sheet_name": str(meta.get("name") or ""),
        "sheet_modified_time": str(meta.get("modifiedTime") or ""),
        "sheet_version": str(meta.get("version") or ""),
        "label_range": label_range,
        "golden_videos_folder_id": golden_videos_folder_id,
        "sheet_url": f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit",
    }
    return build_golden(
        records_to_jsonl(records).encode("utf-8"),
        manifest,
        stem=synth_cfg.name,
        source=f"sheet:{sheet_id}",
        manifest_source=f"drive_folder:{golden_videos_folder_id}",
        origin=origin,
    )


def load_golden_from_sheet(
    sheet_id: str,
    *,
    sheets: Any,
    drive: Any,
    golden_videos_folder_id: str = "",
) -> GoldenSet:
    """Reads the label tab (plus ``Eval_Config`` / ``测评配置`` tab when ``golden_videos_folder_id`` is empty,
    or discovers folders directly from ``golden_videos_folder_id`` when provided) and returns the GoldenSet."""
    if str(golden_videos_folder_id or "").strip():
        return _load_golden_from_sheet_and_folder(
            sheet_id,
            str(golden_videos_folder_id).strip(),
            sheets=sheets,
            drive=drive,
        )
    active_tab = CONFIG_TAB
    try:
        meta = drive.files().get(fileId=sheet_id, fields="name, modifiedTime, version",
                                 supportsAllDrives=True).execute(num_retries=3)
        try:
            config_rows = sheets.spreadsheets().values().get(
                spreadsheetId=sheet_id, range=f"'{active_tab}'").execute(num_retries=3).get("values", [])
        except Exception as first_exc:
            if "Unable to parse range" in str(first_exc):
                legacy_tab = next((t for t in LEGACY_CONFIG_TAB_TITLES if t != active_tab), "")
                if legacy_tab:
                    try:
                        config_rows = sheets.spreadsheets().values().get(
                            spreadsheetId=sheet_id, range=f"'{legacy_tab}'"
                        ).execute(num_retries=3).get("values", [])
                        active_tab = legacy_tab
                    except Exception:
                        raise first_exc
                else:
                    raise
            else:
                raise
    except Exception as exc:  # noqa: BLE001 - mapped to an operator message
        text = str(exc)
        if "Unable to parse range" in text:
            raise GoldenSheetError(f"Spreadsheet {sheet_id} does not have an '{CONFIG_TAB}' tab: please run "
                                   f"python -m eval.golden_sheet init-config first or add it manually per eval/data/README.md") from exc
        raise GoldenSheetError(_http_hint(exc, f"Label sheet {sheet_id}")) from exc
    cfg = parse_config(config_rows, tab=active_tab)
    width = range_width(cfg.label_range)
    try:
        label_values = sheets.spreadsheets().values().get(
            spreadsheetId=sheet_id, range=cfg.label_range).execute(num_retries=3).get("values", [])
    except Exception as exc:  # noqa: BLE001
        raise GoldenSheetError(_http_hint(exc, f"Label range {cfg.label_range}")) from exc
    values = pad_rows(label_values, width)

    listings: dict[str, list[dict[str, Any]]] = {}
    filename_index: dict[str, str] = {}
    problems: list[str] = []
    for f in cfg.folders:
        try:
            listings[f.folder_id] = list_folder_videos(drive, f.folder_id)
        except Exception as exc:  # noqa: BLE001
            raise GoldenSheetError(_http_hint(exc, f"Video folder {f.folder_id} ({active_tab}!D{f.row})")) from exc
        if not listings[f.folder_id]:
            problems.append(f"{active_tab}!D{f.row}: Folder {f.folder_id} contains no videos")
        for v in listings[f.folder_id]:
            prev = filename_index.setdefault(v["filename"], v["file_id"])
            if prev != v["file_id"]:
                problems.append(f"Video filename {v['filename']!r} appears more than once across configured folders ({prev} / {v['file_id']}); "
                                "labels cannot be uniquely mapped")
    if problems:
        raise GoldenSheetError("Video folder errors:\n  - " + "\n  - ".join(problems))

    try:
        records = build_golden_records(
            values, filename_index, source_sheet_id=cfg.source_sheet_id or sheet_id,
            source_sha256=label_values_sha256(values), dev_groups=set(cfg.dev_groups),
            text_compound_parts=cfg.compound, expected_rows=cfg.expected_rows)
    except (GoldenBuildError, ValueError) as exc:
        msg = str(exc)
        if "not found in the Drive listing" in msg:
            msg = ("Labelled video filename(s) not found in the configured folders (must match verbatim, including spaces and parentheses):\n  "
                   + msg.split(":", 1)[1].strip().replace("\n  ", "\n  "))
        raise GoldenSheetError(f"Label range {cfg.label_range} cannot be converted into a golden set: {msg}") from exc
    manifest = build_manifest(cfg, listings)
    origin = {
        "type": "google_sheet", "sheet_id": sheet_id, "sheet_name": str(meta.get("name") or ""),
        "sheet_modified_time": str(meta.get("modifiedTime") or ""), "sheet_version": str(meta.get("version") or ""),
        "label_range": cfg.label_range, "config_tab": active_tab,
        "sheet_url": f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit",
    }
    return build_golden(records_to_jsonl(records).encode("utf-8"), manifest, stem=cfg.name,
                        source=f"sheet:{sheet_id}", manifest_source=f"sheet:{sheet_id}#{active_tab}", origin=origin)


def default_services() -> tuple[Any, Any]:
    """(sheets, drive) clients on the stack's Workspace identity (keyless DWD, cctv_audit.gcp)."""
    from googleapiclient.discovery import build

    from cctv_audit.gcp import workspace_credentials

    creds = workspace_credentials()
    return (build("sheets", "v4", credentials=creds, cache_discovery=False),
            build("drive", "v3", credentials=creds, cache_discovery=False))


def check_report(golden: GoldenSet) -> str:
    """Human-readable read-only summary printed by ``check``."""
    from eval.golden_set import derive_folder_specs

    labelled = {fid for it in golden.items for fid in it["video_file_ids"]}
    stable, stable_src = golden.stable_baseline()
    lines = [
        f"Golden Version: {golden.version} ({VERSION_SCHEME}: changes only with scoring ruler)",
        f"Scoring Ruler Hash: golden={golden.golden_sha256[:16]} manifest={golden.manifest_sha256[:16] or '-'}",
        f"Full-Content Hash (Audit): golden={golden.golden_sha256_full[:16]} manifest={golden.manifest_sha256_full[:16] or '-'}",
        f"Source: {golden.origin.get('sheet_name', '')} {golden.origin.get('sheet_url', golden.source)}"
        f" (modified {golden.origin.get('sheet_modified_time', '?')})",
        f"Items {golden.item_count} / Parts {golden.part_count} / Splits {golden.split_counts}",
        f"Stable Baseline Items: {', '.join(stable) or 'not configured'} ({stable_src})",
        "Video Folders:",
    ]
    total = 0
    for s in derive_folder_specs(golden):
        n_lab = sum(1 for v in s["videos"] if v["file_id"] in labelled)
        total += len(s["videos"])
        lines.append(f"  - {s['group_id']} {s['label']} ({s['folder_id']}): {len(s['videos'])} videos, "
                     f"{n_lab} labelled, {len(s['videos']) - n_lab} unlabelled; cache jobs {s['cache_job_ids'] or 'none'}")
    lines.append(f"Total: {total} videos")
    return "\n".join(lines)


def compare_report(golden: GoldenSet, previous: str) -> str:
    """Compare with a previous ``score.json`` (its ``golden`` block) or a bare ``golden_version``."""
    p = Path(previous)
    if p.is_file():
        block = (json.loads(p.read_text(encoding="utf-8")).get("golden") or {})
        prev_version = str(block.get("golden_version") or "")
        prev_scheme = str(block.get("golden_version_scheme") or "")
        prev_full = (str(block.get("golden_sha256_full") or ""), str(block.get("manifest_sha256_full") or ""))
    else:
        prev_version, prev_scheme, prev_full = previous.strip(), VERSION_SCHEME, ("", "")
    if not prev_version:
        return f"Compare: {previous} has no golden_version; cannot compare"
    if prev_scheme != VERSION_SCHEME:
        return (f"Compare: {previous} uses an older version scheme ({prev_scheme or 'legacy'}), so versions are not directly comparable; "
                "compare against a score.json or version under the same scheme")
    if prev_version != golden.version:
        return f"Compare: scoring ruler changed ({prev_version} -> {golden.version}); recall cannot be directly compared"
    if prev_full == ("", ""):
        return f"Compare: scoring ruler unchanged ({golden.version}); recall is comparable"
    if prev_full == (golden.golden_sha256_full, golden.manifest_sha256_full):
        return f"Compare: identical ({golden.version})"
    return (f"Compare: scoring ruler unchanged ({golden.version}), only non-scoring fields changed (notes, video metadata, cache jobs, "
            "Drive folder IDs, or provenance); recall is comparable")


def init_config_plan(*, existing_titles: Sequence[str], rows: list[list[str]], overwrite: bool) -> dict[str, Any]:
    """What ``init-config`` will send: only the ``Eval_Config`` (or existing ``测评配置``) tab is ever created / cleared / written."""
    target_tab = _resolve_config_tab_title(existing_titles)
    exists = target_tab in existing_titles
    if exists and not overwrite:
        raise GoldenSheetError(f"Spreadsheet already has a '{target_tab}' tab; pass --overwrite to replace it (only this tab is modified)")
    return {
        "add_sheet": None if exists else {"requests": [{"addSheet": {"properties": {"title": target_tab}}}]},
        "clear_range": f"'{target_tab}'" if exists else None,
        "update": {"range": f"'{target_tab}'!A1", "valueInputOption": "RAW", "body": {"values": rows}},
    }


def apply_init_config(sheets: Any, sheet_id: str, plan: Mapping[str, Any]) -> None:
    ss = sheets.spreadsheets()
    if plan["add_sheet"]:
        ss.batchUpdate(spreadsheetId=sheet_id, body=plan["add_sheet"]).execute(num_retries=3)
    if plan["clear_range"]:
        ss.values().clear(spreadsheetId=sheet_id, range=plan["clear_range"], body={}).execute(num_retries=3)
    up = plan["update"]
    ss.values().update(spreadsheetId=sheet_id, range=up["range"], valueInputOption=up["valueInputOption"],
                       body=up["body"]).execute(num_retries=3)


def main(argv: Sequence[str] | None = None, services: Callable[[], tuple[Any, Any]] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_check = sub.add_parser("check", help="read-only: build the golden set from the Sheet and print a summary")
    p_check.add_argument("--sheet-id", required=True)
    p_check.add_argument("--compare", default=None,
                         help="previous score.json (path) or golden_version string to compare against")
    p_init = sub.add_parser("init-config", help=f"write ONLY the {CONFIG_TAB} tab from a build config + manifest")
    p_init.add_argument("--sheet-id", required=True)
    p_init.add_argument("--from-build-config", type=Path, required=True)
    p_init.add_argument("--manifest", type=Path, required=True)
    p_init.add_argument("--golden-name", default=None, help="default: manifest file name stem (e.g. golden_v1)")
    p_init.add_argument("--label-range", default="Sheet1!A1:F40")
    p_init.add_argument("--overwrite", action="store_true", help=f"replace an existing {CONFIG_TAB} tab")
    p_init.add_argument("--dry-run", action="store_true", help="print the tab rows and requests; write nothing")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "init-config":
            manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
            build_cfg = json.loads(args.from_build_config.read_text(encoding="utf-8"))
            name = args.golden_name or args.manifest.name.split(".manifest.json")[0]
            cfg = config_from_files(build_cfg, manifest, name=name, label_range=args.label_range,
                                    sheet_id=args.sheet_id)
            rows = render_config_rows(cfg)
            parse_config(rows)  # what we write must parse back
            sheets, _ = (services or default_services)()
            meta = sheets.spreadsheets().get(spreadsheetId=args.sheet_id,
                                             fields="sheets.properties.title").execute(num_retries=3)
            titles = [s["properties"]["title"] for s in meta.get("sheets", [])]
            plan = init_config_plan(existing_titles=titles, rows=rows, overwrite=args.overwrite)
            if args.dry_run:
                print(json.dumps({"existing_tabs": titles, **plan}, ensure_ascii=False, indent=2))
                return 0
            apply_init_config(sheets, args.sheet_id, plan)
            print(f"Wrote '{CONFIG_TAB}' tab ({len(rows)} rows); other tabs untouched. Next step: python -m eval.golden_sheet check "
                  f"--sheet-id {args.sheet_id}")
            return 0
        sheets, drive = (services or default_services)()
        golden = load_golden_from_sheet(args.sheet_id, sheets=sheets, drive=drive)
        print(check_report(golden))
        if args.compare:
            print(compare_report(golden, args.compare))
        return 0
    except GoldenSetError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
