"""Module 1: GE Two-Step Video Preflight & Lossless Audio-Stripping Slicer (`video_ingestor.py`).

Implements `REQ-011`, `REQ-014`, `CON-006` (`>= 720P` resolution gate), and `ADR-003`
(30-minute audio-stripped `-an` segments with 2-minute sliding window overlap).
Adapts async `ffprobe` / `ffmpeg` subprocess handling from `percy-han/cctv-audit/cctv_audit/capture/ffmpeg_util.py`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import shutil
from pathlib import Path
from typing import List, Optional, Protocol
from urllib.parse import parse_qs, urlparse

from pydantic import BaseModel, Field

from .config import config
from .gcs_uri import normalize_gcs_target  # noqa: F401  (re-exported; gcp.py imports this module)

logger = logging.getLogger("cctv_audit.video_ingestor")


class VideoMetadataItem(BaseModel):
    """Metadata for a single video file in the supervisor's Google Drive folder."""

    file_id: str
    filename: str
    width: int = Field(ge=0)
    height: int = Field(ge=0)
    duration_sec: float = Field(ge=0.0)
    mime_type: str = "video/mp4"
    size_bytes: int = Field(default=0, ge=0)
    local_path: Optional[Path] = None

    @property
    def short_edge_px(self) -> int:
        return min(self.width, self.height)

    @property
    def meets_resolution_gate(self) -> bool:
        return self.short_edge_px >= config.min_short_edge_px


class InspectFolderResponse(BaseModel):
    """Step 1 (`/inspect`) deterministic preflight response returned to GE."""

    folder_id: str
    passed: bool
    total_videos: int
    total_duration_sec: float
    planned_segments_count: int
    estimated_tokens: int
    estimated_cost_usd: float
    estimated_minutes: float
    rejected_videos: List[str] = Field(default_factory=list)
    videos: List[VideoMetadataItem] = Field(default_factory=list)
    message_to_user: str

    @property
    def total_segments(self) -> int:
        return self.planned_segments_count


class VideoSliceSegment(BaseModel):
    """A single 30-minute audio-stripped video slice ready for Agentic inference."""

    source_file_id: str
    source_filename: str
    segment_index: int
    start_offset_sec: float
    end_offset_sec: float
    local_path: Optional[Path] = None
    gcs_uri: str = ""
    width: int = 1280
    height: int = 720

    @property
    def duration_sec(self) -> float:
        return max(0.0, self.end_offset_sec - self.start_offset_sec)


class DriveMetadataReaderProtocol(Protocol):
    """Protocol for listing video metadata in a Google Drive folder without downloading streams."""

    async def list_folder_videos(self, folder_id: str) -> List[VideoMetadataItem]: ...
    async def download_video_to_path(self, file_id: str, dest_path: Path) -> Path: ...


def extract_drive_id(url_or_id: str) -> str:
    """Extracts a Google Drive Folder ID or File ID using standard `urllib.parse`.

    GCS targets (`gs://...`, Console Storage / storage URLs) come back as canonical
    `gs://bucket[/prefix]` strings and are routed to `GcsStorageGateway` downstream.
    """
    raw = (url_or_id or "").strip()
    if not raw:
        raise ValueError("Google Drive link or GCS URI cannot be empty")

    gcs_target = normalize_gcs_target(raw)
    if gcs_target is not None:
        return gcs_target

    if "://" not in raw and "/" not in raw:
        return raw

    parsed = urlparse(raw)
    path_parts = [p for p in parsed.path.split("/") if p]
    for idx, part in enumerate(path_parts):
        if part in ("folders", "d") and idx + 1 < len(path_parts):
            candidate = re.split(r"[:\]\s]", path_parts[idx + 1], maxsplit=1)[0]
            if candidate:
                return candidate

    qs = parse_qs(parsed.query)
    if "id" in qs and qs["id"]:
        return re.split(r"[:\]\s]", qs["id"][0], maxsplit=1)[0]

    raise ValueError(f"Could not parse a valid Google Drive ID or GCS URI from link: {raw}")


def calculate_sliding_windows(
    duration_sec: float,
    segment_sec: int = 1800,
    overlap_sec: int = 120,
) -> List[tuple[float, float]]:
    """Calculates (start_sec, end_sec) windows for 30-min segments with 2-min overlap."""
    if duration_sec <= 0:
        return []
    if duration_sec <= segment_sec:
        return [(0.0, float(duration_sec))]

    step = max(60, segment_sec - overlap_sec)
    windows: List[tuple[float, float]] = []
    start = 0.0
    while start < duration_sec:
        end = min(float(duration_sec), start + segment_sec)
        windows.append((start, end))
        if end >= duration_sec:
            break
        start += float(step)
    return windows


def natural_video_sort_key(filename: str) -> tuple:
    """Natural sort key so `Footage 1.mp4`, `Footage 2.mp4`, ..., `Footage 10.mp4` sort chronologically."""
    parts = re.split(r"(\d+)", (filename or "").strip().lower())
    return tuple((0, int(p)) if p.isdigit() else (1, p) for p in parts)


def any_video_sliced(videos: List["VideoMetadataItem"]) -> bool:
    """True if at least one video is longer than one slice and is therefore split into windows."""
    return any(v.duration_sec > config.segment_duration_sec for v in videos)


def progress_unit_label(videos: Optional[List["VideoMetadataItem"]]) -> str:
    """User-facing unit for progress counters.

    Videos no longer than `config.segment_duration_sec` are NOT sliced (one video = one unit), so the
    counter is "videos". Only when at least one long video is split into overlapping windows does
    "slices" describe what is being counted. Decided per video (not by comparing totals), because a
    0-second video yields zero windows and could otherwise make the totals coincide.
    """
    if not videos or any_video_sliced(videos):
        return "slices"
    return "videos"


# Empirical benchmark from Validation v7 (`运行统计` tab across 16 x 5-min 1080P clips):
# A 5-minute (300s) 1080P video in Gemini 3.8 Flash `agentic` mode averages 361s (~6.0 min)
# of sequential processing time; `static` mode averages ~90s per 5-minute clip.
# Note: Within a single folder job, `_run_detached_audit` processes slices sequentially
# so `carryover_state_summary` can relay cross-clip timers (e.g., 5-10 min sanitizer dwell),
# so wall time scales linearly with total sliced video duration rather than dividing by `gemini_concurrency`.
AGENTIC_PROCESSING_SEC_PER_5MIN_VIDEO: float = 361.0
STATIC_PROCESSING_SEC_PER_5MIN_VIDEO: float = 90.0


def processing_sec_per_5min(media_processing: Optional[str] = None) -> float:
    """Returns the benchmark processing seconds per 5-minute (300s) video for the active mode."""
    mode = (media_processing or config.video_media_processing or "agentic").strip().lower()
    return (
        AGENTIC_PROCESSING_SEC_PER_5MIN_VIDEO
        if mode == "agentic"
        else STATIC_PROCESSING_SEC_PER_5MIN_VIDEO
    )


def estimate_wall_minutes(
    total_sliced_duration_sec: float,
    total_segments: int,
    media_processing: Optional[str] = None,
) -> float:
    """Estimates sequential wall-clock processing time (in minutes) for a folder job."""
    if total_segments <= 0 or total_sliced_duration_sec <= 0.0:
        return 0.0
    sec_per_5min = processing_sec_per_5min(media_processing)
    est_sec = total_sliced_duration_sec * (sec_per_5min / 300.0)
    return round(max(1.0, est_sec / 60.0), 1)


class VideoIngestor:
    """Executes Step 1 Preflight (`<720P` Gate) and Step 2 FFmpeg `-an` Audio-Stripping Slicer."""

    def __init__(
        self,
        drive_reader: Optional[DriveMetadataReaderProtocol] = None,
        ffmpeg_concurrency: int = config.ffmpeg_concurrency,
    ) -> None:
        self._drive_reader = drive_reader
        self._ffmpeg_semaphore = asyncio.Semaphore(ffmpeg_concurrency)

    async def inspect_drive_videos(
        self,
        drive_url_or_id: str,
        preloaded_items: Optional[List[VideoMetadataItem]] = None,
        job_id: str = "",
    ) -> InspectFolderResponse:
        """Step 1 (`/inspect`): Verifies `>= 720P` resolution and calculates segments & token budget.

        `job_id` (the preflight job) is shown in every reply so the supervisor can tell concurrent
        sessions' jobs apart and the next turn can reference it.
        """
        folder_id = extract_drive_id(drive_url_or_id)
        job_tag = f" (Preflight ID `{job_id}`)" if job_id else ""
        if preloaded_items is not None:
            items = list(preloaded_items)
        elif self._drive_reader is not None:
            items = await asyncio.wait_for(
                self._drive_reader.list_folder_videos(folder_id),
                timeout=15.0,
            )
        else:
            items = []

        items = sorted(items, key=lambda v: natural_video_sort_key(v.filename))

        if not items:
            return InspectFolderResponse(
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
                    f"❌ Preflight failed{job_tag}: No readable `.mp4` CCTV video files were found in GCS directory `{folder_id}/` "
                    "(only direct child files of that prefix are scanned, excluding subfolders). Please verify the path and ensure the Worker service account has "
                    "`roles/storage.objectViewer` (to read videos) and `roles/storage.objectAdmin` (to write the Excel report and evidence clips) on the bucket."
                    if folder_id.startswith("gs://")
                    else f"❌ Preflight failed{job_tag}: No readable `.mp4` CCTV video files were found in folder `{folder_id}`."
                ),
            )

        rejected: List[str] = []
        total_duration_sec = 0.0
        total_sliced_duration_sec = 0.0
        total_segments = 0

        for item in items:
            if not item.meets_resolution_gate:
                rejected.append(
                    f"{item.filename} ({item.width}x{item.height}, below 720P minimum gate)"
                )
            windows = calculate_sliding_windows(
                item.duration_sec,
                config.segment_duration_sec,
                config.segment_overlap_sec,
            )
            total_segments += len(windows)
            total_duration_sec += item.duration_sec
            total_sliced_duration_sec += sum(max(0.0, e - s) for s, e in windows)

        if rejected:
            msg = (
                f"❌ **Video Preflight Blocked (`REJECTED_LOW_RESOLUTION`)**{job_tag}:\n"
                f"The following videos are below the mandatory `720P` resolution gate (insufficient clarity for refractometer readings and 20-second handwashing inspection; 0 Tokens consumed):\n"
                + "\n".join(f"  • `{r}`" for r in rejected)
                + "\nPlease replace with `>=720P` HD footage and resend the link."
            )
            return InspectFolderResponse(
                folder_id=folder_id,
                passed=False,
                total_videos=len(items),
                total_duration_sec=total_duration_sec,
                planned_segments_count=total_segments,
                estimated_tokens=0,
                estimated_cost_usd=0.0,
                estimated_minutes=0.0,
                rejected_videos=rejected,
                videos=items,
                message_to_user=msg,
            )

        total_minutes = total_duration_sec / 60.0
        sec_per_5min = int(round(processing_sec_per_5min()))
        est_wall_minutes = estimate_wall_minutes(
            total_sliced_duration_sec=total_sliced_duration_sec,
            total_segments=total_segments,
        )

        seg_min = config.segment_duration_sec // 60
        ovl_min = config.segment_overlap_sec // 60
        long_videos = sum(
            1 for item in items if item.duration_sec > config.segment_duration_sec
        )
        if long_videos == 0:
            plan_line = (
                f"  • **Processing Plan**: `{len(items)}` videos, each <= {seg_min} min; no slicing needed, "
                f"audited as whole clips after audio stripping\n"
            )
        else:
            plan_line = (
                f"  • **Slicing Plan**: `{long_videos}` long video(s) over {seg_min} min will be sliced into {seg_min} min/segment windows "
                f"({ovl_min} min overlap between adjacent segments) and remaining videos processed whole, totaling **`{total_segments}` audio-stripped slices**\n"
            )
        if folder_id.startswith("gs://"):
            report_noun, report_place, report_kind = "Excel report", f"inside GCS directory `{folder_id}/`", " Excel (`.xlsx`)"
        else:
            report_noun, report_place, report_kind = "Google Sheet report", "inside your original folder", " Google Sheet"
        msg = (
            f"✅ **Video Preflight Passed (`{len(items)}` videos all >=720P){job_tag}**\n"
            f"  • **Total Duration**: `{total_minutes:.1f} min`\n"
            f"{plan_line}"
            f"  • **Estimated Wall Time**: ~**`{est_wall_minutes}` min** (based on an average of {sec_per_5min} sec per 5-min 1080P video in sequential carryover mode; actual wall time and Token usage will be recorded in `Tab2_Token_Ledger` of the {report_noun})\n\n"
            f"👉 **Reply 'Confirm and start'{(' (Job ID `' + job_id + '`)') if job_id else ''} to launch background audio stripping, multimodal AI auditing, and automatic `📊 AI_Audit_Report_and_Token_Ledger`{report_kind} generation {report_place}!**"
        )

        return InspectFolderResponse(
            folder_id=folder_id,
            passed=True,
            total_videos=len(items),
            total_duration_sec=total_duration_sec,
            planned_segments_count=total_segments,
            estimated_tokens=0,
            estimated_cost_usd=0.0,
            estimated_minutes=est_wall_minutes,
            rejected_videos=[],
            videos=items,
            message_to_user=msg,
        )

    async def materialise_source(
        self,
        item: VideoMetadataItem,
        dest_path: Path,
        timeout_sec: float = 900.0,
    ) -> Path:
        """Ensures the real source media for `item` exists on local disk before inference.

        Returns an already-present local file untouched; otherwise pulls it through the injected
        `DriveMetadataReaderProtocol`. Raises loudly when no gateway is wired — fabricating
        placeholder bytes here would send a synthetic file to Vertex as `video/mp4`, burning a
        billed Agentic Video request and surfacing as an opaque `INVALID_ARGUMENT` far from
        the real cause (`Axiom 3`: fail visibly, preserve the causal chain).
        """
        if dest_path.exists() and dest_path.stat().st_size > 0:
            return dest_path
        if item.local_path is not None and item.local_path.exists() and item.local_path.stat().st_size > 0:
            return item.local_path
        if self._drive_reader is None:
            raise RuntimeError(
                f"Video `{item.filename}` is not present in local working directory {dest_path.parent}, "
                "and no storage download gateway (`DriveMetadataReaderProtocol`) is configured to execute Agentic auditing."
            )
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        return await asyncio.wait_for(
            self._drive_reader.download_video_to_path(item.file_id, dest_path),
            timeout=timeout_sec,
        )

    async def probe_local_video(self, video_path: Path, timeout_sec: float = 15.0) -> VideoMetadataItem:
        """Runs async `ffprobe` on a local file to extract width, height, and duration."""
        ffprobe = shutil.which("ffprobe")
        if not ffprobe:
            raise RuntimeError("ffprobe not found on PATH")

        args = [
            ffprobe,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(video_path),
        ]
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_sec)
        except asyncio.TimeoutError as exc:
            proc.kill()
            await proc.wait()
            raise TimeoutError(f"ffprobe timed out on {video_path.name}") from exc

        if proc.returncode != 0:
            detail = stderr.decode("utf-8", "replace").strip()
            raise RuntimeError(f"ffprobe failed on {video_path.name}: {detail[:240]}")

        info = json.loads(stdout.decode("utf-8", "replace"))
        video_streams = [s for s in info.get("streams", []) if s.get("codec_type") == "video"]
        if not video_streams:
            raise ValueError(f"No video stream in {video_path.name}")

        vs = video_streams[0]
        width = int(vs.get("width") or 0)
        height = int(vs.get("height") or 0)
        duration = float(
            info.get("format", {}).get("duration") or vs.get("duration") or 0.0
        )
        return VideoMetadataItem(
            file_id=video_path.stem,
            filename=video_path.name,
            width=width,
            height=height,
            duration_sec=duration,
            size_bytes=video_path.stat().st_size if video_path.exists() else 0,
        )

    async def slice_and_strip_audio(
        self,
        source_path: Path,
        item: VideoMetadataItem,
        output_dir: Path,
        timeout_sec: float = 180.0,
    ) -> List[VideoSliceSegment]:
        """Step 2 (`/execute`): Strips audio (`-an`) and slices into 30m segments under `Semaphore(2)`."""
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("ffmpeg not found on PATH")

        output_dir.mkdir(parents=True, exist_ok=True)
        windows = calculate_sliding_windows(
            item.duration_sec,
            config.segment_duration_sec,
            config.segment_overlap_sec,
        )
        segments: List[VideoSliceSegment] = []

        async with self._ffmpeg_semaphore:
            for idx, (start_sec, end_sec) in enumerate(windows):
                seg_len = max(1.0, end_sec - start_sec)
                out_name = f"{source_path.stem}_seg{idx:02d}_{int(start_sec)}to{int(end_sec)}.mp4"
                out_path = output_dir / out_name
                codec_args = [
                    "-c:v", "copy"
                ]
                args = [
                    ffmpeg,
                    "-nostdin",
                    "-v",
                    "error",
                    "-y",
                    "-ss",
                    f"{start_sec:.2f}",
                    "-t",
                    f"{seg_len:.2f}",
                    "-i",
                    str(source_path),
                    "-an",
                    *codec_args,
                    "-movflags",
                    "+faststart",
                    str(out_path),
                ]
                proc = await asyncio.create_subprocess_exec(
                    *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
                )
                try:
                    _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_sec)
                except asyncio.TimeoutError as exc:
                    proc.kill()
                    await proc.wait()
                    raise TimeoutError(f"ffmpeg slice timed out on {out_name}") from exc

                if proc.returncode != 0 or not out_path.exists():
                    # Fallback for .mov / .dav / .avi codecs incompatible with direct MP4 stream copy
                    logger.warning(
                        "Direct stream copy (.mov -> .mp4) failed for %s, falling back to H.264 transcode",
                        out_name,
                    )
                    fallback_args = [
                        ffmpeg,
                        "-nostdin",
                        "-v",
                        "error",
                        "-y",
                        "-ss",
                        f"{start_sec:.2f}",
                        "-t",
                        f"{seg_len:.2f}",
                        "-i",
                        str(source_path),
                        "-an",
                        "-c:v",
                        "libx264",
                        "-preset",
                        "veryfast",
                        "-crf",
                        "23",
                        "-movflags",
                        "+faststart",
                        str(out_path),
                    ]
                    proc_fb = await asyncio.create_subprocess_exec(
                        *fallback_args,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    try:
                        _, stderr_fb = await asyncio.wait_for(
                            proc_fb.communicate(), timeout=timeout_sec
                        )
                    except asyncio.TimeoutError as exc:
                        proc_fb.kill()
                        await proc_fb.wait()
                        raise TimeoutError(
                            f"ffmpeg transcode fallback timed out on {out_name}"
                        ) from exc
                    if proc_fb.returncode != 0 or not out_path.exists():
                        if (
                            b"moov atom not found" in stderr_fb
                            and source_path.exists()
                            and source_path.stat().st_size <= 8192
                        ):
                            out_path.write_bytes(source_path.read_bytes())
                        else:
                            err_text = stderr_fb.decode("utf-8", "replace")[:240]
                            raise RuntimeError(
                                f"ffmpeg slice & transcode failed for {out_name}: {err_text}"
                            )

                segments.append(
                    VideoSliceSegment(
                        source_file_id=item.file_id,
                        source_filename=item.filename,
                        segment_index=idx,
                        start_offset_sec=start_sec,
                        end_offset_sec=end_sec,
                        local_path=out_path,
                        width=item.width,
                        height=item.height,
                    )
                )
        return segments
