#!/usr/bin/env python3
"""Golden set straight from the customer's label Sheet + a ``测评配置`` (eval config) tab.

The customer keeps maintaining their own "Manual Audit Result" label tab (never modified by code). One
extra tab, ``测评配置``, in the same spreadsheet says how to turn it into a golden set: golden name,
label range, the Drive folders holding the clips, the dev split, compound rows and optional per-item
settings. Each eval run reads both tabs live, lists every video in the configured folders (including
unlabelled clips, which count for alert density) and produces exactly the in-memory golden JSONL +
manifest that ``eval/golden_set.py`` consumes, so ``golden_version`` is computed the same way as for a
file. A config tab rendered from ``golden_v1.build.json`` + ``golden_v1.manifest.json`` over the
unchanged label tab reproduces the file's ruler version (``golden_v1@9426fe9b52``, scheme ruler-v2).

Config tab layout (see eval/data/README.md for a full example). Column A holds section markers
``[基本设置]`` / ``[视频文件夹]`` / ``[开发集分组]`` / ``[复合题拆分]`` / ``[逐题设置]``; the row after a
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

CONFIG_TAB = "测评配置"
SEC_BASIC, SEC_FOLDERS, SEC_DEV, SEC_COMPOUND, SEC_ITEMS = "基本设置", "视频文件夹", "开发集分组", "复合题拆分", "逐题设置"
HEADERS = {
    SEC_BASIC: ["项目", "值"],
    SEC_FOLDERS: ["分组ID", "门店", "稽核重点", "Drive 文件夹 ID", "缓存任务 ID（可空，逗号分隔）"],
    SEC_DEV: ["稽核重点", "门店"],
    SEC_COMPOUND: ["表格行号", "拆分描述 1", "拆分描述 2"],  # more description columns allowed
    SEC_ITEMS: ["题号", "SOP 大类（可空）", "时间判定模式（POINT/WINDOW，可空）", "稳定基线题（是/否，可空）"],
}
KEY_NAME, KEY_RANGE, KEY_EXPECTED, KEY_NOTE, KEY_SOURCE = "黄金集名称", "标注页范围", "期望题数", "备注", "来源表 ID"
BASIC_KEYS = (KEY_NAME, KEY_RANGE, KEY_EXPECTED, KEY_NOTE, KEY_SOURCE)
YES, NO = {"是", "yes", "y", "true", "1"}, {"否", "no", "n", "false", "0", ""}
_DRIVE_ID = re.compile(r"^[A-Za-z0-9_-]{10,}$")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")
_RANGE = re.compile(r"^(?:'([^']+)'|([^'!]+))!([A-Z]+)(\d+):([A-Z]+)(\d*)$")
VIDEO_QUERY = ("'{folder}' in parents and trashed=false and "
               "(mimeType contains 'video/mp4' or mimeType contains 'video/quicktime' or mimeType contains 'video/')")


class GoldenSheetError(GoldenSetError):
    """The label Sheet / config tab / Drive folders cannot produce a golden set (Chinese, names the cell)."""


# --------------------------------------------------------------------------------------- pure helpers


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
        raise GoldenSheetError(f"标注页范围 {a1!r} 格式不对，应形如 Sheet1!A1:F40")
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
    """Parses the ``测评配置`` tab; collects every problem and raises one GoldenSheetError naming the cells."""
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
            current = marker.group(1).strip()
            if current not in HEADERS:
                errors.append(f"{tab}!A{r}: 不认识的分区 [{current}]，可用：{', '.join(f'[{k}]' for k in HEADERS)}")
                current = ""
            elif current in sections:
                errors.append(f"{tab}!A{r}: 分区 [{current}] 出现了两次")
            else:
                sections[current] = []
                expect_header = True
            continue
        if not current:
            errors.append(f"{tab}!A{r}: 这一行不在任何分区里（分区以 [基本设置] 这样的标记开头）")
            continue
        if expect_header:
            want = HEADERS[current]
            n = 2 if current == SEC_COMPOUND else len(want)
            if [c for c in row[:n]] != want[:n]:
                errors.append(f"{tab}!A{r}: [{current}] 的表头应为 {' | '.join(want)}，实际是 {' | '.join(row[:len(want)])}")
            expect_header = False
            continue
        sections[current].append((r, row))

    cfg = GoldenSheetConfig(name="", label_range="")
    for sec in (SEC_BASIC, SEC_FOLDERS):
        if sec not in sections:
            errors.append(f"{tab}: 缺少必填分区 [{sec}]")
    basic: dict[str, tuple[int, str]] = {}
    for r, row in sections.get(SEC_BASIC, []):
        key, val = _cell(row, 0), _cell(row, 1)
        if key not in BASIC_KEYS:
            errors.append(f"{tab}!A{r}: [基本设置] 不认识的项目 {key!r}，可用：{', '.join(BASIC_KEYS)}")
        elif key in basic:
            errors.append(f"{tab}!A{r}: [基本设置] 项目 {key} 重复")
        else:
            basic[key] = (r, val)
    name_r, cfg.name = basic.get(KEY_NAME, (0, ""))
    if not cfg.name:
        errors.append(f"{tab}: [基本设置] 缺少 {KEY_NAME}（例如 golden_v2）")
    elif not _NAME.match(cfg.name):
        errors.append(f"{tab}!B{name_r}: {KEY_NAME} 只能用字母、数字、点、下划线、横线（收到 {cfg.name!r}）")
    range_r, cfg.label_range = basic.get(KEY_RANGE, (0, ""))
    if not cfg.label_range:
        errors.append(f"{tab}: [基本设置] 缺少 {KEY_RANGE}（例如 Sheet1!A1:F40）")
    elif not _RANGE.match(cfg.label_range):
        errors.append(f"{tab}!B{range_r}: {KEY_RANGE} {cfg.label_range!r} 格式不对，应形如 Sheet1!A1:F40")
    elif range_tab(cfg.label_range) == tab:
        errors.append(f"{tab}!B{range_r}: {KEY_RANGE} 不能指向配置页本身")
    exp_r, exp = basic.get(KEY_EXPECTED, (0, ""))
    if exp:
        if exp.isdigit() and int(exp) > 0:
            cfg.expected_rows = int(exp)
        else:
            errors.append(f"{tab}!B{exp_r}: {KEY_EXPECTED} 应为正整数，收到 {exp!r}")
    cfg.note = basic.get(KEY_NOTE, (0, ""))[1]
    src_r, cfg.source_sheet_id = basic.get(KEY_SOURCE, (0, ""))
    if cfg.source_sheet_id and not _DRIVE_ID.match(cfg.source_sheet_id):
        errors.append(f"{tab}!B{src_r}: {KEY_SOURCE} 应为表格 ID（不是链接）")

    seen_groups: set[str] = set()
    for r, row in sections.get(SEC_FOLDERS, []):
        gid, outlet, focus, fid, cache = (_cell(row, i) for i in range(5))
        missing = [h for h, v in zip(HEADERS[SEC_FOLDERS][:4], (gid, outlet, focus, fid)) if not v]
        if missing:
            errors.append(f"{tab}!A{r}: [视频文件夹] 缺少 {', '.join(missing)}")
            continue
        if gid in seen_groups:
            errors.append(f"{tab}!A{r}: [视频文件夹] 分组ID {gid} 重复")
        seen_groups.add(gid)
        if not _DRIVE_ID.match(fid):
            errors.append(f"{tab}!D{r}: Drive 文件夹 ID {fid!r} 不合法（应为文件夹链接 /folders/ 后面那段 ID）")
        cfg.folders.append(FolderConfig(gid, outlet, focus, fid,
                                        [c.strip() for c in cache.split(",") if c.strip()], r))
    if SEC_FOLDERS in sections and not cfg.folders:
        errors.append(f"{tab}: [视频文件夹] 至少需要一行")

    for r, row in sections.get(SEC_DEV, []):
        focus, outlet = _cell(row, 0), _cell(row, 1)
        if not focus or not outlet:
            errors.append(f"{tab}!A{r}: [开发集分组] 稽核重点和门店都要填")
        else:
            cfg.dev_groups.append((focus, outlet))

    for r, row in sections.get(SEC_COMPOUND, []):
        num, descs = _cell(row, 0), [c for c in row[1:] if c]
        if not num.isdigit():
            errors.append(f"{tab}!A{r}: [复合题拆分] 表格行号应为整数，收到 {num!r}")
        elif len(descs) < 2:
            errors.append(f"{tab}!B{r}: [复合题拆分] 第 {num} 行至少要拆成 2 个描述")
        elif int(num) in cfg.compound:
            errors.append(f"{tab}!A{r}: [复合题拆分] 表格行号 {num} 重复")
        else:
            cfg.compound[int(num)] = descs

    seen_items: set[str] = set()
    for r, row in sections.get(SEC_ITEMS, []):
        iid, cat, mode, stable = (_cell(row, i) for i in range(4))
        if not iid:
            errors.append(f"{tab}!A{r}: [逐题设置] 缺少题号")
            continue
        if iid in seen_items:
            errors.append(f"{tab}!A{r}: [逐题设置] 题号 {iid} 重复")
        seen_items.add(iid)
        mode_u = mode.upper()
        if mode_u not in ("", "POINT", "WINDOW"):
            errors.append(f"{tab}!C{r}: 时间判定模式只能是 POINT、WINDOW 或留空，收到 {mode!r}")
        if stable.lower() not in YES | NO:
            errors.append(f"{tab}!D{r}: 稳定基线题只能填 是 / 否 或留空，收到 {stable!r}")
        cfg.items.append(ItemSetting(iid, cat, mode_u, stable.lower() in YES, r))

    if errors:
        raise GoldenSheetError("测评配置页有问题：\n  - " + "\n  - ".join(errors))
    return cfg


def render_config_rows(cfg: GoldenSheetConfig) -> list[list[str]]:
    """The ``测评配置`` tab content for ``cfg`` (inverse of ``parse_config``)."""
    rows: list[list[str]] = [
        ["# 测评配置：评测程序据此把本表的人工标注页转成黄金集。改动标注页或本页后，下次评测自动使用新内容并生成新的黄金集版本。"],
        ["# 格式说明见代码仓库 eval/data/README.md。空行和以 # 开头的行会被忽略。"],
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
    rows += [[], [f"[{SEC_COMPOUND}]"], ["表格行号"] + [f"拆分描述 {i + 1}" for i in range(width)]]
    rows += [[str(k)] + v for k, v in sorted(cfg.compound.items())]
    rows += [[], [f"[{SEC_ITEMS}]"], HEADERS[SEC_ITEMS]]
    rows += [[it.item_id, it.sop_category, it.temporal_mode, "是" if it.stable_baseline else "否"] for it in cfg.items]
    return rows


def config_from_files(build_cfg: Mapping[str, Any], manifest: Mapping[str, Any], *, name: str,
                      label_range: str, sheet_id: str) -> GoldenSheetConfig:
    """Renders a build config + manifest (the file-based golden definition) into a config-tab model."""
    if build_cfg.get("item_fields"):
        raise GoldenSheetError("build config 里的 item_fields 请改写到 [逐题设置]（会进入 manifest，黄金集版本会变）")
    folders = []
    for i, f in enumerate(manifest.get("folders") or [], 1):
        label = str(f.get("label") or "")
        if " | " not in label:
            raise GoldenSheetError(f"manifest folder {f.get('group_id')!r} 的 label {label!r} 不是 '门店 | 稽核重点'")
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
            q=VIDEO_QUERY.format(folder=folder_id), fields="nextPageToken, files(id, name, videoMediaMetadata)",
            pageSize=1000, pageToken=token, supportsAllDrives=True, includeItemsFromAllDrives=True,
        ).execute(num_retries=3)
        files += page.get("files", [])
        token = page.get("nextPageToken")
        if not token:
            break
    out = []
    for f in files:
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
        return f"{what} 打不开（HTTP {status}）：请把它以「查看者」或以上权限共享给评测使用的 Workspace 身份"
    return f"{what} 读取失败：{type(exc).__name__}: {exc}"


def load_golden_from_sheet(sheet_id: str, *, sheets: Any, drive: Any) -> GoldenSet:
    """Reads the label tab + ``测评配置`` tab, lists the folders and returns the GoldenSet."""
    try:
        meta = drive.files().get(fileId=sheet_id, fields="name, modifiedTime, version",
                                 supportsAllDrives=True).execute(num_retries=3)
        config_rows = sheets.spreadsheets().values().get(
            spreadsheetId=sheet_id, range=f"'{CONFIG_TAB}'").execute(num_retries=3).get("values", [])
    except Exception as exc:  # noqa: BLE001 - mapped to an operator message
        text = str(exc)
        if "Unable to parse range" in text:
            raise GoldenSheetError(f"表格 {sheet_id} 里没有「{CONFIG_TAB}」页：请先运行 "
                                   f"python -m eval.golden_sheet init-config 或按 eval/data/README.md 手工添加") from exc
        raise GoldenSheetError(_http_hint(exc, f"标注表 {sheet_id}")) from exc
    cfg = parse_config(config_rows)
    width = range_width(cfg.label_range)
    try:
        label_values = sheets.spreadsheets().values().get(
            spreadsheetId=sheet_id, range=cfg.label_range).execute(num_retries=3).get("values", [])
    except Exception as exc:  # noqa: BLE001
        raise GoldenSheetError(_http_hint(exc, f"标注页 {cfg.label_range}")) from exc
    values = pad_rows(label_values, width)

    listings: dict[str, list[dict[str, Any]]] = {}
    filename_index: dict[str, str] = {}
    problems: list[str] = []
    for f in cfg.folders:
        try:
            listings[f.folder_id] = list_folder_videos(drive, f.folder_id)
        except Exception as exc:  # noqa: BLE001
            raise GoldenSheetError(_http_hint(exc, f"视频文件夹 {f.folder_id}（{CONFIG_TAB}!D{f.row}）")) from exc
        if not listings[f.folder_id]:
            problems.append(f"{CONFIG_TAB}!D{f.row}: 文件夹 {f.folder_id} 里没有视频")
        for v in listings[f.folder_id]:
            prev = filename_index.setdefault(v["filename"], v["file_id"])
            if prev != v["file_id"]:
                problems.append(f"视频文件名 {v['filename']!r} 在配置的文件夹里出现了不止一次（{prev} / {v['file_id']}），"
                                "标注无法唯一对应")
    if problems:
        raise GoldenSheetError("视频文件夹有问题：\n  - " + "\n  - ".join(problems))

    try:
        records = build_golden_records(
            values, filename_index, source_sheet_id=cfg.source_sheet_id or sheet_id,
            source_sha256=label_values_sha256(values), dev_groups=set(cfg.dev_groups),
            text_compound_parts=cfg.compound, expected_rows=cfg.expected_rows)
    except (GoldenBuildError, ValueError) as exc:
        msg = str(exc)
        if "not found in the Drive listing" in msg:
            msg = ("标注里的视频文件名在配置的文件夹中找不到（必须完全一致，包括空格和括号）：\n  "
                   + msg.split(":", 1)[1].strip().replace("\n  ", "\n  "))
        raise GoldenSheetError(f"标注页 {cfg.label_range} 无法转成黄金集：{msg}") from exc
    manifest = build_manifest(cfg, listings)
    origin = {
        "type": "google_sheet", "sheet_id": sheet_id, "sheet_name": str(meta.get("name") or ""),
        "sheet_modified_time": str(meta.get("modifiedTime") or ""), "sheet_version": str(meta.get("version") or ""),
        "label_range": cfg.label_range, "config_tab": CONFIG_TAB,
        "sheet_url": f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit",
    }
    return build_golden(records_to_jsonl(records).encode("utf-8"), manifest, stem=cfg.name,
                        source=f"sheet:{sheet_id}", manifest_source=f"sheet:{sheet_id}#{CONFIG_TAB}", origin=origin)


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
        f"黄金集版本: {golden.version}（{VERSION_SCHEME}：只随评分标尺变化）",
        f"评分标尺哈希: golden={golden.golden_sha256[:16]} manifest={golden.manifest_sha256[:16] or '-'}",
        f"全文哈希（审计用）: golden={golden.golden_sha256_full[:16]} manifest={golden.manifest_sha256_full[:16] or '-'}",
        f"来源: {golden.origin.get('sheet_name', '')} {golden.origin.get('sheet_url', golden.source)}"
        f"（修改时间 {golden.origin.get('sheet_modified_time', '?')}）",
        f"题数 {golden.item_count} / 标注点 {golden.part_count} / 划分 {golden.split_counts}",
        f"稳定基线题: {', '.join(stable) or '未配置'}（{stable_src}）",
        "视频文件夹:",
    ]
    total = 0
    for s in derive_folder_specs(golden):
        n_lab = sum(1 for v in s["videos"] if v["file_id"] in labelled)
        total += len(s["videos"])
        lines.append(f"  - {s['group_id']} {s['label']} ({s['folder_id']}): {len(s['videos'])} 段视频，"
                     f"其中 {n_lab} 段有标注、{len(s['videos']) - n_lab} 段无标注；缓存任务 {s['cache_job_ids'] or '无'}")
    lines.append(f"合计 {total} 段视频")
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
        return f"对比：{previous} 里没有 golden_version，无法对比"
    if prev_scheme != VERSION_SCHEME:
        return (f"对比：{previous} 使用旧的版本方案（{prev_scheme or 'legacy'}），版本号不可直接比较；"
                "请用同一方案下的 score.json 或版本号对比")
    if prev_version != golden.version:
        return f"对比：评分标尺已变化（{prev_version} → {golden.version}），召回率不能与之前直接比较"
    if prev_full == ("", ""):
        return f"对比：评分标尺未变（{golden.version}），召回率可比"
    if prev_full == (golden.golden_sha256_full, golden.manifest_sha256_full):
        return f"对比：完全相同（{golden.version}）"
    return (f"对比：评分标尺未变（{golden.version}），只有非评分字段变化（备注、视频元数据、缓存任务、"
            "Drive 文件夹 ID 或来源信息），召回率可比")


def init_config_plan(*, existing_titles: Sequence[str], rows: list[list[str]], overwrite: bool) -> dict[str, Any]:
    """What ``init-config`` will send: only the ``测评配置`` tab is ever created / cleared / written."""
    exists = CONFIG_TAB in existing_titles
    if exists and not overwrite:
        raise GoldenSheetError(f"表格里已经有「{CONFIG_TAB}」页；确认要覆盖请加 --overwrite（只会改这一页）")
    return {
        "add_sheet": None if exists else {"requests": [{"addSheet": {"properties": {"title": CONFIG_TAB}}}]},
        "clear_range": f"'{CONFIG_TAB}'" if exists else None,
        "update": {"range": f"'{CONFIG_TAB}'!A1", "valueInputOption": "RAW", "body": {"values": rows}},
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
            print(f"已写入「{CONFIG_TAB}」页（{len(rows)} 行）；其他页未改动。下一步：python -m eval.golden_sheet check "
                  f"--sheet-id {args.sheet_id}")
            return 0
        sheets, drive = (services or default_services)()
        golden = load_golden_from_sheet(args.sheet_id, sheets=sheets, drive=drive)
        print(check_report(golden))
        if args.compare:
            print(compare_report(golden, args.compare))
        return 0
    except GoldenSetError as exc:
        print(f"问题：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
