#!/usr/bin/env python3
"""Per-run evaluation records, history ledger and round averages for CCTV AI Audit evaluations.

The numbers here are derived only from the local scorer's output (``eval/score_run.py`` ->
``score.json``) plus the run's folder job JSONs (token ledger, latency); nothing is re-scored.

- ``build_eval_monitoring_record``: one structured record per run (recall overall / holdout / dev /
  confirmed-only, hit rate, alert density, POINT-rule timestamp drift, regressions, flip rate,
  cost, latency, tokens, SOP-category / outlet x focus / per-video breakdowns).
- Every record carries ``golden_version`` (eval/golden_set.py) plus item/part counts; recall scored on
  different golden versions is never averaged together.
- ``append_eval_history_jsonl`` / ``write_round_averages_jsonl``: the run ledger
  ``eval/rounds/eval_history.jsonl`` and its per-(round, model, SOP, media mode, golden version) averages
  ``eval/rounds/eval_round_averages.jsonl``; both feed the per-run Google Sheet report
  (``eval/sheet_report.py``).

Eval metrics are no longer published to Cloud Monitoring or Vertex AI Experiments (Round 67), nor to
Agent Platform Evaluation (Round 85); the Google Sheet report is the single dashboard.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
from pathlib import Path
from typing import Any, Sequence

from cctv_audit.config import config
from eval.tune_loop import evaluate_run_guardrails

logger = logging.getLogger("eval.eval_records")

def extract_run_resource_summary(job_docs: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Extract token usage, USD cost, latency, and active model/SOP version from folder job JSONs.

    Whenever per-clip token breakdown fields (``prompt_token_count``, ``thoughts_token_count``,
    ``candidates_token_count``) are present in the ledger row, USD cost is computed uniformly from
    those three buckets using ``config.input_cost_per_million_usd`` and
    ``config.output_cost_per_million_usd`` so historical runs recorded before rate table updates
    are compared on an apples-to-apples basis.
    """
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
    prompt_tokens = sum(int(r.get("prompt_token_count") or 0) for r in ledger_rows)
    thoughts_tokens = sum(int(r.get("thoughts_token_count") or 0) for r in ledger_rows)
    candidates_tokens = sum(int(r.get("candidates_token_count") or 0) for r in ledger_rows)
    total_tokens = sum(
        int(r.get("total_token_count") or 0)
        or (
            int(r.get("prompt_token_count") or 0)
            + int(r.get("thoughts_token_count") or 0)
            + int(r.get("candidates_token_count") or 0)
        )
        for r in ledger_rows
    )

    total_cost_usd = 0.0
    for r in ledger_rows:
        p_tok = int(r.get("prompt_token_count") or 0)
        t_tok = int(r.get("thoughts_token_count") or 0)
        c_tok = int(r.get("candidates_token_count") or 0)
        if (p_tok + t_tok + c_tok) > 0:
            row_cost = (
                (p_tok / 1_000_000.0) * config.input_cost_per_million_usd
                + ((t_tok + c_tok) / 1_000_000.0) * config.output_cost_per_million_usd
            )
        else:
            row_cost = float(r.get("estimated_cost_usd") or 0.0)
        total_cost_usd += row_cost

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
        "prompt_token_count": prompt_tokens,
        "thoughts_token_count": thoughts_tokens,
        "candidates_token_count": candidates_tokens,
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


GOLDEN_UNVERSIONED = "golden (unversioned)"


def golden_fields(golden: dict[str, Any], recall: dict[str, Any] | None = None) -> dict[str, Any]:
    """Golden lineage fields of a run record, from ``score_doc["golden"]`` (eval/score_run.py)."""
    recall = recall or {}
    return {
        "golden_version": str(golden.get("golden_version") or GOLDEN_UNVERSIONED),
        "golden_version_scheme": str(golden.get("golden_version_scheme") or ("legacy" if golden.get("golden_version") else "")),
        "golden_item_count": int(golden.get("item_count") or (recall.get("all") or {}).get("rows") or 0),
        "golden_part_count": int(golden.get("part_count") or 0),
        "golden_split_counts": dict(golden.get("split_counts") or {}),
        "stable_baseline_items": list(golden.get("stable_baseline_items") or []),
    }


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
    resolved_sop = canonical_sop_version(
        sop_version.strip()
        if sop_version and sop_version.strip()
        else res_summary["sop_version"]
    )
    resolved_media = (media_mode or "agentic").strip().lower()

    rec = score_doc.get("recall") or {}
    all_rec = float((rec.get("all") or {}).get("recall", 0.0))
    dev_rec = float((rec.get("dev") or {}).get("recall", 0.0))
    holdout_rec = float((rec.get("holdout") or {}).get("recall", 0.0))

    items_list = score_doc.get("items") or []

    def _compute_split_lenient_recall(split_name: str | None, fallback_val: float) -> float:
        sub_rec = (rec.get(split_name or "all") or {}) if isinstance(rec, dict) else {}
        if sub_rec.get("lenient_recall") is not None:
            return float(sub_rec["lenient_recall"])
        rows = [
            it for it in items_list
            if split_name is None or it.get("split") == split_name
        ]
        if not rows:
            return fallback_val
        l_pts = 0.0
        for it in rows:
            if it.get("lenient_score") is not None:
                l_pts += float(it["lenient_score"])
            else:
                parts = it.get("parts") or []
                if parts:
                    l_pts += sum(1.0 if float(p.get("score") or 0.0) >= 0.5 else 0.0 for p in parts) / len(parts)
                else:
                    l_pts += 1.0 if float(it.get("score") or 0.0) >= 0.5 else 0.0
        return l_pts / len(rows)

    all_lenient_rec = _compute_split_lenient_recall(None, all_rec)
    dev_lenient_rec = _compute_split_lenient_recall("dev", dev_rec)
    holdout_lenient_rec = _compute_split_lenient_recall("holdout", holdout_rec)

    density = score_doc.get("alert_density") or {}
    findings_per_clip = float(density.get("mean_per_clip", 0.0))
    total_findings = int(density.get("findings", 0))

    qm = score_doc.get("quality_metrics") or {}
    confirmed_only_recall = float(qm.get("confirmed_only_recall", all_rec))
    hit_rate = float(qm.get("hit_rate", 0.0))
    mean_point_drift = qm.get("mean_point_timestamp_drift_sec")
    max_point_drift = qm.get("max_point_timestamp_drift_sec")

    guardrails = evaluate_run_guardrails(score_doc)
    golden = score_doc.get("golden") or {}
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
        **golden_fields(golden, rec),
        "metrics": {
            "overall_recall": round(all_rec, 6),
            "lenient_recall": round(all_lenient_rec, 6),
            "holdout_recall": round(holdout_rec, 6),
            "holdout_lenient_recall": round(holdout_lenient_rec, 6),
            "dev_recall": round(dev_rec, 6),
            "dev_lenient_recall": round(dev_lenient_rec, 6),
            "confirmed_only_recall": round(confirmed_only_recall, 6),
            "findings_per_clip": round(findings_per_clip, 6),
            "total_findings": total_findings,
            "hit_rate": round(hit_rate, 6),
            "regressed_stable_items": regressed_count,
            "guardrails_passed": bool(guardrails.get("passed", False)),
            "stable_guardrail": str(guardrails.get("stable_guardrail") or "not_configured"),
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
            "prompt_token_count": res_summary["prompt_token_count"],
            "thoughts_token_count": res_summary["thoughts_token_count"],
            "candidates_token_count": res_summary["candidates_token_count"],
            "total_token_count": res_summary["total_token_count"],
        },
        "sop_category_recall": score_doc.get("sop_category_recall") or {},
        "outlet_focus_recall": score_doc.get("outlet_focus_recall") or {},
        "video_breakdown": score_doc.get("video_breakdown") or {},
    }



_CANONICAL_SOP_VERSION_ALIASES: dict[str, str] = {
    "Prompt_v2.5_V10全量17条标准版": "Prompt_v2.5_Standard",
}


def canonical_sop_version(sop_version: str | None) -> str:
    """Normalize legacy localized SOP tab titles to their canonical English tab identifier."""
    s = str(sop_version or "unknown").strip() or "unknown"
    return _CANONICAL_SOP_VERSION_ALIASES.get(s, s)


def aggregate_round_monitoring_record(records_for_round: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Average multiple per-run monitoring records for the same ``(round_id, model_version, sop_version)``."""
    if not records_for_round:
        raise ValueError("records_for_round must not be empty")

    n = len(records_for_round)
    last = records_for_round[-1]
    mean_keys = (
        "overall_recall",
        "lenient_recall",
        "holdout_recall",
        "holdout_lenient_recall",
        "dev_recall",
        "dev_lenient_recall",
        "confirmed_only_recall",
        "findings_per_clip",
        "hit_rate",
        "cost_per_clip_usd",
        "total_cost_usd",
        "mean_clip_latency_sec",
    )
    fallback_metric_key = {
        "lenient_recall": "overall_recall",
        "holdout_lenient_recall": "holdout_recall",
        "dev_lenient_recall": "dev_recall",
    }
    avg_metrics: dict[str, Any] = {}
    for k in mean_keys:
        fb_k = fallback_metric_key.get(k)
        vals = [
            float(
                (r.get("metrics") or {}).get(
                    k,
                    (r.get("metrics") or {}).get(fb_k) if fb_k else None,
                )
            )
            for r in records_for_round
            if (r.get("metrics") or {}).get(k) is not None
            or (fb_k and (r.get("metrics") or {}).get(fb_k) is not None)
        ]
        avg_metrics[k] = round(sum(vals) / len(vals), 6) if vals else 0.0

    total_findings_vals = [int((r.get("metrics") or {}).get("total_findings") or 0) for r in records_for_round]
    avg_metrics["total_findings"] = round(sum(total_findings_vals) / n, 2)

    reg_vals = [int((r.get("metrics") or {}).get("regressed_stable_items") or 0) for r in records_for_round]
    avg_metrics["regressed_stable_items"] = max(reg_vals) if reg_vals else 0
    avg_metrics["guardrails_passed"] = all(
        bool((r.get("metrics") or {}).get("guardrails_passed", False)) for r in records_for_round
    )
    stable_states = {str((r.get("metrics") or {}).get("stable_guardrail") or "") for r in records_for_round}
    avg_metrics["stable_guardrail"] = (
        "fail" if "fail" in stable_states else "pass" if stable_states == {"pass"} else "not_configured"
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
    for tok_key in ("prompt_token_count", "thoughts_token_count", "candidates_token_count", "total_token_count"):
        tok_vals = [int((r.get("metrics") or {}).get(tok_key) or 0) for r in records_for_round]
        avg_metrics[tok_key] = int(round(sum(tok_vals) / n))

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
        "sop_version": canonical_sop_version(str(last.get("sop_version") or "unknown")),
        "media_mode": str(last.get("media_mode") or "agentic"),
        "judge_model": str(last.get("judge_model") or ""),
        "golden_version": str(last.get("golden_version") or GOLDEN_UNVERSIONED),
        "golden_version_scheme": str(last.get("golden_version_scheme") or ""),
        **({"golden_version_legacy": str(last["golden_version_legacy"])} if last.get("golden_version_legacy") else {}),
        "golden_item_count": int(last.get("golden_item_count") or 0),
        "golden_part_count": int(last.get("golden_part_count") or 0),
        "golden_split_counts": dict(last.get("golden_split_counts") or {}),
        "metrics": avg_metrics,
        "sop_category_recall": avg_sop_cat,
        "outlet_focus_recall": avg_of,
        "video_breakdown": avg_vb,
    }


def compute_all_round_averages(history_records: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group ``history_records`` by ``(round_id, model_version, sop_version, media_mode, golden_version)``
    in order and average. Runs scored on different golden versions are never averaged together."""
    groups: dict[tuple[str, str, str, str, str], list[dict[str, Any]]] = {}
    for rec in history_records:
        key = (
            str(rec.get("round_id") or "r00"),
            str(rec.get("model_version") or "unknown"),
            canonical_sop_version(str(rec.get("sop_version") or "unknown")),
            str(rec.get("media_mode") or "agentic"),
            str(rec.get("golden_version") or GOLDEN_UNVERSIONED),
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
