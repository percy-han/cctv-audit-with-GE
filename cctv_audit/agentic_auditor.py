"""Module 2: Single-Model Agentic Video Auditor & 5-10s Evidence Clipper (`agentic_auditor.py`).

Adapts the battle-tested `Disposition` / `Status` / `Finding` schema and `_normalise_timestamp`
from `percy-han/cctv-audit/cctv_audit/analyzer/schema.py` & `video_analyzer.py`, upgraded for:
1. Video sampling per slice: agentic video understanding (`types.Part.media_processing =
   MediaProcessing.AGENTIC` on the video Part) by default when the Tab 0 `Active_Model_Version`
   is a Gemini 3.x model (`VIDEO_MEDIA_PROCESSING` defaults to `"agentic"`).
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
from typing import List, Optional

from google.genai import types
from pydantic import BaseModel, Field, field_validator

from .config import config
from .gcp import gemini_timeout_ms_for_slice, generate_content_with_retry
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


class Finding(BaseModel):
    """Structured SOP finding with anti-hallucination timestamp & severity normalization."""

    rule_id: str = Field(description="规则编号，如 A1, A2, B1")
    status: Status = Field(default=Status.COMPLIANT, description="判定状态")
    disposition: Disposition = Field(default=Disposition.COMPLIANT, description="五级处置标签")
    confidence: float = Field(default=0.8, ge=0.0, le=1.0, description="置信度 0-1")
    timestamp_in_clip: str = Field(default="00:00", description="片段内相对时间戳 MM:SS 或 HH:MM:SS")
    global_offset_sec: float = Field(default=0.0, description="在原始完整长视频中的全局秒数偏移")
    segment_index: int = Field(default=0, description="所属视频分片序号 (0-based)")
    on_screen_clock: str = Field(default="", description="监控画面角落（通常在左上角或右上角）OSD水印时间，如 08:07:05")
    evidence: str = Field(default="", description="客观视觉事实描述（简体中文）")
    severity: Severity = Field(default=Severity.NONE, description="严重等级")
    evidence_clip_local_path: Optional[str] = Field(default=None, description="本地5-10秒违规短视频路径")
    evidence_drive_url: Optional[str] = Field(default=None, description="Google Drive 永久证据视频链接")

    @field_validator("severity", mode="before")
    @classmethod
    def _normalise_severity(cls, value: object) -> Severity:
        """Normalizes LLM severity aliases (HIGH/CRITICAL -> RED_LINE, MEDIUM/LOW -> NORMAL)."""
        if isinstance(value, Severity):
            return value
        raw = str(value or "").strip().upper()
        if raw in ("RED_LINE", "HIGH", "CRITICAL", "SEVERE", "红线"):
            return Severity.RED_LINE
        if raw in ("NORMAL", "MEDIUM", "LOW", "WARNING", "MINOR", "一般"):
            return Severity.NORMAL
        return Severity.NONE

    @field_validator("timestamp_in_clip")
    @classmethod
    def _normalise_timestamp(cls, value: str) -> str:
        """Normalizes MM:SS / HH:MM:SS and handles full-video omission placeholders ('N/A', '全片')."""
        cleaned = (value or "").strip()
        if not cleaned or cleaned.upper() in ("N/A", "NA", "NONE", "NULL", "END", "全片", "全程"):
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
        description="Step 0 首帧 CoT 校准的监控画面绝对时间及易混位复核说明 (DD-MM-YYYY HH:MM:SS)",
    )
    people: List[str] = Field(
        default_factory=list,
        description="Step 1 全员体貌特征与首次/最后出现时间建档列表 (如 员工A-男-短发-出杯位 [08:00:00-08:05:00])",
    )
    person_trajectories: List[str] = Field(
        default_factory=list,
        description=(
            "Step 1 & Step 2 逐人独立事件轨迹（事件驱动 + 合并心跳）：必须与 people 列表中的每一位在场人员一一对应，"
            "按人员逐条独立输出其在本段内的完整时间线（格式：'员工X [工位]: HH:MM:SS(MM:SS) 动作与接触表面 -> ...'；"
            "连续几分钟无异常的常规操作时段可合并输出如 '10:00:00-10:05:00 [HEARTBEAT] 正常在岗'），"
            "严禁只记录单一显眼人员而漏记同屏其他人员（注意：文本中请统一使用中文「违规表」，勿书写英文键名）"
        ),
    )
    self_check_notes: List[str] = Field(
        default_factory=list,
        description=(
            "Step 4 漏检自查逐项核对结论（在生成最终违规表前，对照 person_trajectories 逐条回答步骤 4 的自查项，"
            "重点核查多人同屏时段其他在场人员的手部接触、多步骤工序首尾 3-5 秒及 5 分钟高危监视窗闭环情况，"
            "并将自查发现的漏检一并列入下方违规数组；注意：文本中请统一使用中文「违规表」，严禁书写英文键名）"
        ),
    )
    findings: List[Finding] = Field(
        default_factory=list,
        description="SOP 规则逐项判定结果（汇总主扫描与 Step 4 漏检自查发现的全部 CONFIRMED / SUSPECTED / UNVERIFIED 记录）",
    )
    carryover_state_summary: str = Field(
        default="",
        description="供下一段切片接力使用的 <=100 字未闭环状态摘要（含本段结尾 OSD 时间及未闭环的跨段计时/污染监视窗/工具药剂状态）",
    )


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


# Agentic video understanding is only served for Gemini 3.x models.
AGENTIC_VIDEO_MODEL_MARKER = "gemini-3"


def wants_agentic_video(model_name: str, media_processing: Optional[str] = None) -> bool:
    """True when agentic video mode is enabled (the default) AND `model_name` is a Gemini 3.x model.

    `media_processing` defaults to `config.video_media_processing` (env `VIDEO_MEDIA_PROCESSING`,
    default "agentic"), read at call time.
    """
    mode = media_processing if media_processing is not None else config.video_media_processing
    return mode == "agentic" and AGENTIC_VIDEO_MODEL_MARKER in (model_name or "")


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
    Gemini 3.x) under the Gemini semaphore and cuts 20s evidence clips."""

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
            f"【前序分段未闭环状态接力摘要（请先核对本段首帧画面角落（通常为左上角或右上角）OSD时间与前序结尾OSD时间是否连续；若同机位且时间连续则继续追踪计时，若跨时段或换机位则忽略）】：{prior_carryover_summary}\n\n"
            if prior_carryover_summary
            else ""
        )
        user_prompt = (
            f"{relay_prefix}请严格依据系统指令对本段门店监控视频（{segment.source_filename} "
            f"分片 #{segment.segment_index}，偏移 {int(segment.start_offset_sec)}s~{int(segment.end_offset_sec)}s）"
            f"执行高召回 SOP 合规稽核，并严格按 JSON Schema 字段顺序输出：\n"
            f"1. 先在 `people` 中穷尽建档所有出现人员，再在 `person_trajectories` 中为每位人员输出一条独立的完整事件轨迹（事件驱动 + 连续常规时段合并心跳），防止多人同屏时被单一显眼人员吸走注意力；\n"
            f"2. 在 `self_check_notes` 中对照每位人员的轨迹逐条完成 `# 步骤 4：漏检自查`（特别是多人并发同秒回扫与瞬时微动作回扫）；\n"
            f"3. 在违规表数组中输出主扫描与漏检自查汇总的全部违规/存疑记录（严守「一行为一记录」与多维正交拆行，严禁合并吞并）；\n"
            f"4. 在 `carryover_state_summary` 中附上本段结尾 OSD 时间及所有尚未闭环的跨段追踪状态/计时起点。"
        )

        video_part = build_video_part(segment, prompt_cfg.active_model_version)

        gen_config = types.GenerateContentConfig(
            system_instruction=prompt_cfg.system_instruction,
            response_mime_type="application/json",
            response_schema=WindowResult,
            temperature=0.0,
            max_output_tokens=65536,
        )

        async with self._gemini_semaphore:
            response = await generate_content_with_retry(
                model=prompt_cfg.active_model_version,
                contents=[video_part, user_prompt],
                gen_config=gen_config,
                timeout_ms=gemini_timeout_ms_for_slice(
                    segment.end_offset_sec - segment.start_offset_sec
                ),
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
                        "evidence_drive_url": f"https://drive.google.com/file/d/{segment.source_file_id}/view"
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
