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
from typing import Any, Sequence

from eval.tune_loop import evaluate_run_guardrails

logger = logging.getLogger("eval.monitoring_publisher")

METRIC_PREFIX = "custom.googleapis.com/cctv_audit/eval"
MAX_TIMESERIES_PER_BATCH = 200


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
            "type": f"{METRIC_PREFIX}/{metric_name}",
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


def build_cloud_monitoring_timeseries(
    record: dict[str, Any],
    *,
    emit_timestamp: str | None = None,
) -> list[dict[str, Any]]:
    """Build GCP Cloud Monitoring v3 ``TimeSeries`` objects from an evaluation record.

    Note: Cloud Monitoring custom metrics require ``interval.endTime`` to be within the last
    25 hours when written live. ``emit_timestamp`` defaults to ``datetime.now(timezone.utc)``
    when publishing live, or can be passed explicitly in tests.
    """
    project_id = str(record["project_id"])
    end_time = emit_timestamp or datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )
    base_labels = {
        "model_version": str(record.get("model_version") or "unknown"),
        "sop_version": str(record.get("sop_version") or "unknown"),
        "media_mode": str(record.get("media_mode") or "agentic"),
        "round_id": str(record.get("round_id") or "r00"),
        "run_id": str(record.get("run_id") or "unknown"),
    }
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
            )
        )

    series.append(
        _make_gauge_series(
            project_id=project_id,
            metric_name="regressed_stable_items",
            labels=base_labels,
            end_time=end_time,
            int64_value=int(m.get("regressed_stable_items") or 0),
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
            )
        )
        series.append(
            _make_gauge_series(
                project_id=project_id,
                metric_name="video_findings_count",
                labels=v_labels,
                end_time=end_time,
                int64_value=int(v_obj.get("findings_count") or 0),
            )
        )

    return series


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


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Publish a CCTV AI Audit evaluation monitoring_record.json to GCP Cloud Monitoring"
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
    args = parser.parse_args(argv)
    record = json.loads(args.record_json.read_text(encoding="utf-8"))
    ts = build_cloud_monitoring_timeseries(record, emit_timestamp=args.emit_timestamp)
    count = publish_eval_timeseries(args.project_id, ts)
    print(f"Published {count} TimeSeries points for run_id={record.get('run_id')} to {args.project_id}.")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())

