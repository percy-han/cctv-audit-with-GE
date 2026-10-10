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
- ``publish_agent_platform_evaluation``: per-question drill-down in Agent Platform Evaluation.

Eval metrics are no longer published to Cloud Monitoring or Vertex AI Experiments (Round 67).
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import re
from typing import Any, Sequence

from eval.tune_loop import evaluate_run_guardrails

logger = logging.getLogger("eval.eval_records")

DEFAULT_AGENT_EVAL_LOCATION = "us-central1"


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
            "holdout_recall": round(holdout_rec, 6),
            "dev_recall": round(dev_rec, 6),
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
            "total_token_count": res_summary["total_token_count"],
        },
        "sop_category_recall": score_doc.get("sop_category_recall") or {},
        "outlet_focus_recall": score_doc.get("outlet_focus_recall") or {},
        "video_breakdown": score_doc.get("video_breakdown") or {},
    }



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
            str(rec.get("sop_version") or "unknown"),
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


def build_agent_platform_evaluation_items(
    *,
    round_id: str,
    run_id: str,
    sop_version: str,
    model_version: str,
    score_doc: dict[str, Any],
    golden_items: Sequence[dict[str, Any]],
    job_docs: Sequence[dict[str, Any]],
    agent_id: str = "chagee_cctv_audit_agent",
    agent_engine_id: str = "",
) -> list[dict[str, Any]]:
    """Build structured ``EvaluationItemRequest`` dicts for Gemini Enterprise Agent Platform Evaluation.

    Each item represents one golden annotation part with:
    - ``prompt``: the golden annotation metadata and reference text
    - ``golden_response``: the reference text
    - ``candidate_responses``: ``AgentData`` containing the user prompt event,
      the ``analyze_cctv_segment`` tool call & response events, and the final
      formatted response containing the ruler verdict and candidate findings.
    """
    from eval import score_run

    findings = score_run.flatten_findings(list(job_docs))
    part_score_map: dict[str, dict[str, Any]] = {}
    for item in score_doc.get("items") or []:
        for p in item.get("parts") or []:
            part_score_map[str(p.get("part_id") or "")] = p

    items_out: list[dict[str, Any]] = []
    for item in golden_items:
        sop_cat = score_run.classify_sop_category(item)
        for part in item.get("parts") or []:
            part_id = str(part.get("part_id") or "")
            t_mode = score_run.classify_temporal_mode(item, part)
            w_sec = score_run.part_window_sec(t_mode)
            ref_text = score_run.render_reference(item, part)
            video_filenames = list(item.get("video_filenames") or [])
            cands = score_run.prefilter(
                {"osd_times": part.get("osd_times") or [], "description": part.get("description") or ""},
                video_filenames,
                findings,
                window_sec=w_sec,
            )
            raw_resp = score_run.render_candidates(cands) or "（该时间窗内无候选告警）"
            p_info = part_score_map.get(part_id, {})
            sc = float(p_info.get("score", 0.0))
            expl = str(p_info.get("explanation", "MATCH=NONE; 该视频在标注时间窗内无候选条目"))
            badge = "✓ 命中 (1.0)" if sc == 1.0 else ("◐ 半对 (0.5)" if sc == 0.5 else "✗ 漏检 (0.0)")
            formatted_resp = (
                f"【尺子判定：{badge}】\n"
                f"【裁判理由】{expl}\n\n"
                f"【AI 稽核原始候选条目】\n{raw_resp}"
            )
            outlet_name = str(item.get("outlet_name") or "")
            split = str(item.get("split") or "holdout")
            prompt_display = (
                f"[{part_id} | {split} | {sop_cat} | {outlet_name} | {t_mode} ±{w_sec}s]\n"
                f"{ref_text}"
            )
            items_out.append(
                {
                    "part_id": part_id,
                    "display_name": f"{part_id} · {outlet_name} · {sop_cat}",
                    "prompt_display": prompt_display,
                    "reference_text": ref_text,
                    "raw_response": raw_resp,
                    "formatted_response": formatted_resp,
                    "score": sc,
                    "explanation": expl,
                    "outlet_name": outlet_name,
                    "video_filenames": video_filenames,
                    "sop_category": sop_cat,
                    "temporal_mode": t_mode,
                    "window_sec": w_sec,
                    "round_id": round_id,
                    "run_id": run_id,
                    "sop_version": sop_version,
                    "model_version": model_version,
                    "agent_id": agent_id,
                    "agent_engine_id": agent_engine_id,
                }
            )
    return items_out


def publish_agent_platform_evaluation(
    *,
    project_id: str,
    gcs_bucket: str,
    round_id: str,
    run_id: str,
    sop_version: str,
    model_version: str,
    score_doc: dict[str, Any],
    golden_items: Sequence[dict[str, Any]],
    job_docs: Sequence[dict[str, Any]],
    location: str = DEFAULT_AGENT_EVAL_LOCATION,
    agent_engine_id: str = "",
) -> dict[str, str]:
    """Register and launch a server-side EvaluationExperiment + EvaluationRun in Agent Platform Evaluation UI."""
    if not project_id or not gcs_bucket or not golden_items:
        return {}

    import os
    import uuid
    import vertexai
    from vertexai import types
    from google.cloud import storage
    from google.genai import types as genai_types
    from eval import score_run

    resolved_engine_id = (
        agent_engine_id
        or os.environ.get("VERTEX_AGENT_ENGINE_ID", "").strip()
    )
    items_spec = build_agent_platform_evaluation_items(
        round_id=round_id,
        run_id=run_id,
        sop_version=sop_version,
        model_version=model_version,
        score_doc=score_doc,
        golden_items=golden_items,
        job_docs=job_docs,
        agent_engine_id=resolved_engine_id,
    )
    if not items_spec:
        return {}

    storage_client = storage.Client(project=project_id)
    bucket_name = gcs_bucket.replace("gs://", "").strip("/").split("/")[0]
    bucket = storage_client.bucket(bucket_name)
    client = vertexai.Client(project=project_id, location=location)

    recall_all = ((score_doc.get("recall") or {}).get("all") or {}).get("recall")
    if recall_all is None:
        recall_all = (score_doc.get("summary") or {}).get("overall_recall", 0.0)
    overall_recall = float(recall_all or 0.0)
    desc = f"{sop_version} · {model_version} (Recall {overall_recall * 100:.1f}%)"
    agent_cfg = types.evals.AgentConfig(
        agent_id="chagee_cctv_audit_agent",
        instruction=f"CHAGEE CCTV AI Audit Agent ({desc})",
        description=f"ReasoningEngine {resolved_engine_id or 'chagee-cctv-audit'} ({round_id}/{run_id})",
    )
    dest_prefix = f"agent_platform_eval/agent_eval_{round_id}_{run_id}"
    item_resource_names: list[str] = []

    for spec in items_spec:
        part_id = spec["part_id"]
        user_event = types.evals.AgentEvent(
            author="user",
            content=genai_types.Content(
                role="user",
                parts=[genai_types.Part(text=spec["prompt_display"])],
            ),
        )
        tool_call_event = types.evals.AgentEvent(
            author="chagee_cctv_audit_agent",
            content=genai_types.Content(
                role="model",
                parts=[
                    genai_types.Part(
                        function_call=genai_types.FunctionCall(
                            name="analyze_cctv_segment",
                            args={
                                "outlet_name": spec["outlet_name"],
                                "video_files": spec["video_filenames"],
                                "sop_version": sop_version,
                                "temporal_mode": spec["temporal_mode"],
                                "window_sec": spec["window_sec"],
                            },
                        )
                    )
                ],
            ),
        )
        tool_resp_event = types.evals.AgentEvent(
            author="analyze_cctv_segment",
            content=genai_types.Content(
                role="user",
                parts=[
                    genai_types.Part(
                        function_response=genai_types.FunctionResponse(
                            name="analyze_cctv_segment",
                            response={
                                "findings": spec["raw_response"],
                                "ruler_score": spec["score"],
                                "ruler_explanation": spec["explanation"],
                            },
                        )
                    )
                ],
            ),
        )
        model_event = types.evals.AgentEvent(
            author="chagee_cctv_audit_agent",
            content=genai_types.Content(
                role="model",
                parts=[genai_types.Part(text=spec["formatted_response"])],
            ),
        )
        agent_data = types.evals.AgentData(
            turns=[
                types.evals.ConversationTurn(
                    turn_index=0,
                    turn_id=f"{part_id}_turn_0",
                    events=[user_event, tool_call_event, tool_resp_event, model_event],
                )
            ],
            agents={"chagee_cctv_audit_agent": agent_cfg},
        )
        req = types.EvaluationItemRequest(
            prompt=types.EvaluationPrompt(text=spec["prompt_display"]),
            golden_response=types.CandidateResponse(text=spec["reference_text"]),
            candidate_responses=[
                types.CandidateResponse(
                    candidate="chagee_cctv_audit_agent",
                    agent_data=agent_data,
                )
            ],
        )
        blob_path = f"{dest_prefix}/agent_req_{part_id}_{uuid.uuid4().hex[:8]}.json"
        bucket.blob(blob_path).upload_from_string(
            json.dumps(req.model_dump(mode="json", by_alias=True, exclude_none=True), ensure_ascii=False),
            content_type="application/json",
        )
        eval_item = client.evals.create_evaluation_item(
            evaluation_item_type=types.EvaluationItemType.REQUEST,
            gcs_uri=f"gs://{bucket_name}/{blob_path}",
            display_name=spec["display_name"],
        )
        item_resource_names.append(eval_item.name)

    eval_set = client.evals.create_evaluation_set(
        evaluation_items=item_resource_names,
        display_name=f"CHAGEE CCTV Agent Golden Set — {round_id} ({run_id})",
    )
    # Note: EvaluationExperiment.labels is a free-form proto map<string, string> in
    # google.cloud.aiplatform.v1beta1.EvaluationExperiment (not a GCE label), and the
    # Cloud Console Angular UI (agents/evaluation/details/details_resolver.ts &
    # get_started_subtask.ts) requires the full EvaluationSet resource path in
    # labels['vertex-ai-evaluation-set-name'] so it can call getEvaluationSet(evalSetName).
    exp_labels: dict[str, str] = {
        "vertex-ai-evaluation-set-name": eval_set.name,
        "vertex-ai-evaluation-agent-engine-location": location,
        "round-id": slugify_experiment_id(round_id, max_len=60),
        "run-id": slugify_experiment_id(run_id, max_len=60),
    }
    if resolved_engine_id:
        exp_labels["vertex-ai-evaluation-agent-engine-id"] = resolved_engine_id

    eval_exp = client.evals.create_evaluation_experiment(
        display_name=f"CHAGEE CCTV AI Audit — {round_id} ({desc})",
        labels=exp_labels,
    )
    label_hit_metric = types.LLMMetric(
        name="label_hit",
        prompt_template=score_run.JUDGE_TEMPLATE,
    )
    run_kwargs: dict[str, Any] = {
        "name": f"{round_id}_{run_id}_agent_eval",
        "display_name": f"CHAGEE CCTV Agent Eval — {round_id} ({run_id})",
        "dataset": types.EvaluationRunDataSource(evaluation_set=eval_set.name),
        "metrics": [
            label_hit_metric,
            types.RubricMetric.FINAL_RESPONSE_QUALITY,
        ],
        "dest": f"gs://{bucket_name}/{dest_prefix}/outputs",
        "agent_info": types.evals.AgentInfo(
            name="chagee_cctv_audit_agent",
            agents={"chagee_cctv_audit_agent": agent_cfg},
        ),
        "evaluation_experiment": eval_exp.name,
        "labels": {
            k: v
            for k, v in exp_labels.items()
            if k != "vertex-ai-evaluation-set-name"
        },
    }
    if resolved_engine_id:
        run_kwargs["agent"] = (
            f"projects/{project_id}/locations/{location}/reasoningEngines/{resolved_engine_id}"
        )

    eval_run = client.evals.create_evaluation_run(**run_kwargs)
    logger.info(
        "Launched Agent Platform EvaluationRun for %s/%s: experiment=%s, run=%s",
        round_id,
        run_id,
        eval_exp.name,
        eval_run.name,
    )
    return {
        "experiment_name": str(eval_exp.name),
        "evaluation_set_name": str(eval_set.name),
        "evaluation_run_name": str(eval_run.name),
    }
