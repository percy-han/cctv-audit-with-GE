"""Multi-tenant user-isolated job store (`jobs.py`, adapted from `percy-han/cctv-audit`).

Enforces `REQ-008` (Multi-user isolation) and `ADR-006` (Zero-DB Cloud State Persistence):
1. Every `JobStore` method requires `user_id` (the caller's verified Google Workspace email
   passed by Gemini Enterprise). There is deliberately NO `get(job_id)` without `user_id`.
2. Each job is bound to the supervisor's `folder_id` (`Folder-as-a-Workspace`).
3. To prevent multi-instance state split-brain across turns (`Turn 1: preflight` -> `Turn 2: confirm`
   -> `Turn 3: status`) without introducing Firestore/Cloud SQL (`ADR-006` Zero-DB), `UserScopedJobStore`
   persists every `AuditJob` as a JSON state blob under `state_dir/{user_hash}/{job_id}.json` and
   (when `STAGING_BUCKET` is configured in GCP) mirrors to `gs://{STAGING_BUCKET}/jobs/{user_hash}/{job_id}.json`.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional

from pydantic import BaseModel, Field

from .agentic_auditor import Finding, TokenLedgerRow
from .video_ingestor import InspectFolderResponse

# Bucket name no stack derives (main.tf: <project>-<name_prefix>-staging) and that is never deployed:
# local runs / tests use it so that nothing reaches a live bucket unless explicitly running in the cloud.
LOCAL_PLACEHOLDER_BUCKET = "local-placeholder-bucket"

logger = logging.getLogger("cctv_audit.jobs")


class JobState(str, Enum):
    PROBING = "probing"
    READY = "ready"
    REJECTED = "rejected"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


TERMINAL_STATES = frozenset({JobState.REJECTED, JobState.DONE, JobState.FAILED})


def new_job_id() -> str:
    """Generates a 6-character hex job ID readable in chat."""
    return uuid.uuid4().hex[:6]


def _user_bucket_slug(user_id: str) -> str:
    """Deterministic, path-safe tenant directory slug from supervisor email."""
    norm = user_id.strip().lower()
    digest = hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]
    safe_prefix = "".join(ch if ch.isalnum() else "_" for ch in norm.split("@")[0])[:24]
    return f"{safe_prefix}_{digest}"


class SegmentCheckpoint(BaseModel):
    """Durable per-slice checkpoint persisted to GCS after each 30-min segment completes."""

    file_id: str
    filename: str
    segment_index: int
    carryover_state_summary: str = ""
    findings: List[Finding] = Field(default_factory=list)
    ledger_row: TokenLedgerRow


class AuditJob(BaseModel):
    """Strongly-typed state record for one supervisor audit session, persisted to GCS."""

    job_id: str = Field(default_factory=new_job_id)
    user_id: str = Field(description="Caller's verified Google Workspace email")
    session_id: str = Field(default="")
    folder_id: str = Field(description="Google Drive Folder ID (Folder-as-a-Workspace)")
    drive_url: str = Field(default="")
    state: JobState = Field(default=JobState.PROBING)
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
    heartbeat_at: float = Field(default_factory=time.time)
    preflight_report: Optional[InspectFolderResponse] = None
    completed_segments: Dict[str, SegmentCheckpoint] = Field(
        default_factory=dict,
        description="Keyed by '{file_id}:{segment_index}' so crashed Tier-2 workers resume without re-billing completed slices",
    )
    resume_count: int = Field(default=0, ge=0)
    report_sheet_url: Optional[str] = None
    active_model_version: str = ""
    active_prompt_version: str = ""
    total_tokens_used: int = 0
    violations_found: int = 0
    error_message: Optional[str] = None
    needs_operator_fix: bool = Field(
        default=False,
        description=(
            "FAILED on a Drive/Sheets setup problem (folder not shared as Editor, domain-wide delegation "
            "not authorised, bot without Drive storage). The watchdog never auto-resumes such a job; the "
            "supervisor fixes the setup and replies 确认开始, which clears the flag."
        ),
    )

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES


class UserScopedJobStore:
    """Write-through JobStore strictly partitioned by `user_id` (email) with Zero-DB disk/GCS state."""

    MAX_JOBS_PER_USER: int = 50

    def __init__(
        self,
        state_dir: Optional[Path] = None,
        gcs_bucket: Optional[str] = None,
        gcs_store: Optional[Dict[str, str]] = None,
    ) -> None:
        self._by_user: Dict[str, Dict[str, AuditJob]] = {}
        default_dir = os.environ.get("JOB_STATE_DIR", "/tmp/chagee_cctv_staging/jobs")
        self._state_dir: Optional[Path] = state_dir if state_dir is not None else Path(default_dir)
        raw_bucket = gcs_bucket if gcs_bucket is not None else os.environ.get("STAGING_BUCKET", "")
        self._gcs_bucket: str = raw_bucket.replace("gs://", "").strip("/")
        self._gcs_store: Optional[Dict[str, str]] = gcs_store
        self._gcs_blob_versions: Dict[str, str] = {}

    @staticmethod
    def _blob_version_token(item: Dict[str, Any]) -> str:
        parts = [
            str(item.get("generation") or ""),
            str(item.get("metageneration") or ""),
            str(item.get("etag") or ""),
            str(item.get("updated") or ""),
            str(item.get("md5Hash") or ""),
        ]
        return "|".join(p for p in parts if p)

    def _local_user_dir(self, user_id: str) -> Optional[Path]:
        if self._state_dir is None:
            return None
        d = self._state_dir / _user_bucket_slug(user_id)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _write_local(self, job: AuditJob) -> None:
        user_dir = self._local_user_dir(job.user_id)
        if user_dir is None:
            return
        target = user_dir / f"{job.job_id}.json"
        tmp = user_dir / f".{job.job_id}.json.tmp"
        tmp.write_text(job.model_dump_json(indent=2), encoding="utf-8")
        tmp.replace(target)

    def _sync_from_local(self, user_id: str) -> None:
        user_dir = self._local_user_dir(user_id)
        if user_dir is not None and user_dir.exists():
            bucket = self._by_user.setdefault(user_id.lower(), {})
            for path in user_dir.glob("*.json"):
                try:
                    loaded = AuditJob.model_validate_json(path.read_text(encoding="utf-8"))
                except Exception as exc:
                    logger.warning("Skipping corrupt job state file %s: %s", path, exc)
                    continue
                existing = bucket.get(loaded.job_id)
                if existing is None or loaded.updated_at >= existing.updated_at:
                    bucket[loaded.job_id] = loaded
        self._sync_from_gcs_sync(user_id)

    def _should_sync_live_gcs(self) -> bool:
        """True when running in Cloud Run / Vertex AI ReasoningEngine or with a real GCS bucket.

        `LOCAL_PLACEHOLDER_BUCKET` is a name that is never a real bucket (local runs and tests that
        inject `gcs_store`); it only reaches GCS when explicitly running in the cloud.
        """
        if not self._gcs_bucket:
            return False
        if self._gcs_bucket != LOCAL_PLACEHOLDER_BUCKET:
            return True
        return bool(
            os.environ.get("K_SERVICE")
            or os.environ.get("CLOUD_RUN_WORKER_URL")
            or os.environ.get("ENABLE_GCS_STATE_SYNC")
        )

    def _upload_gcs_sync(self, job: AuditJob) -> None:
        obj_name = f"jobs/{_user_bucket_slug(job.user_id)}/{job.job_id}.json"
        payload_str = job.model_dump_json(indent=2)
        if self._gcs_store is not None:
            self._gcs_store[obj_name] = payload_str
            return
        if not self._should_sync_live_gcs():
            return
        try:
            import json as _json
            import google.auth
            import google.auth.transport.requests

            creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/devstorage.read_write"])
            creds.refresh(google.auth.transport.requests.Request())
            url = (
                f"https://storage.googleapis.com/upload/storage/v1/b/"
                f"{urllib.parse.quote(self._gcs_bucket, safe='')}/o"
                f"?uploadType=media&name={urllib.parse.quote(obj_name, safe='')}"
            )
            data = payload_str.encode("utf-8")
            req = urllib.request.Request(url, data=data, method="POST")
            req.add_header("Authorization", f"Bearer {creds.token}")
            req.add_header("Content-Type", "application/json")
            with urllib.request.urlopen(req, timeout=10) as resp:
                try:
                    uploaded_meta = _json.loads(resp.read().decode("utf-8"))
                    if isinstance(uploaded_meta, dict):
                        ver_token = self._blob_version_token(uploaded_meta)
                        if ver_token:
                            self._gcs_blob_versions[obj_name] = ver_token
                except Exception:
                    self._gcs_blob_versions.pop(obj_name, None)
        except Exception as exc:
            logger.warning("Non-fatal GCS job state mirror warning for %s: %s", job.job_id, exc)

    def _sync_gcs_prefix_sync(self, prefix: str) -> None:
        import json as _json
        import google.auth
        import google.auth.transport.requests

        if not any(self._by_user.values()):
            self._gcs_blob_versions.clear()

        creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/devstorage.read_only"])
        creds.refresh(google.auth.transport.requests.Request())
        list_url = (
            f"https://storage.googleapis.com/storage/v1/b/"
            f"{urllib.parse.quote(self._gcs_bucket, safe='')}/o"
            f"?prefix={urllib.parse.quote(prefix, safe='')}"
        )
        req = urllib.request.Request(list_url, method="GET")
        req.add_header("Authorization", f"Bearer {creds.token}")
        with urllib.request.urlopen(req, timeout=10) as resp:
            listing = _json.loads(resp.read().decode("utf-8"))

        to_download: List[tuple[str, str]] = []
        for item in listing.get("items", []):
            name = item.get("name", "")
            if not name.endswith(".json"):
                continue
            ver_token = self._blob_version_token(item)
            if ver_token and self._gcs_blob_versions.get(name) == ver_token:
                continue
            to_download.append((name, ver_token))

        if not to_download:
            return

        token_str = creds.token
        bucket_quoted = urllib.parse.quote(self._gcs_bucket, safe="")

        def _fetch_one(pair: tuple[str, str]) -> Optional[tuple[str, str, AuditJob]]:
            name, ver_token = pair
            media_url = (
                f"https://storage.googleapis.com/storage/v1/b/"
                f"{bucket_quoted}/o/{urllib.parse.quote(name, safe='')}?alt=media"
            )
            mreq = urllib.request.Request(media_url, method="GET")
            mreq.add_header("Authorization", f"Bearer {token_str}")
            try:
                with urllib.request.urlopen(mreq, timeout=10) as mresp:
                    loaded = AuditJob.model_validate_json(mresp.read().decode("utf-8"))
                return (name, ver_token, loaded)
            except Exception as exc:
                logger.warning("Skipping unreadable/corrupt GCS job blob %s: %s", name, exc)
                return None

        if len(to_download) == 1:
            fetched = [_fetch_one(to_download[0])]
        else:
            from concurrent.futures import ThreadPoolExecutor

            with ThreadPoolExecutor(max_workers=min(8, len(to_download))) as pool:
                fetched = list(pool.map(_fetch_one, to_download))

        for res in fetched:
            if res is None:
                continue
            name, ver_token, loaded = res
            bucket = self._by_user.setdefault(loaded.user_id.lower(), {})
            existing = bucket.get(loaded.job_id)
            if existing is None or loaded.updated_at >= existing.updated_at:
                bucket[loaded.job_id] = loaded
            if ver_token:
                self._gcs_blob_versions[name] = ver_token

    def _sync_from_gcs_sync(self, user_id: str) -> None:
        prefix = f"jobs/{_user_bucket_slug(user_id)}/"
        bucket = self._by_user.setdefault(user_id.lower(), {})
        if self._gcs_store is not None:
            for key, raw_json in list(self._gcs_store.items()):
                if key.startswith(prefix) and key.endswith(".json"):
                    try:
                        loaded = AuditJob.model_validate_json(raw_json)
                        existing = bucket.get(loaded.job_id)
                        if existing is None or loaded.updated_at >= existing.updated_at:
                            bucket[loaded.job_id] = loaded
                    except Exception as exc:
                        logger.warning("Skipping corrupt in-memory GCS state %s: %s", key, exc)
            return
        if not self._should_sync_live_gcs():
            return
        try:
            if not bucket:
                for k in [k for k in self._gcs_blob_versions if k.startswith(prefix)]:
                    self._gcs_blob_versions.pop(k, None)
            self._sync_gcs_prefix_sync(prefix)
        except Exception as exc:
            logger.warning("Non-fatal GCS job state sync warning for %s: %s", user_id, exc)

    async def save(self, job: AuditJob) -> AuditJob:
        if not job.user_id:
            raise ValueError("user_id (supervisor email) is required for tenant isolation")
        now = time.time()
        updated = job.model_copy(update={"updated_at": now, "heartbeat_at": now})
        user_bucket = self._by_user.setdefault(job.user_id.lower(), {})
        user_bucket[updated.job_id] = updated
        if len(user_bucket) > self.MAX_JOBS_PER_USER:
            terminal = sorted(
                (j for j in user_bucket.values() if j.is_terminal),
                key=lambda j: j.updated_at,
            )
            while len(user_bucket) > self.MAX_JOBS_PER_USER and terminal:
                oldest = terminal.pop(0)
                user_bucket.pop(oldest.job_id, None)
        await asyncio.to_thread(self._write_local, updated)
        if self._gcs_store is not None or self._should_sync_live_gcs():
            await asyncio.to_thread(self._upload_gcs_sync, updated)
        return updated

    async def get(self, user_id: str, job_id: str) -> Optional[AuditJob]:
        """Strictly scoped by `user_id` — supervisor A can never query supervisor B's job."""
        if not user_id or not job_id:
            return None
        await asyncio.to_thread(self._sync_from_local, user_id)
        return self._by_user.get(user_id.lower(), {}).get(job_id)

    async def latest_ready_job(self, user_id: str, session_id: str = "") -> Optional[AuditJob]:
        """Finds the most recent `READY` job awaiting confirmation for `user_id`."""
        await asyncio.to_thread(self._sync_from_local, user_id)
        user_jobs = list(self._by_user.get(user_id.lower(), {}).values())
        ready_jobs = [
            j
            for j in user_jobs
            if j.state == JobState.READY
            and (not session_id or j.session_id == session_id)
        ]
        if not ready_jobs:
            return None
        ready_jobs.sort(key=lambda j: j.updated_at, reverse=True)
        return ready_jobs[0]

    async def list_for_user(self, user_id: str, limit: int = 10) -> List[AuditJob]:
        await asyncio.to_thread(self._sync_from_local, user_id)
        user_jobs = list(self._by_user.get(user_id.lower(), {}).values())
        user_jobs.sort(key=lambda j: j.updated_at, reverse=True)
        return user_jobs[:limit]

    def active_running_count(self) -> int:
        """Returns the number of jobs currently in RUNNING or non-stale PROBING state across all tenants."""
        now_ts = time.time()
        count = 0
        for bucket in self._by_user.values():
            for job in bucket.values():
                if job.state == JobState.RUNNING:
                    count += 1
                elif job.state == JobState.PROBING and (now_ts - job.updated_at) <= 60.0:
                    count += 1
        return count

    def _sync_all_jobs_sync(self) -> None:
        """Synchronizes all tenant jobs from local disk and GCS (`jobs/*/*.json`) for unattended watchdog sweeps."""
        if self._state_dir is not None and self._state_dir.exists():
            for path in self._state_dir.glob("*/*.json"):
                try:
                    loaded = AuditJob.model_validate_json(path.read_text(encoding="utf-8"))
                    bucket = self._by_user.setdefault(loaded.user_id.lower(), {})
                    existing = bucket.get(loaded.job_id)
                    if existing is None or loaded.updated_at >= existing.updated_at:
                        bucket[loaded.job_id] = loaded
                except Exception as exc:
                    logger.warning("Skipping corrupt local job file %s: %s", path, exc)

        if self._gcs_store is not None:
            for key, raw_json in list(self._gcs_store.items()):
                if key.startswith("jobs/") and key.endswith(".json"):
                    try:
                        loaded = AuditJob.model_validate_json(raw_json)
                        bucket = self._by_user.setdefault(loaded.user_id.lower(), {})
                        existing = bucket.get(loaded.job_id)
                        if existing is None or loaded.updated_at >= existing.updated_at:
                            bucket[loaded.job_id] = loaded
                    except Exception as exc:
                        logger.warning("Skipping corrupt in-memory GCS state %s: %s", key, exc)
            return

        if not self._should_sync_live_gcs():
            return
        try:
            self._sync_gcs_prefix_sync("jobs/")
        except Exception as exc:
            logger.warning("Non-fatal GCS global watchdog sweep sync warning: %s", exc)

    async def touch_heartbeat(self, user_id: str, job_id: str) -> Optional[AuditJob]:
        """Refreshes `heartbeat_at` and `updated_at` in GCS while a long 30-min slice is actively running."""
        job = await self.get(user_id, job_id)
        if job is None or job.state != JobState.RUNNING:
            return job
        return await self.save(job)

    async def list_recoverable_jobs(
        self,
        *,
        stale_timeout_sec: float = 180.0,
        max_auto_resumes: int = 3,
        now: Optional[float] = None,
    ) -> List[AuditJob]:
        """Finds all jobs across all tenants whose Tier-2 worker crashed (`FAILED` or stale `RUNNING` heartbeat)
        so the unattended Watchdog (`Cloud Scheduler` / Tier-1 background loop) can auto-resume them without
        waiting for the user to return to GE chat.
        """
        await asyncio.to_thread(self._sync_all_jobs_sync)
        current_ts = now if now is not None else time.time()
        recoverable: List[AuditJob] = []
        for bucket in self._by_user.values():
            for job in bucket.values():
                if job.resume_count >= max_auto_resumes:
                    continue
                if job.needs_operator_fix:
                    continue
                is_stale_running = (
                    job.state == JobState.RUNNING
                    and (current_ts - job.heartbeat_at) > stale_timeout_sec
                )
                is_crashed_failed = job.state == JobState.FAILED
                if is_stale_running or is_crashed_failed:
                    recoverable.append(job)
        recoverable.sort(key=lambda j: j.updated_at)
        return recoverable[:1]

