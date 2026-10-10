#!/usr/bin/env python3
"""Per-run Google Sheet report for CCTV AI Audit evaluation rounds (Round 67).

Every eval run creates ONE new spreadsheet in the Drive folder ``EVAL_RESULTS_FOLDER_ID`` (Terraform
variable ``eval_results_folder_id``), named ``CHAGEE AI稽核测评_<round>_<run>_<YYYYMMDD-HHMMSS>``
with the timestamp in ``EVAL_REPORT_TIME_ZONE`` (Terraform ``scheduler_time_zone``; default
Asia/Singapore). Four tabs:

1. ``本次测评概览``  this run's headline numbers (record built by eval/eval_records.py).
2. ``本次逐题结果``  one row per golden part, copied from this run's ``score.json`` (never re-judged).
3. ``历史轮次对比``  one row per round (runs of the same round/model/SOP/media mode averaged) + 3 charts.
4. ``历史单跑明细``  one row per run in ``eval_history.jsonl`` + 1 chart.

Tabs 3/4 and their charts only include runs scored on the SAME ``golden_version`` as this run (recall
on different golden sets is not comparable); a note cell says how many were left out. Every count,
category, outlet and label comes from the data (score.json / history), never from constants.

Every chart's X axis is the round / run label column (text), never a date. Scoring happens once,
locally, in eval/score_run.py; the Sheet only re-displays those numbers.

Layout: pure functions build the cell values and the Sheets ``batchUpdate`` request bodies
(``build_report_spec``); ``create_report_spreadsheet`` is the thin Drive/Sheets layer;
``publish_run_sheet_report_safely`` is what eval/run_gcp_round.py calls and never raises.

Backfill an existing run without re-running the eval::

    python -m eval.sheet_report --history eval/rounds/eval_history.jsonl \\
        --score eval/results/<run_id>/score.json --folder-id <Drive folder ID> [--dry-run]
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import sys
from typing import Any, Callable, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

THIS_DIR = Path(__file__).resolve().parent
CODE_ROOT = THIS_DIR.parent
if str(CODE_ROOT) not in sys.path:
    sys.path.insert(0, str(CODE_ROOT))

from eval.eval_records import compute_all_round_averages  # noqa: E402
from eval.tune_loop import MAX_MEAN_ALERT_DENSITY  # noqa: E402

logger = logging.getLogger("eval.sheet_report")

ENV_FOLDER_ID = "EVAL_RESULTS_FOLDER_ID"
ENV_TIME_ZONE = "EVAL_REPORT_TIME_ZONE"
DEFAULT_TIME_ZONE = "Asia/Singapore"
TITLE_PREFIX = "CHAGEE AI稽核测评"
SPREADSHEET_MIME = "application/vnd.google-apps.spreadsheet"
REPORT_JSON_NAME = "sheet_report.json"

TAB_OVERVIEW = "本次测评概览"
TAB_ITEMS = "本次逐题结果"
TAB_ROUNDS = "历史轮次对比"
TAB_RUNS = "历史单跑明细"
TAB_GOLDEN = "本次黄金集快照"
# Fixed sheet IDs so the pure layer can address every tab; the spreadsheet's default sheet is deleted.
SHEET_IDS = {TAB_OVERVIEW: 101, TAB_ITEMS: 102, TAB_ROUNDS: 103, TAB_RUNS: 104, TAB_GOLDEN: 105}

PERCENT = {"type": "PERCENT", "pattern": "0.0%"}
USD = {"type": "NUMBER", "pattern": "$0.0000"}
DECIMAL_2 = {"type": "NUMBER", "pattern": "0.00"}
INTEGER = {"type": "NUMBER", "pattern": "#,##0"}

CHART_WIDTH_PX = 900
CHART_HEIGHT_PX = 380
CHART_ROW_STRIDE = 21  # rows between stacked charts (~380 px at the default 21 px row height)


# --------------------------------------------------------------------------------------- pure layer


@dataclass
class Table:
    """One tab: header row + data rows, plus number formats by column (data rows) or by cell."""

    title: str
    rows: list[list[Any]]
    col_formats: dict[int, dict[str, str]] = field(default_factory=dict)
    cell_formats: list[tuple[int, int, dict[str, str]]] = field(default_factory=list)
    wide_cols: dict[int, int] = field(default_factory=dict)  # column -> pixel width (wrapped text)
    note: str = ""  # written to the right of the header row (row 1, one blank column after the table)

    @property
    def sheet_id(self) -> int:
        return SHEET_IDS[self.title]

    def col(self, header: str) -> int:
        return self.rows[0].index(header)


@dataclass
class ReportSpec:
    title: str
    time_zone: str
    created_at_local: str
    tables: list[Table]
    chart_requests: list[dict[str, Any]]


def resolve_time_zone(name: str | None) -> ZoneInfo:
    tz_name = (name or "").strip() or DEFAULT_TIME_ZONE
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(
            f"EVAL_REPORT_TIME_ZONE={tz_name!r} 不是合法的 IANA 时区（例如 Asia/Singapore）"
        ) from exc


def build_report_title(round_id: str, run_id: str, now_utc: datetime, tz: ZoneInfo) -> str:
    stamp = now_utc.astimezone(tz).strftime("%Y%m%d-%H%M%S")
    return f"{TITLE_PREFIX}_{round_id}_{run_id}_{stamp}"


def _sop_short(sop_version: str) -> str:
    s = str(sop_version or "unknown")
    return s[len("Prompt_"):] if s.startswith("Prompt_") else s


def _blank(v: Any) -> Any:
    return "" if v is None else v


def round_labels(round_records: Sequence[Mapping[str, Any]]) -> list[str]:
    """``r00 · v2.5_r00``; model / media mode are appended only when needed to stay unique."""
    base = [f"{r.get('round_id')} · {_sop_short(str(r.get('sop_version')))}" for r in round_records]
    out: list[str] = []
    for lbl, r in zip(base, round_records):
        if base.count(lbl) > 1:
            lbl = f"{lbl} · {r.get('model_version')} · {r.get('media_mode')}"
        out.append(lbl)
    return out


def run_label(record: Mapping[str, Any]) -> str:
    return f"{record.get('round_id')} · {record.get('run_id')}"


def history_up_to_run(history: Sequence[dict[str, Any]], run_id: str) -> list[dict[str, Any]]:
    """History as of ``run_id`` (inclusive, file order). Raises if the run is not in the history."""
    for i, rec in enumerate(history):
        if rec.get("run_id") == run_id:
            return list(history[: i + 1])
    raise ValueError(f"run_id {run_id!r} 不在 eval_history.jsonl 中")


def golden_version_of(record: Mapping[str, Any]) -> str:
    from eval.eval_records import GOLDEN_UNVERSIONED

    return str(record.get("golden_version") or GOLDEN_UNVERSIONED)


def same_golden_history(
    history: Sequence[dict[str, Any]], run_record: Mapping[str, Any]
) -> tuple[list[dict[str, Any]], str]:
    """Only runs scored on this run's golden version, plus the note that says what was left out."""
    ver = golden_version_of(run_record)
    kept = [r for r in history if golden_version_of(r) == ver]
    other = [r for r in history if golden_version_of(r) != ver]
    other_rounds = {(r.get("round_id"), golden_version_of(r)) for r in other}
    note = f"仅对比使用同一版黄金集 {ver} 的轮次"
    note += (f"；其他版本 {len(other_rounds)} 轮（{len(other)} 次运行）未纳入" if other else "；无其他版本的历史运行")
    return kept, note


def build_overview_table(
    run_record: Mapping[str, Any],
    score_doc: Mapping[str, Any],
    *,
    created_at_local: str,
    time_zone: str,
) -> Table:
    m = run_record.get("metrics") or {}
    golden = score_doc.get("golden") or {}
    n_items = int((score_doc.get("recall") or {}).get("all", {}).get("rows")
                  or run_record.get("golden_item_count") or 0)
    splits = run_record.get("golden_split_counts") or golden.get("split_counts") or {}
    density = score_doc.get("alert_density") or {}
    rows: list[list[Any]] = [["指标", "数值", "说明"]]
    cells: list[tuple[int, int, dict[str, str]]] = []

    def add(label: str, value: Any, note: str = "", fmt: dict[str, str] | None = None) -> None:
        rows.append([label, _blank(value), note])
        if fmt is not None and value is not None:
            cells.append((len(rows) - 1, 1, fmt))

    add("测评轮次 (round_id)", run_record.get("round_id"))
    add("运行 ID (run_id)", run_record.get("run_id"))
    add(f"报告生成时间 ({time_zone})", created_at_local)
    add("评分完成时间 (scored_at, UTC)", score_doc.get("scored_at") or run_record.get("timestamp"))
    add("稽核模型 (model_version)", run_record.get("model_version"))
    add("SOP / 提示词版本 (sop_version)", run_record.get("sop_version"))
    add("视频理解模式 (media_mode)", run_record.get("media_mode"))
    add("黄金集版本 (golden_version)", run_record.get("golden_version") or golden.get("golden_version"),
        "文件名@评分标尺哈希（ruler-v2）；不同版本的召回率不可比")
    origin = golden.get("golden_origin") or {}
    add("黄金集来源", origin.get("sheet_url") or golden.get("golden_source") or "",
        (f"{origin.get('sheet_name', '')}（{origin.get('label_range', '')} + 「{origin.get('config_tab', '')}」页，"
         f"读取时表格修改时间 {origin.get('sheet_modified_time', '')}）") if origin else "黄金集文件")
    add("黄金集题数 / 标注点数", f"{n_items} 题 / {run_record.get('golden_part_count') or golden.get('part_count') or ''} 个标注点",
        "；".join(f"{k} {v} 题" for k, v in splits.items()))
    add("裁判模型 (judge_model)", run_record.get("judge_model") or score_doc.get("judge_model"),
        f"每题裁判 {score_doc.get('judge_passes', '')} 次取中位" if score_doc.get("judge_passes") else "")
    rec = score_doc.get("recall") or {}
    add(f"总体召回率 ({n_items} 题)", m.get("overall_recall"), "权威数字：eval/score_run.py 本地评分", PERCENT)
    add(f"留出集召回率 (holdout, {(rec.get('holdout') or {}).get('rows', 0)} 题)", m.get("holdout_recall"), "", PERCENT)
    add(f"开发集召回率 (dev, {(rec.get('dev') or {}).get('rows', 0)} 题)", m.get("dev_recall"), "", PERCENT)
    add("仅 CONFIRMED 召回率", m.get("confirmed_only_recall"), "只算 CONFIRMED 处置的告警", PERCENT)
    add("平均告警数 / 视频 (findings_per_clip)", m.get("findings_per_clip"),
        f"守卫线 ≤ {MAX_MEAN_ALERT_DENSITY:.1f}", DECIMAL_2)
    add("告警总数", m.get("total_findings"), f"{density.get('clips', '')} 段视频", INTEGER)
    add("告警命中率 (hit_rate)", m.get("hit_rate"), "命中人工标注的告警 / 全部告警", PERCENT)
    stable_state = m.get("stable_guardrail") or "not_configured"
    stable_ids = run_record.get("stable_baseline_items") or golden.get("stable_baseline_items") or []
    add("稳定基线题退化数 (regressed_stable_items)", m.get("regressed_stable_items"),
        {"pass": f"稳定基线 {len(stable_ids)} 题全部保持 1 分", "fail": f"稳定基线 {len(stable_ids)} 题中有退化",
         }.get(stable_state, "未配置稳定基线题（黄金集 manifest stable_baseline_items），本项不检查"), INTEGER)
    add("护栏是否通过 (guardrails_passed)", "是" if m.get("guardrails_passed") else "否",
        f"告警密度 ≤ {MAX_MEAN_ALERT_DENSITY:.1f} 且稳定基线零退化")
    add("同轮次翻转率 (flip_rate)", m.get("flip_rate"), "同一轮次多次运行间得分变化的题目比例", PERCENT)
    add("瞬时动作时间漂移均值 (秒)", m.get("mean_point_timestamp_drift_sec"), "仅 POINT 规则 (±20s)", DECIMAL_2)
    add("瞬时动作时间漂移最大 (秒)", m.get("max_point_timestamp_drift_sec"), "", INTEGER)
    add("单视频平均成本 (USD)", m.get("cost_per_clip_usd"), "", USD)
    add("本次总成本 (USD)", m.get("total_cost_usd"), "稽核模型调用，不含裁判", USD)
    add("单视频平均耗时 (秒)", m.get("mean_clip_latency_sec"), "", DECIMAL_2)
    add("单视频最大耗时 (秒)", m.get("max_clip_latency_sec"), "", DECIMAL_2)
    add("总 Token 数", m.get("total_token_count"), "", INTEGER)
    rows.append([
        "说明",
        "召回率由 eval/score_run.py 在本地评分一次得出，是权威数字；本表只展示 score.json 与 "
        "eval_history.jsonl 中的结果，不做二次评分。",
        "",
    ])
    return Table(TAB_OVERVIEW, rows, cell_formats=cells, wide_cols={1: 420, 2: 360})


def _score_badge(score: float) -> str:
    if score >= 1.0:
        return "✓ 命中"
    if score >= 0.5:
        return "◐ 半对"
    return "✗ 漏检"


def build_items_table(
    score_doc: Mapping[str, Any],
    golden_items: Sequence[Mapping[str, Any]] = (),
) -> Table:
    golden_parts: dict[str, Mapping[str, Any]] = {}
    for g in golden_items:
        for p in g.get("parts") or []:
            golden_parts[str(p.get("part_id"))] = p

    header = [
        "part_id", "题号 (item_id)", "门店", "稽核重点", "SOP 大类", "时间判定模式", "时间窗 (±秒)",
        "数据集划分", "人工标注原文", "标注时间点 (OSD)", "本题得分", "整题得分", "判定",
        "时间模式来源", "SOP 大类来源",
        "匹配的 AI 告警", "时间漂移 (秒)", "裁判理由",
    ]
    rows: list[list[Any]] = [header]
    for item in score_doc.get("items") or []:
        matched_by_id = {str(f.get("finding_id")): f for f in item.get("matched") or []}
        for part in item.get("parts") or []:
            pid = str(part.get("part_id") or "")
            gpart = golden_parts.get(pid) or {}
            matched_txt = "\n".join(
                f"{fid} [{f.get('rule_id', '')} · {f.get('disposition', '')} · {f.get('osd', '')}] "
                f"{f.get('evidence', '')}"
                for fid in part.get("matched_ids") or []
                for f in [matched_by_id.get(str(fid), {})]
            )
            score = float(part.get("score") or 0.0)
            rows.append([
                pid,
                item.get("item_id", ""),
                item.get("outlet_name", ""),
                item.get("focus", ""),
                item.get("sop_category", ""),
                part.get("temporal_mode", ""),
                _blank(part.get("window_sec")),
                item.get("split", ""),
                gpart.get("description") or item.get("finding_verbatim", ""),
                ", ".join(gpart.get("osd_times") or []),
                score,
                float(item.get("score") or 0.0),
                _score_badge(score),
                {"explicit": "黄金集指定", "heuristic": "规则推断"}.get(
                    str(part.get("temporal_mode_source") or ""), "未记录"),
                {"explicit": "黄金集指定", "audit_clause": "取稽核条款原文", "focus": "取稽核重点",
                 "none": "未分类"}.get(str(item.get("sop_category_source") or ""), "未记录"),
                matched_txt,
                _blank(part.get("timestamp_drift_sec")),
                part.get("explanation", ""),
            ])
    t = Table(TAB_ITEMS, rows)
    t.wide_cols = {t.col("人工标注原文"): 320, t.col("匹配的 AI 告警"): 420, t.col("裁判理由"): 420}
    return t


def build_golden_snapshot_table(golden_items: Sequence[Mapping[str, Any]], stable_ids: Sequence[str] = ()) -> Table:
    """One row per golden part exactly as this run used it (category / time mode with their source)."""
    from eval.score_run import sop_category_and_source, temporal_mode_and_source

    header = ["题号", "part_id", "门店", "稽核重点", "数据集划分", "稽核条款", "人工标注原文", "标注时间点 (OSD)",
              "视频文件名", "SOP 大类", "SOP 大类来源", "时间判定模式", "时间模式来源", "稳定基线题"]
    rows: list[list[Any]] = [header]
    for it in golden_items:
        cat, cat_src = sop_category_and_source(dict(it))
        for p in it.get("parts") or []:
            mode, mode_src = temporal_mode_and_source(dict(it), dict(p))
            rows.append([
                it.get("item_id", ""), p.get("part_id", ""), it.get("outlet_name", ""), it.get("focus", ""),
                it.get("split", ""), it.get("audit_clause", ""), p.get("description") or it.get("finding_verbatim", ""),
                ", ".join(p.get("osd_times") or []), "\n".join(it.get("video_filenames") or []),
                cat, cat_src, mode, mode_src, "是" if it.get("stable_baseline") or it.get("item_id") in stable_ids else "",
            ])
    t = Table(TAB_GOLDEN, rows)
    t.wide_cols = {t.col("人工标注原文"): 360, t.col("视频文件名"): 320}
    return t


def build_rounds_table(history: Sequence[dict[str, Any]]) -> tuple[Table, list[str], list[str]]:
    """One row per round average. Returns (table, sop category headers, outlet x focus headers)."""
    rounds = compute_all_round_averages(history)
    labels = round_labels(rounds)
    cats = sorted({k for r in rounds for k in (r.get("sop_category_recall") or {})})
    ofs = sorted({k for r in rounds for k in (r.get("outlet_focus_recall") or {})})
    header = [
        "轮次", "round_id", "SOP 版本", "模型版本", "视频模式", "运行次数", "run_ids",
        "总体召回率", "留出集召回率", "开发集召回率", "仅 CONFIRMED 召回率", "告警命中率",
        *cats, *ofs,
        "平均告警数 / 视频", "时间漂移均值 (秒)", "单视频平均成本 (USD)",
    ]
    rows: list[list[Any]] = [header]
    for lbl, r in zip(labels, rounds):
        m = r.get("metrics") or {}
        rows.append([
            lbl, r.get("round_id", ""), r.get("sop_version", ""), r.get("model_version", ""),
            r.get("media_mode", ""), r.get("runs_count", 0), ", ".join(r.get("run_ids") or []),
            _blank(m.get("overall_recall")), _blank(m.get("holdout_recall")), _blank(m.get("dev_recall")),
            _blank(m.get("confirmed_only_recall")), _blank(m.get("hit_rate")),
            *[_blank(((r.get("sop_category_recall") or {}).get(c) or {}).get("recall")) for c in cats],
            *[_blank(((r.get("outlet_focus_recall") or {}).get(o) or {}).get("recall")) for o in ofs],
            _blank(m.get("findings_per_clip")), _blank(m.get("mean_point_timestamp_drift_sec")),
            _blank(m.get("cost_per_clip_usd")),
        ])
    t = Table(TAB_ROUNDS, rows)
    pct = ["总体召回率", "留出集召回率", "开发集召回率", "仅 CONFIRMED 召回率", "告警命中率", *cats, *ofs]
    t.col_formats = {t.col(h): PERCENT for h in pct}
    t.col_formats[t.col("平均告警数 / 视频")] = DECIMAL_2
    t.col_formats[t.col("时间漂移均值 (秒)")] = DECIMAL_2
    t.col_formats[t.col("单视频平均成本 (USD)")] = USD
    return t, cats, ofs


GUARDRAIL_HEADER = f"告警密度守卫线 ({MAX_MEAN_ALERT_DENSITY:.1f})"


def build_runs_table(history: Sequence[dict[str, Any]], current_run_id: str) -> Table:
    header = [
        "运行", "本次", "round_id", "run_id", "SOP 版本", "模型版本", "视频模式",
        "总体召回率", "留出集召回率", "开发集召回率", "平均告警数 / 视频", GUARDRAIL_HEADER,
        "时间漂移均值 (秒)", "单视频平均成本 (USD)", "总成本 (USD)", "单视频平均耗时 (秒)", "护栏通过",
    ]
    rows: list[list[Any]] = [header]
    for rec in history:
        m = rec.get("metrics") or {}
        rows.append([
            run_label(rec), "★" if rec.get("run_id") == current_run_id else "",
            rec.get("round_id", ""), rec.get("run_id", ""), rec.get("sop_version", ""),
            rec.get("model_version", ""), rec.get("media_mode", ""),
            _blank(m.get("overall_recall")), _blank(m.get("holdout_recall")), _blank(m.get("dev_recall")),
            _blank(m.get("findings_per_clip")), MAX_MEAN_ALERT_DENSITY,
            _blank(m.get("mean_point_timestamp_drift_sec")), _blank(m.get("cost_per_clip_usd")),
            _blank(m.get("total_cost_usd")), _blank(m.get("mean_clip_latency_sec")),
            "是" if m.get("guardrails_passed") else "否",
        ])
    t = Table(TAB_RUNS, rows)
    t.col_formats = {t.col(h): PERCENT for h in ("总体召回率", "留出集召回率", "开发集召回率")}
    for h in ("平均告警数 / 视频", GUARDRAIL_HEADER, "时间漂移均值 (秒)", "单视频平均耗时 (秒)"):
        t.col_formats[t.col(h)] = DECIMAL_2
    for h in ("单视频平均成本 (USD)", "总成本 (USD)"):
        t.col_formats[t.col(h)] = USD
    return t


def _col_range(sheet_id: int, col: int, n_rows: int) -> dict[str, Any]:
    return {"sourceRange": {"sources": [{
        "sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": n_rows,
        "startColumnIndex": col, "endColumnIndex": col + 1,
    }]}}


def build_chart_request(
    table: Table,
    *,
    title: str,
    domain_header: str,
    series: Sequence[tuple[str, str, str]],  # (header, "COLUMN"|"LINE", "LEFT_AXIS"|"RIGHT_AXIS")
    bottom_title: str,
    left_title: str,
    right_title: str = "",
    anchor_row: int,
) -> dict[str, Any]:
    """A native ``basicChart`` whose domain is the text label column, so the X axis is categorical."""
    n = len(table.rows)
    types = {s[1] for s in series}
    chart_type = "COMBO" if len(types) > 1 else next(iter(types))
    axis = [{"position": "BOTTOM_AXIS", "title": bottom_title},
            {"position": "LEFT_AXIS", "title": left_title}]
    if right_title:
        axis.append({"position": "RIGHT_AXIS", "title": right_title})
    return {"addChart": {"chart": {
        "spec": {
            "title": title,
            "basicChart": {
                "chartType": chart_type,
                "legendPosition": "BOTTOM_LEGEND",
                "headerCount": 1,
                "axis": axis,
                "domains": [{"domain": _col_range(table.sheet_id, table.col(domain_header), n)}],
                "series": [
                    {"series": _col_range(table.sheet_id, table.col(h), n), "type": typ, "targetAxis": ax}
                    for h, typ, ax in series
                ],
            },
        },
        "position": {"overlayPosition": {
            "anchorCell": {"sheetId": table.sheet_id, "rowIndex": anchor_row, "columnIndex": 0},
            "widthPixels": CHART_WIDTH_PX, "heightPixels": CHART_HEIGHT_PX,
        }},
    }}}


def build_chart_requests(rounds: Table, cats: Sequence[str], ofs: Sequence[str], runs: Table) -> list[dict]:
    # Charts 2 and 3 put rounds on the X axis with one series per SOP category / outlet x focus: the
    # series count is fixed (3 / 4) while the X axis grows by one group per round, so the legend
    # stays readable for any N and all three round charts share the same X axis as chart 1.
    base = len(rounds.rows) + 2
    reqs = [
        build_chart_request(
            rounds, title="图1 各轮次核心召回率与告警命中率", domain_header="轮次",
            series=[("总体召回率", "COLUMN", "LEFT_AXIS"), ("留出集召回率", "COLUMN", "LEFT_AXIS"),
                    ("开发集召回率", "COLUMN", "LEFT_AXIS"), ("告警命中率", "LINE", "LEFT_AXIS")],
            bottom_title="轮次 · SOP 版本", left_title="比例", anchor_row=base),
    ]
    if cats:
        reqs.append(build_chart_request(
            rounds, title="图2 各轮次按 SOP 大类召回率", domain_header="轮次",
            series=[(c, "COLUMN", "LEFT_AXIS") for c in cats],
            bottom_title="轮次 · SOP 版本", left_title="召回率", anchor_row=base + CHART_ROW_STRIDE))
    if ofs:
        reqs.append(build_chart_request(
            rounds, title="图3 各轮次按门店 × 稽核重点召回率", domain_header="轮次",
            series=[(o, "COLUMN", "LEFT_AXIS") for o in ofs],
            bottom_title="轮次 · SOP 版本", left_title="召回率", anchor_row=base + 2 * CHART_ROW_STRIDE))
    reqs.append(build_chart_request(
        runs, title=f"图4 每次运行的告警密度 / 时间漂移 / 单视频成本（守卫线 {MAX_MEAN_ALERT_DENSITY:.1f} 条/视频）",
        domain_header="运行",
        series=[("平均告警数 / 视频", "COLUMN", "LEFT_AXIS"), ("时间漂移均值 (秒)", "COLUMN", "LEFT_AXIS"),
                (GUARDRAIL_HEADER, "LINE", "LEFT_AXIS"), ("单视频平均成本 (USD)", "LINE", "RIGHT_AXIS")],
        bottom_title="轮次 · 运行", left_title="条/视频 · 秒", right_title="USD / 视频",
        anchor_row=len(runs.rows) + 2))
    return reqs


def build_report_spec(
    *,
    history: Sequence[dict[str, Any]],
    score_doc: Mapping[str, Any],
    run_record: Mapping[str, Any],
    golden_items: Sequence[Mapping[str, Any]] = (),
    now_utc: datetime | None = None,
    time_zone: str | None = None,
) -> ReportSpec:
    tz = resolve_time_zone(time_zone)
    now = now_utc or datetime.now(timezone.utc)
    run_id = str(run_record.get("run_id") or "")
    hist_all = history_up_to_run(history, run_id)
    hist, comparability_note = same_golden_history(hist_all, run_record)
    created_local = now.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S")
    overview = build_overview_table(run_record, score_doc, created_at_local=created_local, time_zone=tz.key)
    items = build_items_table(score_doc, golden_items)
    rounds, cats, ofs = build_rounds_table(hist)
    runs = build_runs_table(hist, run_id)
    rounds.note = runs.note = comparability_note
    return ReportSpec(
        title=build_report_title(str(run_record.get("round_id") or ""), run_id, now, tz),
        time_zone=tz.key,
        created_at_local=created_local,
        tables=[overview, items, rounds, runs, build_golden_snapshot_table(
            golden_items, (score_doc.get("golden") or {}).get("stable_baseline_items") or [])],
        chart_requests=build_chart_requests(rounds, cats, ofs, runs),
    )


def build_structure_requests(spec: ReportSpec, default_sheet_ids: Sequence[int]) -> list[dict[str, Any]]:
    reqs: list[dict[str, Any]] = [{"updateSpreadsheetProperties": {
        "properties": {"timeZone": spec.time_zone}, "fields": "timeZone"}}]
    for idx, t in enumerate(spec.tables):
        reqs.append({"addSheet": {"properties": {
            "sheetId": t.sheet_id, "title": t.title, "index": idx,
            "gridProperties": {
                "rowCount": max(len(t.rows) + 4 * CHART_ROW_STRIDE, 100),
                "columnCount": max(len(t.rows[0]) + 3, 26),
                "frozenRowCount": 1,
            },
        }}})
    reqs.extend({"deleteSheet": {"sheetId": sid}} for sid in default_sheet_ids
                if sid not in SHEET_IDS.values())
    return reqs


def _col_letter(idx: int) -> str:
    out = ""
    idx += 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        out = chr(65 + rem) + out
    return out


def build_value_ranges(spec: ReportSpec) -> list[dict[str, Any]]:
    ranges = [{"range": f"'{t.title}'!A1", "values": t.rows} for t in spec.tables]
    for t in spec.tables:
        if t.note:
            ranges.append({"range": f"'{t.title}'!{_col_letter(len(t.rows[0]) + 1)}1", "values": [[t.note]]})
    return ranges


def _fmt_req(sheet_id: int, r0: int, r1: int, c0: int, c1: int, fmt: dict[str, str]) -> dict[str, Any]:
    return {"repeatCell": {
        "range": {"sheetId": sheet_id, "startRowIndex": r0, "endRowIndex": r1,
                  "startColumnIndex": c0, "endColumnIndex": c1},
        "cell": {"userEnteredFormat": {"numberFormat": fmt}},
        "fields": "userEnteredFormat.numberFormat",
    }}


def build_format_requests(spec: ReportSpec) -> list[dict[str, Any]]:
    reqs: list[dict[str, Any]] = []
    for t in spec.tables:
        n_rows, n_cols = len(t.rows), len(t.rows[0])
        reqs.append({"repeatCell": {
            "range": {"sheetId": t.sheet_id, "startRowIndex": 0, "endRowIndex": 1,
                      "startColumnIndex": 0, "endColumnIndex": n_cols},
            "cell": {"userEnteredFormat": {"textFormat": {"bold": True},
                                           "backgroundColor": {"red": 0.91, "green": 0.94, "blue": 0.99}}},
            "fields": "userEnteredFormat(textFormat,backgroundColor)",
        }})
        for col, fmt in sorted(t.col_formats.items()):
            reqs.append(_fmt_req(t.sheet_id, 1, n_rows, col, col + 1, fmt))
        for row, col, fmt in t.cell_formats:
            reqs.append(_fmt_req(t.sheet_id, row, row + 1, col, col + 1, fmt))
        reqs.append({"autoResizeDimensions": {"dimensions": {
            "sheetId": t.sheet_id, "dimension": "COLUMNS", "startIndex": 0, "endIndex": n_cols}}})
        for col, px in sorted(t.wide_cols.items()):
            reqs.append({"updateDimensionProperties": {
                "range": {"sheetId": t.sheet_id, "dimension": "COLUMNS", "startIndex": col, "endIndex": col + 1},
                "properties": {"pixelSize": px}, "fields": "pixelSize"}})
            reqs.append({"repeatCell": {
                "range": {"sheetId": t.sheet_id, "startRowIndex": 0, "endRowIndex": n_rows,
                          "startColumnIndex": col, "endColumnIndex": col + 1},
                "cell": {"userEnteredFormat": {"wrapStrategy": "WRAP", "verticalAlignment": "TOP"}},
                "fields": "userEnteredFormat(wrapStrategy,verticalAlignment)",
            }})
    return reqs


# --------------------------------------------------------------------------------------- API layer


def default_services() -> tuple[Any, Any]:
    """Drive v3 + Sheets v4 clients on the stack's Workspace identity (keyless DWD, cctv_audit.gcp)."""
    from googleapiclient.discovery import build

    from cctv_audit.gcp import workspace_credentials

    creds = workspace_credentials()
    return (build("drive", "v3", credentials=creds, cache_discovery=False),
            build("sheets", "v4", credentials=creds, cache_discovery=False))


class SheetReportError(RuntimeError):
    """The spreadsheet was created but could not be completed; the message carries its URL."""


def create_report_spreadsheet(spec: ReportSpec, folder_id: str, *, drive: Any, sheets: Any) -> tuple[str, str]:
    """Creates the spreadsheet directly inside ``folder_id`` and fills it. Returns (id, url)."""
    created = drive.files().create(
        body={"name": spec.title, "mimeType": SPREADSHEET_MIME, "parents": [folder_id]},
        fields="id, webViewLink",
        supportsAllDrives=True,
    ).execute(num_retries=3)
    sid = str(created["id"])
    url = str(created.get("webViewLink") or f"https://docs.google.com/spreadsheets/d/{sid}/edit")
    try:
        meta = sheets.spreadsheets().get(
            spreadsheetId=sid, fields="sheets.properties.sheetId").execute(num_retries=3)
        default_ids = [int(s["properties"]["sheetId"]) for s in meta.get("sheets", [])]
        sheets.spreadsheets().batchUpdate(
            spreadsheetId=sid, body={"requests": build_structure_requests(spec, default_ids)}
        ).execute(num_retries=3)
        sheets.spreadsheets().values().batchUpdate(
            spreadsheetId=sid, body={"valueInputOption": "RAW", "data": build_value_ranges(spec)}
        ).execute(num_retries=3)
        sheets.spreadsheets().batchUpdate(
            spreadsheetId=sid, body={"requests": build_format_requests(spec) + spec.chart_requests}
        ).execute(num_retries=3)
    except Exception as exc:
        raise SheetReportError(f"表格已创建但未写完（{url}）：{exc}") from exc
    return sid, url


def _describe_error(exc: BaseException, folder_id: str) -> str:
    try:
        from cctv_audit.gcp import _classify_write_error

        mapped = _classify_write_error(exc, folder_id)
    except Exception:  # noqa: BLE001 - classification is best effort
        mapped = exc
    return f"{type(mapped).__name__}: {mapped}"


@dataclass
class SheetReportOutcome:
    status: str  # "created" | "skipped" | "failed"
    status_line: str
    spreadsheet_url: str = ""
    spreadsheet_id: str = ""
    title: str = ""
    error: str = ""


def _write_report_json(res_dir: Path | None, outcome: SheetReportOutcome, folder_id: str, tz: str) -> None:
    if res_dir is None:
        return
    try:
        res_dir.mkdir(parents=True, exist_ok=True)
        (res_dir / REPORT_JSON_NAME).write_text(json.dumps({
            "status": outcome.status,
            "spreadsheet_url": outcome.spreadsheet_url,
            "spreadsheet_id": outcome.spreadsheet_id,
            "title": outcome.title,
            "folder_id": folder_id,
            "time_zone": tz,
            "error": outcome.error,
            "written_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001 - never fail the run over this file
        logger.error("Could not write %s: %r", res_dir / REPORT_JSON_NAME, exc)


def load_history(history_path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in history_path.read_text(encoding="utf-8").splitlines() if line.strip()]


def publish_run_sheet_report_safely(
    *,
    history_path: Path,
    score_doc: Mapping[str, Any],
    run_record: Mapping[str, Any],
    golden_items: Sequence[Mapping[str, Any]] = (),
    res_dir: Path | None = None,
    folder_id: str | None = None,
    time_zone: str | None = None,
    now_utc: datetime | None = None,
    services: Callable[[], tuple[Any, Any]] | None = None,
    env: Mapping[str, str] | None = None,
) -> SheetReportOutcome:
    """Builds and publishes this run's Sheet. Never raises: a failure is logged at ERROR and returned."""
    env = os.environ if env is None else env
    folder = (folder_id if folder_id is not None else env.get(ENV_FOLDER_ID, "")).strip()
    tz_name = (time_zone if time_zone is not None else env.get(ENV_TIME_ZONE, "")).strip() or DEFAULT_TIME_ZONE
    if not folder:
        outcome = SheetReportOutcome(
            status="skipped",
            status_line=(
                f"Sheet report: SKIPPED — Terraform 变量 eval_results_folder_id 未设置（环境变量 "
                f"{ENV_FOLDER_ID} 为空），本次不生成 Google Sheet 测评报告"
            ),
        )
        logger.warning(outcome.status_line)
        _write_report_json(res_dir, outcome, folder, tz_name)
        return outcome
    title = ""
    try:
        spec = build_report_spec(history=load_history(history_path), score_doc=score_doc,
                                 run_record=run_record, golden_items=golden_items,
                                 now_utc=now_utc, time_zone=tz_name)
        title = spec.title
        drive, sheets = (services or default_services)()
        sid, url = create_report_spreadsheet(spec, folder, drive=drive, sheets=sheets)
    except Exception as exc:  # noqa: BLE001 - the eval run must survive any Sheet failure
        msg = _describe_error(exc, folder)
        logger.error("Sheet report FAILED for run %s (folder %s): %s",
                     run_record.get("run_id"), folder, msg, exc_info=True)
        outcome = SheetReportOutcome(
            status="failed", title=title, error=msg,
            status_line=(f"Sheet report: FAILED — {msg}（评测结果已保存在 score.json / GCS，本次评测不受影响；"
                         f"可用 python -m eval.sheet_report 补生成）"),
        )
        _write_report_json(res_dir, outcome, folder, tz_name)
        return outcome
    outcome = SheetReportOutcome(status="created", spreadsheet_url=url, spreadsheet_id=sid, title=title,
                                 status_line=f"Sheet report: {url}")
    logger.info("Sheet report created: %s (%s)", url, title)
    _write_report_json(res_dir, outcome, folder, tz_name)
    return outcome


# --------------------------------------------------------------------------------------- CLI


def main(argv: Sequence[str] | None = None) -> int:
    from eval.tune_loop import DEFAULT_GOLDEN_PATH

    parser = argparse.ArgumentParser(description="Build the per-run Google Sheet report for an existing eval run")
    parser.add_argument("--history", type=Path, required=True, help="eval/rounds/eval_history.jsonl")
    parser.add_argument("--score", type=Path, required=True, help="eval/results/<run_id>/score.json")
    parser.add_argument("--run-id", default=None, help="Default: score.json run_label")
    parser.add_argument("--golden", default=os.environ.get("EVAL_GOLDEN_URI", "") or str(DEFAULT_GOLDEN_PATH),
                        help="Golden the run was scored on (only for label texts / overrides); local or gs://")
    parser.add_argument("--folder-id", default=None, help=f"Default: env {ENV_FOLDER_ID}")
    parser.add_argument("--time-zone", default=None, help=f"Default: env {ENV_TIME_ZONE} or {DEFAULT_TIME_ZONE}")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the spreadsheet title, cell values and batchUpdate requests; call no API")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    score_doc = json.loads(args.score.read_text(encoding="utf-8"))
    run_id = args.run_id or str(score_doc.get("run_label") or "")
    history = load_history(args.history)
    run_record = next((r for r in history if r.get("run_id") == run_id), None)
    if run_record is None:
        print(f"run_id {run_id!r} not found in {args.history}", file=sys.stderr)
        return 2
    from eval.golden_set import load_golden, materialize_golden

    golden = load_golden(materialize_golden(str(args.golden))).items if args.golden else []
    tz_name = args.time_zone if args.time_zone is not None else os.environ.get(ENV_TIME_ZONE, "")

    if args.dry_run:
        spec = build_report_spec(history=history, score_doc=score_doc, run_record=run_record,
                                 golden_items=golden, time_zone=tz_name)
        print(json.dumps({
            "title": spec.title,
            "structure_requests": build_structure_requests(spec, [0]),
            "value_ranges": build_value_ranges(spec),
            "format_requests": build_format_requests(spec),
            "chart_requests": spec.chart_requests,
        }, ensure_ascii=False, indent=2))
        return 0

    folder = (args.folder_id if args.folder_id is not None else os.environ.get(ENV_FOLDER_ID, "")).strip()
    if not folder:
        print(f"No Drive folder: pass --folder-id or set {ENV_FOLDER_ID} "
              "(terraform output -raw eval_results_folder_id)", file=sys.stderr)
        return 2
    outcome = publish_run_sheet_report_safely(
        history_path=args.history, score_doc=score_doc, run_record=run_record, golden_items=golden,
        res_dir=args.score.parent, folder_id=folder, time_zone=tz_name)
    print(outcome.status_line)
    return 0 if outcome.status == "created" else 1


if __name__ == "__main__":
    sys.exit(main())
