"""Module 2: Single-Model Agentic Video Auditor & 5-10s Evidence Clipper (`agentic_auditor.py`).

Adapts the battle-tested `Disposition` / `Status` / `Finding` schema and `_normalise_timestamp`
from `percy-han/cctv-audit/cctv_audit/analyzer/schema.py` & `video_analyzer.py`, upgraded for:
1. Video sampling per slice: agentic video understanding (`types.Part.media_processing =
   MediaProcessing.AGENTIC` on the video Part) by default when the Tab 0 `Active_Model_Version`
   is a Gemini 3.x model (`VIDEO_MEDIA_PROCESSING` defaults to `"agentic"`). A 3.x model that
   answers `400 ... Agentic video processing is not enabled for this model` is marked in
   `_AGENTIC_UNSUPPORTED_MODELS` and falls back to static sampling for the rest of the process
   (`effective_media_mode` reports which mode a model is really running in).
2. `gemini_semaphore = asyncio.Semaphore(5)` concurrency gate.
3. 100-word `carryover_state_summary` for seamless temporal stitching across 30-min segments.
4. `extract_evidence_clip()`: FFmpeg 5-10s MP4 video clipping for `FRAMEABLE = (CONFIRMED, SUSPECTED)`
   events (replacing single JPG frames so supervisors can hover-play 5-10s clips in Google Sheets).
5. Full `TokenLedgerRow` accounting (`prompt_token_count`, `thoughts_token_count`, `candidates_token_count`,
   `model_version_used`, `prompt_version_used`) aligned with SDD 1.4.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import time
import unicodedata
from enum import Enum
from pathlib import Path
from typing import List, Literal, Optional

from google.genai import types
from pydantic import BaseModel, Field, field_validator

from .config import config
from .gcp import AGENTIC_NOT_ENABLED_MARKER, gemini_timeout_ms_for_slice, generate_content_with_retry
from .gcs_gateway import build_source_video_url
from .prompt_manager import PromptModelConfig
from .video_ingestor import VideoSliceSegment

logger = logging.getLogger("cctv_audit.agentic_auditor")


class Status(str, Enum):
    VIOLATION = "VIOLATION"
    COMPLIANT = "COMPLIANT"
    CANNOT_DETERMINE = "CANNOT_DETERMINE"


class Disposition(str, Enum):
    """Five-tier disposition enum from `percy-han/cctv-audit` enforcing High-Recall SOP auditing."""

    CONFIRMED = "CONFIRMED"
    SUSPECTED = "SUSPECTED"
    UNVERIFIED = "UNVERIFIED"
    OUT_OF_SCOPE = "OUT_OF_SCOPE"
    COMPLIANT = "COMPLIANT"


STATUS_FOR_DISPOSITION = {
    Disposition.CONFIRMED: Status.VIOLATION,
    Disposition.SUSPECTED: Status.VIOLATION,
    Disposition.UNVERIFIED: Status.VIOLATION,
    Disposition.OUT_OF_SCOPE: Status.CANNOT_DETERMINE,
    Disposition.COMPLIANT: Status.COMPLIANT,
}

# Only CONFIRMED and SUSPECTED describe a visible moment worth cutting a 5-10s MP4 clip for.
FRAMEABLE = (Disposition.CONFIRMED, Disposition.SUSPECTED)


class Severity(str, Enum):
    RED_LINE = "RED_LINE"
    NORMAL = "NORMAL"
    NONE = "NONE"


def _normalize_timestamp_status(value: object) -> str:
    """Normalizes timestamp calibration status (English or legacy Chinese) to canonical English."""
    raw = str(value or "").strip()
    upper = raw.upper()
    if upper in ("OSD_EXACT", "EXACT") or "精确" in raw:
        return "OSD_EXACT"
    if upper in ("OSD_INTERPOLATED", "INTERPOLATED") or "插值" in raw or "推算" in raw:
        return "OSD_INTERPOLATED"
    if upper in ("UNVERIFIED_REL_TIME", "REL_TIME", "RELATIVE") or "相对时间" in raw or "无OSD" in raw or "未校验" in raw:
        return "UNVERIFIED_REL_TIME"
    return "OSD_EXACT" if raw else "UNVERIFIED_REL_TIME"


def _normalize_severity(value: object) -> str:
    """Normalizes severity label (English or legacy Chinese) to canonical English ('CRITICAL' / 'MEDIUM' / 'NONE')."""
    if isinstance(value, Severity):
        if value == Severity.RED_LINE:
            return "CRITICAL"
        if value == Severity.NORMAL:
            return "MEDIUM"
        return "NONE"
    raw = str(value or "").strip()
    upper = raw.upper()
    if upper in ("RED_LINE", "HIGH", "CRITICAL", "SEVERE") or raw in ("红线", "高危", "严重"):
        return "CRITICAL"
    if upper in ("NORMAL", "MEDIUM", "LOW", "WARNING", "MINOR", "COMMON_DEVIATION") or raw in ("一般", "普通", "中等", "中", "低"):
        return "MEDIUM"
    return "NONE"


def _normalize_confidence(value: object) -> str:
    """Normalizes confidence label/score (English or legacy Chinese) to canonical English ('HIGH' / 'MEDIUM' / 'LOW')."""
    if isinstance(value, (int, float)):
        if float(value) >= 0.8:
            return "HIGH"
        if float(value) >= 0.5:
            return "MEDIUM"
        return "LOW"
    raw = str(value or "").strip()
    upper = raw.upper()
    if upper in ("HIGH", "CONFIRMED") or raw in ("高", "高置信度"):
        return "HIGH"
    if upper in ("MEDIUM", "MED", "MODERATE", "SUSPECTED") or raw in ("中", "中等"):
        return "MEDIUM"
    if upper in ("LOW", "UNVERIFIED") or raw in ("低", "低置信度"):
        return "LOW"
    try:
        num = float(raw)
        return _normalize_confidence(num)
    except ValueError:
        return "MEDIUM"


class Finding(BaseModel):
    """Structured SOP finding with anti-hallucination timestamp & severity normalization."""

    rule_id: str = Field(description="SOP rule ID, e.g. A1, A2, B1")
    status: Status = Field(default=Status.COMPLIANT, description="Compliance verdict status")
    disposition: Disposition = Field(default=Disposition.COMPLIANT, description="Five-tier disposition label")
    confidence: float = Field(default=0.8, ge=0.0, le=1.0, description="Confidence score between 0.0 and 1.0")
    timestamp_in_clip: str = Field(default="00:00", description="Relative timestamp within clip (MM:SS or HH:MM:SS)")
    global_offset_sec: float = Field(default=0.0, description="Global offset in seconds within the full source video")
    segment_index: int = Field(default=0, description="Zero-based video segment slice index")
    on_screen_clock: str = Field(default="", description="On-screen display (OSD) clock watermark in video corner (usually top-left or top-right), e.g. 08:07:05")
    evidence: str = Field(default="", description="Objective visual factual description in English")
    severity: Severity = Field(default=Severity.NONE, description="Severity tier")
    evidence_clip_local_path: Optional[str] = Field(default=None, description="Local file path to the 5-10s MP4 evidence video clip")
    evidence_drive_url: Optional[str] = Field(default=None, description="Permanent Google Drive or GCS link to the evidence video clip")

    @field_validator("confidence", mode="before")
    @classmethod
    def _normalise_confidence_score(cls, value: object) -> float:
        if isinstance(value, (int, float)):
            return max(0.0, min(1.0, float(value)))
        raw = str(value or "").strip()
        try:
            return max(0.0, min(1.0, float(raw)))
        except ValueError:
            label = _normalize_confidence(raw)
            if label == "HIGH":
                return 0.9
            if label == "LOW":
                return 0.4
            return 0.7

    @field_validator("severity", mode="before")
    @classmethod
    def _normalise_severity(cls, value: object) -> Severity:
        """Normalizes LLM severity aliases (HIGH/CRITICAL -> RED_LINE, MEDIUM/LOW -> NORMAL)."""
        if isinstance(value, Severity):
            return value
        raw = str(value or "").strip().upper()
        if raw in ("RED_LINE", "HIGH", "CRITICAL", "SEVERE", "红线", "高危", "严重"):
            return Severity.RED_LINE
        if raw in ("NORMAL", "MEDIUM", "LOW", "WARNING", "MINOR", "COMMON_DEVIATION", "一般", "普通", "中等", "中", "低"):
            return Severity.NORMAL
        return Severity.NONE

    @field_validator("timestamp_in_clip")
    @classmethod
    def _normalise_timestamp(cls, value: str) -> str:
        """Normalizes MM:SS / HH:MM:SS and handles full-video omission placeholders ('N/A', 'FULL_CLIP', '全片')."""
        cleaned = (value or "").strip()
        if not cleaned or cleaned.upper() in ("N/A", "NA", "NONE", "NULL", "END", "FULL_CLIP", "ENTIRE_CLIP", "全片", "全程"):
            return "00:00"
        match = re.search(r"(\d{1,3}):(\d{2})(?::(\d{2}))?", cleaned)
        if not match:
            logger.warning("Unreadable timestamp from model: %r", cleaned[:40])
            return cleaned
        a, b, c = match.groups()
        if c is not None:
            return f"{int(a) * 60 + int(b):02d}:{int(c):02d}"
        return f"{int(a):02d}:{int(b):02d}"

    @property
    def offset_seconds(self) -> float:
        match = re.search(r"(\d{2,}):(\d{2})", self.timestamp_in_clip or "")
        if not match:
            return 0.0
        mins, secs = match.groups()
        return float(int(mins) * 60 + int(secs))

    def with_segment_context(self, segment_index: int, start_offset_sec: float) -> "Finding":
        """Enriches slice-local `timestamp_in_clip` with global video timestamp and `global_offset_sec`."""
        clip_sec = self.offset_seconds
        global_sec = float(start_offset_sec) + clip_sec
        g_mm, g_ss = divmod(int(round(global_sec)), 60)
        raw_clip_ts = self.timestamp_in_clip or "00:00"
        if "Slice#" not in raw_clip_ts:
            formatted_ts = f"{g_mm:02d}:{g_ss:02d} (Slice#{segment_index} @{raw_clip_ts})"
        else:
            formatted_ts = raw_clip_ts
        return self.model_copy(
            update={
                "global_offset_sec": global_sec,
                "segment_index": int(segment_index),
                "timestamp_in_clip": formatted_ts,
            }
        )

    def sanitise(self) -> "Finding":
        """Enforces `STATUS_FOR_DISPOSITION` invariant so disposition and status never contradict."""
        derived_status = STATUS_FOR_DISPOSITION[self.disposition]
        derived_severity = (
            Severity.NONE
            if derived_status != Status.VIOLATION
            else (self.severity if self.severity != Severity.NONE else Severity.RED_LINE)
        )
        return self.model_copy(
            update={"status": derived_status, "severity": derived_severity}
        )


_SEVERITY_RANK = {Severity.RED_LINE: 2, Severity.NORMAL: 1, Severity.NONE: 0}

# Same slice = one model call. The only duplicate a single call produces is the same finding written
# twice, so a same-slice merge needs the same evidence text (after `_normalise_evidence_text`) within
# SAME_SLICE_WINDOW_SEC. There is deliberately no fuzzy-similarity threshold: the whole difference
# between two people can be one character ("员工A未戴帽子" vs "员工B未戴帽子" has a difflib ratio of
# 0.857), so any ratio loose enough to absorb rewording also merges two different people.
SAME_SLICE_WINDOW_SEC = 5.0

# Different slices = two model calls over the shared overlap zone. They describe the same moment in
# different words and assign person labels independently (员工A in one call can be 员工B in the
# other), so their texts cannot be compared at all. A cross-slice merge needs instead the same rule,
# disposition and severity (a RED_LINE and a NORMAL are never folded together), global times within
# the overlap window and, when both findings carry a readable OSD clock, clocks at most
# CROSS_SLICE_OSD_TOLERANCE_SEC apart (the two calls may timestamp the start and the end of one
# multi-second action, such as a handwash).
CROSS_SLICE_OSD_TOLERANCE_SEC = 15.0

_OSD_HMS = re.compile(r"(\d{1,2}):(\d{2}):(\d{2})")


def _rule_key(rule_id: str) -> str:
    """First token of the rule id, upper-cased ("c4 手机" -> "C4"); "" for an empty id."""
    parts = (rule_id or "").split()
    return parts[0].upper() if parts else ""


def _normalise_evidence_text(text: str) -> str:
    """Canonical form for the same-slice verbatim-repeat check.

    Folds full-width/half-width forms (NFKC) and case, and drops whitespace and punctuation except a
    '.' or ':' between two digits, so "1.5秒" vs "15秒" and "12:05:10" vs "120510" stay different.
    """
    folded = unicodedata.normalize("NFKC", text or "").casefold()
    return re.sub(r"(?<!\d)[.:]|[.:](?!\d)|[^\w.:]+", "", folded)


def _osd_seconds(clock: str) -> Optional[int]:
    """Seconds since midnight of the first HH:MM:SS in an OSD reading; None when unreadable."""
    match = _OSD_HMS.search(clock or "")
    if not match:
        return None
    hours, minutes, seconds = (int(part) for part in match.groups())
    if hours > 23 or minutes > 59 or seconds > 59:
        return None
    return hours * 3600 + minutes * 60 + seconds


def _osd_clocks_disagree(a: str, b: str) -> bool:
    """True only when BOTH OSD readings are readable and more than CROSS_SLICE_OSD_TOLERANCE_SEC apart."""
    sec_a, sec_b = _osd_seconds(a), _osd_seconds(b)
    if sec_a is None or sec_b is None:
        return False
    gap = abs(sec_a - sec_b)
    return min(gap, 86_400 - gap) > CROSS_SLICE_OSD_TOLERANCE_SEC  # 23:59:58 vs 00:00:03 = 5s


def _merge_keep_rank(finding: Finding) -> tuple:
    """Which copy of a duplicate to keep: higher severity first, then a cut clip, then confidence."""
    return (
        _SEVERITY_RANK.get(finding.severity, 0),
        bool(finding.evidence_clip_local_path),
        finding.confidence,
    )


def _is_verbatim_repeat(candidate: Finding, report: List[Finding]) -> bool:
    """Whether `candidate` repeats `report` (one model call's report of one event) word for word."""
    first = report[0]
    if (
        candidate.segment_index != first.segment_index
        or _rule_key(candidate.rule_id) != _rule_key(first.rule_id)
        or candidate.disposition != first.disposition
        or _normalise_evidence_text(candidate.evidence) != _normalise_evidence_text(first.evidence)
    ):
        return False
    return any(
        abs(candidate.global_offset_sec - m.global_offset_sec) <= SAME_SLICE_WINDOW_SEC for m in report
    )


def _is_cross_slice_repeat(a: Finding, b: Finding, overlap_window_sec: float) -> bool:
    """Whether two reports from different slices can describe the same moment of the overlap zone."""
    return (
        a.segment_index != b.segment_index
        and _rule_key(a.rule_id) == _rule_key(b.rule_id)
        and a.disposition == b.disposition
        and a.severity == b.severity
        and abs(a.global_offset_sec - b.global_offset_sec) <= overlap_window_sec
        and not _osd_clocks_disagree(a.on_screen_clock, b.on_screen_clock)
    )


def _pair_gap_sec(a: Finding, b: Finding) -> float:
    """Time between two reports: by the OSD clocks when both are readable, else by clip timestamps."""
    sec_a, sec_b = _osd_seconds(a.on_screen_clock), _osd_seconds(b.on_screen_clock)
    if sec_a is not None and sec_b is not None:
        gap = abs(sec_a - sec_b)
        return float(min(gap, 86_400 - gap))
    return abs(a.global_offset_sec - b.global_offset_sec)


def deduplicate_overlapping_findings(
    findings: List[Finding], overlap_window_sec: float = 60.0
) -> List[Finding]:
    """Merges findings that report the same event more than once; distinct events are kept.

    Called once per source video (`audit_service`), so `segment_index` tells apart the slices of that
    one video.
    1. Same slice (one model call): only verbatim repeats are folded, i.e. same rule id (first
       token), disposition and normalised evidence text within `SAME_SLICE_WINDOW_SEC`. Two empty
       texts count as the same; an empty text never matches a non-empty one. Same rule + same OSD
       second with different evidence are different events (e.g. C4 no-hat vs C4 phone use at
       12:14:58, or 员工A vs 员工B) and are both kept.
    2. Different slices (the sliding-window overlap): reports are paired when rule id, disposition
       and severity match, global times are within `overlap_window_sec`, and OSD clocks are at most
       `CROSS_SLICE_OSD_TOLERANCE_SEC` apart when both are readable. Evidence text is not compared
       (see the constants above). The closest pairs are taken first, and an event never holds two
       different reports from the same slice, so the result does not depend on listing order.
    3. Each event is reported once, in first-seen order, as its most severe copy (RED_LINE > NORMAL),
       then the one with a cut evidence clip, then the higher confidence; ties keep the earliest.

    Known limit: two different events with the same rule and severity, each reported by only one of
    two adjacent slices, inside the overlap zone and within the OSD tolerance (or without readable
    OSD clocks), are still merged into one.
    """
    # 1. Same slice: fold verbatim repeats into one report (lists of indices into `findings`).
    reports: List[List[int]] = []
    for idx, candidate in enumerate(findings):
        for members in reports:
            if _is_verbatim_repeat(candidate, [findings[m] for m in members]):
                members.append(idx)
                break
        else:
            reports.append([idx])
    faces = [max((findings[m] for m in members), key=_merge_keep_rank) for members in reports]

    # 2. Different slices: closest pairs first; an event takes at most one report per slice.
    pairs = sorted(
        (_pair_gap_sec(faces[i], faces[j]), i, j)
        for i in range(len(faces))
        for j in range(i + 1, len(faces))
        if _is_cross_slice_repeat(faces[i], faces[j], overlap_window_sec)
    )
    root = list(range(len(reports)))
    slices_in_event = [{face.segment_index} for face in faces]

    def _event_of(i: int) -> int:
        while root[i] != i:
            i = root[i]
        return i

    for _gap, i, j in pairs:
        event_i, event_j = _event_of(i), _event_of(j)
        if event_i == event_j or slices_in_event[event_i] & slices_in_event[event_j]:
            continue
        keep, absorbed = min(event_i, event_j), max(event_i, event_j)
        root[absorbed] = keep
        slices_in_event[keep] |= slices_in_event[absorbed]

    # 3. One row per event, in first-seen order.
    events: dict[int, List[int]] = {}
    for i, members in enumerate(reports):
        events.setdefault(_event_of(i), []).extend(members)
    return [
        max((findings[m] for m in sorted(members)), key=_merge_keep_rank)
        for _event, members in sorted(events.items())
    ]


class WindowResult(BaseModel):
    """Structured JSON output schema for a single 30-minute video segment."""

    calibrated_wall_clock_start: str = Field(
        default="",
        description="Step 0 first-frame CoT calibrated wall-clock timestamp on the CCTV OSD and ambiguous digit verification notes (DD-MM-YYYY HH:MM:SS)",
    )
    people: List[str] = Field(
        default_factory=list,
        description="Step 1 exhaustive roster of all people observed with appearance traits and first/last seen timestamps (e.g., Staff_A-Male-ShortHair-BarStation [08:00:00-08:05:00])",
    )
    person_trajectories: List[str] = Field(
        default_factory=list,
        description=(
            "Step 1 & Step 2 per-person independent event trajectories (event-driven + merged heartbeats): "
            "must correspond 1-to-1 with every person in the people list, outputting an independent complete timeline "
            "for each person in this segment (format: 'Staff_X [Station]: HH:MM:SS(MM:SS) action and contacted surface -> ...'; "
            "routine anomaly-free spans of several minutes may be merged as '10:00:00-10:05:00 [HEARTBEAT] routine work at station'). "
            "Never record only a single salient individual while omitting other people on screen."
        ),
    )
    self_check_notes: List[str] = Field(
        default_factory=list,
        description=(
            "Step 4 missed-detection self-audit item-by-item verification notes (before generating the final findings list, "
            "answer each Step 4 self-audit item against person_trajectories, checking other on-screen staff hand contacts during "
            "multi-person intervals, the first 3s / last 5s of multi-step procedures, and 5-minute high-risk monitoring window closures, "
            "and append any missed violations uncovered during self-audit to the findings array below)."
        ),
    )
    findings: List[Finding] = Field(
        default_factory=list,
        description="SOP rule evaluation results combining all CONFIRMED, SUSPECTED, and UNVERIFIED findings from both the primary scan and Step 4 self-audit",
    )
    carryover_state_summary: str = Field(
        default="",
        description="<=100-word unclosed state summary for the next video slice (including end-of-slice OSD clock timestamp and any unclosed cross-slice timers, contamination monitoring windows, or tool/chemical states)",
    )


SOPFinding = Finding
WindowAuditPayload = WindowResult
SegmentAuditOutput = WindowResult


class TokenLedgerRow(BaseModel):
    """Per-video / per-segment token & latency ledger written to Tab 2 of the Google Sheet (REQ-012)."""

    audit_id: str
    auditor_folder_id: str
    video_filename: str
    video_duration_sec: float
    resolution: str
    model_version_used: str
    prompt_version_used: str
    prompt_token_count: int = 0
    thoughts_token_count: int = 0
    candidates_token_count: int = 0
    total_token_count: int = 0
    estimated_cost_usd: float = 0.0
    e2e_latency_ms: int = 0
    flagged_events_count: int = 0
    fallback_warning: Optional[str] = None


async def extract_evidence_clip(
    source_clip_path: Path,
    center_offset_sec: float,
    out_path: Path,
    pre_sec: float = 2.0,
    post_sec: float = 18.0,
    timeout_sec: float = 60.0,
) -> bool:
    """Cuts a 20s (`[T-2s, T+18s]`) MP4 evidence video clip around `center_offset_sec` via FFmpeg."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or not source_clip_path.exists():
        return False

    out_path.parent.mkdir(parents=True, exist_ok=True)
    start_sec = max(0.0, center_offset_sec - pre_sec)
    duration_sec = pre_sec + post_sec

    codec_flags = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-movflags", "+faststart"]
    args = [
        ffmpeg,
        "-nostdin",
        "-v",
        "error",
        "-y",
        "-ss",
        f"{start_sec:.2f}",
        "-t",
        f"{duration_sec:.2f}",
        "-i",
        str(source_clip_path),
        "-an",
        *codec_flags,
        str(out_path),
    ]
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_sec)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        logger.warning("Evidence clip extraction timed out for %s", out_path.name)
        return False

    if proc.returncode != 0 or not out_path.exists():
        # Fallback to copy if libx264 isn't available or fails
        logger.warning(
            "libx264 encode failed for %s (%s), falling back to stream copy",
            out_path.name,
            stderr.decode("utf-8", "replace").strip()[:200] or "no stderr",
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
            f"{duration_sec:.2f}",
            "-i",
            str(source_clip_path),
            "-an",
            "-c:v",
            "copy",
            "-movflags",
            "+faststart",
            str(out_path),
        ]
        proc_fb = await asyncio.create_subprocess_exec(
            *fallback_args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )
        try:
            _, stderr_fb = await asyncio.wait_for(proc_fb.communicate(), timeout=timeout_sec)
        except asyncio.TimeoutError:
            proc_fb.kill()
            await proc_fb.wait()
            logger.warning("Evidence clip fallback extraction timed out for %s", out_path.name)
            return False

        if proc_fb.returncode != 0 or not out_path.exists():
            logger.warning(
                "Evidence clip extraction failed for %s: %s",
                out_path.name,
                stderr_fb.decode("utf-8", "replace")[:200],
            )
            return False
    return True


def _parse_window_result_or_repair(raw_text: str) -> WindowResult:
    """Parses `WindowResult` JSON and gracefully salvages complete findings if output was truncated at token limit."""
    cleaned = (raw_text or "").strip()
    try:
        return WindowResult.model_validate_json(cleaned)
    except Exception as first_err:
        m = re.search(r'"findings"\s*:\s*\[', cleaned)
        findings_idx = m.start() if m else -1
        last_brace = cleaned.rfind("}")
        if findings_idx != -1 and last_brace > findings_idx:
            pos = last_brace
            while pos > findings_idx:
                candidate = cleaned[: pos + 1].rstrip().rstrip(",") + "]}"
                try:
                    salvaged = WindowResult.model_validate_json(candidate)
                    logger.warning(
                        "Salvaged %d complete findings from truncated WindowResult JSON response.",
                        len(salvaged.findings),
                    )
                    return salvaged
                except Exception:
                    pos = cleaned.rfind("}", findings_idx, pos)
        raise first_err


# Agentic video understanding is served for Gemini 3.x and Gemini 4.x models (live probe on Vertex
# `global` and `asia-southeast1`, 2026-10-22: `gemini-4-argon` and `gemini-3.8-flash` both accept
# `media_processing=AGENTIC`).
AGENTIC_VIDEO_MODEL_MARKER = "gemini-3"
AGENTIC_VIDEO_MODEL_MARKERS = ("gemini-3", "gemini-4")

# Not every Gemini 3.x model has agentic video enabled: on Vertex `global` (probe 2026-10-20)
# `gemini-3.8-flash` accepts `media_processing=AGENTIC`, while `gemini-3.5-flash` and
# `gemini-3.1-pro-preview` answer `400 INVALID_ARGUMENT: Agentic video processing is not enabled
# for this model`. The first slice that hits that error marks the model here and every later
# slice in this process goes straight to static sampling, so a Tab3 evaluation of such a model
# costs one failed call instead of failing every clip. Per process on purpose: the registry is
# learned from the live endpoint, so a model that gains agentic serving is picked up on restart.
_AGENTIC_UNSUPPORTED_MODELS: set[str] = set()


def is_agentic_not_enabled_error(exc: BaseException) -> bool:
    """True when `exc` is Vertex's `Agentic video processing is not enabled for this model` 400.

    google-genai's `APIError` keeps the server message in `.message` and repeats it inside
    `str(exc)` (`400 INVALID_ARGUMENT. {'error': {...}}`); both are checked.
    """
    texts = (str(exc), str(getattr(exc, "message", "") or ""))
    return any(AGENTIC_NOT_ENABLED_MARKER in text for text in texts)


def mark_agentic_unsupported(model_name: str) -> None:
    """Remembers for this process that `model_name` rejects agentic video (see `wants_agentic_video`)."""
    if model_name:
        _AGENTIC_UNSUPPORTED_MODELS.add(model_name)


def wants_agentic_video(model_name: str, media_processing: Optional[str] = None) -> bool:
    """True when agentic video mode is enabled (the default) AND `model_name` is a Gemini 3.x model
    that has not been marked as rejecting agentic video (`mark_agentic_unsupported`).

    `media_processing` defaults to `config.video_media_processing` (env `VIDEO_MEDIA_PROCESSING`,
    default "agentic"), read at call time.
    """
    mode = media_processing if media_processing is not None else config.video_media_processing
    if mode != "agentic" or not any(
        marker in (model_name or "") for marker in (AGENTIC_VIDEO_MODEL_MARKER, *AGENTIC_VIDEO_MODEL_MARKERS)
    ):
        return False
    return model_name not in _AGENTIC_UNSUPPORTED_MODELS


def effective_media_mode(
    model_name: str, media_processing: Optional[str] = None
) -> Literal["agentic", "static"]:
    """The sampling mode `build_video_part` uses for `model_name` right now: `"agentic"` only when
    `wants_agentic_video` holds, `"static"` otherwise (switch off, non-3.x model, or a model that
    fell back after the not-enabled 400). Eval records report this, not the configured switch."""
    return "agentic" if wants_agentic_video(model_name, media_processing) else "static"


def enable_agentic_media_processing(video_part: types.Part) -> bool:
    """Sets `media_processing = MediaProcessing.AGENTIC` on a video Part.

    In google-genai `media_processing` is a field of `types.Part`, not of `GenerateContentConfig`
    (setting it on the config raises `ValueError: ... has no field "media_processing"`). If the
    installed SDK has no such field, log a WARNING and leave the Part on static frame sampling
    rather than failing silently.
    """
    agentic = getattr(getattr(types, "MediaProcessing", None), "AGENTIC", None)
    part_fields = getattr(type(video_part), "model_fields", None) or {}
    if agentic is None or "media_processing" not in part_fields:
        logger.warning(
            "Installed google-genai SDK has no Part.media_processing / MediaProcessing.AGENTIC; "
            "this slice falls back to static frame sampling."
        )
        return False
    video_part.media_processing = agentic
    return True


def build_video_part(
    segment: VideoSliceSegment, model_name: str, media_processing: Optional[str] = None
) -> types.Part:
    """Builds the video Part for one slice (gs:// URI when uploaded, inline bytes otherwise).

    Uses agentic video mode by default when `wants_agentic_video(model_name, media_processing)` is
    True; the switch applies the same way on both branches.
    """
    if segment.gcs_uri:
        video_part = types.Part.from_uri(file_uri=segment.gcs_uri, mime_type="video/mp4")
    elif segment.local_path is not None and segment.local_path.exists():
        video_part = types.Part.from_bytes(
            data=segment.local_path.read_bytes(), mime_type="video/mp4"
        )
    else:
        raise FileNotFoundError(
            f"VideoSliceSegment for '{segment.source_filename}' has neither gcs_uri nor an existing local_path ({segment.local_path!r})."
        )
    if wants_agentic_video(model_name, media_processing):
        enable_agentic_media_processing(video_part)
    return video_part


class AgenticAuditor:
    """Runs the Tab 0 `Active_Model_Version` over each slice (agentic video mode by default for
    Gemini 3.x, static fallback for a model that rejects it) under the Gemini semaphore and cuts
    20s evidence clips."""

    def __init__(self, gemini_concurrency: int = config.gemini_concurrency) -> None:
        self._gemini_semaphore = asyncio.Semaphore(gemini_concurrency)

    async def analyze_segment(
        self,
        *,
        audit_id: str,
        folder_id: str,
        segment: VideoSliceSegment,
        prompt_cfg: PromptModelConfig,
        prior_carryover_summary: str = "",
        evidence_dir: Optional[Path] = None,
    ) -> tuple[WindowResult, TokenLedgerRow]:
        """Analyzes one segment (see `build_video_part` for the agentic switch) and returns sanitized
        findings + ledger row."""
        t0 = time.monotonic()
        relay_prefix = (
            f"[Prior Segment Unclosed State Carryover Summary (First verify whether the first-frame OSD clock in the video corner—usually top-left or top-right—is continuous with the prior segment's ending OSD clock; if same camera angle and continuous within <=5 min, continue tracking timers and states; if across different time periods or camera angles, ignore)]: {prior_carryover_summary}\n\n"
            if prior_carryover_summary
            else ""
        )
        user_prompt = (
            f"{relay_prefix}Strictly follow the system instruction to perform a high-recall SOP compliance audit "
            f"on this store CCTV video segment ({segment.source_filename} "
            f"Slice #{segment.segment_index}, offset {int(segment.start_offset_sec)}s~{int(segment.end_offset_sec)}s), "
            f"and output strictly in the JSON Schema field order in English:\n"
            f"1. First exhaustively catalog all individuals in `people`, then output an independent complete event trajectory for each person in `person_trajectories` (event-driven + merged heartbeats for routine spans) to prevent attention masking when multiple staff are on screen;\n"
            f"2. In `self_check_notes`, complete `# Step 4: Missed-Detection Self-Audit` item by item against each person's trajectory (especially same-second multi-person rescan and transient micro-action rescan);\n"
            f"3. In `findings`, output all violation/suspected/unverified records combined from the primary scan and Step 4 self-audit (strictly enforcing one-action-one-record and multi-dimensional orthogonal row splitting; never merge or swallow companion violations);\n"
            f"4. In `carryover_state_summary`, attach the end-of-slice OSD clock timestamp and all unclosed cross-slice tracking states or timer start points."
        )

        model_name = prompt_cfg.active_model_version
        # Decided before the call: a concurrent slice may mark the model unsupported while this
        # request is in flight, and the fallback below must key off what THIS request sent.
        sent_agentic = wants_agentic_video(model_name)
        video_part = build_video_part(segment, model_name)

        gen_config = types.GenerateContentConfig(
            system_instruction=prompt_cfg.system_instruction,
            response_mime_type="application/json",
            response_schema=WindowResult,
            temperature=0.0,
            max_output_tokens=24576,
        )
        timeout_ms = gemini_timeout_ms_for_slice(segment.end_offset_sec - segment.start_offset_sec)

        async with self._gemini_semaphore:
            try:
                response = await generate_content_with_retry(
                    model=model_name,
                    contents=[video_part, user_prompt],
                    gen_config=gen_config,
                    timeout_ms=timeout_ms,
                )
            except Exception as exc:
                if not (sent_agentic and is_agentic_not_enabled_error(exc)):
                    raise
                # `_is_transient_error` excludes this marker, so the 400 arrives after a single
                # attempt. Mark the model, rebuild the Part (now static) and call once more; any
                # failure of the static call propagates unchanged.
                mark_agentic_unsupported(model_name)
                logger.warning(
                    "Model %s does not support agentic video; falling back to static sampling for the rest of this process",
                    model_name,
                )
                video_part = build_video_part(segment, model_name)
                response = await generate_content_with_retry(
                    model=model_name,
                    contents=[video_part, user_prompt],
                    gen_config=gen_config,
                    timeout_ms=timeout_ms,
                )

        elapsed_ms = int((time.monotonic() - t0) * 1000)
        parsed: WindowResult = (
            response.parsed
            if getattr(response, "parsed", None) is not None
            else _parse_window_result_or_repair(response.text or "")
        )

        sanitised_findings: List[Finding] = []
        for idx, raw_f in enumerate(parsed.findings):
            clean_f = raw_f.sanitise()
            slice_offset_sec = clean_f.offset_seconds
            if (
                clean_f.disposition in FRAMEABLE
                and evidence_dir is not None
                and segment.local_path is not None
                and segment.local_path.exists()
            ):
                clip_name = (
                    f"{Path(segment.source_filename).stem}"
                    f"_seg{segment.segment_index:02d}_{clean_f.rule_id}_{idx:02d}.mp4"
                )
                clip_out = evidence_dir / clip_name
                ok = await extract_evidence_clip(
                    segment.local_path, slice_offset_sec, clip_out
                )
                if ok:
                    clean_f = clean_f.model_copy(
                        update={"evidence_clip_local_path": str(clip_out)}
                    )
            if (
                clean_f.status == Status.VIOLATION
                and not clean_f.evidence_drive_url
                and segment.source_file_id
            ):
                clean_f = clean_f.model_copy(
                    update={
                        "evidence_drive_url": build_source_video_url(segment.source_file_id)
                    }
                )
            clean_f = clean_f.with_segment_context(
                segment.segment_index, segment.start_offset_sec
            )
            sanitised_findings.append(clean_f)

        parsed = parsed.model_copy(update={"findings": sanitised_findings})

        usage = getattr(response, "usage_metadata", None)
        # Agentic video mode bills the frames the model pulls in as tool-use prompt tokens, not as
        # prompt tokens (Round 49: 198k-247k of them for one 302 s slice, vs 9.6k prompt tokens).
        # They are input tokens, so count them in the input column and price them at the input
        # rate; otherwise Tab 2 shows a total that its cost and its own columns do not add up to.
        tool_tokens = int(getattr(usage, "tool_use_prompt_token_count", 0) or 0)
        p_tokens = int(getattr(usage, "prompt_token_count", 0) or 0) + tool_tokens
        t_tokens = int(getattr(usage, "thoughts_token_count", 0) or 0)
        c_tokens = int(getattr(usage, "candidates_token_count", 0) or 0)
        tot_tokens = int(
            getattr(usage, "total_token_count", 0) or (p_tokens + t_tokens + c_tokens)
        )

        cost_usd = round(
            (p_tokens / 1_000_000.0) * config.input_cost_per_million_usd
            + ((t_tokens + c_tokens) / 1_000_000.0) * config.output_cost_per_million_usd,
            4,
        )
        violation_count = sum(
            1 for f in sanitised_findings if f.status == Status.VIOLATION
        )

        ledger = TokenLedgerRow(
            audit_id=audit_id,
            auditor_folder_id=folder_id,
            video_filename=segment.source_filename,
            video_duration_sec=segment.end_offset_sec - segment.start_offset_sec,
            resolution=f"{segment.width}x{segment.height}",
            model_version_used=prompt_cfg.active_model_version,
            prompt_version_used=prompt_cfg.active_prompt_version,
            prompt_token_count=p_tokens,
            thoughts_token_count=t_tokens,
            candidates_token_count=c_tokens,
            total_token_count=tot_tokens,
            estimated_cost_usd=cost_usd,
            e2e_latency_ms=elapsed_ms,
            flagged_events_count=violation_count,
            fallback_warning=prompt_cfg.model_fallback_warning,
        )
        return parsed, ledger
