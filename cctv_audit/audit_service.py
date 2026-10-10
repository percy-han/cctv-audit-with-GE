"""Orchestrator Service (`audit_service.py`, adapted from `percy-han/cctv-audit/cctv_audit/audit_service.py`).

Implements the two-step controlled workflow required by Gemini Enterprise's 602s/900s timeout limits:
1. `preflight(user_id, drive_url, session_id)` -> Calls `VideoIngestor.inspect_drive_videos`.
   If any video is `< 720P`, transitions job to `REJECTED` (0 token spend).
   If all pass `>= 720P`, transitions job to `READY` and returns the preflight summary for confirmation.
2. `start_audit(user_id, job_id)` -> Detaches execution into a background task (or Cloud Tasks),
   immediately returning the running `AuditJob` so GE never times out.
   The background worker loads `[Tab 0]` `Active_Prompt_Version` & `Active_Model_Version` from
   Google Sheets (`PromptManager`), slices/strips audio (`VideoIngestor`), runs `AgenticAuditor`
   with 100-word cross-segment state relay, and writes the in-folder `Evidence/` clips and
   Dual-Tab Google Sheet (`WorkspaceReporter`).
3. `get_status(user_id, job_id)` -> Returns live job progress or the completed Google Sheet URL.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
import time
from typing import List, Optional, Set

from .agentic_auditor import (
    AgenticAuditor,
    Finding,
    Status,
    TokenLedgerRow,
    deduplicate_overlapping_findings,
)
from .config import config
from .gcp import WorkspaceConfigError, gemini_timeout_ms_for_slice
from .gcs_gateway import build_source_video_url, safe_storage_id_slug
from .jobs import AuditJob, JobState, SegmentCheckpoint, UserScopedJobStore
from .prompt_manager import PromptManager, PromptModelConfig
from .video_ingestor import (
    InspectFolderResponse,
    VideoIngestor,
    VideoMetadataItem,
    VideoSliceSegment,
    calculate_sliding_windows,
    extract_drive_id,
)
from .workspace_reporter import EVIDENCE_SUBFOLDER_NAME, WorkspaceReporter

logger = logging.getLogger("cctv_audit.service")

HEARTBEAT_INTERVAL_SEC: float = 30.0
STALE_WORKER_HEARTBEAT_SEC: float = 180.0
MAX_AUTO_RESUMES: int = 3
SLICE_STALL_TIMEOUT_SEC: float = 1920.0  # source download + FFmpeg slicing only (not Gemini calls)
CLEANUP_TIMEOUT_SEC: float = 30.0


def _slice_stall_timeout_sec(slice_duration_sec: float = 0.0) -> float:
    """In-container watchdog timeout for `analyze_segment`: the slice's Gemini timeout
    (`gcp.gemini_timeout_ms_for_slice`: 2.5 x slice length, at least `config.gemini_timeout_ms`)
    plus 120s headroom (for 20s evidence clip cutting) so the container watchdog never kills an
    in-flight Gemini call before the SDK HTTP timeout (5-min slice: 750s -> 870s; 10-min: 1620s).
    """
    return max(600.0, gemini_timeout_ms_for_slice(slice_duration_sec) / 1000.0 + 120.0)


class AuditService:
    """Stateless orchestrator tying together Ingestor, PromptManager, Auditor, and WorkspaceReporter."""

    _slice_stall_timeout_sec = staticmethod(_slice_stall_timeout_sec)

    def __init__(
        self,
        *,
        job_store: Optional[UserScopedJobStore] = None,
        ingestor: Optional[VideoIngestor] = None,
        prompt_manager: Optional[PromptManager] = None,
        auditor: Optional[AgenticAuditor] = None,
        reporter: Optional[WorkspaceReporter] = None,
    ) -> None:
        self.jobs = job_store or UserScopedJobStore()
        self.ingestor = ingestor or VideoIngestor()
        self.prompt_manager = prompt_manager or PromptManager()
        self.auditor = auditor or AgenticAuditor()
        self.reporter = reporter or WorkspaceReporter()
        self._background_tasks: Set[asyncio.Task] = set()
        self._warmup_tasks: Set[asyncio.Task] = set()
        self._in_flight_jobs: Set[str] = set()

    async def _warm_prompt_config_quietly(self, folder_id: str = "") -> None:
        """Pre-warms the PromptManager Sheet TTL cache in the background after preflight
        so the subsequent `confirm` turn (`start_audit`) hits memory in <1ms without
        adding any latency to the `inspect` turn response.
        """
        try:
            await self.prompt_manager.load_active_config(sheet_id=config.effective_sop_source(folder_id))
        except (asyncio.CancelledError, Exception) as exc:
            logger.debug("Background prompt config warm-up skipped/cancelled: %s", exc)

    def _schedule_prompt_config_warmup(self, folder_id: str = "") -> asyncio.Task:
        task = asyncio.create_task(self._warm_prompt_config_quietly(folder_id))
        self._warmup_tasks.add(task)
        task.add_done_callback(self._warmup_tasks.discard)
        return task

    async def preflight(
        self,
        *,
        user_id: str,
        drive_url: str,
        session_id: str = "",
        preloaded_items: Optional[List[VideoMetadataItem]] = None,
    ) -> AuditJob:
        """Step 1 (`/inspect`): Runs `< 720P` resolution check in `< 3s` (zero token estimation)."""
        folder_id = extract_drive_id(drive_url)
        job = AuditJob(
            user_id=user_id,
            session_id=session_id,
            folder_id=folder_id,
            drive_url=drive_url,
            state=JobState.PROBING,
        )
        await self.jobs.save(job)

        setup_problem = await self._workspace_setup_problem(folder_id)
        if setup_problem:
            rejected = InspectFolderResponse(
                folder_id=folder_id,
                passed=False,
                total_videos=0,
                total_duration_sec=0.0,
                planned_segments_count=0,
                estimated_tokens=0,
                estimated_cost_usd=0.0,
                estimated_minutes=0.0,
                rejected_videos=[],
                videos=[],
                message_to_user=(
                    f"❌ 预检未通过（本次 0 Token 消耗）：{setup_problem}\n"
                    "• 处理好之后，重新发送一次这个文件夹链接即可（视频不用重新上传）。"
                ),
            )
            job = job.model_copy(
                update={"state": JobState.REJECTED, "preflight_report": rejected}
            )
            return await self.jobs.save(job)

        inspect_res = await self.ingestor.inspect_drive_videos(
            drive_url, preloaded_items=preloaded_items
        )
        new_state = JobState.READY if inspect_res.passed else JobState.REJECTED
        job = job.model_copy(
            update={
                "state": new_state,
                "preflight_report": inspect_res,
            }
        )
        saved_job = await self.jobs.save(job)
        if new_state == JobState.READY and preloaded_items is None:
            self._schedule_prompt_config_warmup(folder_id)
        return saved_job

    async def _workspace_setup_problem(self, folder_id: str) -> str:
        """'' when the Workspace identity can write `folder_id` and read the SOP Sheet.

        Otherwise the operator-actionable reason (who to share with / DWD not authorised / bot has
        no Drive storage). Transient API faults propagate unchanged: they are not setup problems.
        """
        gateway = self.reporter._gateway
        if gateway is None:
            return ""
        checks = [asyncio.wait_for(gateway.probe_write_access(folder_id), timeout=30.0)]
        # Any configured SOP source is verified before spend; the routing gateway sends a
        # `gs://.../master_sheet.xlsx|.json` to GCS and a Google Sheet ID to Workspace (also for
        # hybrid deployments with videos in GCS). "" = Zero-GWS built-in rules, nothing to check.
        sop_id = config.effective_sop_source(folder_id)
        if sop_id:
            checks.append(
                asyncio.wait_for(gateway.check_sheet_readable(sop_id), timeout=15.0)
            )
        results = await asyncio.gather(*checks, return_exceptions=True)
        for res in results:
            if isinstance(res, WorkspaceConfigError):
                logger.warning("Workspace setup check failed for folder %s: %s", folder_id, res)
                return str(res)
            if isinstance(res, BaseException):
                raise res
        return ""

    async def start_audit(
        self,
        *,
        user_id: str,
        job_id: Optional[str] = None,
        session_id: str = "",
        wait_for_completion: bool = False,
    ) -> AuditJob:
        """Step 2 (`/execute`): Confirms a `READY` job (or resumes a `FAILED` / stale `RUNNING` job)
        and dispatches execution so GE returns immediately.
        """
        if job_id:
            job = await self.jobs.get(user_id, job_id)
        else:
            job = await self.jobs.latest_ready_job(user_id, session_id=session_id)
            if job is None:
                recent_list = await self.jobs.list_for_user(user_id, limit=5)
                for candidate in recent_list:
                    if candidate.state == JobState.FAILED or (
                        candidate.state == JobState.RUNNING
                        and (time.time() - candidate.heartbeat_at) > STALE_WORKER_HEARTBEAT_SEC
                    ):
                        job = candidate
                        break

        if job is None:
            raise ValueError(
                "未找到待确认的预检任务（或可断点续跑的中断任务），请先发送 Google Drive 监控视频文件夹链接进行预检。"
            )
        if job.state not in (JobState.READY, JobState.FAILED, JobState.RUNNING) and not (
            job_id and job.state == JobState.DONE
        ):
            raise ValueError(
                f"当前任务 `{job.job_id}` 状态为 `{job.state.value}`，无法重复启动。"
            )

        # Every restart of a FAILED / stale-RUNNING job counts toward MAX_AUTO_RESUMES -- including one
        # that failed before its first checkpoint; otherwise the watchdog would retry it forever.
        is_restart = job.state in (JobState.FAILED, JobState.RUNNING)
        prompt_cfg = await self.prompt_manager.load_active_config(
            sheet_id=config.effective_sop_source(job.folder_id)
        )
        job = job.model_copy(
            update={
                "state": JobState.RUNNING,
                "active_model_version": prompt_cfg.active_model_version,
                "active_prompt_version": prompt_cfg.active_prompt_version,
                "resume_count": job.resume_count + (1 if is_restart else 0),
                "error_message": None,
                "needs_operator_fix": False,
            }
        )
        job = await self.jobs.save(job)

        if wait_for_completion:
            await self._run_detached_audit(job, prompt_cfg)
            refreshed = await self.jobs.get(user_id, job.job_id)
            return refreshed or job

        task = asyncio.create_task(self._dispatch_or_run_detached(job, prompt_cfg))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return job

    async def _dispatch_or_run_detached(
        self, job: AuditJob, prompt_cfg: PromptModelConfig
    ) -> None:
        """Dispatches heavy FFmpeg slicing + multimodal inference to the elastic Cloud Run Worker Pool
        when `CLOUD_RUN_WORKER_URL` is configured, or executes in-process as fallback.
        """
        import os

        worker_url = os.environ.get("CLOUD_RUN_WORKER_URL", "").strip().rstrip("/")
        if worker_url:
            dispatched = await asyncio.to_thread(
                self._post_to_cloud_run_worker_sync, worker_url, job.user_id, job.job_id
            )
            if dispatched:
                logger.info(
                    "Dispatched job %s for %s to Cloud Run Worker Pool %s",
                    job.job_id,
                    job.user_id,
                    worker_url,
                )
                return
            latest = await self.jobs.get(job.user_id, job.job_id)
            if latest is not None:
                if latest.state == JobState.DONE:
                    logger.info(
                        "Cloud Run Worker HTTP connection dropped for job %s, but remote worker already completed it (state=DONE); skipping local fallback.",
                        job.job_id,
                    )
                    return
                if (
                    latest.state == JobState.RUNNING
                    and (
                        latest.heartbeat_at > job.heartbeat_at
                        or len(latest.completed_segments) > len(job.completed_segments)
                    )
                    and (time.time() - latest.heartbeat_at) < STALE_WORKER_HEARTBEAT_SEC
                ):
                    logger.info(
                        "Cloud Run Worker HTTP connection dropped for job %s, but remote worker is still actively heartbeating (%.1fs ago, %d segments); skipping duplicate local fallback.",
                        job.job_id,
                        time.time() - latest.heartbeat_at,
                        len(latest.completed_segments),
                    )
                    return
                job = latest
            logger.warning(
                "Cloud Run Worker dispatch to %s failed for job %s; falling back to local execution",
                worker_url,
                job.job_id,
            )
        await self._run_detached_audit(job, prompt_cfg)

    @staticmethod
    def _post_to_cloud_run_worker_sync(worker_url: str, user_id: str, job_id: str) -> bool:
        """Sends an OIDC-authenticated POST to `${CLOUD_RUN_WORKER_URL}/internal/jobs/execute`."""
        import json
        import urllib.error
        import urllib.request

        target_url = f"{worker_url}/internal/jobs/execute"
        payload = json.dumps(
            {"user_id": user_id, "job_id": job_id, "hold_connection": True}
        ).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        try:
            import google.auth.transport.requests
            import google.oauth2.id_token

            auth_req = google.auth.transport.requests.Request()
            id_token = google.oauth2.id_token.fetch_id_token(auth_req, worker_url)
            headers["Authorization"] = f"Bearer {id_token}"
        except Exception as exc:
            logger.debug("OIDC token fetch skipped/failed for %s: %s", worker_url, exc)

        backoff_schedule = (1.0, 2.0, 4.0)
        for attempt in range(len(backoff_schedule) + 1):
            try:
                req = urllib.request.Request(target_url, data=payload, headers=headers, method="POST")
                with urllib.request.urlopen(req, timeout=3600) as resp:
                    return 200 <= resp.status < 300
            except urllib.error.HTTPError as exc:
                if exc.code in (429, 502, 503, 504) and attempt < len(backoff_schedule):
                    delay = backoff_schedule[attempt]
                    logger.info(
                        "Cloud Run Worker %s returned HTTP %d during scale-out for job %s (attempt %d/%d); retrying in %.1fs",
                        target_url,
                        exc.code,
                        job_id,
                        attempt + 1,
                        len(backoff_schedule) + 1,
                        delay,
                    )
                    time.sleep(delay)
                    continue
                logger.warning("HTTP dispatch to Cloud Run Worker %s failed: %s", target_url, exc)
                return False
            except Exception as exc:
                logger.warning("HTTP dispatch to Cloud Run Worker %s failed: %s", target_url, exc)
                return False
        return False

    async def _intra_slice_heartbeat(self, user_id: str, job_id: str) -> None:
        """Background ticker that refreshes `heartbeat_at` in GCS every 30s while Tier-2 is alive.
        If the Tier-2 container crashes (OOM / SIGKILL / host preemption), this ticker stops immediately,
        allowing the unattended Watchdog (`sweep_and_resume_stale_jobs`) to detect the dead heartbeat
        within 180s even if the supervisor has closed the GE chat window.
        """
        try:
            while True:
                await asyncio.sleep(HEARTBEAT_INTERVAL_SEC)
                await self.jobs.touch_heartbeat(user_id, job_id)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.debug("Heartbeat ticker warning for %s: %s", job_id, exc)

    @staticmethod
    async def _await_with_bounded_cleanup(coro, *, timeout_sec: float, label: str):
        """Runs `coro` with a watchdog timeout (`SLICE_STALL_TIMEOUT_SEC`) and bounded cleanup (`CLEANUP_TIMEOUT_SEC`).
        Directly adopts `percy-han/cctv-audit/cctv_audit/audit_service.py:_watch/_cancel` so a wedged FFmpeg/Drive/Gemini
        call inside a live container never hangs the worker forever.
        """
        task = asyncio.ensure_future(coro)
        done, _ = await asyncio.wait({task}, timeout=timeout_sec)
        if done:
            return task.result()
        task.cancel()
        cleaned, _ = await asyncio.wait({task}, timeout=CLEANUP_TIMEOUT_SEC)
        if not cleaned:
            logger.error(
                "%s did not stop within %.0fs of cancellation; abandoning hung coroutine.",
                label,
                CLEANUP_TIMEOUT_SEC,
            )
        else:
            if not task.cancelled():
                try:
                    task.exception()
                except Exception:
                    pass
        raise TimeoutError(
            f"{label} 超过 {int(timeout_sec)} 秒未返回，已被容器内看门狗终止（将由跨实例巡检自动从最新切片断点续跑）"
        )

    async def _run_detached_audit(
        self, job: AuditJob, prompt_cfg: PromptModelConfig
    ) -> None:
        """Executes slicing, Agentic inference, 20s evidence clipping, and in-folder Sheet reporting.
        Persists a per-segment `SegmentCheckpoint` to GCS (`gs://${STAGING_BUCKET}/jobs/...`) after
        every completed 30-min slice so that if Tier-1 or Tier-2 crashes/restarts mid-flight, a resumed
        worker skips all completed slices (0 duplicate Gemini token charges) and continues seamlessly.
        """
        self._in_flight_jobs.add(job.job_id)
        hb_task = asyncio.create_task(self._intra_slice_heartbeat(job.user_id, job.job_id))
        completed_map = dict(job.completed_segments)
        try:
            findings_by_video: List[tuple[str, List[Finding]]] = []
            ledger_rows: List[TokenLedgerRow] = []
            from cctv_audit.video_ingestor import natural_video_sort_key

            videos = sorted(
                (
                    job.preflight_report.videos
                    if job.preflight_report is not None
                    else []
                ),
                key=lambda v: natural_video_sort_key(v.filename),
            )
            work_dir = config.local_work_dir / job.job_id
            work_dir.mkdir(parents=True, exist_ok=True)
            evidence_dir = work_dir / "evidence"
            evidence_dir.mkdir(parents=True, exist_ok=True)

            evidence_subfolder_id: Optional[str] = None
            gateway = self.reporter._gateway
            if gateway is not None:
                # Fail fast BEFORE the first Gemini call: a setup problem (DWD not authorised, folder
                # not shared as Editor, bot out of Drive storage) would otherwise surface only at
                # Sheet creation, after every slice had been billed. A WorkspaceConfigError here
                # marks the job FAILED with the supervisor-facing fix-it message.
                await asyncio.wait_for(gateway.probe_write_access(job.folder_id), timeout=60.0)
                evidence_subfolder_id = await asyncio.wait_for(
                    gateway.ensure_subfolder(job.folder_id, EVIDENCE_SUBFOLDER_NAME),
                    timeout=30.0,
                )

            carryover = ""
            for video_item in videos:
                video_findings: List[Finding] = []
                win_list = calculate_sliding_windows(
                    video_item.duration_sec,
                    config.segment_duration_sec,
                    config.segment_overlap_sec,
                )
                all_segments_cached = all(
                    f"{video_item.file_id}:{idx}" in completed_map
                    for idx in range(len(win_list))
                )
                
                sliced_segments: List[VideoSliceSegment] = []
                if not all_segments_cached:
                    # Materialise the real source media only when at least one slice still needs execution
                    source_path = await self._await_with_bounded_cleanup(
                        self.ingestor.materialise_source(
                            video_item, work_dir / video_item.filename
                        ),
                        timeout_sec=SLICE_STALL_TIMEOUT_SEC,
                        label=f"Job {job.job_id} materialise_source({video_item.filename})",
                    )
                    # Slice the source media using FFmpeg
                    sliced_segments = await self._await_with_bounded_cleanup(
                        self.ingestor.slice_and_strip_audio(
                            source_path, video_item, work_dir, timeout_sec=SLICE_STALL_TIMEOUT_SEC
                        ),
                        timeout_sec=SLICE_STALL_TIMEOUT_SEC,
                        label=f"Job {job.job_id} slice_and_strip_audio({video_item.filename})",
                    )

                for idx, (s_sec, e_sec) in enumerate(win_list):
                    ckpt_key = f"{video_item.file_id}:{idx}"
                    if ckpt_key in completed_map:
                        ckpt = completed_map[ckpt_key]
                        carryover = ckpt.carryover_state_summary
                        video_findings.extend(ckpt.findings)
                        ledger_rows.append(ckpt.ledger_row)
                        logger.info(
                            "Job %s resumed segment %s from GCS checkpoint (0 duplicate tokens)",
                            job.job_id,
                            ckpt_key,
                        )
                        continue

                    # Grab the actual transcoded slice for this window index
                    try:
                        seg = next(s for s in sliced_segments if s.segment_index == idx)
                    except StopIteration:
                        raise RuntimeError(f"Missing transcoded segment {idx} for {video_item.file_id}")

                    # GCS Upload Offload to bypass Gemini 256MB inline chunk limits (v1beta File API natively supports gs:// URIs)
                    if self.jobs._should_sync_live_gcs() and self.jobs._gcs_bucket:
                        gcs_obj_name = f"jobs/media/{job.job_id}/{safe_storage_id_slug(video_item.file_id)}/seg_{idx}.mp4"
                        logger.info("Offloading video slice %s to gs://%s/%s to bypass 256MB inline constraints", ckpt_key, self.jobs._gcs_bucket, gcs_obj_name)
                        
                        def _upload_media() -> None:
                            import urllib.parse
                            import urllib.request
                            import google.auth
                            import google.auth.transport.requests
                            creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/devstorage.read_write"])
                            creds.refresh(google.auth.transport.requests.Request())
                            url = (
                                f"https://storage.googleapis.com/upload/storage/v1/b/"
                                f"{urllib.parse.quote(self.jobs._gcs_bucket, safe='')}/o"
                                f"?uploadType=media&name={urllib.parse.quote(gcs_obj_name, safe='')}"
                            )
                            file_size = seg.local_path.stat().st_size
                            with seg.local_path.open("rb") as f_stream:
                                req = urllib.request.Request(url, data=f_stream, method="POST")
                                req.add_header("Authorization", f"Bearer {creds.token}")
                                req.add_header("Content-Type", "video/mp4")
                                req.add_header("Content-Length", str(file_size))
                                with urllib.request.urlopen(req, timeout=120):
                                    pass

                        await asyncio.to_thread(_upload_media)
                        seg.gcs_uri = f"gs://{self.jobs._gcs_bucket}/{gcs_obj_name}"
                    try:
                        win_res, ledger = await self._await_with_bounded_cleanup(
                            self.auditor.analyze_segment(
                                audit_id=job.job_id,
                                folder_id=job.folder_id,
                                segment=seg,
                                prompt_cfg=prompt_cfg,
                                prior_carryover_summary=carryover,
                                evidence_dir=evidence_dir,
                            ),
                            timeout_sec=_slice_stall_timeout_sec(seg.end_offset_sec - seg.start_offset_sec),
                            label=f"Job {job.job_id} slice {ckpt_key}",
                        )
                    finally:
                        # Free the slice MP4 from /tmp (tmpfs RAM) immediately after evidence clips are cut
                        try:
                            if seg.local_path and seg.local_path.exists():
                                seg.local_path.unlink(missing_ok=True)
                        except Exception:
                            pass

                    # Upload 20s evidence .mp4 clips to Google Drive BEFORE saving SegmentCheckpoint
                    # so that if a container restarts and resumes from GCS, every finding in the
                    # checkpoint already holds its permanent playable Drive video link.
                    enriched_slice_findings: List[Finding] = []
                    source_video_url = build_source_video_url(video_item.file_id)
                    for f_item in win_res.findings:
                        if (
                            f_item.evidence_clip_local_path
                            and self.reporter._gateway is not None
                            and evidence_subfolder_id
                        ):
                            clip_path = Path(f_item.evidence_clip_local_path)
                            if clip_path.exists():
                                try:
                                    uploaded_url = await asyncio.wait_for(
                                        self.reporter._gateway.upload_evidence_mp4(
                                            evidence_subfolder_id, clip_path
                                        ),
                                        timeout=60.0,
                                    )
                                    if uploaded_url:
                                        f_item = f_item.model_copy(
                                            update={"evidence_drive_url": uploaded_url}
                                        )
                                        clip_path.unlink(missing_ok=True)
                                except Exception as exc:
                                    logger.warning(
                                        "Job %s slice %s clip upload warning: %s",
                                        job.job_id,
                                        ckpt_key,
                                        exc,
                                    )
                        if (
                            f_item.status == Status.VIOLATION
                            and not f_item.evidence_drive_url
                            and source_video_url
                        ):
                            f_item = f_item.model_copy(
                                update={"evidence_drive_url": source_video_url}
                            )
                        enriched_slice_findings.append(f_item)

                    win_res = win_res.model_copy(
                        update={"findings": enriched_slice_findings}
                    )
                    carryover = win_res.carryover_state_summary
                    video_findings.extend(win_res.findings)
                    ledger_rows.append(ledger)

                    # Flush durable per-slice checkpoint to GCS immediately, merging any segments
                    # already persisted by another worker so checkpoints are monotonic.
                    latest_remote = await self.jobs.get(job.user_id, job.job_id)
                    if latest_remote is not None and latest_remote.completed_segments:
                        for r_key, r_ckpt in latest_remote.completed_segments.items():
                            completed_map.setdefault(r_key, r_ckpt)
                    completed_map[ckpt_key] = SegmentCheckpoint(
                        file_id=video_item.file_id,
                        filename=video_item.filename,
                        segment_index=idx,
                        carryover_state_summary=carryover,
                        findings=win_res.findings,
                        ledger_row=ledger,
                    )
                    base_job = latest_remote if latest_remote is not None else job
                    job = base_job.model_copy(
                        update={
                            "completed_segments": dict(completed_map),
                            "total_tokens_used": sum(
                                c.ledger_row.total_token_count for c in completed_map.values()
                            ),
                        }
                    )
                    job = await self.jobs.save(job)

                # Free downloaded raw source media in work_dir once all its slices are completed
                try:
                    downloaded_src = work_dir / video_item.filename
                    if downloaded_src.exists() and downloaded_src != video_item.local_path:
                        downloaded_src.unlink(missing_ok=True)
                except Exception:
                    pass

                deduped_findings = deduplicate_overlapping_findings(
                    video_findings,
                    overlap_window_sec=float(config.segment_overlap_sec),
                )
                findings_by_video.append((video_item.filename, deduped_findings))

            report = await self.reporter.publish_in_folder_report(
                audit_id=job.job_id,
                user_email=job.user_id,
                parent_folder_id=job.folder_id,
                findings_by_video=findings_by_video,
                ledger_rows=ledger_rows,
            )
            done_job = job.model_copy(
                update={
                    "state": JobState.DONE,
                    "completed_segments": dict(completed_map),
                    "report_sheet_url": report.report_sheet_url,
                    "total_tokens_used": sum(r.total_token_count for r in ledger_rows),
                    "violations_found": report.violation_rows_count,
                    "error_message": None,
                }
            )
            await self.jobs.save(done_job)
        except Exception as exc:
            needs_fix = isinstance(exc, WorkspaceConfigError)
            if needs_fix:
                logger.error("Detached audit job %s blocked by Workspace setup: %s", job.job_id, exc)
            else:
                logger.exception("Detached audit job %s failed: %s", job.job_id, exc)
            latest_remote = await self.jobs.get(job.user_id, job.job_id)
            if latest_remote is not None and latest_remote.state == JobState.DONE:
                logger.info(
                    "Job %s already marked DONE in store by another worker; not overwriting with FAILED.",
                    job.job_id,
                )
                return
            if latest_remote is not None and latest_remote.completed_segments:
                for r_key, r_ckpt in latest_remote.completed_segments.items():
                    completed_map.setdefault(r_key, r_ckpt)
            base_job = latest_remote if latest_remote is not None else job
            failed_job = base_job.model_copy(
                update={
                    "state": JobState.FAILED,
                    "completed_segments": dict(completed_map),
                    "total_tokens_used": sum(
                        c.ledger_row.total_token_count for c in completed_map.values()
                    ),
                    "error_message": str(exc),
                    "needs_operator_fix": needs_fix,
                }
            )
            await self.jobs.save(failed_job)
        finally:
            self._in_flight_jobs.discard(job.job_id)
            hb_task.cancel()

    async def sweep_and_resume_stale_jobs(
        self,
        *,
        stale_timeout_sec: float = STALE_WORKER_HEARTBEAT_SEC,
        max_auto_resumes: int = MAX_AUTO_RESUMES,
        now: Optional[float] = None,
        wait_for_completion: bool = False,
    ) -> List[AuditJob]:
        """Unattended Watchdog (`POST /internal/jobs/sweep` called every 2 min by Cloud Scheduler + Tier-1 loop):
        Scans `gs://${STAGING_BUCKET}/jobs/*/*.json` across all supervisors. If any Tier-2 worker crashed
        20 minutes after confirmation (leaving a stale `RUNNING` heartbeat > 180s or `FAILED` state) while
        the supervisor is away from GE chat, this method automatically resumes the job on a healthy Tier-2
        instance from the last GCS `SegmentCheckpoint` with zero human intervention.
        """
        import os

        if self.has_active_work() and not os.environ.get("CLOUD_RUN_WORKER_URL", "").strip():
            logger.info(
                "Skipping stale job sweep on this instance because active_jobs_count=%d (strict 1-job-per-container isolation)",
                self.active_jobs_count(),
            )
            return []

        candidates = await self.jobs.list_recoverable_jobs(
            stale_timeout_sec=stale_timeout_sec,
            max_auto_resumes=max_auto_resumes,
            now=now,
        )
        resumed_list: List[AuditJob] = []
        for candidate in candidates:
            logger.warning(
                "Unattended Watchdog auto-resuming crashed/stale job %s for %s (state=%s, completed_segments=%d, resume_count=%d)",
                candidate.job_id,
                candidate.user_id,
                candidate.state.value,
                len(candidate.completed_segments),
                candidate.resume_count,
            )
            resumed = await self.start_audit(
                user_id=candidate.user_id,
                job_id=candidate.job_id,
                wait_for_completion=wait_for_completion,
            )
            resumed_list.append(resumed)
        return resumed_list

    async def get_status(
        self,
        user_id: str,
        job_id: Optional[str] = None,
        *,
        auto_resume_stale: bool = True,
        stale_timeout_sec: float = STALE_WORKER_HEARTBEAT_SEC,
    ) -> Optional[AuditJob]:
        if job_id:
            job = await self.jobs.get(user_id, job_id)
        else:
            recent = await self.jobs.list_for_user(user_id, limit=1)
            job = recent[0] if recent else None

        if job is None:
            return None

        # Auto-heal stale RUNNING jobs when a Tier-2 worker instance was killed/preempted mid-slice
        if (
            auto_resume_stale
            and job.state == JobState.RUNNING
            and len(self._background_tasks) == 0
            and len(self._in_flight_jobs) == 0
            and (time.time() - job.heartbeat_at) > stale_timeout_sec
        ):
            logger.warning(
                "Detected stale RUNNING job %s (last heartbeat %.1fs ago); auto-resuming from %d checkpointed segments",
                job.job_id,
                time.time() - job.heartbeat_at,
                len(job.completed_segments),
            )
            prompt_cfg = await self.prompt_manager.load_active_config(
                sheet_id=config.effective_sop_source(job.folder_id)
            )
            job = job.model_copy(
                update={
                    "resume_count": job.resume_count + 1,
                    "error_message": None,
                }
            )
            job = await self.jobs.save(job)
            task = asyncio.create_task(self._dispatch_or_run_detached(job, prompt_cfg))
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)

        return job

    def active_jobs_count(self) -> int:
        """Returns number of active jobs executing in this container process."""
        import os

        if os.environ.get("CLOUD_RUN_WORKER_URL", "").strip():
            # Tier-1 dispatcher: outbound HTTP hold-connections in `_background_tasks` run on
            # Tier-2 Cloud Run, not locally. Count only jobs actually executing in-process here.
            return len(self._in_flight_jobs)
        local_in_flight = max(len(self._background_tasks), len(self._in_flight_jobs))
        if self.jobs._should_sync_live_gcs():
            return local_in_flight
        return max(local_in_flight, self.jobs.active_running_count())

    def has_active_work(self) -> bool:
        """True if any detached background audit or preflight probe is in flight in this process."""
        return self.active_jobs_count() > 0


