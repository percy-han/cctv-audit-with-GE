#!/usr/bin/env python3
"""Per-run Google Sheet report for CCTV AI Audit evaluation rounds (Round 67).

Every eval run creates ONE new spreadsheet in the Drive folder ``EVAL_RESULTS_FOLDER_ID`` (Terraform
variable ``eval_results_folder_id``), named ``CHAGEE_AI_Audit_Eval_<round>_<run>_<YYYYMMDD-HHMMSS>``
with the timestamp in ``EVAL_REPORT_TIME_ZONE`` (Terraform ``scheduler_time_zone``; default
Asia/Singapore). Five tabs:

1. ``1. Executive Summary``            this run's headline numbers (record built by eval/eval_records.py).
2. ``2. Per-Question Breakdown``       one row per golden part, copied from this run's ``score.json`` (never re-judged).
3. ``3. Historical Round Comparison``  one row per round (runs of the same round/model/SOP/media mode averaged) + 3 charts.
4. ``4. Historical Run Ledger``        one row per run in ``eval_history.jsonl`` + 1 chart.
5. ``5. Metric Guide & Tuning Playbook`` golden set snapshot as evaluated by this run.

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

from eval.eval_records import canonical_sop_version, compute_all_round_averages  # noqa: E402
from eval.tune_loop import MAX_MEAN_ALERT_DENSITY  # noqa: E402

logger = logging.getLogger("eval.sheet_report")

ENV_FOLDER_ID = "EVAL_RESULTS_FOLDER_ID"
ENV_TIME_ZONE = "EVAL_REPORT_TIME_ZONE"
DEFAULT_TIME_ZONE = "Asia/Singapore"
TITLE_PREFIX = "CHAGEE_AI_Audit_Eval"
SPREADSHEET_MIME = "application/vnd.google-apps.spreadsheet"
REPORT_JSON_NAME = "sheet_report.json"

TAB_OVERVIEW = "1. Executive Summary"
TAB_ITEMS = "2. Per-Question Breakdown"
TAB_ROUNDS = "3. Historical Round Comparison"
TAB_RUNS = "4. Historical Run Ledger"
TAB_GOLDEN = "5. Metric Guide & Tuning Playbook"
# Fixed sheet IDs so the pure layer can address every tab; the spreadsheet's default sheet is deleted.
SHEET_IDS = {TAB_OVERVIEW: 101, TAB_ITEMS: 102, TAB_ROUNDS: 103, TAB_RUNS: 104, TAB_GOLDEN: 105}

PERCENT = {"type": "PERCENT", "pattern": "0.0%"}
USD = {"type": "NUMBER", "pattern": "$0.0000"}
DECIMAL_2 = {"type": "NUMBER", "pattern": "0.00"}
INTEGER = {"type": "NUMBER", "pattern": "#,##0"}

CHART_WIDTH_PX = 900
CHART_HEIGHT_PX = 380
CHART_ROW_STRIDE = 21  # rows between stacked charts (~380 px at the default 21 px row height)
MAX_CHART_ROUNDS = 12
MAX_CHART_RUNS = 15


# --------------------------------------------------------------------------------------- pure layer


@dataclass
class ExtraBlock:
    """A secondary table block placed below the main table on the same sheet tab (starting at Column A)."""

    title: str
    start_row: int  # 0-based row index where `rows[0]` (the header row) is written
    rows: list[list[Any]]
    col_formats: dict[int, dict[str, str]] = field(default_factory=dict)

    def col(self, header: str) -> int:
        return self.rows[0].index(header)


@dataclass
class Table:
    """One tab: header row + data rows, plus number formats by column (data rows) or by cell."""

    title: str
    rows: list[list[Any]]
    col_formats: dict[int, dict[str, str]] = field(default_factory=dict)
    cell_formats: list[tuple[int, int, dict[str, str]]] = field(default_factory=list)
    wide_cols: dict[int, int] = field(default_factory=dict)  # column -> pixel width (wrapped text)
    note: str = ""  # written to the right of the header row (row 1, one blank column after the table)
    extra_blocks: list[ExtraBlock] = field(default_factory=list)

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
            f"EVAL_REPORT_TIME_ZONE={tz_name!r} is not a valid IANA time zone (e.g. Asia/Singapore)"
        ) from exc


def build_report_title(round_id: str, run_id: str, now_utc: datetime, tz: ZoneInfo) -> str:
    stamp = now_utc.astimezone(tz).strftime("%Y%m%d-%H%M%S")
    return f"{TITLE_PREFIX}_{round_id}_{run_id}_{stamp}"


def _sop_short(sop_version: str) -> str:
    s = canonical_sop_version(sop_version)
    return s[len("Prompt_"):] if s.startswith("Prompt_") else s


def prompt_mode_label(sop_version: str, media_mode: str) -> str:
    short = _sop_short(sop_version)
    mode = str(media_mode or "").strip()
    return f"{short} ({mode})" if mode else short


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
    raise ValueError(f"run_id {run_id!r} not found in eval_history.jsonl")


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
    note = f"Comparing only rounds evaluated on the same golden set version ({ver})"
    note += (
        f"; {len(other_rounds)} round(s) ({len(other)} run(s)) on other versions excluded"
        if other
        else "; no historical runs on other versions"
    )
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
    rows: list[list[Any]] = [["Metric", "Value", "Notes"]]
    cells: list[tuple[int, int, dict[str, str]]] = []

    def add(label: str, value: Any, note: str = "", fmt: dict[str, str] | None = None) -> None:
        rows.append([label, _blank(value), note])
        if fmt is not None and value is not None:
            cells.append((len(rows) - 1, 1, fmt))

    add("Evaluation Round (round_id)", run_record.get("round_id"))
    add("Run ID (run_id)", run_record.get("run_id"))
    add(f"Report Generated At ({time_zone})", created_at_local)
    add("Scoring Completed At (scored_at, UTC)", score_doc.get("scored_at") or run_record.get("timestamp"))
    add("Audit Model (model_version)", run_record.get("model_version"))
    add("SOP / Prompt Version (sop_version)", run_record.get("sop_version"))
    add("Video Processing Mode (media_mode)", run_record.get("media_mode"))
    add("Golden Set Version (golden_version)", run_record.get("golden_version") or golden.get("golden_version"),
        "stem@ruler-hash (ruler-v2); recall across different golden versions is not comparable")
    origin = golden.get("golden_origin") or {}
    add("Golden Set Source", origin.get("sheet_url") or golden.get("golden_source") or "",
        (f"{origin.get('sheet_name', '')} ({origin.get('label_range', '')}"
         + (f" + '{origin.get('config_tab', '')}' tab" if origin.get("config_tab") else "")
         + f", sheet modified at {origin.get('sheet_modified_time', '')})") if origin else "Golden set file")
    add("Golden Set Items / Parts", f"{n_items} items / {run_record.get('golden_part_count') or golden.get('part_count') or ''} parts",
        "")
    add("Judge Model (judge_model)", run_record.get("judge_model") or score_doc.get("judge_model"),
        f"Video analysis runs 1x per run; Judge runs {score_doc.get('judge_passes', 3)}x per part and takes the median vote (1.0 / 0.5 / 0.0)"
        if score_doc.get("judge_passes") else "Video analysis runs 1x per run; Judge runs 3x per part and takes the median vote")
    add(f"Overall Recall ({n_items} items)", m.get("overall_recall"),
        "Weighted recall: exact match (timestamp + sub-clause) = 1.0, partial match (same event/chain caught) = 0.5, miss = 0.0, divided by total items",
        PERCENT)
    lenient_val = (
        m.get("lenient_recall")
        if m.get("lenient_recall") is not None
        else (
            ((score_doc.get("recall") or {}).get("all") or {}).get("lenient_recall")
            if ((score_doc.get("recall") or {}).get("all") or {}).get("lenient_recall") is not None
            else m.get("overall_recall")
        )
    )
    add(f"Lenient Recall (Partial=1) ({n_items} items)", lenient_val,
        "Event-level recall: both Full (1.0) and Partial (0.5) hits count as 1.0 (measures whether AI flagged the violation for the supervisor)",
        PERCENT)
    add("CONFIRMED-Only Recall", m.get("confirmed_only_recall"),
        "Recall counting ONLY high-certainty CONFIRMED findings (excluding SUSPECTED findings)",
        PERCENT)
    add("Mean Alert Density / Clip (findings_per_clip)", m.get("findings_per_clip"),
        f"Average AI findings reported per 5-min video clip (Guardrail <= {MAX_MEAN_ALERT_DENSITY:.1f})", DECIMAL_2)
    add("Total Findings", m.get("total_findings"), f"{density.get('clips', '')} video clips", INTEGER)
    add("Alert Hit Rate (hit_rate)", m.get("hit_rate"),
        "Matched AI findings / total AI findings (~10%-15% is normal because human labels only mark 16 sample violations while AI scans every violation in full clips; NOT precision)",
        PERCENT)
    stable_state = m.get("stable_guardrail") or "not_configured"
    stable_ids = run_record.get("stable_baseline_items") or golden.get("stable_baseline_items") or []
    add("Regressed Stable Baseline Items (regressed_stable_items)", m.get("regressed_stable_items"),
        {"pass": f"✅ Pass — all {len(stable_ids)} stable baseline items held at 1.0",
         "fail": f"🔴 Regression — regression detected among {len(stable_ids)} stable baseline items",
         }.get(stable_state, "Not configured (no stable_baseline_items in golden manifest); check skipped"), INTEGER)
    guardrail_badge = (
        "✅ Pass"
        if m.get("guardrails_passed")
        else ("🔴 Regression" if int(m.get("regressed_stable_items") or 0) > 0 else "⚠️ Review")
    )
    add("Guardrails Status (guardrails_passed)", guardrail_badge,
        f"Alert density <= {MAX_MEAN_ALERT_DENSITY:.1f} and zero stable baseline regressions")
    add("Intra-Round Flip Rate (flip_rate)", m.get("flip_rate"), "Fraction of items whose score changed across runs in the same round", PERCENT)
    add("Mean Point-Action Timestamp Drift (sec)", m.get("mean_point_timestamp_drift_sec"), "POINT rules only (±20s)", DECIMAL_2)
    add("Max Point-Action Timestamp Drift (sec)", m.get("max_point_timestamp_drift_sec"), "", INTEGER)
    add("Mean Cost per Clip (USD)", m.get("cost_per_clip_usd"),
        "Normalized at Input ($0.75/1M) + Thinking ($3.75/1M) + Output ($3.75/1M)", USD)
    add("Total Run Cost (USD)", m.get("total_cost_usd"), "Audit model calls only, excluding Judge", USD)
    add("Mean Clip Latency (sec)", m.get("mean_clip_latency_sec"), "", DECIMAL_2)
    add("Max Clip Latency (sec)", m.get("max_clip_latency_sec"), "", DECIMAL_2)
    add("Input Tokens (incl. Agentic Frame Sampling)", m.get("prompt_token_count"),
        "Video stream + prompt + Agentic high-res frame sampling callbacks (tool_use)", INTEGER)
    add("Thinking Tokens", m.get("thoughts_token_count"), "Internal multi-step visual reasoning tokens", INTEGER)
    add("Output Tokens", m.get("candidates_token_count"), "Final structured JSON report output", INTEGER)
    add("Total Tokens", m.get("total_token_count"), "Input + Thinking + Output total", INTEGER)
    rows.append([
        "Note",
        "Recall is computed once locally by eval/score_run.py as the single source of truth; this sheet "
        "displays results from score.json and eval_history.jsonl without re-scoring.",
        "",
    ])
    return Table(TAB_OVERVIEW, rows, cell_formats=cells, wide_cols={1: 420, 2: 360})


def _score_badge(score: float, *, candidate_ids: Sequence[str] | None = None, explanation: str = "") -> str:
    if score >= 1.0:
        return "✅ Hit"
    if score >= 0.5:
        return "⚠️ Partial"
    if candidate_ids is not None:
        return "❌ Miss (Judge)" if len(candidate_ids) > 0 else "❌ Miss (Window)"
    if "Judge call skipped" in explanation or "没有任何 AI 条目" in explanation or "代码预筛" in explanation:
        return "❌ Miss (Window)"
    return "❌ Miss (Judge)" if explanation else "❌ Miss (Window)"


def build_items_table(
    score_doc: Mapping[str, Any],
    golden_items: Sequence[Mapping[str, Any]] = (),
) -> Table:
    golden_parts: dict[str, Mapping[str, Any]] = {}
    for g in golden_items:
        for p in g.get("parts") or []:
            golden_parts[str(p.get("part_id"))] = p

    header = [
        "part_id", "Item ID (item_id)", "Outlet", "Audit Focus", "SOP Category", "Temporal Mode", "Time Window (±sec)",
        "Human Label (Verbatim)", "Labelled Timestamp (OSD)", "Part Score", "Item Score", "Lenient Score (Partial=1)", "Verdict",
        "Temporal Mode Source", "SOP Category Source",
        "Matched AI Findings", "Timestamp Drift (sec)", "Judge Explanation",
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
            lenient_part_score = 1.0 if score >= 0.5 else 0.0
            cands = part.get("candidate_ids")
            expl = str(part.get("explanation") or "")
            rows.append([
                pid,
                item.get("item_id", ""),
                item.get("outlet_name", ""),
                item.get("focus", ""),
                item.get("sop_category", ""),
                part.get("temporal_mode", ""),
                (part.get("window_label") if part.get("window_label") and (
                    part.get("dwell_sec") or part.get("window_before_sec") != part.get("window_after_sec"))
                 else _blank(part.get("window_sec"))),
                gpart.get("description") or item.get("finding_verbatim", ""),
                ", ".join(gpart.get("osd_times") or []),
                score,
                float(item.get("score") or 0.0),
                lenient_part_score,
                _score_badge(score, candidate_ids=cands if isinstance(cands, list) else None, explanation=expl),
                {"explicit": "Golden Set Explicit", "heuristic": "Rule Heuristic"}.get(
                    str(part.get("temporal_mode_source") or ""), "Unrecorded"),
                {"explicit": "Golden Set Explicit", "audit_clause": "Audit Clause Text", "focus": "Audit Focus",
                 "none": "Uncategorized"}.get(str(item.get("sop_category_source") or ""), "Unrecorded"),
                matched_txt,
                _blank(part.get("timestamp_drift_sec")),
                expl,
            ])
    t = Table(TAB_ITEMS, rows)
    t.wide_cols = {
        t.col("Human Label (Verbatim)"): 320,
        t.col("Matched AI Findings"): 420,
        t.col("Judge Explanation"): 420,
    }
    return t


def build_metric_guide_block(*, start_row: int) -> ExtraBlock:
    """Plain-language explanation of the core evaluation metrics and 1x-video / 3x-judge scoring flow."""
    header = [
        "Metric / Mechanism (指标与机制)",
        "Formula / Rule (计算口径)",
        "Plain-Language Explanation (通俗说明 — 说人话)",
    ]
    rows: list[list[Any]] = [
        header,
        [
            "Overall Recall (带权重召回率)",
            "(1.0 × Full Hits + 0.5 × Partial Hits) ÷ Total Golden Items (16)",
            "严格按权重计分：AI 在对应时间窗内抓到违规且具体子条款完全一致算 1.0 分；抓到了同一违规事件/因果链但子原因或前序触发物描述有细微差异（Partial 半对）算 0.5 分；漏报算 0 分。",
        ],
        [
            "Lenient Recall (Partial=1) (宽松召回率 / 半对算对)",
            "(1.0 × Full Hits + 1.0 × Partial Hits) ÷ Total Golden Items (16)",
            "业务事件级召回率：只要 AI 在该时间段弹出了对应违规告警（无论是 1.0 全对还是 0.5 半对），统一按 1.0 分计算。对应督导真实复核体感——只要 AI 弹出切片，督导点开 3 秒即可确认违规。",
        ],
        [
            "CONFIRMED-Only Recall (仅高确信告警召回率)",
            "Weighted Recall using ONLY findings where disposition == CONFIRMED",
            "只统计 AI 标为 CONFIRMED（画面铁证确凿）的告警、剔除 SUSPECTED（疑似/遮挡待核实）告警后的召回率。用来衡量“如果只看高确信告警，会少抓多少违规”。",
        ],
        [
            "Alert Hit Rate (人工标注命中占比)",
            "AI Findings Matched to Golden Set ÷ Total AI Findings across all 13 clips",
            "⚠️ 注意：这不是准确率（Precision）！因为人工在 13 段视频里只挑了 16 个典型问题标注，而 AI 会把全片所有未戴发网、抹布乱放、茶桶未按时搅拌全部报出来（约 90~100 条），分母远大于 16，所以该比例天然在 10%~15% 左右，仅作告警密度参考。",
        ],
        [
            "Mean Findings / Clip (平均每段视频告警数)",
            "Total AI Findings ÷ Number of Video Clips (13 clips, Guardrail <= 8.0)",
            "衡量 AI 是否“乱报刷屏”的核心护栏指标：平均每段 5 分钟视频报出的违规条数不能超过 8.0 条，防止模型靠无限刷告警来混高召回率。",
        ],
        [
            "Evaluation Execution: 1x Video Pass + 3x Judge Voting (视频跑 1 遍 + 裁判打分跑 3 遍)",
            "Audit Model: 1 pass per trigger | Judge Model (gemini-3.1-pro-preview): 3 passes → Median",
            "每次从工作台触发一轮测评时：① 13 段监控视频由被测视觉模型（如 gemini-3.8-flash 或 gemini-4-argon）实际推理分析 1 遍；② 打分阶段先由代码硬过滤时间窗（瞬时动作 ±20s、跨时段因果链 ±60s、5分钟静置 -60s/+330s），再由裁判模型（gemini-3.1-pro-preview）对每道有候选的题独立打分 3 遍并取中位数（Median of 3），消除裁判模型偶发抖动。",
        ],
    ]
    return ExtraBlock(
        title="Metric & Scoring Mechanism Guide (核心指标与打分机制说明)",
        start_row=start_row,
        rows=rows,
    )


def build_golden_snapshot_table(golden_items: Sequence[Mapping[str, Any]], stable_ids: Sequence[str] = ()) -> Table:
    """One row per golden part exactly as this run used it (category / time mode with their source)."""
    from eval.score_run import sop_category_and_source, temporal_mode_and_source

    header = ["Item ID", "part_id", "Outlet", "Audit Focus", "Audit Clause", "Human Label (Verbatim)",
              "Labelled Timestamp (OSD)", "Video Filename(s)", "SOP Category", "SOP Category Source",
              "Temporal Mode", "Temporal Mode Source", "Stable Baseline Item"]
    rows: list[list[Any]] = [header]
    for it in golden_items:
        cat, cat_src = sop_category_and_source(dict(it))
        for p in it.get("parts") or []:
            mode, mode_src = temporal_mode_and_source(dict(it), dict(p))
            rows.append([
                it.get("item_id", ""), p.get("part_id", ""), it.get("outlet_name", ""), it.get("focus", ""),
                it.get("audit_clause", ""), p.get("description") or it.get("finding_verbatim", ""),
                ", ".join(p.get("osd_times") or []), "\n".join(it.get("video_filenames") or []),
                cat, cat_src, mode, mode_src, "Yes" if it.get("stable_baseline") or it.get("item_id") in stable_ids else "",
            ])
    t = Table(TAB_GOLDEN, rows)
    t.wide_cols = {t.col("Human Label (Verbatim)"): 360, t.col("Video Filename(s)"): 320}
    t.extra_blocks = [build_metric_guide_block(start_row=len(t.rows) + 2)]
    return t


def _collect_model_prompt_matrix(
    history: Sequence[dict[str, Any]],
) -> tuple[list[str], list[str], dict[str, str], dict[tuple[str, str], list[dict[str, Any]]]]:
    """Collect ordered unique models, ordered unique prompt labels, prompt->media_mode, and grouped runs."""
    models: list[str] = []
    prompts: list[str] = []
    prompt_media: dict[str, str] = {}
    cells: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for rec in history:
        m_ver = str(rec.get("model_version") or "unknown").strip() or "unknown"
        media = str(rec.get("media_mode") or "agentic").strip() or "agentic"
        p_lbl = prompt_mode_label(str(rec.get("sop_version") or "unknown"), media)
        if m_ver not in models:
            models.append(m_ver)
        if p_lbl not in prompts:
            prompts.append(p_lbl)
            prompt_media[p_lbl] = media
        cells.setdefault((m_ver, p_lbl), []).append(rec)
    return models, prompts, prompt_media, cells


def _mean_metric(recs: Sequence[dict[str, Any]], key: str, fallback_key: str | None = None) -> float | str:
    vals: list[float] = []
    for r in recs:
        m = r.get("metrics") or {}
        v = m.get(key)
        if v is None and fallback_key:
            v = m.get(fallback_key)
        if v is not None:
            vals.append(float(v))
    return round(sum(vals) / len(vals), 6) if vals else ""


def build_by_model_block(
    history: Sequence[dict[str, Any]],
    *,
    start_row: int,
) -> tuple[ExtraBlock, list[str]]:
    """Pivot table grouping by Model (`Same Model -> Different SOP Prompts`)."""
    models, prompts, _, cells = _collect_model_prompt_matrix(history)
    header = [
        "Model (Same Model -> Different SOP Prompts)",
        "Runs Count",
        *[f"{p} · Weighted Recall" for p in prompts],
        *[f"{p} · Lenient Recall (Partial=1)" for p in prompts],
    ]
    rows: list[list[Any]] = [header]
    for m_ver in models:
        m_runs = sum(len(cells.get((m_ver, p), ())) for p in prompts)
        w_vals = [_mean_metric(cells.get((m_ver, p), ()), "overall_recall") for p in prompts]
        l_vals = [_mean_metric(cells.get((m_ver, p), ()), "lenient_recall", "overall_recall") for p in prompts]
        rows.append([m_ver, m_runs, *w_vals, *l_vals])
    col_formats: dict[int, dict[str, str]] = {1: INTEGER}
    for c in range(2, len(header)):
        col_formats[c] = PERCENT
    return (
        ExtraBlock(
            title="By Model: Same Model -> Different SOP Prompts",
            start_row=start_row,
            rows=rows,
            col_formats=col_formats,
        ),
        prompts,
    )


def build_by_prompt_block(
    history: Sequence[dict[str, Any]],
    *,
    start_row: int,
) -> tuple[ExtraBlock, list[str]]:
    """Pivot table grouping by SOP Prompt (`Same SOP Prompt -> Different Models`)."""
    models, prompts, prompt_media, cells = _collect_model_prompt_matrix(history)
    header = [
        "SOP Prompt (Same Prompt -> Different Models)",
        "Media Mode",
        "Runs Count",
        *[f"{m_ver} · Weighted Recall" for m_ver in models],
        *[f"{m_ver} · Lenient Recall (Partial=1)" for m_ver in models],
    ]
    rows: list[list[Any]] = [header]
    for p_lbl in prompts:
        p_runs = sum(len(cells.get((m_ver, p_lbl), ())) for m_ver in models)
        w_vals = [_mean_metric(cells.get((m_ver, p_lbl), ()), "overall_recall") for m_ver in models]
        l_vals = [_mean_metric(cells.get((m_ver, p_lbl), ()), "lenient_recall", "overall_recall") for m_ver in models]
        rows.append([p_lbl, prompt_media.get(p_lbl, ""), p_runs, *w_vals, *l_vals])
    col_formats: dict[int, dict[str, str]] = {2: INTEGER}
    for c in range(3, len(header)):
        col_formats[c] = PERCENT
    return (
        ExtraBlock(
            title="By SOP Prompt: Same SOP Prompt -> Different Models",
            start_row=start_row,
            rows=rows,
            col_formats=col_formats,
        ),
        models,
    )


def build_rounds_table(history: Sequence[dict[str, Any]]) -> tuple[Table, list[str], list[str]]:
    """One row per round average. Returns (table, sop category headers, outlet x focus headers)."""
    rounds = compute_all_round_averages(history)
    labels = round_labels(rounds)
    cats = sorted({k for r in rounds for k in (r.get("sop_category_recall") or {})})
    ofs = sorted({k for r in rounds for k in (r.get("outlet_focus_recall") or {})})
    header = [
        "Round", "round_id", "SOP Version", "Model Version", "Media Mode", "Runs Count", "run_ids",
        "Overall Recall", "Lenient Recall (Partial=1)", "CONFIRMED-Only Recall", "Alert Hit Rate",
        *cats, *ofs,
        "Mean Findings / Clip", "Mean Timestamp Drift (sec)", "Mean Cost / Clip (USD)",
    ]
    rows: list[list[Any]] = [header]
    for lbl, r in zip(labels, rounds):
        m = r.get("metrics") or {}
        rows.append([
            lbl, r.get("round_id", ""), canonical_sop_version(r.get("sop_version", "")), r.get("model_version", ""),
            r.get("media_mode", ""), r.get("runs_count", 0), ", ".join(r.get("run_ids") or []),
            _blank(m.get("overall_recall")),
            _blank(m.get("lenient_recall", m.get("overall_recall"))),
            _blank(m.get("confirmed_only_recall")), _blank(m.get("hit_rate")),
            *[_blank(((r.get("sop_category_recall") or {}).get(c) or {}).get("recall")) for c in cats],
            *[_blank(((r.get("outlet_focus_recall") or {}).get(o) or {}).get("recall")) for o in ofs],
            _blank(m.get("findings_per_clip")), _blank(m.get("mean_point_timestamp_drift_sec")),
            _blank(m.get("cost_per_clip_usd")),
        ])
    t = Table(TAB_ROUNDS, rows)
    pct = ["Overall Recall", "Lenient Recall (Partial=1)", "CONFIRMED-Only Recall", "Alert Hit Rate", *cats, *ofs]
    t.col_formats = {t.col(h): PERCENT for h in pct}
    t.col_formats[t.col("Mean Findings / Clip")] = DECIMAL_2
    t.col_formats[t.col("Mean Timestamp Drift (sec)")] = DECIMAL_2
    t.col_formats[t.col("Mean Cost / Clip (USD)")] = USD

    b1_start = len(t.rows) + 2
    by_model_block, _ = build_by_model_block(history, start_row=b1_start)
    b2_start = b1_start + len(by_model_block.rows) + 2
    by_prompt_block, _ = build_by_prompt_block(history, start_row=b2_start)
    b3_start = b2_start + len(by_prompt_block.rows) + 2
    metric_guide_block = build_metric_guide_block(start_row=b3_start)
    t.extra_blocks = [by_model_block, by_prompt_block, metric_guide_block]
    return t, cats, ofs


GUARDRAIL_HEADER = f"Alert Density Guardrail ({MAX_MEAN_ALERT_DENSITY:.1f})"


def build_runs_table(history: Sequence[dict[str, Any]], current_run_id: str) -> Table:
    header = [
        "Run", "Current", "round_id", "run_id", "SOP Version", "Model Version", "Media Mode",
        "Overall Recall", "Lenient Recall (Partial=1)", "CONFIRMED-Only Recall", "Mean Findings / Clip", GUARDRAIL_HEADER,
        "Mean Timestamp Drift (sec)", "Mean Cost / Clip (USD)", "Total Cost (USD)",
        "Input Tokens", "Thinking Tokens", "Output Tokens", "Total Tokens",
        "Mean Clip Latency (sec)", "Guardrails Status",
    ]
    rows: list[list[Any]] = [header]
    for rec in history:
        m = rec.get("metrics") or {}
        g_status = (
            "✅ Pass"
            if m.get("guardrails_passed")
            else ("🔴 Regression" if int(m.get("regressed_stable_items") or 0) > 0 else "⚠️ Review")
        )
        rows.append([
            run_label(rec), "★" if rec.get("run_id") == current_run_id else "",
            rec.get("round_id", ""), rec.get("run_id", ""), canonical_sop_version(rec.get("sop_version", "")),
            rec.get("model_version", ""), rec.get("media_mode", ""),
            _blank(m.get("overall_recall")), _blank(m.get("lenient_recall", m.get("overall_recall"))),
            _blank(m.get("confirmed_only_recall")),
            _blank(m.get("findings_per_clip")), MAX_MEAN_ALERT_DENSITY,
            _blank(m.get("mean_point_timestamp_drift_sec")), _blank(m.get("cost_per_clip_usd")),
            _blank(m.get("total_cost_usd")),
            _blank(m.get("prompt_token_count")), _blank(m.get("thoughts_token_count")),
            _blank(m.get("candidates_token_count")), _blank(m.get("total_token_count")),
            _blank(m.get("mean_clip_latency_sec")),
            g_status,
        ])
    t = Table(TAB_RUNS, rows)
    t.col_formats = {t.col(h): PERCENT for h in ("Overall Recall", "Lenient Recall (Partial=1)", "CONFIRMED-Only Recall")}
    for h in ("Mean Findings / Clip", GUARDRAIL_HEADER, "Mean Timestamp Drift (sec)", "Mean Clip Latency (sec)"):
        t.col_formats[t.col(h)] = DECIMAL_2
    for h in ("Mean Cost / Clip (USD)", "Total Cost (USD)"):
        t.col_formats[t.col(h)] = USD
    for h in ("Input Tokens", "Thinking Tokens", "Output Tokens", "Total Tokens"):
        t.col_formats[t.col(h)] = INTEGER
    return t


def _col_range(
    sheet_id: int,
    col: int,
    n_rows: int,
    *,
    max_data_rows: int | None = None,
) -> dict[str, Any]:
    if max_data_rows is not None and max_data_rows > 0 and (n_rows - 1) > max_data_rows:
        start_data_row = n_rows - max_data_rows
        return {
            "sourceRange": {
                "sources": [
                    {
                        "sheetId": sheet_id,
                        "startRowIndex": 0,
                        "endRowIndex": 1,
                        "startColumnIndex": col,
                        "endColumnIndex": col + 1,
                    },
                    {
                        "sheetId": sheet_id,
                        "startRowIndex": start_data_row,
                        "endRowIndex": n_rows,
                        "startColumnIndex": col,
                        "endColumnIndex": col + 1,
                    },
                ]
            }
        }
    return {"sourceRange": {"sources": [{
        "sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": n_rows,
        "startColumnIndex": col, "endColumnIndex": col + 1,
    }]}}


def _block_col_range(sheet_id: int, start_row: int, col: int, n_rows: int) -> dict[str, Any]:
    return {
        "sourceRange": {
            "sources": [
                {
                    "sheetId": sheet_id,
                    "startRowIndex": start_row,
                    "endRowIndex": start_row + n_rows,
                    "startColumnIndex": col,
                    "endColumnIndex": col + 1,
                }
            ]
        }
    }


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
    anchor_col: int = 0,
    max_data_rows: int | None = None,
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
                "domains": [{"domain": _col_range(table.sheet_id, table.col(domain_header), n, max_data_rows=max_data_rows)}],
                "series": [
                    {"series": _col_range(table.sheet_id, table.col(h), n, max_data_rows=max_data_rows), "type": typ, "targetAxis": ax}
                    for h, typ, ax in series
                ],
            },
        },
        "position": {"overlayPosition": {
            "anchorCell": {"sheetId": table.sheet_id, "rowIndex": anchor_row, "columnIndex": anchor_col},
            "widthPixels": CHART_WIDTH_PX, "heightPixels": CHART_HEIGHT_PX,
        }},
    }}}


def build_block_chart_request(
    sheet_id: int,
    block: ExtraBlock,
    *,
    title: str,
    domain_header: str,
    series: Sequence[tuple[str, str, str]],
    bottom_title: str,
    left_title: str,
    right_title: str = "",
    anchor_row: int,
    anchor_col: int = 0,
) -> dict[str, Any]:
    """A native ``basicChart`` backed by an ``ExtraBlock`` pivot table on the same sheet tab."""
    n = len(block.rows)
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
                "domains": [{"domain": _block_col_range(sheet_id, block.start_row, block.col(domain_header), n)}],
                "series": [
                    {"series": _block_col_range(sheet_id, block.start_row, block.col(h), n), "type": typ, "targetAxis": ax}
                    for h, typ, ax in series
                ],
            },
        },
        "position": {"overlayPosition": {
            "anchorCell": {"sheetId": sheet_id, "rowIndex": anchor_row, "columnIndex": anchor_col},
            "widthPixels": CHART_WIDTH_PX, "heightPixels": CHART_HEIGHT_PX,
        }},
    }}}


def build_chart_requests(rounds: Table, cats: Sequence[str], ofs: Sequence[str], runs: Table) -> list[dict]:
    last_table_row = max(
        [len(rounds.rows), *[b.start_row + len(b.rows) for b in rounds.extra_blocks]]
    )
    base = last_table_row + 2
    reqs: list[dict[str, Any]] = []

    if len(rounds.extra_blocks) >= 2:
        by_model_block, by_prompt_block = rounds.extra_blocks[0], rounds.extra_blocks[1]
        model_lenient_headers = [h for h in by_model_block.rows[0][2:] if h.endswith("· Lenient Recall (Partial=1)")]
        model_weighted_headers = [h for h in by_model_block.rows[0][2:] if h.endswith("· Weighted Recall")]
        prompt_series_headers = list(by_prompt_block.rows[0][3:])
        if model_lenient_headers:
            reqs.append(build_block_chart_request(
                rounds.sheet_id,
                by_model_block,
                title="Chart 1: By Model — Same Model Across Different SOP Prompts (Lenient Recall Partial=1)",
                domain_header="Model (Same Model -> Different SOP Prompts)",
                series=[(h, "COLUMN", "LEFT_AXIS") for h in model_lenient_headers],
                bottom_title="Model Version",
                left_title="Lenient Recall (Partial=1)",
                anchor_row=base,
                anchor_col=0,
            ))
        if model_weighted_headers:
            reqs.append(build_block_chart_request(
                rounds.sheet_id,
                by_model_block,
                title="Chart 2: By Model — Same Model Across Different SOP Prompts (Overall Weighted Recall)",
                domain_header="Model (Same Model -> Different SOP Prompts)",
                series=[(h, "COLUMN", "LEFT_AXIS") for h in model_weighted_headers],
                bottom_title="Model Version",
                left_title="Overall Recall (Weighted)",
                anchor_row=base,
                anchor_col=7,
            ))
        if prompt_series_headers:
            reqs.append(build_block_chart_request(
                rounds.sheet_id,
                by_prompt_block,
                title="Chart 3: By SOP Prompt — Same SOP Prompt Across Different Models (Weighted & Lenient Recall)",
                domain_header="SOP Prompt (Same Prompt -> Different Models)",
                series=[(h, "COLUMN", "LEFT_AXIS") for h in prompt_series_headers],
                bottom_title="SOP Prompt (Media Mode)",
                left_title="Recall",
                anchor_row=base + CHART_ROW_STRIDE,
                anchor_col=0,
            ))
        round_chart_base = base + CHART_ROW_STRIDE
        round_chart1_col = 7
    else:
        round_chart_base = base
        round_chart1_col = 0

    reqs.append(build_chart_request(
        rounds, title="Chart 4: Core Recall & Alert Hit Rate by Round", domain_header="Round",
        series=[("Overall Recall", "COLUMN", "LEFT_AXIS"),
                ("Lenient Recall (Partial=1)", "COLUMN", "LEFT_AXIS"),
                ("CONFIRMED-Only Recall", "COLUMN", "LEFT_AXIS"),
                ("Alert Hit Rate", "LINE", "LEFT_AXIS")],
        bottom_title="Round · SOP Version", left_title="Rate", anchor_row=round_chart_base,
        anchor_col=round_chart1_col,
        max_data_rows=MAX_CHART_ROUNDS))
    if cats:
        reqs.append(build_chart_request(
            rounds, title="Chart 5: Recall by SOP Category across Rounds", domain_header="Round",
            series=[(c, "COLUMN", "LEFT_AXIS") for c in cats],
            bottom_title="Round · SOP Version", left_title="Recall", anchor_row=round_chart_base + CHART_ROW_STRIDE,
            anchor_col=0,
            max_data_rows=MAX_CHART_ROUNDS))
    if ofs:
        reqs.append(build_chart_request(
            rounds, title="Chart 6: Recall by Outlet × Audit Focus across Rounds", domain_header="Round",
            series=[(o, "COLUMN", "LEFT_AXIS") for o in ofs],
            bottom_title="Round · SOP Version", left_title="Recall", anchor_row=round_chart_base + CHART_ROW_STRIDE,
            anchor_col=7,
            max_data_rows=MAX_CHART_ROUNDS))
    reqs.append(build_chart_request(
        runs, title=f"Chart 7: Per-Run Alert Density / Timestamp Drift / Cost per Clip (Guardrail {MAX_MEAN_ALERT_DENSITY:.1f} findings/clip)",
        domain_header="Run",
        series=[("Mean Findings / Clip", "COLUMN", "LEFT_AXIS"), ("Mean Timestamp Drift (sec)", "COLUMN", "LEFT_AXIS"),
                (GUARDRAIL_HEADER, "LINE", "LEFT_AXIS"), ("Mean Cost / Clip (USD)", "LINE", "RIGHT_AXIS")],
        bottom_title="Round · Run", left_title="Findings/Clip · Seconds", right_title="USD / Clip",
        anchor_row=len(runs.rows) + 2,
        max_data_rows=MAX_CHART_RUNS))
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
        content_rows = max([len(t.rows), *[b.start_row + len(b.rows) for b in t.extra_blocks]])
        max_cols = max([len(t.rows[0]), *[len(b.rows[0]) for b in t.extra_blocks if b.rows]])
        reqs.append({"addSheet": {"properties": {
            "sheetId": t.sheet_id, "title": t.title, "index": idx,
            "gridProperties": {
                "rowCount": max(content_rows + 5 * CHART_ROW_STRIDE, 120),
                "columnCount": max(max_cols + 3, 26),
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
        for b in t.extra_blocks:
            ranges.append({"range": f"'{t.title}'!A{b.start_row + 1}", "values": b.rows})
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
        for b in t.extra_blocks:
            b_rows, b_cols = len(b.rows), len(b.rows[0])
            reqs.append({"repeatCell": {
                "range": {"sheetId": t.sheet_id, "startRowIndex": b.start_row, "endRowIndex": b.start_row + 1,
                          "startColumnIndex": 0, "endColumnIndex": b_cols},
                "cell": {"userEnteredFormat": {"textFormat": {"bold": True},
                                               "backgroundColor": {"red": 0.91, "green": 0.94, "blue": 0.99}}},
                "fields": "userEnteredFormat(textFormat,backgroundColor)",
            }})
            for col, fmt in sorted(b.col_formats.items()):
                reqs.append(_fmt_req(t.sheet_id, b.start_row + 1, b.start_row + b_rows, col, col + 1, fmt))
        for row, col, fmt in t.cell_formats:
            reqs.append(_fmt_req(t.sheet_id, row, row + 1, col, col + 1, fmt))
        max_cols = max([n_cols, *[len(b.rows[0]) for b in t.extra_blocks if b.rows]])
        reqs.append({"autoResizeDimensions": {"dimensions": {
            "sheetId": t.sheet_id, "dimension": "COLUMNS", "startIndex": 0, "endIndex": max_cols}}})
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
        raise SheetReportError(f"Spreadsheet created but failed to complete writing ({url}): {exc}") from exc
    return sid, url


def update_report_spreadsheet(spec: ReportSpec, spreadsheet_id: str, *, sheets: Any) -> str:
    """Refreshes an existing report spreadsheet in-place (preserving its file ID and URL)."""
    sid = str(spreadsheet_id).strip()
    url = f"https://docs.google.com/spreadsheets/d/{sid}/edit"
    scratch_sheet_id = 999
    try:
        meta = sheets.spreadsheets().get(
            spreadsheetId=sid, fields="sheets.properties(sheetId,title)").execute(num_retries=3)
        existing_ids = [int(s["properties"]["sheetId"]) for s in meta.get("sheets", [])]
        prep_reqs: list[dict[str, Any]] = []
        if scratch_sheet_id not in existing_ids:
            prep_reqs.append({"addSheet": {"properties": {"sheetId": scratch_sheet_id, "title": "_refresh_tmp"}}})
        for eid in existing_ids:
            if eid != scratch_sheet_id:
                prep_reqs.append({"deleteSheet": {"sheetId": eid}})
        if prep_reqs:
            sheets.spreadsheets().batchUpdate(
                spreadsheetId=sid, body={"requests": prep_reqs}
            ).execute(num_retries=3)
        sheets.spreadsheets().batchUpdate(
            spreadsheetId=sid, body={"requests": build_structure_requests(spec, [scratch_sheet_id])}
        ).execute(num_retries=3)
        sheets.spreadsheets().values().batchUpdate(
            spreadsheetId=sid, body={"valueInputOption": "RAW", "data": build_value_ranges(spec)}
        ).execute(num_retries=3)
        sheets.spreadsheets().batchUpdate(
            spreadsheetId=sid, body={"requests": build_format_requests(spec) + spec.chart_requests}
        ).execute(num_retries=3)
    except Exception as exc:
        raise SheetReportError(f"Failed to refresh spreadsheet in-place ({url}): {exc}") from exc
    return url


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
                f"Sheet report: SKIPPED — Terraform variable eval_results_folder_id is not set (env "
                f"{ENV_FOLDER_ID} is empty); skipping Google Sheet evaluation report generation"
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
            status_line=(f"Sheet report: FAILED — {msg} (evaluation results are saved in score.json / GCS; "
                         f"backfill with python -m eval.sheet_report)"),
        )
        _write_report_json(res_dir, outcome, folder, tz_name)
        return outcome
    outcome = SheetReportOutcome(status="created", spreadsheet_url=url, spreadsheet_id=sid, title=title,
                                 status_line=f"Sheet report: {url}")
    logger.info("Sheet report created: %s (%s)", url, title)
    _write_report_json(res_dir, outcome, folder, tz_name)
    return outcome


publish_eval_sheet = publish_run_sheet_report_safely


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
    parser.add_argument("--update-sheet-id", default=None,
                        help="Refresh an existing spreadsheet ID in-place instead of creating a new file")
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

    if args.update_sheet_id:
        spec = build_report_spec(history=history, score_doc=score_doc, run_record=run_record,
                                 golden_items=golden, time_zone=tz_name)
        _, sheets = default_services()
        url = update_report_spreadsheet(spec, str(args.update_sheet_id), sheets=sheets)
        print(f"Sheet report updated in-place: {url}")
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
