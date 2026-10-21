"""REQ-009: Daily Scheduled Auto-Audit Engine (Root Folder + Lookback Window + Cloud Run Jobs 24h).

Implements the 6 architectural components approved in SDD v4.2:
1. `[Tab 2: 定时巡检配置表]` (`Tab2_定时巡检配置表`) rule loading & `Last_Run_Status` write-back.
2. Timezone-aware trigger evaluation + GCS atomic daily lock (`ifGenerationMatch=0`).
3. Bottom-up Google Drive `createdTime` incremental discovery + Time-Agnostic Closest-Ancestor
   Carve-Out (`无视触发时间的全表 ENABLED 行就近归属`) + smallest-`row_index` tie-breaker.
4. 3-state per-file deduplication ledger (`DISPATCHED` / `COMPLETED` / `FAILED`) with automatic
   re-claim when a prior `DISPATCHED` job failed.
5. Leaf-folder store job splitting (`execution_backend="cloud_run_job"`) and Cloud Run Jobs v2
   `:run` API execution with container env overrides (`CCTV_AUDIT_JOB_ID`, `CCTV_AUDIT_USER_ID`,
   `CCTV_AUDIT_BATCH_ID`).
6. Optimistic-concurrency (`ifGenerationMatch`) batch fan-in aggregator that sends a single
   consolidated multi-store Google Chat summary card when the last store in the batch finishes.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import posixpath
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from .config import config
from .gcs_gateway import EVIDENCE_DIR_MARKER, is_gcs_target, parse_gcs_uri
from .jobs import AuditJob, JobState
from .prompt_manager import PromptManager, ScheduledAuditRule, ScheduledEvalRule
from .video_ingestor import VideoMetadataItem, natural_video_sort_key
from .workspace_reporter import EVIDENCE_SUBFOLDER_NAME

logger = logging.getLogger(__name__)


def _safe_slug(raw: str, max_len: int = 48) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "_", (raw or "").strip()).strip("_")
    digest = hashlib.sha256((raw or "").encode("utf-8")).hexdigest()[:12]
    return f"{cleaned[:max_len]}_{digest}" if cleaned else digest


class FileLedgerState(str, Enum):
    DISPATCHED = "DISPATCHED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    REJECTED_LOW_RES = "REJECTED_LOW_RES"


class FileLedgerEntry(BaseModel):
    """Per-video deduplication ledger stored at `schedules/ledgers/files/{safe_file_id}.json`."""

    file_id: str
    filename: str
    leaf_folder_id: str
    rule_id: str
    batch_id: str
    job_id: str
    user_id: str
    state: FileLedgerState = FileLedgerState.DISPATCHED
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
    error_message: str = ""


class ScheduledStoreTask(BaseModel):
    """Status of one leaf-folder store `AuditJob` within a scheduled batch."""

    job_id: str
    user_id: str
    folder_id: str
    folder_name: str
    video_file_ids: List[str] = Field(default_factory=list)
    video_count: int = 0
    skipped_low_res_videos: List[str] = Field(default_factory=list)
    state: str = "RUNNING"
    violations_found: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    report_sheet_url: str = ""
    error_message: str = ""


class ScheduledBatchRecord(BaseModel):
    """Multi-store batch state record stored at `batches/{batch_id}.json` with CAS optimistic locking."""

    batch_id: str
    rule_id: str
    rule_name: str
    row_index: int
    root_folder_id: str
    root_folder_url: str
    supervisor_email: str
    local_date: str
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
    store_tasks: Dict[str, ScheduledStoreTask] = Field(default_factory=dict)
    start_notification_sent: bool = False
    notification_sent: bool = False
    status_written: bool = False


class DiscoveredVideoCandidate(BaseModel):
    """One newly uploaded video matched to its closest-ancestor enabled rule and leaf store folder."""

    file_id: str
    filename: str
    leaf_folder_id: str
    leaf_folder_name: str
    owner_rule_id: str
    created_time_iso: str = ""
    width: int = 1920
    height: int = 1080
    duration_sec: float = 300.0
    mime_type: str = "video/mp4"
    size_bytes: int = 0


class ScheduledEvalTaskRecord(BaseModel):
    """State record for one Sheet-triggered golden-set evaluation (`Tab3_黄金集评测工作台`) stored at `eval_tasks/{eval_task_id}.json`."""

    eval_task_id: str
    eval_id: str
    eval_name: str
    row_index: int
    prompt_tab_name: str
    model_version: str
    golden_videos_folder_url: str = ""
    golden_videos_folder_id: str = ""
    golden_sheet_url: str
    golden_source_id: str
    results_folder_url: str = ""
    results_folder_id: str = ""
    supervisor_email: str
    state: str = "RUNNING"
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
    report_sheet_url: str = ""
    summary_text: str = ""
    error_message: str = ""


class PreconditionFailedError(RuntimeError):
    """Raised when a GCS `ifGenerationMatch` CAS precondition fails (HTTP 412)."""


class ScheduledStateStore:
    """Atomic CAS state store for daily locks, 3-state file ledgers, and batch records.

    Uses live GCS (`gs://<staging_bucket>/...`) with `if_generation_match` when configured, and
    supports an in-memory / local directory store with exact generation numbers for unit tests.
    """

    def __init__(
        self,
        *,
        gcs_bucket_name: Optional[str] = None,
        local_dir: Optional[Path] = None,
        memory_store: Optional[Dict[str, str]] = None,
    ) -> None:
        self._bucket_name = (
            gcs_bucket_name if gcs_bucket_name is not None else (config.staging_bucket or "")
        ).replace("gs://", "").strip("/")
        self._local_dir = local_dir
        self._memory: Dict[str, str] = memory_store if memory_store is not None else {}
        self._generations: Dict[str, int] = {}
        self._use_memory = memory_store is not None or not self._bucket_name or bool(
            os.environ.get("PYTEST_CURRENT_TEST") and gcs_bucket_name is None
        )
        self._creds: Any = None

    def _get_token(self) -> str:
        import google.auth
        import google.auth.transport.requests

        if self._creds is None:
            self._creds, _ = google.auth.default(
                scopes=["https://www.googleapis.com/auth/devstorage.read_write"]
            )
        if not getattr(self._creds, "valid", False):
            self._creds.refresh(google.auth.transport.requests.Request())
        return str(self._creds.token)

    def _request_gcs(
        self,
        method: str,
        url: str,
        *,
        body: Optional[bytes] = None,
        content_type: str = "",
        timeout: float = 20.0,
    ) -> Tuple[int, bytes]:
        req = urllib.request.Request(url, data=body, method=method)
        req.add_header("Authorization", f"Bearer {self._get_token()}")
        if content_type:
            req.add_header("Content-Type", content_type)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return int(resp.status), resp.read()
        except urllib.error.HTTPError as exc:
            return int(exc.code), exc.read() or b""

    def read_text_with_generation(self, key: str) -> Tuple[Optional[str], int]:
        """Returns `(raw_json_text, generation)` or `(None, 0)` if the object does not exist."""
        if self._use_memory:
            if key in self._memory:
                return self._memory[key], self._generations.get(key, 1)
            if self._local_dir is not None:
                p = self._local_dir / key
                if p.exists():
                    raw = p.read_text(encoding="utf-8")
                    gen = self._generations.setdefault(key, 1)
                    self._memory[key] = raw
                    return raw, gen
            return None, 0

        bkt = urllib.parse.quote(self._bucket_name, safe="")
        obj = urllib.parse.quote(key, safe="")
        meta_url = f"https://storage.googleapis.com/storage/v1/b/{bkt}/o/{obj}?fields=generation"
        status, meta_body = self._request_gcs("GET", meta_url)
        if status == 404:
            return None, 0
        if status >= 400:
            raise RuntimeError(
                f"GCS metadata GET gs://{self._bucket_name}/{key} failed (HTTP {status}): "
                f"{meta_body[:200].decode('utf-8', 'replace')}"
            )
        meta_doc = json.loads(meta_body.decode("utf-8") or "{}")
        gen = int(meta_doc.get("generation") or 1)
        media_url = f"https://storage.googleapis.com/storage/v1/b/{bkt}/o/{obj}?alt=media&generation={gen}"
        m_status, m_body = self._request_gcs("GET", media_url)
        if m_status == 404:
            return None, 0
        if m_status >= 400:
            raise RuntimeError(
                f"GCS media GET gs://{self._bucket_name}/{key}#{gen} failed (HTTP {m_status}): "
                f"{m_body[:200].decode('utf-8', 'replace')}"
            )
        return m_body.decode("utf-8"), gen

    def write_text_cas(
        self, key: str, payload: str, *, if_generation_match: Optional[int] = None
    ) -> int:
        """Writes `payload` to `key` with optional `if_generation_match` precondition.

        - `if_generation_match == 0`: succeeds only if `key` does not exist yet.
        - `if_generation_match == N` (`N > 0`): succeeds only if current generation is `N`.
        - Raises `PreconditionFailedError` on generation mismatch.
        Returns the new generation number.
        """
        if self._use_memory:
            current_raw, current_gen = self.read_text_with_generation(key)
            exists = current_raw is not None
            if if_generation_match is not None:
                if if_generation_match == 0 and exists:
                    raise PreconditionFailedError(f"Key {key} already exists (gen={current_gen})")
                if if_generation_match > 0 and current_gen != if_generation_match:
                    raise PreconditionFailedError(
                        f"Key {key} generation mismatch: expected {if_generation_match}, got {current_gen}"
                    )
            new_gen = current_gen + 1
            self._memory[key] = payload
            self._generations[key] = new_gen
            if self._local_dir is not None:
                p = self._local_dir / key
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(payload, encoding="utf-8")
            return new_gen

        bkt = urllib.parse.quote(self._bucket_name, safe="")
        obj = urllib.parse.quote(key, safe="")
        upload_url = f"https://storage.googleapis.com/upload/storage/v1/b/{bkt}/o?uploadType=media&name={obj}"
        if if_generation_match is not None:
            upload_url += f"&ifGenerationMatch={int(if_generation_match)}"
        status, resp_body = self._request_gcs(
            "POST",
            upload_url,
            body=payload.encode("utf-8"),
            content_type="application/json; charset=utf-8",
        )
        if status == 412:
            raise PreconditionFailedError(
                f"GCS CAS precondition failed (ifGenerationMatch={if_generation_match}) on gs://{self._bucket_name}/{key}"
            )
        if status >= 400:
            raise RuntimeError(
                f"GCS CAS upload gs://{self._bucket_name}/{key} failed (HTTP {status}): "
                f"{resp_body[:200].decode('utf-8', 'replace')}"
            )
        resp_doc = json.loads(resp_body.decode("utf-8") or "{}")
        return int(resp_doc.get("generation") or 1)

    @staticmethod
    def lock_key(rule_id: str, local_date: str) -> str:
        return f"schedules/locks/{_safe_slug(rule_id)}_{local_date}.json"

    @staticmethod
    def file_ledger_key(file_id: str) -> str:
        return f"schedules/ledgers/files/{_safe_slug(file_id)}.json"

    @staticmethod
    def batch_key(batch_id: str) -> str:
        return f"batches/{batch_id}.json"

    @staticmethod
    def eval_lock_key(eval_id: str) -> str:
        return f"schedules/eval_locks/{_safe_slug(eval_id)}.json"

    @staticmethod
    def eval_task_key(eval_task_id: str) -> str:
        return f"eval_tasks/{eval_task_id}.json"


def is_rule_due(
    rule: ScheduledAuditRule,
    now_utc: datetime,
    *,
    grace_minutes: int = 120,
) -> Tuple[bool, str]:
    """Evaluates whether `rule` is within its daily trigger window `[Trigger_Time, Trigger_Time + grace]`.

    Checks both today's local trigger timestamp and yesterday's local trigger timestamp so a
    late-night schedule (e.g. `23:30` with a 120-minute grace window spanning into `00:00–01:30`
    the next day) is never missed across the midnight boundary and always binds to its scheduled
    cycle date (`YYYY-MM-DD`).
    """
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    tz = ZoneInfo(rule.timezone)
    local_now = now_utc.astimezone(tz)
    local_date = local_now.strftime("%Y-%m-%d")
    if not rule.enabled:
        return False, local_date

    parts = rule.trigger_time.strip().split(":")
    hh, mm = int(parts[0]), int(parts[1])
    grace_sec = grace_minutes * 60.0

    target_today = local_now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    delta_today = (local_now - target_today).total_seconds()
    if 0.0 <= delta_today <= grace_sec:
        return True, target_today.strftime("%Y-%m-%d")

    target_yesterday = target_today - timedelta(days=1)
    delta_yesterday = (local_now - target_yesterday).total_seconds()
    if 0.0 <= delta_yesterday <= grace_sec:
        return True, target_yesterday.strftime("%Y-%m-%d")

    return False, local_date


def resolve_canonical_enabled_rules(
    rules: Sequence[ScheduledAuditRule],
) -> Tuple[List[ScheduledAuditRule], List[Tuple[ScheduledAuditRule, ScheduledAuditRule]]]:
    """Resolves all `ENABLED` rules and applies the smallest-`row_index` tie-breaker for identical roots.

    Returns:
      - `canonical_rules`: One winning `ScheduledAuditRule` per unique `root_folder_id`.
      - `shadowed_pairs`: List of `(duplicate_rule, winner_rule)` pairs where two or more enabled
        rules pointed to the exact same `root_folder_id`.
    """
    by_root: Dict[str, List[ScheduledAuditRule]] = {}
    for r in rules:
        if not r.enabled or not r.root_folder_id:
            continue
        by_root.setdefault(r.root_folder_id, []).append(r)

    canonical: List[ScheduledAuditRule] = []
    shadowed: List[Tuple[ScheduledAuditRule, ScheduledAuditRule]] = []
    for root_id, group in by_root.items():
        group_sorted = sorted(group, key=lambda item: (item.row_index, item.rule_id))
        winner = group_sorted[0]
        canonical.append(winner)
        for dup in group_sorted[1:]:
            shadowed.append((dup, winner))
    canonical.sort(key=lambda item: item.row_index)
    return canonical, shadowed


def _parse_rfc3339_utc(ts_str: str) -> Optional[datetime]:
    cleaned = (ts_str or "").strip()
    if not cleaned:
        return None
    try:
        if cleaned.endswith("Z"):
            cleaned = cleaned[:-1] + "+00:00"
        dt = datetime.fromisoformat(cleaned)
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def discover_and_route_drive_videos_sync(
    drive_service: Any,
    *,
    canonical_enabled_rules: Sequence[ScheduledAuditRule],
    max_lookback_hours: float,
    now_utc: datetime,
    ffprobe_fallback: Optional[Callable[[str], Tuple[int, int, float]]] = None,
) -> Dict[str, List[DiscoveredVideoCandidate]]:
    """Bottom-up Google Drive incremental video discovery with Time-Agnostic Closest-Ancestor Carve-Out.

    1. Queries all non-trashed videos with `createdTime >= now_utc - max_lookback_hours`.
    2. For each video, walks up its `parents` folder chain (`max_depth = 15`), caching folder
       metadata (`id, name, parents, trashed`) so thousands of videos in the same store folders
       reuse cached parent lookups.
    3. Ignores any video inside `📁 违规证据切片_Evidence` or a trashed ancestor.
    4. At the FIRST ancestor folder (starting from `leaf_folder_id` upward) that matches ANY rule
       in `canonical_enabled_rules` (`enabled_root_owners`), assigns the video exclusively to that
       closest-ancestor rule, **regardless of each rule's `Trigger_Time`**.
    5. Filters each matched video against its assigned rule's own `lookback_hours` window.
    """
    if not canonical_enabled_rules:
        return {}
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)

    enabled_root_owners: Dict[str, ScheduledAuditRule] = {
        r.root_folder_id: r for r in canonical_enabled_rules if not is_gcs_target(r.root_folder_id)
    }
    if not enabled_root_owners:
        return {}

    global_cutoff = now_utc - timedelta(hours=max(1.0, float(max_lookback_hours)))
    cutoff_iso = global_cutoff.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    raw_files: List[Dict[str, Any]] = []
    page_token: Optional[str] = None
    query = (
        f"mimeType contains 'video/' and trashed = false and createdTime >= '{cutoff_iso}'"
    )
    while True:
        resp = (
            drive_service.files()
            .list(
                q=query,
                fields=(
                    "nextPageToken, "
                    "files(id, name, mimeType, size, parents, createdTime, videoMediaMetadata)"
                ),
                pageSize=500,
                pageToken=page_token,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute(num_retries=3)
        )
        raw_files.extend(resp.get("files", []) or [])
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    folder_cache: Dict[str, Dict[str, Any]] = {}

    def _get_folder_meta(fid: str) -> Optional[Dict[str, Any]]:
        if fid in folder_cache:
            return folder_cache[fid]
        try:
            meta = (
                drive_service.files()
                .get(
                    fileId=fid,
                    fields="id, name, parents, trashed",
                    supportsAllDrives=True,
                )
                .execute(num_retries=3)
            )
            folder_cache[fid] = meta
            return meta
        except Exception as exc:
            logger.debug("Could not fetch Drive folder metadata for %s: %s", fid, exc)
            folder_cache[fid] = {}
            return None

    routed: Dict[str, List[DiscoveredVideoCandidate]] = {
        r.rule_id: [] for r in canonical_enabled_rules
    }

    for f in raw_files:
        vid_id = str(f.get("id") or "").strip()
        vid_name = str(f.get("name") or "").strip()
        parents = f.get("parents") or []
        if not vid_id or not vid_name or not parents:
            continue
        leaf_folder_id = str(parents[0]).strip()
        if not leaf_folder_id:
            continue

        # Walk bottom-up from leaf_folder_id to find the FIRST ancestor matching any enabled rule
        curr_id: Optional[str] = leaf_folder_id
        leaf_folder_name = leaf_folder_id
        matched_rule: Optional[ScheduledAuditRule] = None
        skip_video = False
        visited: set[str] = set()

        for depth in range(15):
            if not curr_id or curr_id in visited:
                break
            visited.add(curr_id)
            meta = _get_folder_meta(curr_id)
            if not meta:
                break
            if meta.get("trashed"):
                skip_video = True
                break
            fname = str(meta.get("name") or curr_id).strip()
            if depth == 0:
                leaf_folder_name = fname
            if fname == EVIDENCE_SUBFOLDER_NAME or EVIDENCE_DIR_MARKER in fname:
                skip_video = True
                break
            if curr_id in enabled_root_owners:
                matched_rule = enabled_root_owners[curr_id]
                break
            p_list = meta.get("parents") or []
            curr_id = str(p_list[0]).strip() if p_list else None

        if skip_video or matched_rule is None:
            continue

        # Verify the video's createdTime is within the matched rule's own lookback_hours
        created_str = str(f.get("createdTime") or "")
        created_dt = _parse_rfc3339_utc(created_str)
        if created_dt is not None:
            rule_cutoff = now_utc - timedelta(hours=float(matched_rule.lookback_hours))
            if created_dt < rule_cutoff:
                continue

        vmeta = f.get("videoMediaMetadata") or {}
        width = int(vmeta.get("width") or 0)
        height = int(vmeta.get("height") or 0)
        dur_ms = int(vmeta.get("durationMillis") or 0)
        duration_sec = round(dur_ms / 1000.0, 2) if dur_ms > 0 else 0.0
        if (width <= 0 or height <= 0 or duration_sec <= 0.0) and ffprobe_fallback is not None:
            try:
                pw, ph, pd = ffprobe_fallback(vid_id)
                width = width or pw
                height = height or ph
                duration_sec = duration_sec or pd
            except Exception:
                pass
        if width <= 0 or height <= 0:
            width, height = 1920, 1080
        if duration_sec <= 0.0:
            duration_sec = 300.0

        routed[matched_rule.rule_id].append(
            DiscoveredVideoCandidate(
                file_id=vid_id,
                filename=vid_name,
                leaf_folder_id=leaf_folder_id,
                leaf_folder_name=leaf_folder_name,
                owner_rule_id=matched_rule.rule_id,
                created_time_iso=created_str,
                width=width,
                height=height,
                duration_sec=duration_sec,
                mime_type=str(f.get("mimeType") or "video/mp4"),
                size_bytes=int(f.get("size") or 0),
            )
        )

    for r_id in routed:
        routed[r_id].sort(
            key=lambda c: (c.leaf_folder_name, natural_video_sort_key(c.filename))
        )
    return routed


def discover_and_route_gcs_videos_sync(
    gcs_gateway: Any,
    *,
    canonical_enabled_rules: Sequence[ScheduledAuditRule],
    now_utc: datetime,
) -> Dict[str, List[DiscoveredVideoCandidate]]:
    """GCS prefix hierarchical video discovery with Time-Agnostic Closest-Ancestor Carve-Out."""
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    gcs_rules = [r for r in canonical_enabled_rules if is_gcs_target(r.root_folder_id)]
    if not gcs_rules:
        return {}

    enabled_prefix_owners: Dict[str, ScheduledAuditRule] = {}
    buckets_to_scan: Dict[str, List[ScheduledAuditRule]] = {}
    for r in gcs_rules:
        bkt, pfx = parse_gcs_uri(r.root_folder_id)
        norm_uri = f"gs://{bkt}/{pfx}".rstrip("/")
        enabled_prefix_owners[norm_uri] = r
        buckets_to_scan.setdefault(bkt, []).append(r)

    routed: Dict[str, List[DiscoveredVideoCandidate]] = {r.rule_id: [] for r in gcs_rules}
    for bucket, b_rules in buckets_to_scan.items():
        # List all objects under each top-level prefix
        seen_objects: Dict[str, Dict[str, Any]] = {}
        for r in b_rules:
            _, pfx = parse_gcs_uri(r.root_folder_id)
            page_token = ""
            while True:
                url = (
                    f"https://storage.googleapis.com/storage/v1/b/{urllib.parse.quote(bucket, safe='')}/o"
                    f"?prefix={urllib.parse.quote(pfx + '/' if pfx else '', safe='')}"
                    "&fields=nextPageToken,items(name,size,contentType,timeCreated,updated,metadata)"
                )
                if page_token:
                    url += f"&pageToken={urllib.parse.quote(page_token, safe='')}"
                status, body = gcs_gateway._request("GET", url, timeout=20.0)
                if status >= 400:
                    break
                page = json.loads(body.decode("utf-8") or "{}")
                for item in page.get("items", []) or []:
                    obj_name = str(item.get("name") or "")
                    if obj_name and gcs_gateway._is_video_object(item):
                        seen_objects[obj_name] = item
                page_token = str(page.get("nextPageToken") or "")
                if not page_token:
                    break

        for obj_name, item in seen_objects.items():
            parent_dir = posixpath.dirname(obj_name).strip("/")
            leaf_uri = f"gs://{bucket}/{parent_dir}".rstrip("/")
            curr_dir = parent_dir
            matched_rule: Optional[ScheduledAuditRule] = None
            while True:
                cand_uri = f"gs://{bucket}/{curr_dir}".rstrip("/")
                if cand_uri in enabled_prefix_owners:
                    matched_rule = enabled_prefix_owners[cand_uri]
                    break
                if not curr_dir:
                    break
                curr_dir = posixpath.dirname(curr_dir).strip("/") if "/" in curr_dir else ""

            if matched_rule is None:
                continue

            created_str = str(item.get("timeCreated") or item.get("updated") or "")
            created_dt = _parse_rfc3339_utc(created_str)
            if created_dt is not None:
                rule_cutoff = now_utc - timedelta(hours=float(matched_rule.lookback_hours))
                if created_dt < rule_cutoff:
                    continue

            w, h, d = gcs_gateway._object_dims(bucket, item)
            if w <= 0 or h <= 0:
                w, h = 1920, 1080
            if d <= 0.0:
                d = 300.0
            routed[matched_rule.rule_id].append(
                DiscoveredVideoCandidate(
                    file_id=f"gs://{bucket}/{obj_name}",
                    filename=posixpath.basename(obj_name),
                    leaf_folder_id=leaf_uri,
                    leaf_folder_name=posixpath.basename(parent_dir) or bucket,
                    owner_rule_id=matched_rule.rule_id,
                    created_time_iso=created_str,
                    width=w,
                    height=h,
                    duration_sec=d,
                    mime_type=str(item.get("contentType") or "video/mp4"),
                    size_bytes=int(item.get("size") or 0),
                )
            )

    for r_id in routed:
        routed[r_id].sort(
            key=lambda c: (c.leaf_folder_name, natural_video_sort_key(c.filename))
        )
    return routed


class ScheduledAuditEngine:
    """Orchestrates daily scheduled rules, 3-state file ledgers, Cloud Run Jobs dispatch, and Batch fan-in."""

    def __init__(
        self,
        *,
        audit_service: Any,
        state_store: Optional[ScheduledStateStore] = None,
        drive_service_factory: Optional[Callable[[str], Any]] = None,
        cloud_run_job_dispatcher: Optional[Callable[[AuditJob], Any]] = None,
        eval_runner: Optional[Callable[..., Any]] = None,
    ) -> None:
        self.service = audit_service
        self.state_store = state_store if state_store is not None else ScheduledStateStore()
        self._drive_service_factory = drive_service_factory
        self._cloud_run_job_dispatcher = cloud_run_job_dispatcher
        self._eval_runner = eval_runner

    def _get_drive_service(self, supervisor_email: str) -> Any:
        if self._drive_service_factory is not None:
            return self._drive_service_factory(supervisor_email)
        from .gcp import GoogleWorkspaceGateway

        gw = GoogleWorkspaceGateway(impersonate_user=supervisor_email)
        return gw._drive_service()

    def try_acquire_daily_rule_lock(
        self, rule: ScheduledAuditRule, local_date: str, now_utc: datetime
    ) -> bool:
        """Atomically acquires `schedules/locks/{rule_id}_{local_date}.json` (`if_generation_match=0`).

        If a lock for `(rule_id, local_date)` already exists, inspects its stored `trigger_time` and
        `timezone`: if the operator changed `Trigger_Hour_UTC` / `Trigger_Minute_UTC` in the Sheet
        on the same day, CAS-updates the lock (`if_generation_match=existing_gen`) so the rescheduled
        time executes immediately without being blocked by an earlier hour's lock.
        """
        key = self.state_store.lock_key(rule.rule_id, local_date)
        payload = json.dumps(
            {
                "rule_id": rule.rule_id,
                "rule_name": rule.rule_name,
                "local_date": local_date,
                "timezone": rule.timezone,
                "trigger_time": rule.trigger_time,
                "acquired_at": now_utc.isoformat(),
            },
            ensure_ascii=False,
        )
        try:
            self.state_store.write_text_cas(key, payload, if_generation_match=0)
            return True
        except PreconditionFailedError:
            raw_existing, existing_gen = self.state_store.read_text_with_generation(key)
            if raw_existing and existing_gen > 0:
                try:
                    existing_doc = json.loads(raw_existing)
                except Exception:
                    existing_doc = {}
                prev_time = str(existing_doc.get("trigger_time") or "").strip()
                prev_tz = str(existing_doc.get("timezone") or "").strip()
                if prev_time != rule.trigger_time or prev_tz != rule.timezone:
                    try:
                        self.state_store.write_text_cas(
                            key, payload, if_generation_match=existing_gen
                        )
                        return True
                    except PreconditionFailedError:
                        return False
            return False

    async def claim_video_file(
        self,
        candidate: DiscoveredVideoCandidate,
        *,
        rule: ScheduledAuditRule,
        batch_id: str,
        provisional_job_id: str,
    ) -> bool:
        """Attempts to claim `candidate` in the 3-state file ledger (`DISPATCHED` / `COMPLETED` / `FAILED`).

        - If no ledger exists: creates with `if_generation_match=0` (`DISPATCHED`) -> `True`.
        - If `COMPLETED`: skips -> `False`.
        - If `FAILED`: re-claims with `if_generation_match=gen` -> `True`.
        - If `DISPATCHED`: inspects the referenced `AuditJob`:
          - If `DONE`: upgrades ledger to `COMPLETED` and returns `False`.
          - If `FAILED` or missing or stale (`> 24h`): re-claims with `if_generation_match=gen` -> `True`.
          - Otherwise (actively running): returns `False`.
        """
        key = self.state_store.file_ledger_key(candidate.file_id)
        raw, gen = await asyncio.to_thread(self.state_store.read_text_with_generation, key)
        now_ts = time.time()

        if raw is None:
            entry = FileLedgerEntry(
                file_id=candidate.file_id,
                filename=candidate.filename,
                leaf_folder_id=candidate.leaf_folder_id,
                rule_id=rule.rule_id,
                batch_id=batch_id,
                job_id=provisional_job_id,
                user_id=rule.supervisor_email,
                state=FileLedgerState.DISPATCHED,
                created_at=now_ts,
                updated_at=now_ts,
            )
            try:
                await asyncio.to_thread(
                    self.state_store.write_text_cas,
                    key,
                    entry.model_dump_json(indent=2),
                    if_generation_match=0,
                )
                return True
            except PreconditionFailedError:
                return False

        try:
            existing = FileLedgerEntry.model_validate_json(raw)
        except Exception:
            existing = None

        if existing is not None:
            if existing.state in (
                FileLedgerState.COMPLETED,
                FileLedgerState.REJECTED_LOW_RES,
            ):
                return False
            if existing.state == FileLedgerState.DISPATCHED:
                prev_job = await self.service.jobs.get(existing.user_id, existing.job_id)
                if prev_job is not None:
                    if prev_job.state == JobState.DONE:
                        completed_entry = existing.model_copy(
                            update={"state": FileLedgerState.COMPLETED, "updated_at": now_ts}
                        )
                        try:
                            await asyncio.to_thread(
                                self.state_store.write_text_cas,
                                key,
                                completed_entry.model_dump_json(indent=2),
                                if_generation_match=gen,
                            )
                        except PreconditionFailedError:
                            pass
                        return False
                    if (
                        prev_job.state == JobState.RUNNING
                        and (now_ts - prev_job.heartbeat_at) < 86400.0
                    ):
                        return False
                elif (now_ts - existing.updated_at) < 3600.0:
                    return False

        reclaimed = FileLedgerEntry(
            file_id=candidate.file_id,
            filename=candidate.filename,
            leaf_folder_id=candidate.leaf_folder_id,
            rule_id=rule.rule_id,
            batch_id=batch_id,
            job_id=provisional_job_id,
            user_id=rule.supervisor_email,
            state=FileLedgerState.DISPATCHED,
            created_at=existing.created_at if existing else now_ts,
            updated_at=now_ts,
        )
        try:
            await asyncio.to_thread(
                self.state_store.write_text_cas,
                key,
                reclaimed.model_dump_json(indent=2),
                if_generation_match=gen,
            )
            return True
        except PreconditionFailedError:
            return False

    async def mark_file_ledger_terminal(
        self,
        file_id: str,
        *,
        job_id: str,
        succeeded: bool,
        error_message: str = "",
        terminal_state: Optional[FileLedgerState] = None,
    ) -> None:
        """Transitions a file's ledger entry to `COMPLETED`, `REJECTED_LOW_RES`, or `FAILED` when its store job finishes."""
        key = self.state_store.file_ledger_key(file_id)
        target_state = (
            terminal_state
            if terminal_state is not None
            else (FileLedgerState.COMPLETED if succeeded else FileLedgerState.FAILED)
        )
        for _ in range(3):
            raw, gen = await asyncio.to_thread(self.state_store.read_text_with_generation, key)
            if raw is None:
                return
            try:
                entry = FileLedgerEntry.model_validate_json(raw)
            except Exception:
                return
            if (
                entry.job_id
                and entry.job_id != job_id
                and entry.state in (FileLedgerState.COMPLETED, FileLedgerState.REJECTED_LOW_RES)
            ):
                return
            updated = entry.model_copy(
                update={
                    "job_id": job_id,
                    "state": target_state,
                    "updated_at": time.time(),
                    "error_message": (
                        ""
                        if target_state == FileLedgerState.COMPLETED
                        else (error_message or "")[:240]
                    ),
                }
            )
            try:
                await asyncio.to_thread(
                    self.state_store.write_text_cas,
                    key,
                    updated.model_dump_json(indent=2),
                    if_generation_match=gen,
                )
                return
            except PreconditionFailedError:
                await asyncio.sleep(0.05)

    def _trigger_cloud_run_job_sync(self, job: AuditJob) -> bool:
        """Calls Cloud Run Admin API v2 `jobs/{job_name}:run` with container env overrides."""
        job_name = (config.cloud_run_job_name or "").strip()
        project = (config.gcp_project or "").strip()
        region = (config.gcp_location or "").strip()
        if not job_name or not project or not region:
            return False

        import google.auth
        import google.auth.transport.requests

        creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        if not creds.valid:
            creds.refresh(google.auth.transport.requests.Request())

        url = (
            f"https://run.googleapis.com/v2/projects/{urllib.parse.quote(project, safe='')}"
            f"/locations/{urllib.parse.quote(region, safe='')}"
            f"/jobs/{urllib.parse.quote(job_name, safe='')}:run"
        )
        body = {
            "overrides": {
                "containerOverrides": [
                    {
                        "env": [
                            {"name": "CCTV_AUDIT_JOB_ID", "value": job.job_id},
                            {"name": "CCTV_AUDIT_USER_ID", "value": job.user_id},
                            {"name": "CCTV_AUDIT_BATCH_ID", "value": job.batch_id},
                        ]
                    }
                ]
            }
        }
        data = json.dumps(body).encode("utf-8")
        for attempt in range(1, 4):
            req = urllib.request.Request(
                url,
                data=data,
                method="POST",
                headers={
                    "Authorization": f"Bearer {creds.token}",
                    "Content-Type": "application/json",
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=20.0) as resp:
                    if 200 <= resp.status < 300:
                        logger.info(
                            "Triggered Cloud Run Job %s for AuditJob %s (batch=%s)",
                            job_name,
                            job.job_id,
                            job.batch_id,
                        )
                        return True
            except urllib.error.HTTPError as exc:
                if exc.code in (429, 502, 503, 504) and attempt < 3:
                    time.sleep(1.0 * attempt)
                    continue
                logger.error(
                    "Cloud Run Job %s :run failed for job %s (HTTP %s): %s",
                    job_name,
                    job.job_id,
                    exc.code,
                    exc.read().decode("utf-8", "replace")[:300],
                )
                return False
            except Exception as exc:
                logger.error("Cloud Run Job %s :run error for job %s: %s", job_name, job.job_id, exc)
                return False
        return False

    async def dispatch_store_job(self, job: AuditJob) -> None:
        """Dispatches a store `AuditJob` to Cloud Run Jobs (24h) or local async execution fallback."""
        if self._cloud_run_job_dispatcher is not None:
            res = self._cloud_run_job_dispatcher(job)
            if asyncio.iscoroutine(res):
                await res
            return

        if config.cloud_run_job_name and not os.environ.get("PYTEST_CURRENT_TEST"):
            dispatched = await asyncio.to_thread(self._trigger_cloud_run_job_sync, job)
            if dispatched:
                return
            logger.warning(
                "Cloud Run Job dispatch returned False for job %s; falling back to in-process execution",
                job.job_id,
            )

        await self.execute_scheduled_store_job(
            job_id=job.job_id, user_id=job.user_id, batch_id=job.batch_id
        )

    async def execute_scheduled_store_job(
        self, *, job_id: str, user_id: str, batch_id: str = ""
    ) -> Optional[AuditJob]:
        """Executes one store's scheduled `AuditJob` to completion, updates 3-state file ledgers,
        and runs the CAS batch fan-in aggregator.
        """
        job = await self.service.jobs.get(user_id, job_id)
        if job is None:
            logger.error("Scheduled store job %s (user=%s) not found in JobStore", job_id, user_id)
            return None

        effective_batch_id = batch_id or job.batch_id
        prompt_cfg = await self.service.prompt_manager.load_active_config(
            config.effective_sop_source(job.folder_id)
        )
        running_job = job.model_copy(
            update={
                "state": JobState.RUNNING,
                "active_model_version": prompt_cfg.active_model_version,
                "active_prompt_version": prompt_cfg.active_prompt_version,
                "batch_id": effective_batch_id,
                "execution_backend": "cloud_run_job",
            }
        )
        running_job = await self.service.jobs.save(running_job)

        await self.service._run_detached_audit(running_job, prompt_cfg)
        finished_job = await self.service.jobs.get(user_id, job_id)
        if finished_job is None:
            return None

        # Update per-file 3-state ledgers based on completed checkpoints / job outcome
        videos = (
            finished_job.preflight_report.videos
            if finished_job.preflight_report and finished_job.preflight_report.videos
            else []
        )
        completed_file_ids = {
            ckpt.file_id for ckpt in (finished_job.completed_segments or {}).values()
        }
        for v_item in videos:
            v_ok = (
                finished_job.state == JobState.DONE
                or v_item.file_id in completed_file_ids
            )
            await self.mark_file_ledger_terminal(
                v_item.file_id,
                job_id=finished_job.job_id,
                succeeded=v_ok,
                error_message=finished_job.error_message or "",
            )

        if effective_batch_id:
            await self.finalize_store_in_batch(effective_batch_id, finished_job)
        return finished_job

    async def finalize_store_in_batch(self, batch_id: str, finished_job: AuditJob) -> bool:
        """CAS-updates `batches/{batch_id}.json` with `finished_job`'s outcome and, if this is the
        last store to reach a terminal state, atomically claims `notification_sent = True` and sends
        the single consolidated multi-store Google Chat summary card + Tab 2 status write-back.
        """
        b_key = self.state_store.batch_key(batch_id)
        claimed_for_notification = False
        final_batch: Optional[ScheduledBatchRecord] = None

        for _ in range(8):
            raw, gen = await asyncio.to_thread(self.state_store.read_text_with_generation, b_key)
            if raw is None:
                return False
            batch = ScheduledBatchRecord.model_validate_json(raw)
            task_entry = batch.store_tasks.get(finished_job.job_id)
            if task_entry is not None:
                cost_usd = round(
                    sum(
                        c.ledger_row.estimated_cost_usd
                        for c in (finished_job.completed_segments or {}).values()
                    ),
                    4,
                )
                resolved_err = (
                    finished_job.error_message
                    or (
                        finished_job.preflight_report.message_to_user[:300]
                        if finished_job.preflight_report is not None
                        and not finished_job.preflight_report.passed
                        else ""
                    )
                    or task_entry.error_message
                    or ""
                )
                task_entry = task_entry.model_copy(
                    update={
                        "state": finished_job.state.value.upper(),
                        "violations_found": finished_job.violations_found,
                        "total_tokens": finished_job.total_tokens_used,
                        "cost_usd": cost_usd,
                        "report_sheet_url": finished_job.report_sheet_url or "",
                        "error_message": resolved_err[:300],
                    }
                )
                batch.store_tasks[finished_job.job_id] = task_entry

            all_terminal = all(
                t.state.upper() in ("DONE", "FAILED", "REJECTED")
                for t in batch.store_tasks.values()
            )
            should_claim_notify = all_terminal and not batch.notification_sent
            if should_claim_notify:
                batch.notification_sent = True
                batch.status_written = True
            batch.updated_at = time.time()

            try:
                await asyncio.to_thread(
                    self.state_store.write_text_cas,
                    b_key,
                    batch.model_dump_json(indent=2),
                    if_generation_match=gen,
                )
                claimed_for_notification = should_claim_notify
                final_batch = batch
                break
            except PreconditionFailedError:
                await asyncio.sleep(0.05)

        if not claimed_for_notification or final_batch is None:
            return False

        summaries = [
            t.model_dump()
            for t in sorted(
                final_batch.store_tasks.values(),
                key=lambda x: (x.folder_name, x.job_id),
            )
        ]
        await self.service.reporter.send_batch_summary_notification(
            batch_id=final_batch.batch_id,
            rule_id=final_batch.rule_id,
            rule_name=final_batch.rule_name,
            supervisor_email=final_batch.supervisor_email,
            root_folder_url=final_batch.root_folder_url,
            store_summaries=summaries,
        )

        total_stores = len(summaries)
        ok_stores = sum(1 for s in summaries if s["state"] == "DONE")
        total_videos = sum(int(s["video_count"]) for s in summaries)
        total_viol = sum(int(s["violations_found"]) for s in summaries)
        total_tok = sum(int(s["total_tokens"]) for s in summaries)
        skipped_low_res_cnt = sum(
            len(s.get("skipped_low_res_videos") or [])
            for s in summaries
            if s["state"] == "DONE"
        )
        failed_notes = [
            f"[{s['folder_name']}] {(s.get('error_message') or '预检/执行未通过').replace(chr(10), ' ')[:140]}"
            for s in summaries
            if s["state"] != "DONE"
        ]
        prefix_icon = "✅" if ok_stores == total_stores else "⚠️"
        status_msg = (
            f"{prefix_icon} {final_batch.local_date} 批次 {final_batch.batch_id} 完成："
            f"门店 {ok_stores}/{total_stores} 家（共 {total_videos} 支视频），"
            f"检出 {total_viol} 项，总计 {total_tok:,} Tokens"
        )
        if skipped_low_res_cnt > 0:
            status_msg += f"（另已自动跳过 {skipped_low_res_cnt} 支 <720P 低清视频）"
        if failed_notes:
            status_msg += f"；未通过原因：{'；'.join(failed_notes)}"
        await self.service.prompt_manager.update_scheduled_rule_status(
            final_batch.row_index, status_msg
        )
        return True

    def try_acquire_eval_lock(self, eval_id: str, eval_task_id: str, now_ts: float) -> bool:
        """Atomically acquires `schedules/eval_locks/{safe_eval_id}.json` (`if_generation_match=0`).

        If an older lock exists whose `state` is terminal (`DONE` / `FAILED`) or stale (`> 24h`),
        CAS-updates it (`if_generation_match=existing_gen`) to claim the new run.
        """
        key = self.state_store.eval_lock_key(eval_id)
        payload = json.dumps(
            {
                "eval_id": eval_id,
                "eval_task_id": eval_task_id,
                "state": "RUNNING",
                "acquired_at": now_ts,
                "updated_at": now_ts,
            },
            ensure_ascii=False,
        )
        try:
            self.state_store.write_text_cas(key, payload, if_generation_match=0)
            return True
        except PreconditionFailedError:
            raw_existing, existing_gen = self.state_store.read_text_with_generation(key)
            if raw_existing and existing_gen > 0:
                try:
                    existing_doc = json.loads(raw_existing)
                except Exception:
                    existing_doc = {}
                prev_state = str(existing_doc.get("state") or "").upper()
                prev_ts = float(existing_doc.get("updated_at") or existing_doc.get("acquired_at") or 0.0)
                if prev_state in ("DONE", "FAILED", "IDLE") or (now_ts - prev_ts) >= 86400.0:
                    try:
                        self.state_store.write_text_cas(
                            key, payload, if_generation_match=existing_gen
                        )
                        return True
                    except PreconditionFailedError:
                        return False
            return False

    def finish_eval_lock(self, eval_id: str, eval_task_id: str, state: str) -> None:
        """Marks `schedules/eval_locks/{safe_eval_id}.json` as `DONE` or `FAILED` so future `START_EVAL` runs can acquire it."""
        key = self.state_store.eval_lock_key(eval_id)
        for _ in range(3):
            raw_existing, existing_gen = self.state_store.read_text_with_generation(key)
            if raw_existing is None or existing_gen <= 0:
                return
            try:
                doc = json.loads(raw_existing)
            except Exception:
                doc = {"eval_id": eval_id}
            if doc.get("eval_task_id") and doc.get("eval_task_id") != eval_task_id:
                return
            doc["state"] = state
            doc["updated_at"] = time.time()
            try:
                self.state_store.write_text_cas(
                    key, json.dumps(doc, ensure_ascii=False), if_generation_match=existing_gen
                )
                return
            except PreconditionFailedError:
                time.sleep(0.05)

    async def _preflight_scheduled_eval_rule(self, rule: ScheduledEvalRule) -> Tuple[bool, str, str]:
        """Runs a fast 0-Token preflight check on a `Tab3_黄金集评测工作台` row before dispatching a Cloud Run Job.

        Returns `(passed, error_message, effective_results_folder_id)`.
        """
        effective_results_folder_id = (
            str(rule.results_folder_id or "").strip()
            or os.environ.get("EVAL_RESULTS_FOLDER_ID", "").strip()
        )
        if not effective_results_folder_id:
            return (
                False,
                "❌ 评测预检未通过：未配置 Results_Folder_URL（且系统环境变量 EVAL_RESULTS_FOLDER_ID 为空），无法保存 5-Tab 测评报告 Sheet（0 Token 消耗）",
                "",
            )

        # 1. Verify Prompt_Tab_Name exists and is parseable + Model_Version is valid
        load_tab_fn = getattr(self.service.prompt_manager, "load_config_for_tab", None)
        if callable(load_tab_fn):
            try:
                await load_tab_fn(
                    prompt_tab_name=rule.prompt_tab_name,
                    model_version=rule.model_version,
                )
            except Exception as exc:
                return (
                    False,
                    f"❌ 评测预检未通过：SOP 标签页 '{rule.prompt_tab_name}' 或模型 '{rule.model_version}' 校验失败（{str(exc)[:180]}）（0 Token 消耗）",
                    effective_results_folder_id,
                )

        # 2. If Drive service factory is configured or running in production, verify Golden_Videos_Folder_URL & Results_Folder_URL
        if self._drive_service_factory is not None:
            try:
                drive_svc = await asyncio.to_thread(
                    self._get_drive_service, rule.supervisor_email
                )
            except Exception as exc:
                return (
                    False,
                    f"❌ 评测预检未通过：无法初始化 Google Drive 访问客户端：{str(exc)[:160]}（0 Token 消耗）",
                    effective_results_folder_id,
                )
            if rule.golden_videos_folder_id:
                try:
                    await asyncio.to_thread(
                        lambda: drive_svc.files()
                        .get(
                            fileId=rule.golden_videos_folder_id,
                            fields="id,name,mimeType",
                            supportsAllDrives=True,
                        )
                        .execute()
                    )
                except Exception as exc:
                    return (
                        False,
                        f"❌ 评测预检未通过：无法访问黄金集视频目录 ({rule.golden_videos_folder_id})：{str(exc)[:160]}（0 Token 消耗）",
                        effective_results_folder_id,
                    )
            try:
                folder_meta = await asyncio.to_thread(
                    lambda: drive_svc.files()
                    .get(
                        fileId=effective_results_folder_id,
                        fields="id,name,capabilities(canAddChildren)",
                        supportsAllDrives=True,
                    )
                    .execute()
                )
                caps = folder_meta.get("capabilities") or {}
                if caps.get("canAddChildren") is False:
                    return (
                        False,
                        f"❌ 评测预检未通过：结果归档目录 ({effective_results_folder_id}) 缺少写入权限 (canAddChildren=False)（0 Token 消耗）",
                        effective_results_folder_id,
                    )
            except Exception as exc:
                return (
                    False,
                    f"❌ 评测预检未通过：无法访问结果归档目录 ({effective_results_folder_id})：{str(exc)[:160]}（0 Token 消耗）",
                    effective_results_folder_id,
                )

        return True, "", effective_results_folder_id

    def _trigger_cloud_run_eval_job_sync(self, eval_task_id: str) -> bool:
        """Calls Cloud Run Admin API v2 `jobs/{job_name}:run` with `CCTV_EVAL_TASK_ID` override."""
        job_name = (config.cloud_run_job_name or "").strip()
        project = (config.gcp_project or "").strip()
        region = (config.gcp_location or "").strip()
        if not job_name or not project or not region:
            return False

        import google.auth
        import google.auth.transport.requests

        creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        if not creds.valid:
            creds.refresh(google.auth.transport.requests.Request())

        url = (
            f"https://run.googleapis.com/v2/projects/{urllib.parse.quote(project, safe='')}"
            f"/locations/{urllib.parse.quote(region, safe='')}"
            f"/jobs/{urllib.parse.quote(job_name, safe='')}:run"
        )
        body = {
            "overrides": {
                "containerOverrides": [
                    {
                        "env": [
                            {"name": "CCTV_EVAL_TASK_ID", "value": eval_task_id},
                        ]
                    }
                ]
            }
        }
        data = json.dumps(body).encode("utf-8")
        for attempt in range(1, 4):
            req = urllib.request.Request(
                url,
                data=data,
                method="POST",
                headers={
                    "Authorization": f"Bearer {creds.token}",
                    "Content-Type": "application/json",
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=20.0) as resp:
                    if 200 <= resp.status < 300:
                        logger.info(
                            "Triggered Cloud Run Job %s for ScheduledEvalTask %s",
                            job_name,
                            eval_task_id,
                        )
                        return True
            except urllib.error.HTTPError as exc:
                if exc.code in (429, 502, 503, 504) and attempt < 3:
                    time.sleep(1.0 * attempt)
                    continue
                logger.error(
                    "Cloud Run Job %s :run failed for eval_task %s (HTTP %s): %s",
                    job_name,
                    eval_task_id,
                    exc.code,
                    exc.read().decode("utf-8", "replace")[:300],
                )
                return False
            except Exception as exc:
                logger.error(
                    "Cloud Run Job %s :run error for eval_task %s: %s",
                    job_name,
                    eval_task_id,
                    exc,
                )
                return False
        return False

    async def dispatch_eval_job(self, task_rec: ScheduledEvalTaskRecord) -> None:
        """Dispatches a `ScheduledEvalTaskRecord` to Cloud Run Jobs (24h) or in-process fallback."""
        if config.cloud_run_job_name and not os.environ.get("PYTEST_CURRENT_TEST"):
            dispatched = await asyncio.to_thread(
                self._trigger_cloud_run_eval_job_sync, task_rec.eval_task_id
            )
            if dispatched:
                return
            logger.warning(
                "Cloud Run Job dispatch returned False for eval_task %s; falling back to in-process execution",
                task_rec.eval_task_id,
            )

        await self.execute_scheduled_eval_task(task_rec.eval_task_id)

    async def execute_scheduled_eval_task(
        self, eval_task_id: str
    ) -> Optional[ScheduledEvalTaskRecord]:
        """Executes a `ScheduledEvalTaskRecord` inside the 24h Cloud Run Job worker, writes the report
        link & metrics back to `Tab3_黄金集评测工作台`, and sends a Google Chat completion card.
        """
        t_key = self.state_store.eval_task_key(eval_task_id)
        raw, gen = await asyncio.to_thread(self.state_store.read_text_with_generation, t_key)
        if raw is None:
            logger.error("ScheduledEvalTaskRecord %s not found in GCS state store", eval_task_id)
            return None

        task_rec = ScheduledEvalTaskRecord.model_validate_json(raw)
        try:
            if self._eval_runner is not None:
                res = self._eval_runner(task_rec)
                summary = await res if asyncio.iscoroutine(res) else res
            else:
                from eval.run_gcp_round import run_sheet_triggered_eval

                summary = await run_sheet_triggered_eval(
                    eval_id=task_rec.eval_id,
                    eval_name=task_rec.eval_name,
                    prompt_tab_name=task_rec.prompt_tab_name,
                    model_version=task_rec.model_version,
                    golden_source_id=task_rec.golden_source_id,
                    golden_videos_folder_id=task_rec.golden_videos_folder_id,
                    results_folder_id=task_rec.results_folder_id,
                    supervisor_email=task_rec.supervisor_email,
                    eval_task_id=task_rec.eval_task_id,
                )

            sheet_url = str(summary.get("spreadsheet_url") or "")
            overall_pct = float(summary.get("overall_recall", 0.0)) * 100.0
            confirmed_pct = float(
                summary.get("confirmed_only_recall", summary.get("overall_recall", 0.0))
            ) * 100.0
            fpc = float(summary.get("findings_per_clip", 0.0))
            cost_usd = float(summary.get("total_cost_usd", 0.0))
            guardrails_ok = bool(summary.get("guardrails_passed", False))
            guard_tag = "护栏通过" if guardrails_ok else "触发护栏"
            # Effective video mode (`agentic` / `static`): a model without agentic video serving
            # runs static, and a Tab3 model comparison must show that next to the recall.
            media_mode = str(summary.get("media_mode") or "").strip()
            report_msg = (
                f"✅ 评测完成 ({task_rec.eval_task_id})：总体召回率 {overall_pct:.1f}% "
                f"(仅 CONFIRMED {confirmed_pct:.1f}%) | "
                f"密度 {fpc:.2f} 条/段 ({guard_tag}) | 成本 ${cost_usd:.2f}"
            )
            if media_mode:
                report_msg += f" | 视频模式 {media_mode}"
            if sheet_url:
                report_msg += f" | 报告：{sheet_url}"
            elif summary.get("sheet_error"):
                report_msg += f" | ⚠️ 报告表生成提示：{str(summary['sheet_error'])[:120]}"

            finished_rec = task_rec.model_copy(
                update={
                    "state": "DONE",
                    "updated_at": time.time(),
                    "report_sheet_url": sheet_url,
                    "summary_text": report_msg,
                    "error_message": "",
                }
            )
            await asyncio.to_thread(
                self.state_store.write_text_cas,
                t_key,
                finished_rec.model_dump_json(indent=2),
                if_generation_match=gen,
            )
            await asyncio.to_thread(
                self.finish_eval_lock, task_rec.eval_id, task_rec.eval_task_id, "DONE"
            )
            update_eval_fn = getattr(
                self.service.prompt_manager, "update_scheduled_eval_row", None
            )
            if callable(update_eval_fn):
                await update_eval_fn(
                    task_rec.row_index,
                    trigger_action="DONE",
                    last_eval_report=report_msg,
                )
            send_eval_fn = getattr(
                self.service.reporter, "send_eval_completion_notification", None
            )
            if callable(send_eval_fn):
                await send_eval_fn(
                    eval_id=task_rec.eval_id,
                    eval_name=task_rec.eval_name,
                    run_id=task_rec.eval_task_id,
                    prompt_tab_name=task_rec.prompt_tab_name,
                    model_version=task_rec.model_version,
                    supervisor_email=task_rec.supervisor_email,
                    status="DONE",
                    summary=summary,
                )
            return finished_rec
        except Exception as exc:
            err_str = str(exc).replace("\n", " ")[:240]
            fail_msg = f"❌ 评测执行异常 ({task_rec.eval_task_id})：{err_str}"
            logger.error("Scheduled evaluation %s failed: %s", eval_task_id, exc)
            failed_rec = task_rec.model_copy(
                update={
                    "state": "FAILED",
                    "updated_at": time.time(),
                    "error_message": err_str,
                    "summary_text": fail_msg,
                }
            )
            try:
                _, latest_gen = await asyncio.to_thread(
                    self.state_store.read_text_with_generation, t_key
                )
                await asyncio.to_thread(
                    self.state_store.write_text_cas,
                    t_key,
                    failed_rec.model_dump_json(indent=2),
                    if_generation_match=latest_gen if latest_gen > 0 else None,
                )
            except Exception:
                pass
            await asyncio.to_thread(
                self.finish_eval_lock, task_rec.eval_id, task_rec.eval_task_id, "FAILED"
            )
            update_eval_fn = getattr(
                self.service.prompt_manager, "update_scheduled_eval_row", None
            )
            if callable(update_eval_fn):
                await update_eval_fn(
                    task_rec.row_index,
                    trigger_action="FAILED",
                    last_eval_report=fail_msg,
                )
            send_eval_fn = getattr(
                self.service.reporter, "send_eval_completion_notification", None
            )
            if callable(send_eval_fn):
                await send_eval_fn(
                    eval_id=task_rec.eval_id,
                    eval_name=task_rec.eval_name,
                    run_id=task_rec.eval_task_id,
                    prompt_tab_name=task_rec.prompt_tab_name,
                    model_version=task_rec.model_version,
                    supervisor_email=task_rec.supervisor_email,
                    status="FAILED",
                    error_message=err_str,
                )
            return failed_rec

    async def _process_scheduled_eval_rows(self, effective_now: datetime) -> Dict[str, Any]:
        """Scans `Tab3_黄金集评测工作台` for rows with `Trigger_Action == 'START_EVAL'`, runs fast preflight,
        acquires the atomic eval lock, transitions `START_EVAL -> RUNNING`, and dispatches the Cloud Run Job.
        """
        load_eval_fn = getattr(self.service.prompt_manager, "load_scheduled_eval_rules", None)
        if not callable(load_eval_fn):
            return {"triggered_evals": [], "invalid_eval_rows": []}

        update_eval_fn = getattr(self.service.prompt_manager, "update_scheduled_eval_row", None)
        eval_rules = await load_eval_fn()
        eval_errors = list(
            getattr(self.service.prompt_manager, "last_scheduled_eval_errors", []) or []
        )
        invalid_eval_rows: List[int] = []
        for err in eval_errors:
            invalid_eval_rows.append(err.row_index)
            if callable(update_eval_fn) and err.previous_report != err.error_message:
                await update_eval_fn(
                    err.row_index,
                    trigger_action="FAILED",
                    last_eval_report=err.error_message,
                )

        triggered_evals: List[Dict[str, Any]] = []
        for rule in eval_rules:
            if rule.trigger_action != "START_EVAL":
                continue

            passed, preflight_err, effective_folder_id = await self._preflight_scheduled_eval_rule(
                rule
            )
            if not passed:
                if callable(update_eval_fn):
                    await update_eval_fn(
                        rule.row_index,
                        trigger_action="FAILED",
                        last_eval_report=preflight_err,
                    )
                triggered_evals.append(
                    {
                        "eval_id": rule.eval_id,
                        "eval_task_id": None,
                        "status": "preflight_failed",
                        "error": preflight_err,
                    }
                )
                continue

            eval_task_id = (
                f"eval_{_safe_slug(rule.eval_id, 16)}_"
                f"{effective_now.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
            )
            acquired = await asyncio.to_thread(
                self.try_acquire_eval_lock,
                rule.eval_id,
                eval_task_id,
                effective_now.timestamp(),
            )
            if not acquired:
                continue

            task_rec = ScheduledEvalTaskRecord(
                eval_task_id=eval_task_id,
                eval_id=rule.eval_id,
                eval_name=rule.eval_name,
                row_index=rule.row_index,
                prompt_tab_name=rule.prompt_tab_name,
                model_version=rule.model_version,
                golden_videos_folder_url=rule.golden_videos_folder_url,
                golden_videos_folder_id=rule.golden_videos_folder_id,
                golden_sheet_url=rule.golden_sheet_url,
                golden_source_id=rule.golden_source_id,
                results_folder_url=rule.results_folder_url,
                results_folder_id=effective_folder_id,
                supervisor_email=rule.supervisor_email,
                state="RUNNING",
            )
            await asyncio.to_thread(
                self.state_store.write_text_cas,
                self.state_store.eval_task_key(eval_task_id),
                task_rec.model_dump_json(indent=2),
                if_generation_match=0,
            )
            if callable(update_eval_fn):
                await update_eval_fn(
                    rule.row_index,
                    trigger_action="RUNNING",
                    last_eval_report=(
                        f"⏳ 正在后台执行黄金集评测 ({eval_task_id})："
                        f"SOP={rule.prompt_tab_name} | 模型={rule.model_version}"
                    ),
                )
            send_eval_start_fn = getattr(
                self.service.reporter, "send_eval_start_notification", None
            )
            if callable(send_eval_start_fn):
                await send_eval_start_fn(
                    eval_id=rule.eval_id,
                    eval_name=rule.eval_name,
                    run_id=eval_task_id,
                    prompt_tab_name=rule.prompt_tab_name,
                    model_version=rule.model_version,
                    supervisor_email=rule.supervisor_email,
                    golden_videos_folder_url=rule.golden_videos_folder_url,
                    human_labels_sheet_url=rule.human_labels_sheet_url or rule.golden_sheet_url,
                    results_folder_url=rule.results_folder_url,
                )
            await self.dispatch_eval_job(task_rec)
            triggered_evals.append(
                {
                    "eval_id": rule.eval_id,
                    "eval_task_id": eval_task_id,
                    "status": "dispatched",
                }
            )

        return {
            "triggered_evals": triggered_evals,
            "invalid_eval_rows": invalid_eval_rows,
        }

    async def run_scheduled_tick(
        self, *, now_utc: Optional[datetime] = None
    ) -> Dict[str, Any]:
        """Executes one scheduled tick (triggered every 15m by Cloud Scheduler `POST /internal/jobs/scheduled-tick`)."""
        effective_now = now_utc if now_utc is not None else datetime.now(timezone.utc)
        if effective_now.tzinfo is None:
            effective_now = effective_now.replace(tzinfo=timezone.utc)

        ensure_fn = getattr(
            self.service.prompt_manager, "ensure_master_sheet_validations", None
        )
        if callable(ensure_fn):
            try:
                await ensure_fn()
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("Non-fatal error ensuring master sheet validations: %s", exc)

        eval_tick_res = await self._process_scheduled_eval_rows(effective_now)
        triggered_evals = eval_tick_res["triggered_evals"]
        invalid_eval_rows = eval_tick_res["invalid_eval_rows"]

        all_rules = await self.service.prompt_manager.load_scheduled_rules()
        row_errors = list(
            getattr(self.service.prompt_manager, "last_scheduled_rule_errors", []) or []
        )

        invalid_rows: List[int] = []
        for err in row_errors:
            invalid_rows.append(err.row_index)
            if err.previous_status != err.error_message:
                await self.service.prompt_manager.update_scheduled_rule_status(
                    err.row_index, err.error_message
                )

        if not all_rules:
            return {
                "status": "triggered" if triggered_evals else "no_rules",
                "evaluated_rules": 0,
                "invalid_rows": invalid_rows,
                "triggered_batches": [],
                "triggered_evals": triggered_evals,
                "invalid_eval_rows": invalid_eval_rows,
            }

        canonical_rules, shadowed_pairs = resolve_canonical_enabled_rules(all_rules)

        # Write tie-breaker overlap notice for shadowed duplicate rules that are due
        shadowed_notices: List[str] = []
        for dup_rule, winner_rule in shadowed_pairs:
            due, _ = is_rule_due(dup_rule, effective_now)
            if due:
                msg = (
                    f"⚠️ 配置重叠，已由第 {winner_rule.row_index} 行规则 "
                    f"[{winner_rule.rule_id}] 代理执行，未重复计费"
                )
                await self.service.prompt_manager.update_scheduled_rule_status(
                    dup_rule.row_index, msg
                )
                shadowed_notices.append(dup_rule.rule_id)

        due_rules_with_date: List[Tuple[ScheduledAuditRule, str]] = []
        for rule in canonical_rules:
            due, local_date = is_rule_due(rule, effective_now)
            if due:
                if self.try_acquire_daily_rule_lock(rule, local_date, effective_now):
                    due_rules_with_date.append((rule, local_date))

        if not due_rules_with_date:
            return {
                "status": "triggered" if triggered_evals else "idle",
                "evaluated_rules": len(canonical_rules),
                "invalid_rows": invalid_rows,
                "shadowed_rules": shadowed_notices,
                "triggered_batches": [],
                "triggered_evals": triggered_evals,
                "invalid_eval_rows": invalid_eval_rows,
            }

        # Group due Drive rules by supervisor_email to run bottom-up discovery once per identity,
        # while passing ALL canonical_enabled_rules so the Time-Agnostic Closest-Ancestor Carve-Out
        # respects child rules even when their Trigger_Time has not arrived yet!
        routed_by_rule: Dict[str, List[DiscoveredVideoCandidate]] = {}
        drive_due_rules = [
            r for r, _ in due_rules_with_date if not is_gcs_target(r.root_folder_id)
        ]
        if drive_due_rules:
            by_supervisor: Dict[str, List[ScheduledAuditRule]] = {}
            for r in drive_due_rules:
                by_supervisor.setdefault(r.supervisor_email.lower(), []).append(r)
            for _, sup_rules in by_supervisor.items():
                max_lb = max(r.lookback_hours for r in sup_rules)
                drive_svc = await asyncio.to_thread(
                    self._get_drive_service, sup_rules[0].supervisor_email
                )
                discovered_map = await asyncio.to_thread(
                    discover_and_route_drive_videos_sync,
                    drive_svc,
                    canonical_enabled_rules=canonical_rules,
                    max_lookback_hours=max_lb,
                    now_utc=effective_now,
                )
                for r in sup_rules:
                    routed_by_rule[r.rule_id] = discovered_map.get(r.rule_id, [])

        gcs_due_rules = [
            r for r, _ in due_rules_with_date if is_gcs_target(r.root_folder_id)
        ]
        if gcs_due_rules:
            from .gcs_gateway import GcsStorageGateway

            gw = GcsStorageGateway()
            gcs_map = await asyncio.to_thread(
                discover_and_route_gcs_videos_sync,
                gw,
                canonical_enabled_rules=canonical_rules,
                now_utc=effective_now,
            )
            for r in gcs_due_rules:
                routed_by_rule[r.rule_id] = gcs_map.get(r.rule_id, [])

        triggered_batches: List[Dict[str, Any]] = []
        for rule, local_date in due_rules_with_date:
            candidates = routed_by_rule.get(rule.rule_id, [])
            batch_id = f"batch_{_safe_slug(rule.rule_id, 16)}_{local_date.replace('-', '')}_{uuid.uuid4().hex[:6]}"

            # Group candidates by leaf_folder_id and claim each video in the 3-state file ledger
            by_leaf: Dict[str, List[DiscoveredVideoCandidate]] = {}
            for cand in candidates:
                by_leaf.setdefault(cand.leaf_folder_id, []).append(cand)

            claimed_by_leaf: Dict[str, Tuple[str, List[DiscoveredVideoCandidate]]] = {}
            for leaf_id, leaf_cands in by_leaf.items():
                claimed_list: List[DiscoveredVideoCandidate] = []
                for cand in leaf_cands:
                    ok = await self.claim_video_file(
                        cand,
                        rule=rule,
                        batch_id=batch_id,
                        provisional_job_id=f"pending_{batch_id}",
                    )
                    if ok:
                        claimed_list.append(cand)
                if claimed_list:
                    claimed_by_leaf[leaf_id] = (
                        claimed_list[0].leaf_folder_name,
                        claimed_list,
                    )

            if not claimed_by_leaf:
                days_val = getattr(rule, "lookback_days", round(rule.lookback_hours / 24.0, 2))
                days_str = (
                    str(int(days_val))
                    if float(days_val).is_integer()
                    else str(days_val)
                )
                no_vid_msg = (
                    f"✅ {local_date} {rule.trigger_time} 巡检完成："
                    f"过去 {days_str} 天（{int(rule.lookback_hours)} 小时）内无新增待检视频（0 Token 消耗）"
                )
                await self.service.prompt_manager.update_scheduled_rule_status(
                    rule.row_index, no_vid_msg
                )
                triggered_batches.append(
                    {
                        "rule_id": rule.rule_id,
                        "batch_id": None,
                        "stores_count": 0,
                        "videos_count": 0,
                        "status": "no_new_videos",
                    }
                )
                continue

            store_tasks: Dict[str, ScheduledStoreTask] = {}
            jobs_to_dispatch: List[AuditJob] = []

            for leaf_id, (leaf_name, leaf_cands) in claimed_by_leaf.items():
                valid_cands = [c for c in leaf_cands if min(c.width, c.height) >= 720]
                low_res_cands = [c for c in leaf_cands if min(c.width, c.height) < 720]
                skipped_low_res_labels = [
                    f"{c.filename} ({c.width}x{c.height})" for c in low_res_cands
                ]

                # Option A: If a folder contains both >=720P and <720P videos, skip only the <720P
                # videos (marking them REJECTED_LOW_RES in the file ledger so they are not re-claimed
                # on subsequent days) and audit the remaining >=720P videos normally.
                cands_for_preflight = valid_cands if valid_cands else low_res_cands
                preloaded = [
                    VideoMetadataItem(
                        file_id=c.file_id,
                        filename=c.filename,
                        width=c.width,
                        height=c.height,
                        duration_sec=c.duration_sec,
                        mime_type=c.mime_type,
                        size_bytes=c.size_bytes,
                    )
                    for c in cands_for_preflight
                ]
                job = await self.service.preflight(
                    user_id=rule.supervisor_email,
                    drive_url=leaf_id,
                    session_id=f"sched_{batch_id}_{_safe_slug(leaf_id, 12)}",
                    preloaded_items=preloaded,
                )

                for low_c in low_res_cands:
                    await self.mark_file_ledger_terminal(
                        low_c.file_id,
                        job_id=job.job_id,
                        succeeded=False,
                        error_message=(
                            f"视频分辨率 {low_c.width}x{low_c.height} 低于 720P 门禁 ({low_c.filename})"
                        ),
                        terminal_state=FileLedgerState.REJECTED_LOW_RES,
                    )

                if job.state != JobState.READY:
                    err_reason = (
                        (
                            job.preflight_report.message_to_user
                            if job.preflight_report
                            else job.error_message
                        )
                        or "预检未通过"
                    )
                    is_low_res_err = "REJECTED_LOW_RESOLUTION" in err_reason
                    for c in valid_cands:
                        await self.mark_file_ledger_terminal(
                            c.file_id,
                            job_id=job.job_id,
                            succeeded=False,
                            error_message=err_reason,
                            terminal_state=(
                                FileLedgerState.REJECTED_LOW_RES
                                if is_low_res_err
                                else FileLedgerState.FAILED
                            ),
                        )
                    store_tasks[job.job_id] = ScheduledStoreTask(
                        job_id=job.job_id,
                        user_id=rule.supervisor_email,
                        folder_id=leaf_id,
                        folder_name=leaf_name,
                        video_file_ids=[c.file_id for c in cands_for_preflight],
                        video_count=len(cands_for_preflight),
                        skipped_low_res_videos=skipped_low_res_labels,
                        state=job.state.value.upper(),
                        error_message=err_reason[:300],
                    )
                    continue

                job = job.model_copy(
                    update={
                        "batch_id": batch_id,
                        "rule_id": rule.rule_id,
                        "folder_name": leaf_name or job.folder_name,
                        "execution_backend": "cloud_run_job",
                        "state": JobState.RUNNING,
                    }
                )
                job = await self.service.jobs.save(job)

                # Bind the real job_id into each claimed >=720P video's ledger entry
                for c in valid_cands:
                    f_key = self.state_store.file_ledger_key(c.file_id)
                    raw_f, gen_f = await asyncio.to_thread(
                        self.state_store.read_text_with_generation, f_key
                    )
                    if raw_f is not None:
                        try:
                            entry_f = FileLedgerEntry.model_validate_json(raw_f).model_copy(
                                update={"job_id": job.job_id, "updated_at": time.time()}
                            )
                            await asyncio.to_thread(
                                self.state_store.write_text_cas,
                                f_key,
                                entry_f.model_dump_json(indent=2),
                                if_generation_match=gen_f,
                            )
                        except Exception:
                            pass

                store_tasks[job.job_id] = ScheduledStoreTask(
                    job_id=job.job_id,
                    user_id=rule.supervisor_email,
                    folder_id=leaf_id,
                    folder_name=leaf_name,
                    video_file_ids=[c.file_id for c in valid_cands],
                    video_count=len(valid_cands),
                    skipped_low_res_videos=skipped_low_res_labels,
                    state="RUNNING",
                )
                jobs_to_dispatch.append(job)

            batch_rec = ScheduledBatchRecord(
                batch_id=batch_id,
                rule_id=rule.rule_id,
                rule_name=rule.rule_name,
                row_index=rule.row_index,
                root_folder_id=rule.root_folder_id,
                root_folder_url=rule.root_folder_url,
                supervisor_email=rule.supervisor_email,
                local_date=local_date,
                store_tasks=store_tasks,
                start_notification_sent=True,
            )
            await asyncio.to_thread(
                self.state_store.write_text_cas,
                self.state_store.batch_key(batch_id),
                batch_rec.model_dump_json(indent=2),
                if_generation_match=0,
            )

            total_vids = sum(t.video_count for t in store_tasks.values())
            running_msg = (
                f"⏳ {local_date} {rule.trigger_time} 已触发批次 {batch_id}："
                f"共 {len(store_tasks)} 家门店 / {total_vids} 支新增视频正在后台稽核"
            )
            await self.service.prompt_manager.update_scheduled_rule_status(
                rule.row_index, running_msg
            )

            try:
                await self.service.reporter.send_batch_start_notification(
                    batch_id=batch_id,
                    rule_id=rule.rule_id,
                    rule_name=rule.rule_name,
                    supervisor_email=rule.supervisor_email,
                    root_folder_url=rule.root_folder_url,
                    store_summaries=[
                        t.model_dump()
                        for t in sorted(
                            store_tasks.values(),
                            key=lambda x: (x.folder_name, x.job_id),
                        )
                    ],
                )
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning(
                    "Non-fatal error sending batch start notification for %s: %s",
                    batch_id,
                    exc,
                )

            if jobs_to_dispatch:
                for job in jobs_to_dispatch:
                    await self.dispatch_store_job(job)
            else:
                # All stores failed preflight; finalize batch notification immediately
                first_failed_job_id = next(iter(store_tasks.keys()))
                failed_job = await self.service.jobs.get(
                    rule.supervisor_email, first_failed_job_id
                )
                if failed_job is not None:
                    await self.finalize_store_in_batch(batch_id, failed_job)

            triggered_batches.append(
                {
                    "rule_id": rule.rule_id,
                    "batch_id": batch_id,
                    "stores_count": len(store_tasks),
                    "videos_count": total_vids,
                    "status": "dispatched",
                }
            )

        return {
            "status": "triggered",
            "evaluated_rules": len(canonical_rules),
            "invalid_rows": invalid_rows,
            "shadowed_rules": shadowed_notices,
            "triggered_batches": triggered_batches,
            "triggered_evals": triggered_evals,
            "invalid_eval_rows": invalid_eval_rows,
        }
