#!/usr/bin/env python3
"""GCP Cloud Monitoring custom metric publisher & history archiver for CCTV AI Audit evaluations.

Publishes evaluation metrics under ``custom.googleapis.com/cctv_audit/eval/*`` with full
version lineage labels (``model_version``, ``sop_version``, ``media_mode``, ``round_id``,
``run_id``) and dimensional drill-downs (``sop_category``, ``outlet_focus``, ``video_name``),
so that the GCP Cloud Monitoring Dashboard (provisioned via Terraform ``google_monitoring_dashboard``)
can compare Recall, Confirmed-Only Recall, Alert Density, Hit Rate, Point-Action Timestamp Drift
(``POINT`` ±20s rules only; ``WINDOW`` ±60s process rules exempt), Regressions, Flip Rate,
Token Cost, and Latency across models and SOP versions over the 24-month Cloud Monitoring
retention window.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import re
from typing import Any, Sequence

from eval.tune_loop import evaluate_run_guardrails

logger = logging.getLogger("eval.monitoring_publisher")

METRIC_PREFIX = "custom.googleapis.com/cctv_audit/eval"
ROUND_METRIC_PREFIX = "custom.googleapis.com/cctv_audit/eval_round"
MAX_TIMESERIES_PER_BATCH = 200

DEFAULT_EXPERIMENT_LOCATION = "asia-southeast1"
DEFAULT_ROUND_EXPERIMENT_NAME = "chagee-cctv-audit-eval"
DEFAULT_RUNS_EXPERIMENT_NAME = "chagee-cctv-audit-eval-runs"


def extract_run_resource_summary(job_docs: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Extract token usage, USD cost, latency, and active model/SOP version from folder job JSONs."""
    ledger_rows: list[dict[str, Any]] = []
    model_version = ""
    sop_version = ""

    for job in job_docs:
        p_cfg = job.get("prompt_config") or {}
        if not model_version:
            model_version = str(
                job.get("active_model_version")
                or p_cfg.get("active_model_version")
                or ""
            ).strip()
        if not sop_version:
            sop_version = str(
                job.get("active_prompt_version")
                or p_cfg.get("active_prompt_version")
                or ""
            ).strip()

        job_rows: list[dict[str, Any]] = []
        segs = job.get("completed_segments") or {}
        if segs:
            for seg in segs.values():
                lrow = seg.get("ledger_row")
                if isinstance(lrow, dict) and lrow:
                    job_rows.append(lrow)
        if not job_rows and isinstance(job.get("token_ledger"), list):
            for lrow in job["token_ledger"]:
                if isinstance(lrow, dict) and lrow:
                    job_rows.append(lrow)
        ledger_rows.extend(job_rows)

    for row in ledger_rows:
        if not model_version and row.get("model_version_used"):
            model_version = str(row["model_version_used"]).strip()
        if not sop_version and row.get("prompt_version_used"):
            sop_version = str(row["prompt_version_used"]).strip()

    clips_count = len(ledger_rows)
    total_tokens = sum(int(r.get("total_token_count") or 0) for r in ledger_rows)
    total_cost_usd = sum(float(r.get("estimated_cost_usd") or 0.0) for r in ledger_rows)
    latencies_sec: list[float] = []
    for r in ledger_rows:
        if r.get("e2e_latency_ms") is not None:
            latencies_sec.append(float(r["e2e_latency_ms"]) / 1000.0)
        elif r.get("processing_latency_sec") is not None:
            latencies_sec.append(float(r["processing_latency_sec"]))
    mean_latency_sec = (sum(latencies_sec) / len(latencies_sec)) if latencies_sec else 0.0
    max_latency_sec = max(latencies_sec) if latencies_sec else 0.0

    return {
        "model_version": model_version or "unknown",
        "sop_version": sop_version or "unknown",
        "clips_count": clips_count,
        "total_token_count": total_tokens,
        "total_cost_usd": round(total_cost_usd, 6),
        "cost_per_clip_usd": round(total_cost_usd / clips_count, 6) if clips_count else 0.0,
        "mean_clip_latency_sec": round(mean_latency_sec, 3),
        "max_clip_latency_sec": round(max_latency_sec, 3),
    }


def compute_round_flip_rate(
    round_manifest: dict[str, Any] | None,
    current_row_scores: dict[str, float] | None = None,
) -> float:
    """Compute the fraction of golden items whose score flipped across runs in the same round.

    Returns ``0.0`` when fewer than 2 runs exist for the round.
    """
    score_maps: list[dict[str, float]] = []
    if round_manifest and isinstance(round_manifest.get("runs"), list):
        for r in round_manifest["runs"]:
            rs = r.get("row_scores")
            if isinstance(rs, dict) and rs:
                score_maps.append({str(k): float(v) for k, v in rs.items()})
    if current_row_scores:
        norm_curr = {str(k): float(v) for k, v in current_row_scores.items()}
        if norm_curr not in score_maps:
            score_maps.append(norm_curr)

    if len(score_maps) < 2:
        return 0.0

    all_item_ids = sorted({iid for sm in score_maps for iid in sm})
    if not all_item_ids:
        return 0.0

    flipped = 0
    for iid in all_item_ids:
        vals = {round(sm.get(iid, 0.0), 2) for sm in score_maps if iid in sm}
        if len(vals) > 1:
            flipped += 1
    return round(flipped / len(all_item_ids), 6)


def build_eval_monitoring_record(
    *,
    project_id: str,
    round_id: str,
    run_id: str,
    model_version: str,
    sop_version: str,
    media_mode: str,
    score_doc: dict[str, Any],
    job_docs: Sequence[dict[str, Any]] = (),
    round_manifest: dict[str, Any] | None = None,
    timestamp_iso: str | None = None,
) -> dict[str, Any]:
    """Assemble a complete structured evaluation telemetry record from ``score_doc`` and ``job_docs``."""
    res_summary = extract_run_resource_summary(job_docs)
    resolved_model = (
        model_version.strip()
        if model_version and model_version.strip()
        else res_summary["model_version"]
    )
    resolved_sop = (
        sop_version.strip()
        if sop_version and sop_version.strip()
        else res_summary["sop_version"]
    )
    resolved_media = (media_mode or "agentic").strip().lower()

    rec = score_doc.get("recall") or {}
    all_rec = float((rec.get("all") or {}).get("recall", 0.0))
    dev_rec = float((rec.get("dev") or {}).get("recall", 0.0))
    holdout_rec = float((rec.get("holdout") or {}).get("recall", 0.0))

    density = score_doc.get("alert_density") or {}
    findings_per_clip = float(density.get("mean_per_clip", 0.0))
    total_findings = int(density.get("findings", 0))

    qm = score_doc.get("quality_metrics") or {}
    confirmed_only_recall = float(qm.get("confirmed_only_recall", all_rec))
    hit_rate = float(qm.get("hit_rate", 0.0))
    mean_point_drift = qm.get("mean_point_timestamp_drift_sec")
    max_point_drift = qm.get("max_point_timestamp_drift_sec")

    guardrails = evaluate_run_guardrails(score_doc)
    regressed_count = len(guardrails.get("regressed_stable_items") or [])

    row_scores = {
        str(it.get("item_id")): float(it.get("score", 0.0))
        for it in (score_doc.get("items") or [])
        if it.get("item_id")
    }
    flip_rate = compute_round_flip_rate(round_manifest, row_scores)

    ts_iso = timestamp_iso or str(
        score_doc.get("scored_at") or datetime.now(timezone.utc).isoformat(timespec="seconds")
    )
    if ts_iso.endswith("+00:00"):
        ts_iso = ts_iso[:-6] + "Z"
    elif not ts_iso.endswith("Z") and "+" not in ts_iso[10:]:
        ts_iso = ts_iso + "Z"

    return {
        "timestamp": ts_iso,
        "project_id": project_id,
        "round_id": round_id,
        "run_id": run_id,
        "model_version": resolved_model,
        "sop_version": resolved_sop,
        "media_mode": resolved_media,
        "judge_model": str(score_doc.get("judge_model") or ""),
        "metrics": {
            "overall_recall": round(all_rec, 6),
            "holdout_recall": round(holdout_rec, 6),
            "dev_recall": round(dev_rec, 6),
            "confirmed_only_recall": round(confirmed_only_recall, 6),
            "findings_per_clip": round(findings_per_clip, 6),
            "total_findings": total_findings,
            "hit_rate": round(hit_rate, 6),
            "regressed_stable_items": regressed_count,
            "guardrails_passed": bool(guardrails.get("passed", False)),
            "flip_rate": round(flip_rate, 6),
            "mean_point_timestamp_drift_sec": (
                round(float(mean_point_drift), 3) if mean_point_drift is not None else None
            ),
            "max_point_timestamp_drift_sec": (
                int(max_point_drift) if max_point_drift is not None else None
            ),
            "point_window_sec": int(qm.get("point_window_sec", 20)),
            "window_sec": int(qm.get("window_sec", 60)),
            "cost_per_clip_usd": res_summary["cost_per_clip_usd"],
            "total_cost_usd": res_summary["total_cost_usd"],
            "mean_clip_latency_sec": res_summary["mean_clip_latency_sec"],
            "max_clip_latency_sec": res_summary["max_clip_latency_sec"],
            "total_token_count": res_summary["total_token_count"],
        },
        "sop_category_recall": score_doc.get("sop_category_recall") or {},
        "outlet_focus_recall": score_doc.get("outlet_focus_recall") or {},
        "video_breakdown": score_doc.get("video_breakdown") or {},
    }


def _make_gauge_series(
    *,
    project_id: str,
    metric_name: str,
    labels: dict[str, str],
    end_time: str,
    double_value: float | None = None,
    int64_value: int | None = None,
    metric_prefix: str = METRIC_PREFIX,
) -> dict[str, Any]:
    if int64_value is not None:
        value_type = "INT64"
        point_val: dict[str, Any] = {"int64Value": str(int(int64_value))}
    else:
        value_type = "DOUBLE"
        point_val = {"doubleValue": float(double_value or 0.0)}

    clean_labels = {k: str(v)[:100] for k, v in labels.items() if v is not None}
    return {
        "metric": {
            "type": f"{metric_prefix}/{metric_name}",
            "labels": clean_labels,
        },
        "resource": {
            "type": "global",
            "labels": {"project_id": project_id},
        },
        "metricKind": "GAUGE",
        "valueType": value_type,
        "points": [
            {
                "interval": {"endTime": end_time},
                "value": point_val,
            }
        ],
    }


def _build_timeseries_with_prefix(
    record: dict[str, Any],
    *,
    metric_prefix: str,
    base_labels: dict[str, str],
    emit_timestamp: str | None = None,
) -> list[dict[str, Any]]:
    project_id = str(record["project_id"])
    end_time = emit_timestamp or datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )
    m = record.get("metrics") or {}
    series: list[dict[str, Any]] = []

    double_metrics = (
        "overall_recall",
        "holdout_recall",
        "dev_recall",
        "confirmed_only_recall",
        "findings_per_clip",
        "hit_rate",
        "flip_rate",
        "cost_per_clip_usd",
        "mean_clip_latency_sec",
    )
    for name in double_metrics:
        if m.get(name) is not None:
            series.append(
                _make_gauge_series(
                    project_id=project_id,
                    metric_name=name,
                    labels=base_labels,
                    end_time=end_time,
                    double_value=float(m[name]),
                    metric_prefix=metric_prefix,
                )
            )

    if m.get("mean_point_timestamp_drift_sec") is not None:
        series.append(
            _make_gauge_series(
                project_id=project_id,
                metric_name="mean_point_timestamp_drift_sec",
                labels=base_labels,
                end_time=end_time,
                double_value=float(m["mean_point_timestamp_drift_sec"]),
                metric_prefix=metric_prefix,
            )
        )

    series.append(
        _make_gauge_series(
            project_id=project_id,
            metric_name="regressed_stable_items",
            labels=base_labels,
            end_time=end_time,
            int64_value=int(m.get("regressed_stable_items") or 0),
            metric_prefix=metric_prefix,
        )
    )

    for sop_cat, cat_obj in sorted((record.get("sop_category_recall") or {}).items()):
        series.append(
            _make_gauge_series(
                project_id=project_id,
                metric_name="sop_category_recall",
                labels={**base_labels, "sop_category": str(sop_cat)},
                end_time=end_time,
                double_value=float(cat_obj.get("recall", 0.0)),
                metric_prefix=metric_prefix,
            )
        )

    for of_key, of_obj in sorted((record.get("outlet_focus_recall") or {}).items()):
        series.append(
            _make_gauge_series(
                project_id=project_id,
                metric_name="outlet_focus_recall",
                labels={
                    **base_labels,
                    "outlet_focus": str(of_key),
                    "outlet_name": str(of_obj.get("outlet_name") or ""),
                    "focus": str(of_obj.get("focus") or ""),
                },
                end_time=end_time,
                double_value=float(of_obj.get("recall", 0.0)),
                metric_prefix=metric_prefix,
            )
        )

    for vname, v_obj in sorted((record.get("video_breakdown") or {}).items()):
        short_vname = vname.split("_")[0] if "_" in vname else vname
        v_labels = {
            **base_labels,
            "video_name": short_vname,
            "video_filename": vname[:100],
        }
        series.append(
            _make_gauge_series(
                project_id=project_id,
                metric_name="video_recall",
                labels=v_labels,
                end_time=end_time,
                double_value=float(v_obj.get("recall", 0.0)),
                metric_prefix=metric_prefix,
            )
        )
        if metric_prefix == ROUND_METRIC_PREFIX:
            series.append(
                _make_gauge_series(
                    project_id=project_id,
                    metric_name="video_findings_count",
                    labels=v_labels,
                    end_time=end_time,
                    double_value=float(v_obj.get("findings_count") or 0.0),
                    metric_prefix=metric_prefix,
                )
            )
        else:
            series.append(
                _make_gauge_series(
                    project_id=project_id,
                    metric_name="video_findings_count",
                    labels=v_labels,
                    end_time=end_time,
                    int64_value=int(round(float(v_obj.get("findings_count") or 0))),
                    metric_prefix=metric_prefix,
                )
            )

    return series


def build_cloud_monitoring_timeseries(
    record: dict[str, Any],
    *,
    emit_timestamp: str | None = None,
) -> list[dict[str, Any]]:
    """Build per-run GCP Cloud Monitoring v3 ``TimeSeries`` objects under ``eval/*``."""
    base_labels = {
        "model_version": str(record.get("model_version") or "unknown"),
        "sop_version": str(record.get("sop_version") or "unknown"),
        "media_mode": str(record.get("media_mode") or "agentic"),
        "round_id": str(record.get("round_id") or "r00"),
        "run_id": str(record.get("run_id") or "unknown"),
    }
    return _build_timeseries_with_prefix(
        record,
        metric_prefix=METRIC_PREFIX,
        base_labels=base_labels,
        emit_timestamp=emit_timestamp,
    )


def build_round_monitoring_timeseries(
    round_record: dict[str, Any],
    *,
    emit_timestamp: str | None = None,
) -> list[dict[str, Any]]:
    """Build round-averaged GCP Cloud Monitoring v3 ``TimeSeries`` objects under ``eval_round/*``.

    Each ``(round_id, model_version, sop_version, media_mode)`` combination emits exactly ONE
    averaged point per metric so trend charts show a single representative point per round
    without duplicate same-round run dots.
    """
    base_labels = {
        "model_version": str(round_record.get("model_version") or "unknown"),
        "sop_version": str(round_record.get("sop_version") or "unknown"),
        "media_mode": str(round_record.get("media_mode") or "agentic"),
        "round_id": str(round_record.get("round_id") or "r00"),
        "runs_count": str(int(round_record.get("runs_count") or 1)),
    }
    return _build_timeseries_with_prefix(
        round_record,
        metric_prefix=ROUND_METRIC_PREFIX,
        base_labels=base_labels,
        emit_timestamp=emit_timestamp,
    )


def aggregate_round_monitoring_record(records_for_round: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Average multiple per-run monitoring records for the same ``(round_id, model_version, sop_version)``."""
    if not records_for_round:
        raise ValueError("records_for_round must not be empty")

    n = len(records_for_round)
    last = records_for_round[-1]
    mean_keys = (
        "overall_recall",
        "holdout_recall",
        "dev_recall",
        "confirmed_only_recall",
        "findings_per_clip",
        "hit_rate",
        "cost_per_clip_usd",
        "total_cost_usd",
        "mean_clip_latency_sec",
    )
    avg_metrics: dict[str, Any] = {}
    for k in mean_keys:
        vals = [
            float((r.get("metrics") or {}).get(k, 0.0))
            for r in records_for_round
            if (r.get("metrics") or {}).get(k) is not None
        ]
        avg_metrics[k] = round(sum(vals) / len(vals), 6) if vals else 0.0

    total_findings_vals = [int((r.get("metrics") or {}).get("total_findings") or 0) for r in records_for_round]
    avg_metrics["total_findings"] = round(sum(total_findings_vals) / n, 2)

    reg_vals = [int((r.get("metrics") or {}).get("regressed_stable_items") or 0) for r in records_for_round]
    avg_metrics["regressed_stable_items"] = max(reg_vals) if reg_vals else 0
    avg_metrics["guardrails_passed"] = all(
        bool((r.get("metrics") or {}).get("guardrails_passed", False)) for r in records_for_round
    )

    flip_vals = [float((r.get("metrics") or {}).get("flip_rate") or 0.0) for r in records_for_round]
    avg_metrics["flip_rate"] = round(max(flip_vals), 6) if flip_vals else 0.0

    drift_vals = [
        float((r.get("metrics") or {})["mean_point_timestamp_drift_sec"])
        for r in records_for_round
        if (r.get("metrics") or {}).get("mean_point_timestamp_drift_sec") is not None
    ]
    avg_metrics["mean_point_timestamp_drift_sec"] = (
        round(sum(drift_vals) / len(drift_vals), 3) if drift_vals else None
    )
    max_drift_vals = [
        int((r.get("metrics") or {})["max_point_timestamp_drift_sec"])
        for r in records_for_round
        if (r.get("metrics") or {}).get("max_point_timestamp_drift_sec") is not None
    ]
    avg_metrics["max_point_timestamp_drift_sec"] = max(max_drift_vals) if max_drift_vals else None

    last_m = last.get("metrics") or {}
    avg_metrics["point_window_sec"] = int(last_m.get("point_window_sec", 20))
    avg_metrics["window_sec"] = int(last_m.get("window_sec", 60))
    max_lat_vals = [float((r.get("metrics") or {}).get("max_clip_latency_sec") or 0.0) for r in records_for_round]
    avg_metrics["max_clip_latency_sec"] = round(max(max_lat_vals), 3) if max_lat_vals else 0.0
    tok_vals = [int((r.get("metrics") or {}).get("total_token_count") or 0) for r in records_for_round]
    avg_metrics["total_token_count"] = int(round(sum(tok_vals) / n))

    # Aggregate sop_category_recall
    cat_keys = sorted({k for r in records_for_round for k in (r.get("sop_category_recall") or {})})
    avg_sop_cat: dict[str, Any] = {}
    for ck in cat_keys:
        objs = [(r.get("sop_category_recall") or {}).get(ck) for r in records_for_round]
        objs = [o for o in objs if isinstance(o, dict)]
        if objs:
            avg_sop_cat[ck] = {
                "points": round(sum(float(o.get("points", 0.0)) for o in objs) / len(objs), 4),
                "rows": int(objs[-1].get("rows", 0)),
                "recall": round(sum(float(o.get("recall", 0.0)) for o in objs) / len(objs), 6),
            }

    # Aggregate outlet_focus_recall
    of_keys = sorted({k for r in records_for_round for k in (r.get("outlet_focus_recall") or {})})
    avg_of: dict[str, Any] = {}
    for ok in of_keys:
        objs = [(r.get("outlet_focus_recall") or {}).get(ok) for r in records_for_round]
        objs = [o for o in objs if isinstance(o, dict)]
        if objs:
            avg_of[ok] = {
                "outlet_name": str(objs[-1].get("outlet_name") or ""),
                "focus": str(objs[-1].get("focus") or ""),
                "points": round(sum(float(o.get("points", 0.0)) for o in objs) / len(objs), 4),
                "rows": int(objs[-1].get("rows", 0)),
                "recall": round(sum(float(o.get("recall", 0.0)) for o in objs) / len(objs), 6),
            }

    # Aggregate video_breakdown
    v_keys = sorted({k for r in records_for_round for k in (r.get("video_breakdown") or {})})
    avg_vb: dict[str, Any] = {}
    for vk in v_keys:
        objs = [(r.get("video_breakdown") or {}).get(vk) for r in records_for_round]
        objs = [o for o in objs if isinstance(o, dict)]
        if objs:
            avg_vb[vk] = {
                "points": round(sum(float(o.get("points", 0.0)) for o in objs) / len(objs), 4),
                "rows": int(objs[-1].get("rows", 0)),
                "recall": round(sum(float(o.get("recall", 0.0)) for o in objs) / len(objs), 6),
                "findings_count": round(sum(float(o.get("findings_count", 0.0)) for o in objs) / len(objs), 2),
            }

    return {
        "timestamp": str(last.get("timestamp") or ""),
        "project_id": str(last.get("project_id") or ""),
        "round_id": str(last.get("round_id") or "r00"),
        "runs_count": n,
        "run_ids": [str(r.get("run_id") or "") for r in records_for_round],
        "model_version": str(last.get("model_version") or "unknown"),
        "sop_version": str(last.get("sop_version") or "unknown"),
        "media_mode": str(last.get("media_mode") or "agentic"),
        "judge_model": str(last.get("judge_model") or ""),
        "metrics": avg_metrics,
        "sop_category_recall": avg_sop_cat,
        "outlet_focus_recall": avg_of,
        "video_breakdown": avg_vb,
    }


def compute_all_round_averages(history_records: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group ``history_records`` by ``(round_id, model_version, sop_version, media_mode)`` in order and average."""
    groups: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
    for rec in history_records:
        key = (
            str(rec.get("round_id") or "r00"),
            str(rec.get("model_version") or "unknown"),
            str(rec.get("sop_version") or "unknown"),
            str(rec.get("media_mode") or "agentic"),
        )
        groups.setdefault(key, []).append(rec)
    return [aggregate_round_monitoring_record(recs) for recs in groups.values()]


def write_round_averages_jsonl(history_path: Path, output_path: Path | None = None) -> list[dict[str, Any]]:
    """Read ``eval_history.jsonl``, compute per-round averages, and write ``eval_round_averages.jsonl``."""
    if output_path is None:
        output_path = history_path.parent / "eval_round_averages.jsonl"
    records: list[dict[str, Any]] = []
    if history_path.exists():
        for line in history_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                records.append(json.loads(line))
    round_records = compute_all_round_averages(records)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in round_records) + ("\n" if round_records else ""),
        encoding="utf-8",
    )
    return round_records


def append_eval_history_jsonl(record: dict[str, Any], history_path: Path) -> None:
    """Idempotently append or update ``run_id`` in ``eval_history.jsonl``."""
    history_path.parent.mkdir(parents=True, exist_ok=True)
    existing: list[dict[str, Any]] = []
    if history_path.exists():
        for line in history_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if obj.get("run_id") != record.get("run_id"):
                    existing.append(obj)
            except Exception:
                continue
    existing.append(record)
    history_path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in existing) + "\n",
        encoding="utf-8",
    )


def publish_eval_timeseries(
    project_id: str,
    time_series: Sequence[dict[str, Any]],
    credentials: Any = None,
) -> int:
    """Publish ``time_series`` to Cloud Monitoring REST API v3 in batches of <= 200."""
    if not project_id or not time_series:
        return 0

    import google.auth
    from google.auth.transport.requests import AuthorizedSession

    if credentials is None:
        credentials, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/monitoring.write"]
        )
    authed_session = AuthorizedSession(credentials)
    url = f"https://monitoring.googleapis.com/v3/projects/{project_id}/timeSeries"

    written = 0
    for start in range(0, len(time_series), MAX_TIMESERIES_PER_BATCH):
        chunk = list(time_series[start : start + MAX_TIMESERIES_PER_BATCH])
        resp = authed_session.post(url, json={"timeSeries": chunk}, timeout=30)
        if resp.status_code >= 300:
            raise RuntimeError(
                f"Cloud Monitoring timeSeries.create failed (HTTP {resp.status_code}): {resp.text[:500]}"
            )
        written += len(chunk)

    logger.info(
        "Published %d evaluation TimeSeries points to Cloud Monitoring (project=%s)",
        written,
        project_id,
    )
    return written


def slugify_experiment_id(raw: str, max_len: int = 60) -> str:
    """Convert an arbitrary identifier into a valid Vertex AI Metadata resource ID.

    Vertex AI Metadata ``Context`` IDs must match ``^[a-z0-9][a-z0-9-]{0,126}[a-z0-9]$``
    (lowercase letters, digits, and hyphens only).
    """
    cleaned = re.sub(r"[^a-z0-9]+", "-", str(raw or "").strip().lower()).strip("-")
    if not cleaned:
        cleaned = "run"
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len].rstrip("-")
    if len(cleaned) < 2:
        cleaned = f"{cleaned}-0"
    return cleaned


def build_vertex_experiment_run_payload(
    record: dict[str, Any],
    *,
    is_round_average: bool = False,
) -> dict[str, Any]:
    """Build ``{"run_name": str, "params": dict, "metrics": dict}`` for Vertex AI / Agent Platform Experiments."""
    round_id = str(record.get("round_id") or "r00")
    model_version = str(record.get("model_version") or "unknown")
    sop_version = str(record.get("sop_version") or "unknown")
    media_mode = str(record.get("media_mode") or "agentic")
    judge_model = str(record.get("judge_model") or "")
    timestamp_iso = str(record.get("timestamp") or "")
    m = record.get("metrics") or {}

    if is_round_average:
        raw_name = f"{round_id}-{sop_version}-{model_version}"
        runs_count = int(record.get("runs_count") or 1)
        run_ids_str = ",".join(str(x) for x in (record.get("run_ids") or []))
    else:
        run_id = str(record.get("run_id") or "run")
        raw_name = f"{round_id}-{run_id}-{sop_version}"
        runs_count = 1
        run_ids_str = run_id

    run_name = slugify_experiment_id(raw_name, max_len=60)
    params: dict[str, float | int | str] = {
        "round_id": round_id,
        "sop_version": sop_version,
        "model_version": model_version,
        "media_mode": media_mode,
        "judge_model": judge_model,
        "runs_count": runs_count,
        "run_ids": run_ids_str[:120],
        "guardrails_passed": "true" if m.get("guardrails_passed") else "false",
        "point_window_sec": int(m.get("point_window_sec", 20)),
        "window_sec": int(m.get("window_sec", 60)),
        "evaluated_at": timestamp_iso,
    }

    metrics: dict[str, float | int | str] = {
        "overall_recall": round(float(m.get("overall_recall", 0.0)), 6),
        "holdout_recall": round(float(m.get("holdout_recall", 0.0)), 6),
        "dev_recall": round(float(m.get("dev_recall", 0.0)), 6),
        "confirmed_only_recall": round(float(m.get("confirmed_only_recall", 0.0)), 6),
        "hit_rate": round(float(m.get("hit_rate", 0.0)), 6),
        "findings_per_clip": round(float(m.get("findings_per_clip", 0.0)), 6),
        "total_findings": round(float(m.get("total_findings", 0.0)), 2),
        "flip_rate": round(float(m.get("flip_rate", 0.0)), 6),
        "regressed_stable_items": int(m.get("regressed_stable_items", 0)),
        "cost_per_clip_usd": round(float(m.get("cost_per_clip_usd", 0.0)), 6),
        "total_cost_usd": round(float(m.get("total_cost_usd", 0.0)), 6),
        "mean_clip_latency_sec": round(float(m.get("mean_clip_latency_sec", 0.0)), 3),
        "max_clip_latency_sec": round(float(m.get("max_clip_latency_sec", 0.0)), 3),
        "total_token_count": int(m.get("total_token_count", 0)),
    }
    if m.get("mean_point_timestamp_drift_sec") is not None:
        metrics["mean_point_timestamp_drift_sec"] = round(
            float(m["mean_point_timestamp_drift_sec"]), 3
        )
    if m.get("max_point_timestamp_drift_sec") is not None:
        metrics["max_point_timestamp_drift_sec"] = int(m["max_point_timestamp_drift_sec"])

    for sop_cat, cat_obj in sorted((record.get("sop_category_recall") or {}).items()):
        safe_cat = re.sub(r"[^a-zA-Z0-9_]+", "_", str(sop_cat)).strip("_")
        metrics[f"recall_sop_{safe_cat}"] = round(float(cat_obj.get("recall", 0.0)), 6)

    for of_key, of_obj in sorted((record.get("outlet_focus_recall") or {}).items()):
        safe_of = re.sub(r"[^a-zA-Z0-9_]+", "_", str(of_key)).strip("_")
        metrics[f"recall_store_{safe_of}"] = round(float(of_obj.get("recall", 0.0)), 6)

    return {
        "run_name": run_name,
        "params": params,
        "metrics": metrics,
    }


def publish_vertex_experiment_records(
    project_id: str,
    records: Sequence[dict[str, Any]],
    *,
    location: str = DEFAULT_EXPERIMENT_LOCATION,
    experiment_name: str = DEFAULT_ROUND_EXPERIMENT_NAME,
    experiment_description: str = (
        "CHAGEE CCTV AI Audit MLOps Evaluation (Model x SOP Version x Round Comparison)"
    ),
    is_round_average: bool = False,
    credentials: Any = None,
) -> list[str]:
    """Log evaluation records as runs in Vertex AI / Agent Platform Experiments."""
    if not project_id or not records:
        return []

    try:
        from google.cloud import aiplatform
    except ImportError as exc:
        logger.warning(
            "google-cloud-aiplatform is not installed; skipping Vertex AI Experiment logging: %s",
            exc,
        )
        return []

    exp_slug = slugify_experiment_id(experiment_name, max_len=60)
    aiplatform.init(
        project=project_id,
        location=location,
        experiment=exp_slug,
        experiment_description=experiment_description,
        experiment_tensorboard=False,
        credentials=credentials,
    )

    logged_runs: list[str] = []
    for rec in records:
        payload = build_vertex_experiment_run_payload(
            rec, is_round_average=is_round_average
        )
        run_name = payload["run_name"]
        try:
            try:
                aiplatform.start_run(run=run_name, resume=True)
            except Exception:
                aiplatform.start_run(run=run_name, resume=False)
            try:
                aiplatform.log_params(payload["params"])
                aiplatform.log_metrics(payload["metrics"])
            finally:
                aiplatform.end_run()
            logged_runs.append(run_name)
        except Exception as exc:
            logger.warning(
                "Failed to log run '%s' to Vertex AI Experiment '%s': %s",
                run_name,
                exp_slug,
                exc,
            )

    logger.info(
        "Logged %d/%d evaluation runs to Vertex AI Experiment '%s' (%s/%s): %s",
        len(logged_runs),
        len(records),
        exp_slug,
        project_id,
        location,
        logged_runs,
    )
    return logged_runs


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Publish a CCTV AI Audit evaluation monitoring_record.json to GCP Cloud Monitoring & Vertex AI Experiments"
    )
    parser.add_argument("--project-id", required=True, help="Target GCP project ID")
    parser.add_argument(
        "--record-json",
        type=Path,
        required=True,
        help="Path to monitoring_record.json produced by eval/run_gcp_round.py",
    )
    parser.add_argument(
        "--emit-timestamp",
        default=None,
        help="Optional RFC3339 UTC timestamp override (defaults to current UTC time when original timestamp is >24h old)",
    )
    parser.add_argument(
        "--experiment-location",
        default=DEFAULT_EXPERIMENT_LOCATION,
        help="Vertex AI / Agent Platform Experiment region (default: asia-southeast1)",
    )
    parser.add_argument(
        "--skip-experiments",
        action="store_true",
        help="Skip logging to Vertex AI / Agent Platform Experiments",
    )
    args = parser.parse_args(argv)
    record = json.loads(args.record_json.read_text(encoding="utf-8"))
    ts = build_cloud_monitoring_timeseries(record, emit_timestamp=args.emit_timestamp)
    count = publish_eval_timeseries(args.project_id, ts)
    print(f"Published {count} TimeSeries points for run_id={record.get('run_id')} to {args.project_id}.")
    if not args.skip_experiments:
        try:
            runs = publish_vertex_experiment_records(
                args.project_id,
                [record],
                location=args.experiment_location,
                experiment_name=DEFAULT_RUNS_EXPERIMENT_NAME,
                experiment_description="CHAGEE CCTV AI Audit Per-Run Detailed Evaluation Ledger",
                is_round_average=False,
            )
            print(f"Logged Vertex AI Experiment run(s): {runs}")
        except Exception as exc:
            logger.warning("Vertex AI Experiment publish warning (non-fatal): %s", exc)
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())


