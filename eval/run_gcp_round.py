#!/usr/bin/env python3
"""Serverless GCP execution & scoring runner for a Step 2 prompt-tuning round (`rNN`).

Responsibilities:
1. Loads immutable round snapshot `eval/rounds/<round_id>/` (`system_instruction.md`,
   `layer2_rules.json`, `sheet_rows.json`, `manifest.json`).
2. Optionally archives `Prompt_v2.6_<round_id>` tab (`A1:I25`) to the Master SOP Sheet
   (`MASTER_PROMPT_SHEET_ID`) via keyless DWD without touching
   `Tab0_版本总控与回滚开关`.
3. Resolves and probes the active model via `PromptManager.load_active_config` /
   `PromptManager.resolve_and_probe_model` — zero hardcoded model versions.
4. Evaluates the 4 validation folders (16 CCTV clips) in `agentic` video mode using
   `AgenticAuditor.analyze_segment`, reusing pre-sliced audio-stripped MP4s in GCS when
   available and falling back to Drive download + FFmpeg slice if missing.
5. Scores the resulting 4 folder JSONs against `eval/data/golden_v1.jsonl` via
   `eval/score_run.py` (Vertex AI GenAI Eval SDK `LLMMetric` judge), updates
   `eval/rounds/<round_id>/manifest.json` + `eval/rounds/ledger.{json,md}`, and syncs
   artifacts to `gs://<staging_bucket>/eval/rounds/<round_id>/<run_id>/`.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
from typing import Any, Sequence

THIS_DIR = Path(__file__).resolve().parent
CODE_ROOT = THIS_DIR.parent
if str(CODE_ROOT) not in sys.path:
    sys.path.insert(0, str(CODE_ROOT))

# Legacy env aliases, applied BEFORE importing cctv_audit: cctv_audit.config builds a module-level
# `config` singleton at import time, and cctv_audit.gcp uses it for the Gemini client.
for _alias, _name in (("GCP_PROJECT_ID", "GCP_PROJECT"), ("GCS_STAGING_BUCKET", "STAGING_BUCKET"),
                      ("SOP_SHEET_ID", "MASTER_PROMPT_SHEET_ID")):
    if os.environ.get(_alias) and not os.environ.get(_name):
        os.environ[_name] = os.environ[_alias]
os.environ.setdefault("VIDEO_MEDIA_PROCESSING", "agentic")

from cctv_audit.agentic_auditor import (  # noqa: E402
    AgenticAuditor,
    Finding,
    TokenLedgerRow,
    WindowResult,
    deduplicate_overlapping_findings,
)
from cctv_audit.config import AuditConfig, config  # noqa: E402
from cctv_audit.gcp import GoogleWorkspaceGateway, workspace_credentials  # noqa: E402
from cctv_audit.prompt_manager import (  # noqa: E402
    GoogleSheetsConfigClient,
    PromptManager,
    PromptModelConfig,
    load_rules_from_yaml,
)
from cctv_audit.video_ingestor import (  # noqa: E402
    VideoIngestor,
    VideoMetadataItem,
    VideoSliceSegment,
)
from eval.monitoring_publisher import (  # noqa: E402
    DEFAULT_EXPERIMENT_LOCATION,
    DEFAULT_ROUND_EXPERIMENT_NAME,
    DEFAULT_RUNS_EXPERIMENT_NAME,
    append_eval_history_jsonl,
    build_cloud_monitoring_timeseries,
    build_eval_monitoring_record,
    build_round_monitoring_timeseries,
    publish_eval_timeseries,
    publish_vertex_experiment_records,
    write_round_averages_jsonl,
)
from eval.score_run import render_markdown, score_run  # noqa: E402
from eval.tune_loop import (  # noqa: E402
    DEFAULT_GOLDEN_PATH,
    DEFAULT_RESULTS_DIR,
    DEFAULT_ROUNDS_DIR,
    deserialize_rules_json,
    record_round_run,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("eval.run_gcp_round")

BASELINE_RUN_DIR = THIS_DIR / "data" / "runs" / "v6_0928_0811"
FALLBACK_RUN_DIR = THIS_DIR / "data" / "runs" / "r_0928_1009"


EXPECTED_VIDEO_COUNT = 16  # 4 folders of the golden set (6 + 3 + 5 + 2 clips)


def load_frozen_folder_specs(
    primary_run_dir: Path = BASELINE_RUN_DIR,
    secondary_run_dir: Path = FALLBACK_RUN_DIR,
) -> list[dict[str, Any]]:
    """Loads the 4 folder specs and 16 video file metadata from the frozen baseline job JSONs."""
    secondary_job_ids: dict[str, str] = {}
    if secondary_run_dir.exists():
        for p in sorted(secondary_run_dir.glob("job_*.json")):
            doc = json.loads(p.read_text(encoding="utf-8"))
            secondary_job_ids[str(doc["folder_id"])] = str(doc["job_id"])

    specs: list[dict[str, Any]] = []
    for p in sorted(primary_run_dir.glob("job_*.json")):
        doc = json.loads(p.read_text(encoding="utf-8"))
        folder_id = str(doc["folder_id"])
        primary_jid = str(doc["job_id"])
        sec_jid = secondary_job_ids.get(folder_id)
        candidate_jids = [primary_jid] + ([sec_jid] if sec_jid else [])

        # Production job JSON (jobs.py AuditJob) stores the preflight under "preflight_report".
        preflight = doc.get("preflight_report") or doc.get("preflight") or {}
        videos = preflight.get("videos") or []
        if not videos:
            raise RuntimeError(f"Baseline job {primary_jid} ({p.name}) lists no preflight videos")
        specs.append(
            {
                "baseline_job_id": primary_jid,
                "candidate_job_ids": candidate_jids,
                "folder_id": folder_id,
                "preflight": preflight,
                "videos": videos,
            }
        )
    if len(specs) != 4:
        raise RuntimeError(f"Expected 4 baseline folder specs in {primary_run_dir}, found {len(specs)}")
    n_videos = sum(len(s["videos"]) for s in specs)
    if n_videos != EXPECTED_VIDEO_COUNT:
        raise RuntimeError(f"Expected {EXPECTED_VIDEO_COUNT} baseline videos, found {n_videos}")
    return specs


def archive_sop_tab_to_master_sheet(
    sheet_id: str,
    tab_name: str,
    sheet_rows: list[list[str]],
) -> None:
    """Creates or updates `tab_name` (`Prompt_v2.6_rNN!A1:I25`) in the Master SOP Sheet.

    Strictly refuses to touch `Tab0_版本总控与回滚开关`.
    """
    if tab_name == GoogleSheetsConfigClient.TAB0_TITLE or "Tab0" in tab_name:
        raise ValueError(f"Refusing to modify control tab '{tab_name}'.")

    from googleapiclient.discovery import build

    service = build("sheets", "v4", credentials=workspace_credentials(), cache_discovery=False)
    meta = service.spreadsheets().get(spreadsheetId=sheet_id).execute(num_retries=3)
    existing_titles = {
        s.get("properties", {}).get("title", "") for s in meta.get("sheets", [])
    }

    if tab_name not in existing_titles:
        logger.info("Creating new archive tab '%s' on SOP Master Sheet %s", tab_name, sheet_id)
        service.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id,
            body={
                "requests": [
                    {
                        "addSheet": {
                            "properties": {
                                "title": tab_name,
                                "gridProperties": {"rowCount": 40, "columnCount": 12},
                            }
                        }
                    }
                ]
            },
        ).execute(num_retries=3)

    service.spreadsheets().values().clear(
        spreadsheetId=sheet_id,
        range=f"'{tab_name}'!A1:I100",
        body={},
    ).execute(num_retries=3)

    service.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"'{tab_name}'!A1",
        valueInputOption="RAW",
        body={"values": sheet_rows},
    ).execute(num_retries=3)
    logger.info("Archived %d rows to '%s'!A1:I%d", len(sheet_rows), tab_name, len(sheet_rows))


async def resolve_or_ingest_video_slice(
    *,
    cfg: AuditConfig,
    bucket_name: str,
    video_dict: dict[str, Any],
    candidate_job_ids: Sequence[str],
    ingestor: VideoIngestor | None,
) -> VideoSliceSegment:
    """Reuses existing audio-stripped MP4 slice in GCS if present, else ingests from Drive."""
    from google.cloud import storage

    file_id = str(video_dict["file_id"])
    filename = str(video_dict["filename"])
    duration_sec = float(video_dict.get("duration_sec", 302.028))
    width = int(video_dict.get("width", 2560))
    height = int(video_dict.get("height", 1440))

    def _check_gcs() -> str | None:
        storage_client = storage.Client(project=cfg.gcp_project)
        bucket = storage_client.bucket(bucket_name)
        candidate_blob_paths = [
            f"eval/media/{file_id}/seg_0.mp4",
            *[f"jobs/media/{jid}/{file_id}/seg_0.mp4" for jid in candidate_job_ids],
        ]
        for blob_path in candidate_blob_paths:
            blob = bucket.blob(blob_path)
            if blob.exists():
                return f"gs://{bucket_name}/{blob_path}"
        return None

    cached_uri = await asyncio.to_thread(_check_gcs)
    if cached_uri:
        logger.info("Reusing cached GCS slice for %s (%s): %s", filename, file_id, cached_uri)
        return VideoSliceSegment(
            source_file_id=file_id,
            source_filename=filename,
            segment_index=0,
            start_offset_sec=0.0,
            end_offset_sec=duration_sec,
            local_path=None,
            gcs_uri=cached_uri,
            width=width,
            height=height,
        )

    if ingestor is None:
        raise RuntimeError(
            f"Cached GCS slice not found for {filename} ({file_id}) and Drive ingestor is disabled."
        )

    logger.info("Cached GCS slice not found for %s (%s); ingesting from Drive...", filename, file_id)
    vmeta = VideoMetadataItem(
        file_id=file_id,
        filename=filename,
        size_bytes=int(video_dict.get("size_bytes", 0)),
        duration_sec=duration_sec,
        width=width,
        height=height,
    )
    with tempfile.TemporaryDirectory(prefix=f"eval_ingest_{file_id}_") as tmp_dir:
        work_dir = Path(tmp_dir)
        dest_src = work_dir / filename
        src_path = await ingestor.materialise_source(vmeta, dest_src)
        segments = await ingestor.slice_and_strip_audio(
            src_path,
            vmeta,
            work_dir,
        )
        if not segments:
            raise RuntimeError(f"VideoIngestor produced 0 segments for {filename} ({file_id})")
        seg = segments[0]

        def _upload_slice() -> str:
            storage_client = storage.Client(project=cfg.gcp_project)
            bucket = storage_client.bucket(bucket_name)
            blob_path = f"eval/media/{file_id}/seg_0.mp4"
            bucket.blob(blob_path).upload_from_filename(str(seg.local_path), content_type="video/mp4")
            return f"gs://{bucket_name}/{blob_path}"

        gcs_uri = await asyncio.to_thread(_upload_slice)
        return VideoSliceSegment(
            source_file_id=file_id,
            source_filename=filename,
            segment_index=0,
            start_offset_sec=seg.start_offset_sec,
            end_offset_sec=seg.end_offset_sec,
            local_path=None,
            gcs_uri=gcs_uri,
            width=width,
            height=height,
        )


CLIP_MAX_ATTEMPTS = 3
CLIP_RETRY_BASE_SEC = 30.0


class ClipCheckpointStore:
    """Per-clip result store so a failed round resumes instead of redoing every clip.

    GCS-backed in Cloud Build (``gs://<bucket>/eval/runs/<run_id>/ckpt/``); local-dir-backed in tests.
    One JSON per clip, written only after the clip's model call succeeded."""

    def __init__(self, *, run_id: str, bucket_name: str | None, project: str | None,
                 local_dir: Path | None = None) -> None:
        self.prefix = f"eval/runs/{run_id}/ckpt"
        self.bucket_name, self.project, self.local_dir = bucket_name, project, local_dir

    def _name(self, baseline_jid: str, file_id: str) -> str:
        return f"{self.prefix}/{baseline_jid}/{file_id}.json"

    def get(self, baseline_jid: str, file_id: str) -> dict[str, Any] | None:
        name = self._name(baseline_jid, file_id)
        if self.local_dir is not None:
            path = self.local_dir / name
            return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
        from google.cloud import storage
        blob = storage.Client(project=self.project).bucket(self.bucket_name).blob(name)
        return json.loads(blob.download_as_text()) if blob.exists() else None

    def put(self, baseline_jid: str, file_id: str, doc: dict[str, Any]) -> None:
        name = self._name(baseline_jid, file_id)
        data = json.dumps(doc, ensure_ascii=False)
        if self.local_dir is not None:
            path = self.local_dir / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(data, encoding="utf-8")
            return
        from google.cloud import storage
        storage.Client(project=self.project).bucket(self.bucket_name).blob(name).upload_from_string(
            data, content_type="application/json")


async def analyze_clip_with_retry(auditor: AgenticAuditor, *, label: str, **kwargs: Any):
    """Retries one clip's model call on any failure (incl. bare TimeoutError, whose empty message
    the production retry filter in cctv_audit.gcp does not recognise). Raises after the last try."""
    for attempt in range(1, CLIP_MAX_ATTEMPTS + 1):
        try:
            return await auditor.analyze_segment(**kwargs)
        except Exception as exc:  # noqa: BLE001 - every failure mode gets the same bounded retry
            if attempt == CLIP_MAX_ATTEMPTS:
                logger.error("[%s] clip failed after %d attempts: %r", label, attempt, exc)
                raise
            delay = CLIP_RETRY_BASE_SEC * attempt
            logger.warning("[%s] clip attempt %d/%d failed (%r); retrying in %.0fs",
                           label, attempt, CLIP_MAX_ATTEMPTS, exc, delay)
            await asyncio.sleep(delay)
    raise AssertionError("unreachable")


async def evaluate_single_folder(
    *,
    folder_spec: dict[str, Any],
    run_id: str,
    cfg: AuditConfig,
    auditor: AgenticAuditor,
    prompt_cfg: PromptModelConfig,
    ingestor: VideoIngestor | None,
    ckpt: ClipCheckpointStore | None = None,
) -> dict[str, Any]:
    """Runs `AgenticAuditor.analyze_segment` across all clips in a single folder with state carryover."""
    folder_id = str(folder_spec["folder_id"])
    baseline_jid = str(folder_spec["baseline_job_id"])
    job_id = f"{run_id}_{baseline_jid}"
    videos = folder_spec["videos"]

    logger.info(
        "[%s] Starting folder %s (%d videos, baseline=%s)",
        job_id,
        folder_id,
        len(videos),
        baseline_jid,
    )

    all_findings: list[Finding] = []
    window_results: list[WindowResult] = []
    token_ledger: list[TokenLedgerRow] = []
    completed_segments: dict[str, dict[str, Any]] = {}
    carryover_summary = ""

    for vdict in videos:
        file_id = str(vdict["file_id"])
        saved = await asyncio.to_thread(ckpt.get, baseline_jid, file_id) if ckpt else None
        if saved is not None:
            logger.info("[%s] Resuming: clip %s already done in this run, skipping", job_id, vdict["filename"])
            w_res = WindowResult.model_validate(saved["window_result"])
            ledger_row = TokenLedgerRow.model_validate(saved["ledger_row"])
            carryover_summary = w_res.carryover_state_summary
            window_results.append(w_res)
            all_findings.extend(w_res.findings)
            token_ledger.append(ledger_row)
            completed_segments[saved["ckpt_key"]] = saved["segment"]
            continue
        seg = await resolve_or_ingest_video_slice(
            cfg=cfg,
            bucket_name=cfg.staging_bucket,
            video_dict=vdict,
            candidate_job_ids=folder_spec["candidate_job_ids"],
            ingestor=ingestor,
        )
        w_res, ledger_row = await analyze_clip_with_retry(
            auditor,
            label=f"{job_id}:{vdict['filename']}",
            audit_id=job_id,
            folder_id=folder_id,
            segment=seg,
            prompt_cfg=prompt_cfg,
            prior_carryover_summary=carryover_summary,
            evidence_dir=None,
        )
        carryover_summary = w_res.carryover_state_summary
        window_results.append(w_res)
        all_findings.extend(w_res.findings)
        token_ledger.append(ledger_row)
        # Same shape as production SegmentCheckpoint (jobs.py), which is what the r00 baseline runs
        # were scored on: per-segment findings, keyed "<file_id>:<segment_index>".
        ckpt_key = f"{vdict['file_id']}:{seg.segment_index}"
        completed_segments[ckpt_key] = {
            "file_id": vdict["file_id"],
            "filename": vdict["filename"],
            "segment_index": seg.segment_index,
            "carryover_state_summary": w_res.carryover_state_summary,
            "findings": [f.model_dump(mode="json") for f in w_res.findings],
            "ledger_row": ledger_row.model_dump(mode="json"),
        }
        if ckpt is not None:
            await asyncio.to_thread(ckpt.put, baseline_jid, file_id, {
                "ckpt_key": ckpt_key,
                "segment": completed_segments[ckpt_key],
                "window_result": w_res.model_dump(mode="json"),
                "ledger_row": ledger_row.model_dump(mode="json"),
            })

    deduped = deduplicate_overlapping_findings(all_findings, overlap_window_sec=60.0)
    logger.info(
        "[%s] Completed folder %s: raw_findings=%d, deduped_findings=%d",
        job_id,
        folder_id,
        len(all_findings),
        len(deduped),
    )

    return {
        "job_id": job_id,
        "baseline_job_id": baseline_jid,
        "folder_id": folder_id,
        "state": "completed",
        "preflight": folder_spec["preflight"],
        "prompt_config": prompt_cfg.model_dump(mode="json"),
        # Scored input (score_run.flatten_findings reads completed_segments, like the r00 baseline).
        "completed_segments": completed_segments,
        # Cross-slice dedup view, kept for diagnostics only; not scored.
        "deduped_findings": [f.model_dump(mode="json") for f in deduped],
        "window_results": [w.model_dump(mode="json") for w in window_results],
        "token_ledger": [t.model_dump(mode="json") for t in token_ledger],
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def upload_directory_to_gcs(bucket_name: str, project_id: str, local_dir: Path, gcs_prefix: str) -> None:
    from google.cloud import storage

    client = storage.Client(project=project_id)
    bucket = client.bucket(bucket_name)
    for path in sorted(local_dir.rglob("*")):
        if path.is_file():
            rel = path.relative_to(local_dir).as_posix()
            blob_name = f"{gcs_prefix.rstrip('/')}/{rel}"
            bucket.blob(blob_name).upload_from_filename(str(path))
    logger.info("Synced %s -> gs://%s/%s/", local_dir, bucket_name, gcs_prefix.rstrip("/"))


async def run_round_async(args: argparse.Namespace) -> int:
    # Ensure agentic media processing is strictly enabled by default
    cfg = AuditConfig()
    # cctv_audit.gcp builds its Gemini client from the import-time `config` singleton; refuse to run
    # if that singleton and this run's config disagree (e.g. project fell back to a code default).
    if (config.gcp_project, config.staging_bucket) != (cfg.gcp_project, cfg.staging_bucket):
        raise RuntimeError(
            f"cctv_audit.config was built with project={config.gcp_project!r}, "
            f"bucket={config.staging_bucket!r} but this run resolved project={cfg.gcp_project!r}, "
            f"bucket={cfg.staging_bucket!r}; set GCP_PROJECT / STAGING_BUCKET in the environment."
        )
    if not cfg.gcp_project:
        raise RuntimeError("GCP_PROJECT is not set; refusing to fall back to the code default project.")
    logger.info("Config: project=%s bucket=%s media=%s", cfg.gcp_project, cfg.staging_bucket,
                cfg.video_media_processing)

    round_dir = args.rounds_dir / args.round
    if not round_dir.exists():
        raise FileNotFoundError(f"Round snapshot directory not found: {round_dir}")

    manifest = json.loads((round_dir / "manifest.json").read_text(encoding="utf-8"))
    system_instruction = (round_dir / "system_instruction.md").read_text(encoding="utf-8")
    layer2_rules = deserialize_rules_json(
        json.loads((round_dir / "layer2_rules.json").read_text(encoding="utf-8"))
    )
    sheet_rows = json.loads((round_dir / "sheet_rows.json").read_text(encoding="utf-8"))
    scan_targets, _ = load_rules_from_yaml()

    run_id = args.run_id or f"{args.round}_{datetime.now(timezone.utc).strftime('%m%d_%H%M')}"
    logger.info("Starting Step 2 GCP Round evaluation: round=%s, run_id=%s", args.round, run_id)

    if args.sync_sop_tab or manifest.get("mutated_layer") == "layer2":
        try:
            await asyncio.to_thread(
                archive_sop_tab_to_master_sheet,
                cfg.master_prompt_sheet_id,
                str(manifest["sop_tab_name"]),
                sheet_rows,
            )
        except Exception as exc:
            logger.warning("SOP Sheet tab archive warning (non-fatal for eval run): %s", exc)

    sheet_client = GoogleSheetsConfigClient()
    pm = PromptManager(sheet_client=sheet_client)
    active_base_cfg = await pm.load_active_config(cfg.master_prompt_sheet_id)
    logger.info(
        "Resolved live Vertex AI model via Tab 0 / PromptManager: active=%s, fallback=%s",
        active_base_cfg.active_model_version,
        active_base_cfg.fallback_model_version,
    )

    prompt_cfg = PromptModelConfig(
        active_prompt_version=str(manifest["sop_tab_name"]),
        active_model_version=active_base_cfg.active_model_version,
        fallback_model_version=active_base_cfg.fallback_model_version,
        model_fallback_warning=active_base_cfg.model_fallback_warning,
        visual_scan_targets=scan_targets,
        rules=layer2_rules,
        system_instruction=system_instruction,
    )

    auditor = AgenticAuditor(gemini_concurrency=cfg.gemini_concurrency)
    try:
        gateway = GoogleWorkspaceGateway()
        ingestor: VideoIngestor | None = VideoIngestor(drive_reader=gateway)
    except Exception as exc:
        logger.warning("Drive ingestor init skipped (%s); relying on cached GCS slices.", exc)
        ingestor = None

    folder_specs = load_frozen_folder_specs()
    sem = asyncio.Semaphore(max(1, int(args.folder_concurrency)))

    ckpt = ClipCheckpointStore(
        run_id=run_id,
        bucket_name=cfg.staging_bucket,
        project=cfg.gcp_project,
        local_dir=args.ckpt_local_dir,
    )

    async def _run_guarded(spec: dict[str, Any]) -> dict[str, Any]:
        async with sem:
            return await evaluate_single_folder(
                folder_spec=spec,
                run_id=run_id,
                cfg=cfg,
                auditor=auditor,
                prompt_cfg=prompt_cfg,
                ingestor=ingestor,
                ckpt=ckpt,
            )

    job_docs = await asyncio.gather(*[_run_guarded(spec) for spec in folder_specs])

    run_data_dir = THIS_DIR / "data" / "runs" / run_id
    run_data_dir.mkdir(parents=True, exist_ok=True)
    for doc in job_docs:
        out_file = run_data_dir / f"job_{doc['baseline_job_id']}.json"
        out_file.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info(
            "Wrote %s (%d findings)",
            out_file,
            sum(len(c["findings"]) for c in doc["completed_segments"].values()),
        )

    # Score run with Vertex AI GenAI Eval SDK LLM Judge (score_run.py)
    logger.info("Scoring run %s against golden dataset %s...", run_id, args.golden)
    score_doc = await asyncio.to_thread(
        score_run,
        run_dir=run_data_dir,
        golden_path=args.golden,
        project=cfg.gcp_project,
        run_label=run_id,
        judge_model=args.judge_model,
    )
    sdk_res = score_doc.pop("_sdk_result", None)
    score_md = render_markdown(run_id, score_doc)

    res_dir = args.results_dir / run_id
    res_dir.mkdir(parents=True, exist_ok=True)
    (res_dir / "score.json").write_text(
        json.dumps(score_doc, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (res_dir / "score.md").write_text(score_md, encoding="utf-8")
    if sdk_res is not None:
        (res_dir / "sdk_eval_result.json").write_text(
            sdk_res.model_dump_json(fallback=str), encoding="utf-8"
        )

    loop_state = record_round_run(
        rounds_dir=args.rounds_dir,
        round_id=args.round,
        run_id=run_id,
        score_doc=score_doc,
        score_md=score_md,
    )

    updated_manifest = json.loads((round_dir / "manifest.json").read_text(encoding="utf-8"))
    mon_record = build_eval_monitoring_record(
        project_id=cfg.gcp_project,
        round_id=args.round,
        run_id=run_id,
        model_version=str(prompt_cfg.active_model_version),
        sop_version=str(prompt_cfg.active_prompt_version),
        media_mode=str(cfg.video_media_processing),
        score_doc=score_doc,
        job_docs=job_docs,
        round_manifest=updated_manifest,
    )
    (res_dir / "monitoring_record.json").write_text(
        json.dumps(mon_record, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    history_jsonl = args.rounds_dir / "eval_history.jsonl"
    append_eval_history_jsonl(mon_record, history_jsonl)
    round_averages = write_round_averages_jsonl(
        history_jsonl,
        args.rounds_dir / "eval_round_averages.jsonl",
    )
    matching_round_avg = next(
        (
            r
            for r in reversed(round_averages)
            if r.get("round_id") == args.round
            and r.get("model_version") == mon_record["model_version"]
            and r.get("sop_version") == mon_record["sop_version"]
        ),
        None,
    )

    if not getattr(args, "skip_monitoring_publish", False) and not args.skip_gcs_sync:
        try:
            ts_payload = build_cloud_monitoring_timeseries(mon_record)
            if matching_round_avg is not None:
                ts_payload.extend(build_round_monitoring_timeseries(matching_round_avg))
            await asyncio.to_thread(
                publish_eval_timeseries,
                cfg.gcp_project,
                ts_payload,
            )
        except Exception as exc:
            logger.warning("Cloud Monitoring publish warning (non-fatal for eval run): %s", exc)
        exp_location = os.environ.get("GCP_REGION") or DEFAULT_EXPERIMENT_LOCATION
        try:
            await asyncio.to_thread(
                publish_vertex_experiment_records,
                cfg.gcp_project,
                [mon_record],
                location=exp_location,
                experiment_name=DEFAULT_RUNS_EXPERIMENT_NAME,
                experiment_description="CHAGEE CCTV AI Audit Per-Run Detailed Evaluation Ledger",
                is_round_average=False,
            )
        except Exception as exc:
            logger.warning("Vertex AI Experiments per-run publish warning (non-fatal for eval run): %s", exc)
        if matching_round_avg is not None:
            try:
                await asyncio.to_thread(
                    publish_vertex_experiment_records,
                    cfg.gcp_project,
                    [matching_round_avg],
                    location=exp_location,
                    experiment_name=DEFAULT_ROUND_EXPERIMENT_NAME,
                    experiment_description=(
                        "CHAGEE CCTV AI Audit MLOps Evaluation (Model x SOP Version x Round Comparison)"
                    ),
                    is_round_average=True,
                )
            except Exception as exc:
                logger.warning("Vertex AI Experiments round-avg publish warning (non-fatal for eval run): %s", exc)

    if not args.skip_gcs_sync:
        gcs_prefix = f"eval/rounds/{args.round}"
        await asyncio.to_thread(
            upload_directory_to_gcs,
            cfg.staging_bucket,
            cfg.gcp_project,
            round_dir,
            gcs_prefix,
        )
        await asyncio.to_thread(
            upload_directory_to_gcs,
            cfg.staging_bucket,
            cfg.gcp_project,
            run_data_dir,
            f"eval/runs/{run_id}",
        )
        await asyncio.to_thread(
            upload_directory_to_gcs,
            cfg.staging_bucket,
            cfg.gcp_project,
            res_dir,
            f"eval/results/{run_id}",
        )
        from google.cloud import storage

        st_client = storage.Client(project=cfg.gcp_project)
        bucket = st_client.bucket(cfg.staging_bucket)
        for fname in ("ledger.json", "ledger.md", "eval_history.jsonl", "eval_round_averages.jsonl"):
            fpath = args.rounds_dir / fname
            if fpath.exists():
                bucket.blob(f"eval/rounds/{fname}").upload_from_filename(str(fpath))

    print(score_md)
    print("\n=== LOOP STATUS ===")
    print(
        f"Status: {loop_state['status']} | Best Round: {loop_state['best_round_id']} | "
        f"Action: {loop_state['recommended_action']}"
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run and score a Chagee CCTV prompt-tuning round on GCP")
    parser.add_argument("--round", required=True, help="Round ID to execute (e.g. r01)")
    parser.add_argument("--run-id", default=None, help="Optional explicit run ID (default: <round>_<MMDD_HHMM>)")
    parser.add_argument("--rounds-dir", type=Path, default=DEFAULT_ROUNDS_DIR)
    parser.add_argument("--results-dir", type=Path, default=DEFAULT_RESULTS_DIR)
    parser.add_argument("--golden", type=Path, default=DEFAULT_GOLDEN_PATH)
    parser.add_argument("--judge-model", default=None, help="Optional judge model override (default: newest Pro)")
    parser.add_argument("--folder-concurrency", type=int, default=2, help="Max concurrent folders (default: 2)")
    parser.add_argument("--sync-sop-tab", action="store_true", help="Archive Prompt_v2.6_rNN tab to Master SOP Sheet")
    parser.add_argument("--skip-gcs-sync", action="store_true", help="Skip uploading round results to GCS")
    parser.add_argument("--skip-monitoring-publish", action="store_true",
                        help="Skip publishing custom evaluation metrics to Cloud Monitoring API")
    parser.add_argument("--ckpt-local-dir", type=Path, default=None,
                        help="Keep per-clip checkpoints in this local dir instead of GCS (tests/local runs)")
    args = parser.parse_args(argv)
    return asyncio.run(run_round_async(args))


if __name__ == "__main__":
    sys.exit(main())
