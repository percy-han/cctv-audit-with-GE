"""Score one audit run against the frozen golden dataset (the "ruler").

Pipeline
  1. Flatten every AI finding of the run's job JSONs (status == VIOLATION).
  2. Code hard filter, per labelled part: same video file, and the finding's
     on-screen time is within +-WINDOW_SEC of the labelled OSD time, or the
     time span the finding describes covers it. Parts with no labelled time
     (e.g. sheet row 13) keep every finding on that video.
  3. LLM judge (Vertex AI GenAI Eval SDK, ``client.evals.evaluate`` with an
     ``LLMMetric``) grades each part: 1 / 0.5 / 0 plus the matched finding id.
  4. Row score = mean of its part scores (compound rows: both caught = 1.0,
     one caught = 0.5). Weighted recall = sum(row scores) / rows, reported
     for dev / holdout / all.

Guardrail metric: alert density = findings per 5-minute clip (mean and max).

Dwell-aware asymmetric windows (Round 80)
  For "wait / dwell N minutes" SOP parts (sanitiser contact time before wiping / installing back,
  "stir within 30s" ...) the human and the AI often anchor different moments of the same
  violation: the label marks the spray (start) while the AI marks the premature wipe / install
  (end), up to N minutes later. Every part therefore gets an asymmetric window
  ``[label - before, label + after]`` on the 24-hour ring (signed gap = finding - label):

  - Defaults (unchanged behaviour): POINT 20/20, WINDOW 60/60.
  - Dwell parts (WINDOW only, never POINT): ``after = min(max(default_after, dwell + 30), 330)``
    (``DWELL_PAD_SEC`` = 30, cap ``SPAN_MAX_SEC``); ``before`` stays the default. Rationale: every
    observed miss had the AI *later* than the label (IC2 +56s, IC6 +33s, IC5a +101s), and the
    customer's sentences ("After spray sanitiser, do not wait 5 minutes ... before wiping") read as
    anchored on the spray. The mirror direction (``before = min(max(default_before, dwell + 30),
    330)``) is applied only when the label text carries an explicit end-anchor cue
    (``_END_ANCHOR`` e.g. "when wiping", "installed back at", "擦拭时", "装回时"). A finding that
    describes the whole dwell as a span still matches through the unchanged span rule.
  - Dwell detection precedence, deterministic: (a) explicit ``dwell_sec`` on the part, else on the
    item (manifest ``item_overrides`` may also set it); (b) parse the part description (fallback:
    item ``finding_verbatim``) with ``_DWELL_PATTERNS`` (EN ``not wait [for] N min|s``,
    ``within N s|min``; ZH ``未满/不足/未等/静置/浸泡 N 分钟/秒``); (c) no dwell.
    Explicit ``window_before_sec`` / ``window_after_sec`` (part, else item) override each side last.
  - Derived at scoring time from the label text, so the golden file and ``golden_version`` are
    unchanged. Near-miss diagnostics are computed outside the (new) window.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Iterable

POINT_WINDOW_SEC = 20  # ±20s (40s total span) for instant point actions (POINT); drift enabled
WINDOW_SEC = 60  # ±60s for continuous process / dwell / causal-chain actions (WINDOW); drift exempt
NEAR_MISS_SEC = 180  # diagnostics only: same-video findings just outside the window
SPAN_MAX_SEC = 330  # a described span longer than one clip is not trusted
CLIP_SEC = 300
_HMS = re.compile(r"(?<![\d:])(\d{1,2}):(\d{2}):(\d{2})(?![\d:])")
_MS = re.compile(r"(?<![\d:])(\d{1,2}):(\d{2})(?![\d:])")
_MATCH = re.compile(r"MATCH\s*=\s*([^;]*);", re.I)
_FID = re.compile(r"\bF\d{3,}\b")

POINT_ACTION_KEYWORDS: tuple[str, ...] = (
    "soaping time is less than 20s",
    "apply soap before wet hand",
    "not dry hand with hand towel",
    "not rinse hand before applying handwashing gel",
)


def classify_temporal_mode(item: dict[str, Any], part: dict[str, Any] | None = None) -> str:
    """Temporal mode of a golden part; see ``temporal_mode_and_source``."""
    return temporal_mode_and_source(item, part)[0]


def temporal_mode_and_source(item: dict[str, Any], part: dict[str, Any] | None = None) -> tuple[str, str]:
    """Classify a golden item/part as ``POINT`` (±20s, timestamp drift enabled) or
    ``WINDOW`` (±60s + span containment, exempt from single-point timestamp drift).

    Returns ``(mode, source)``. ``source == "explicit"`` when the golden part / item (or the golden
    manifest's ``item_overrides``) sets ``temporal_mode``; that always wins. Otherwise
    ``source == "heuristic"``: the documented fallback below (clause / keyword based, tuned on the
    customer's first label sheet). New golden sets should set ``temporal_mode`` explicitly.

    - ``POINT``: instant single-step actions at the handwashing sink (e.g. Clause 1.5
      soaping <20s, applying soap before wetting hands, not rinsing before gel, not
      drying hands with a paper towel) that have an explicit OSD timestamp.
    - ``WINDOW``: continuous processes, 5-10 min sanitizer dwell windows, multi-step
      equipment cleaning, 3-stage cross-contamination causal chains (trigger -> no
      handwash -> resume production), 30s tea-stirring timers, or unanchored whole-clip
      state checks (e.g. R13 apron storage with no OSD timestamp).
    """
    explicit = str((part or {}).get("temporal_mode") or item.get("temporal_mode") or "").strip().upper()
    if explicit in ("POINT", "WINDOW"):
        return explicit, "explicit"
    osd_list = (part or {}).get("osd_times") if part is not None else item.get("osd_times")
    if not osd_list:
        return "WINDOW", "heuristic"
    clause = str(item.get("audit_clause") or "").strip().lower()
    desc = str((part or {}).get("description") or item.get("finding_verbatim") or "").strip().lower()
    if "1.5 handwashing" in clause:
        return "POINT", "heuristic"
    if any(kw in desc for kw in POINT_ACTION_KEYWORDS):
        return "POINT", "heuristic"
    return "WINDOW", "heuristic"


def part_window_sec(mode: str) -> int:
    """Return the prefilter window in seconds for the given temporal mode."""
    return POINT_WINDOW_SEC if str(mode).upper() == "POINT" else WINDOW_SEC


DWELL_PAD_SEC = 30  # slack after the dwell for the premature wipe / install to happen and be seen
_UNIT_SEC = {"min": 60, "mins": 60, "minute": 60, "minutes": 60, "分钟": 60,
             "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1, "秒": 1}
_DWELL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"not\s+wait\s+(?:for\s+)?(\d+(?:\.\d+)?)\s*(minutes|minute|mins|min|seconds|secs|sec|s)\b", re.I),
    re.compile(r"within\s+(\d+(?:\.\d+)?)\s*(minutes|minute|mins|min|seconds|secs|sec|s)\b", re.I),
    re.compile(r"(?:未满|不足|未等|静置|浸泡)\s*(\d+(?:\.\d+)?)\s*(分钟|秒)"),
)
_END_ANCHOR = re.compile(
    r"\b(?:when|while)\s+(?:wiping|brushing|installing|cleaning)\b|\binstall(?:ed|ing)?\s+back\s+at\b"
    r"|\bwiped\s+at\b|(?:擦拭|装回|安装|刷洗)时",
    re.I,
)


def parse_dwell_sec(text: str) -> int | None:
    """Dwell / timer length in seconds named by a label text, or None. First pattern that matches
    wins (see ``_DWELL_PATTERNS``); decimals are rounded to whole seconds."""
    for pat in _DWELL_PATTERNS:
        m = pat.search(text or "")
        if m:
            return int(round(float(m.group(1)) * _UNIT_SEC[m.group(2).lower()]))
    return None


@dataclasses.dataclass(frozen=True)
class PartWindow:
    before: int  # seconds a finding may precede the label
    after: int  # seconds a finding may follow the label
    dwell_sec: int | None = None
    dwell_source: str = ""  # "explicit" | "label_text" | ""

    @property
    def symmetric(self) -> bool:
        return self.before == self.after

    def label(self) -> str:
        """Human-readable window, e.g. ``±60s`` or ``−60s / +330s（静置 5 分钟）``."""
        core = f"±{self.before}s" if self.symmetric else f"−{self.before}s / +{self.after}s"
        if self.dwell_sec:
            d = self.dwell_sec
            dwell_txt = f"{d // 60} 分钟" if d % 60 == 0 else f"{d} 秒"
            core += f"（静置 {dwell_txt}）"
        return core


def _int_or_none(v: Any) -> int | None:
    if v is None or v == "":
        return None
    return int(round(float(v)))


def part_time_window(item: dict[str, Any], part: dict[str, Any] | None, mode: str) -> PartWindow:
    """Asymmetric scoring window of a golden part (see the module docstring)."""
    part = part or {}
    default = part_window_sec(mode)
    before, after = default, default
    dwell, src = None, ""
    if str(mode).upper() != "POINT":
        explicit = _int_or_none(part.get("dwell_sec"))
        if explicit is None:
            explicit = _int_or_none(item.get("dwell_sec"))
        text = str(part.get("description") or item.get("finding_verbatim") or "")
        if explicit is not None:
            dwell, src = (explicit or None), "explicit"
        else:
            dwell = parse_dwell_sec(text)
            src = "label_text" if dwell else ""
        if dwell:
            ext = min(max(default, dwell + DWELL_PAD_SEC), SPAN_MAX_SEC)
            after = ext
            if _END_ANCHOR.search(text):
                before = ext
    for key, side in (("window_before_sec", "before"), ("window_after_sec", "after")):
        v = _int_or_none(part.get(key))
        if v is None:
            v = _int_or_none(item.get(key))
        if v is not None:
            if side == "before":
                before = v
            else:
                after = v
    return PartWindow(before=before, after=after, dwell_sec=dwell, dwell_source=src if dwell else "")


UNCATEGORIZED = "未分类"


def sop_category_and_source(item: dict[str, Any]) -> tuple[str, str]:
    """SOP category of a golden item for drill-down, with its source.

    ``explicit``: the golden item (or the manifest's ``item_overrides``) sets ``sop_category``.
    ``audit_clause``: fallback, the item's own audit clause text, so a new clause becomes its own
    category instead of being forced into a fixed bucket. ``focus`` / ``none``: further fallbacks.
    """
    explicit = str(item.get("sop_category") or "").strip()
    if explicit:
        return explicit, "explicit"
    clause = str(item.get("audit_clause") or "").strip()
    if clause:
        return clause, "audit_clause"
    focus = str(item.get("focus") or "").strip()
    if focus:
        return focus, "focus"
    return UNCATEGORIZED, "none"


def classify_sop_category(item: dict[str, Any]) -> str:
    return sop_category_and_source(item)[0]


JUDGE_TEMPLATE = """你是连锁茶饮门店 CCTV 稽核的阅卷老师。任务：判断 AI 稽核输出里，有没有抓到下面这 1 条人工标注的违规。

【人工标注（标准答案）】
{reference}

【同一视频、时间相近的 AI 稽核条目（候选，已由代码按视频文件和时间预筛）】
{response}

评分规则（只看候选条目，不要自己脑补视频内容；SOP 条款编号不同不影响判分）：
- 1 分：至少有一条候选描述的是「同一时刻、同一人、同一种违规行为」。措辞可以不同。
  例：标注“搓洗时间少于 20 秒”，候选必须明确指出搓洗/揉搓/起泡时间不足或洗手步骤时长不够。
- 0.5 分：候选指向同一时刻、同一个动作或同一件物品，但报的是不同的毛病，或只覆盖了标注的一部分。
  例：标注“搓洗时间少于 20 秒”，候选只说“洗手不规范/未按五步洗手”而没提时长。
- 0 分：没有候选说到这件事。

只返回 JSON，不要任何其他文字：
{{"score": <0 或 0.5 或 1>, "explanation": "MATCH=<命中的候选编号，多个用逗号，没有写 NONE>; <一句中文理由，引用候选里的关键原话>"}}"""


def hms_to_sec(text: str) -> int | None:
    m = _HMS.search(text or "")
    if not m:
        return None
    h, mi, s = (int(x) for x in m.groups())
    return h * 3600 + mi * 60 + s


def sec_to_hms(sec: float) -> str:
    sec = int(round(sec)) % 86400
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


@dataclasses.dataclass
class Finding:
    finding_id: str
    job_id: str
    filename: str
    rule_id: str
    disposition: str
    severity: str
    confidence: float
    osd: str
    clip_ts: str
    evidence: str
    osd_times: list[int]  # every OSD second the finding mentions


def finding_osd_times(f: dict[str, Any]) -> list[int]:
    """OSD seconds referenced by a finding: its own clock, explicit HH:MM:SS
    in the evidence, and clip-relative MM:SS in the evidence converted via
    the clip's start OSD (on_screen_clock - global_offset_sec)."""
    times: list[int] = []
    own = hms_to_sec(str(f.get("on_screen_clock") or ""))
    if own is not None:
        times.append(own)
    ev = str(f.get("evidence") or "")
    for h, mi, s in _HMS.findall(ev):
        times.append(int(h) * 3600 + int(mi) * 60 + int(s))
    if own is not None and f.get("global_offset_sec") is not None:
        start = own - float(f["global_offset_sec"])
        for mi, s in _MS.findall(ev):
            times.append(int(start + int(mi) * 60 + int(s)))
    return sorted(set(times))


def flatten_findings(jobs: Iterable[dict[str, Any]]) -> list[Finding]:
    out: list[Finding] = []
    for job in jobs:
        segs = job.get("completed_segments") or {}
        ordered = sorted(
            segs.values(),
            key=lambda s: (str(s.get("filename")), int(s.get("segment_index") or 0)),
        )
        for seg in ordered:
            for f in seg.get("findings") or []:
                if str(f.get("status", "VIOLATION")).upper() != "VIOLATION":
                    continue
                out.append(
                    Finding(
                        finding_id=f"F{len(out) + 1:03d}",
                        job_id=str(job.get("job_id")),
                        filename=str(seg.get("filename") or f.get("filename") or ""),
                        rule_id=str(f.get("rule_id") or ""),
                        disposition=str(f.get("disposition") or f.get("violation_disposition") or ""),
                        severity=str(f.get("severity") or ""),
                        confidence=float(f.get("confidence") or 0.0),
                        osd=str(f.get("on_screen_clock") or ""),
                        clip_ts=str(f.get("timestamp_in_clip") or ""),
                        evidence=str(f.get("evidence") or ""),
                        osd_times=finding_osd_times(f),
                    )
                )
    return out


def ring_gap(a: int, b: int) -> int:
    """Seconds between two OSD clock readings on a 24-hour ring."""
    d = abs(a - b) % 86400
    return min(d, 86400 - d)


def signed_ring_gap(finding_sec: int, label_sec: int) -> int:
    """``finding - label`` in seconds on a 24-hour ring, in [-43200, 43200)."""
    d = (finding_sec - label_sec) % 86400
    return d - 86400 if d >= 43200 else d


def time_matches_window(label_sec: int, times: list[int], before: int, after: int) -> bool:
    """Asymmetric match: any finding time with signed gap in ``[-before, +after]``, or a described
    span (``max - min`` <= span limit) containing the label. Span limit is ``2 * w`` for windows
    narrower than ``WINDOW_SEC`` (``w = max(before, after)``) else ``SPAN_MAX_SEC`` -- identical to
    the historical symmetric rule when ``before == after``."""
    if not times:
        return False
    if any(-before <= signed_ring_gap(t, label_sec) <= after for t in times):
        return True
    lo, hi = min(times), max(times)
    w = max(before, after)
    span_limit = (2 * w) if w < WINDOW_SEC else SPAN_MAX_SEC
    return hi - lo <= span_limit and lo <= label_sec <= hi


def time_matches(label_sec: int, times: list[int], window_sec: int = WINDOW_SEC) -> bool:
    return time_matches_window(label_sec, times, window_sec, window_sec)


def prefilter(
    part: dict[str, Any],
    files: list[str],
    findings: list[Finding],
    window_sec: int = WINDOW_SEC,
    window_before_sec: int | None = None,
    window_after_sec: int | None = None,
) -> list[Finding]:
    """Same-video findings inside the part's window. ``window_before_sec`` / ``window_after_sec``
    (each defaulting to ``window_sec``) give the asymmetric dwell window."""
    before = window_sec if window_before_sec is None else int(window_before_sec)
    after = window_sec if window_after_sec is None else int(window_after_sec)
    same_video = [f for f in findings if f.filename in files]
    label_secs = [hms_to_sec(t) for t in part["osd_times"]]
    label_secs = [s for s in label_secs if s is not None]
    if not label_secs:
        return same_video
    return [f for f in same_video
            if any(time_matches_window(s, f.osd_times, before, after) for s in label_secs)]


def compute_point_timestamp_drift(
    part: dict[str, Any],
    matched_findings: list[Finding],
    mode: str,
) -> int | None:
    """Compute OSD timestamp drift in seconds for a matched ``POINT`` action part.

    Returns ``None`` for ``WINDOW`` parts (continuous processes / dwell windows /
    multi-step causal chains are exempt from single-point timestamp drift), or when
    the part is unmatched or has no labelled OSD timestamp.
    """
    if str(mode).upper() != "POINT" or not matched_findings:
        return None
    label_secs = [s for s in (hms_to_sec(t) for t in part.get("osd_times") or []) if s is not None]
    if not label_secs:
        return None
    candidate_secs: list[int] = []
    for f in matched_findings:
        primary_sec = hms_to_sec(f.osd)
        if primary_sec is not None:
            candidate_secs.append(primary_sec)
        elif f.osd_times:
            candidate_secs.extend(f.osd_times)
    if not candidate_secs:
        return None
    return min(ring_gap(ft, ls) for ft in candidate_secs for ls in label_secs)


def render_candidates(cands: list[Finding]) -> str:
    lines = []
    for f in cands:
        lines.append(
            f"[{f.finding_id}] OSD {f.osd or '?'} | 条款 {f.rule_id} | {f.disposition}/{f.severity} | {f.evidence}"
        )
    return "\n".join(lines)


def render_reference(item: dict[str, Any], part: dict[str, Any]) -> str:
    when = "、".join(part["osd_times"]) or "（标注未给时间）"
    return (
        f"条款：{item['audit_clause']}\n"
        f"视频：{', '.join(item['video_filenames'])}\n"
        f"OSD 时间：{when}\n"
        f"违规描述（客户原文）：{part['description']}"
    )


def parse_match(explanation: str) -> list[str]:
    """Finding ids cited after ``MATCH=`` (up to the first ';'). Tolerates
    brackets and separators such as ``[F019], [F020]``; ``NONE`` -> []."""
    m = _MATCH.search(explanation or "")
    if not m:
        return []
    return _FID.findall(m.group(1))


def pick_judge_model(model_names: list[str]) -> str:
    """Newest Gemini Pro text model; falls back to newest Flash.

    Accepts ``gemini-X.Y-pro``, ``-preview`` and numbered builds (``-001``).
    Ranking: version, then GA over preview, then build number."""
    pat = re.compile(r"gemini-(\d+(?:\.\d+)?)-(pro|flash)(?:-(preview|\d{3}))?$")

    def ver(n: str) -> tuple[float, int, int]:
        m = pat.match(n)
        suffix = m.group(3) or ""
        return (float(m.group(1)), 0 if suffix == "preview" else 1,
                int(suffix) if suffix.isdigit() else 0)

    for family in ("pro", "flash"):
        pool = [n for n in model_names if (m := pat.match(n)) and m.group(2) == family]
        if pool:
            return max(pool, key=ver)
    raise RuntimeError("no Gemini pro/flash model visible in this project")


def near_misses(part: dict[str, Any], files: list[str], findings: list[Finding],
                exclude: set[str], window: PartWindow | None = None) -> list[str]:
    """Same-video findings just outside the scoring window. Diagnostics for calibrating the
    window; never used for the score. The near band on each side is ``NEAR_MISS_SEC`` from the
    label, or ``side + (NEAR_MISS_SEC - WINDOW_SEC)`` when a (dwell) side is wider than
    ``WINDOW_SEC`` -- identical to the historical ``gap <= NEAR_MISS_SEC`` for default windows."""
    label_secs = [s for s in (hms_to_sec(t) for t in part["osd_times"]) if s is not None]
    extra = NEAR_MISS_SEC - WINDOW_SEC

    def band(side: int) -> int:
        return side + extra if side > WINDOW_SEC else NEAR_MISS_SEC

    lo_band = band(window.before) if window else NEAR_MISS_SEC
    hi_band = band(window.after) if window else NEAR_MISS_SEC
    out = []
    for f in findings:
        if f.filename not in files or f.finding_id in exclude or not f.osd_times:
            continue
        for s in label_secs:
            gaps = [signed_ring_gap(t, s) for t in f.osd_times]
            if any(-lo_band <= g <= hi_band for g in gaps):
                gap = min(abs(g) for g in gaps)
                out.append(f"{f.finding_id} {f.osd} {f.rule_id} (差 {gap}s)：{f.evidence[:60]}")
                break
    return out


def _genai_judge_fallback(
    cases: list[dict[str, str]],
    project: str,
    judge_model: str,
    credentials: Any = None,
) -> tuple[list[tuple[float | None, str]], Any]:
    """Direct ``google.genai`` judge fallback when ``agentplatform`` is not installed in the worker image."""
    from google import genai
    from google.genai import types as gt

    client = genai.Client(
        vertexai=True,
        project=project,
        location="global",
        credentials=credentials,
    )
    out: list[tuple[float | None, str]] = []
    for c in cases:
        prompt_text = JUDGE_TEMPLATE.format(
            reference=c["reference"],
            response=c["response"],
        )
        try:
            resp = client.models.generate_content(
                model=judge_model,
                contents=prompt_text,
                config=gt.GenerateContentConfig(
                    temperature=0.0,
                    response_mime_type="application/json",
                ),
            )
            raw_text = (resp.text or "").strip()
            parsed = json.loads(raw_text)
            raw_score = parsed.get("score")
            score_val = float(raw_score) if raw_score is not None else None
            expl = str(parsed.get("explanation") or "")
            out.append((score_val, expl))
        except Exception as exc:  # noqa: BLE001
            out.append((None, f"JUDGE_ERROR: {type(exc).__name__}: {exc}"))
    return out, None


def sdk_judge(cases: list[dict[str, str]], project: str, judge_model: str,
              credentials: Any = None) -> tuple[list[tuple[float | None, str]], Any]:
    """Grade cases with the Vertex AI GenAI Eval SDK (or ``google.genai`` fallback in slim containers).

    Returns ([(score, explanation), ...], raw EvaluationResult)."""
    try:
        import agentplatform
        from agentplatform import types
        from google.genai import types as gt
    except ImportError:
        return _genai_judge_fallback(cases, project, judge_model, credentials)

    client = agentplatform.Client(project=project, location="global", credentials=credentials)
    metric = types.LLMMetric(
        name="label_hit",
        prompt_template=JUDGE_TEMPLATE,
        judge_model=f"projects/{project}/locations/global/publishers/google/models/{judge_model}",
    )
    ds = types.EvaluationDataset(eval_cases=[
        types.EvalCase(
            prompt=gt.UserContent(c["case_id"]),
            responses=[types.ResponseCandidate(response=gt.ModelContent(c["response"]))],
            reference=types.ResponseCandidate(response=gt.ModelContent(c["reference"])),
        )
        for c in cases
    ])
    result = client.evals.evaluate(dataset=ds, metrics=[metric])
    out: list[tuple[float | None, str]] = []
    for case in result.eval_case_results:
        mr = case.response_candidate_results[0].metric_results["label_hit"]
        err = getattr(mr, "error_message", None)
        out.append((mr.score, mr.explanation or (f"JUDGE_ERROR: {err}" if err else "")))
    if len(out) != len(cases):
        raise RuntimeError(f"judge returned {len(out)} results for {len(cases)} cases")
    return out, result


def vote(passes: list[list[tuple[float | None, str]]]) -> list[tuple[float | None, str]]:
    """Median-of-N per case. The returned explanation comes from a pass whose
    score equals the median, prefixed with the vote tally when passes disagree."""
    if not passes:
        return []
    n_cases = len(passes[0])
    if any(len(p) != n_cases for p in passes):
        raise RuntimeError("judge passes returned different case counts")
    out: list[tuple[float | None, str]] = []
    for i in range(n_cases):
        votes = [p[i] for p in passes]
        if any(s is None for s, _ in votes):
            bad = next(e for s, e in votes if s is None)
            out.append((None, bad))
            continue
        scores = sorted(float(s) for s, _ in votes)
        med = scores[len(scores) // 2]
        expl = next(e for s, e in votes if float(s) == med)
        if len(set(scores)) > 1:
            expl = f"[{len(votes)} 次投票 {'/'.join(f'{x:g}' for x in scores)} 取中位] " + expl
        out.append((med, expl))
    return out


def assign_exclusive_candidates(item: dict[str, Any], per_part: dict[str, list[Finding]]) -> None:
    """A single AI finding may be a candidate for only one of a row's
    time-distinct parts: the part whose labelled time is nearest.

    Two-timestamp rows (e.g. "121036 & 121100") are two separate events; a
    finding about one of them must not also be offered for the other. Done
    before judging so every score is earned only from the part's own
    candidates. Parts that share one labelled time (text compounds) may share
    a finding. Mutates ``per_part`` in place.
    """
    parts = item["parts"]
    if len({tuple(p["osd_times"]) for p in parts}) < 2:
        return
    owner: dict[str, tuple[int, str]] = {}
    for p in parts:
        label = [s for s in (hms_to_sec(t) for t in p["osd_times"]) if s is not None]
        for f in per_part[p["part_id"]]:
            gap = min((ring_gap(t, s) for t in f.osd_times for s in label), default=10**9)
            if f.finding_id not in owner or gap < owner[f.finding_id][0]:
                owner[f.finding_id] = (gap, p["part_id"])
    for p in parts:
        per_part[p["part_id"]] = [
            f for f in per_part[p["part_id"]] if owner[f.finding_id][1] == p["part_id"]
        ]


def score(items: list[dict[str, Any]], findings: list[Finding],
          judge: Callable[[list[dict[str, str]]], list[tuple[float | None, str]]]) -> dict[str, Any]:
    by_id = {f.finding_id: f for f in findings}
    cases: list[dict[str, str]] = []
    part_rows: list[dict[str, Any]] = []
    part_spec_by_id: dict[str, dict[str, Any]] = {}
    for it in items:
        per_part: dict[str, list[Finding]] = {}
        part_modes: dict[str, str] = {}
        part_mode_src: dict[str, str] = {}
        part_windows: dict[str, PartWindow] = {}
        for p in it["parts"]:
            mode, mode_src = temporal_mode_and_source(it, p)
            pw = part_time_window(it, p, mode)
            part_modes[p["part_id"]] = mode
            part_mode_src[p["part_id"]] = mode_src
            part_windows[p["part_id"]] = pw
            part_spec_by_id[p["part_id"]] = p
            per_part[p["part_id"]] = prefilter(
                p, it["video_filenames"], findings, window_sec=part_window_sec(mode),
                window_before_sec=pw.before, window_after_sec=pw.after,
            )
        assign_exclusive_candidates(it, per_part)
        for p in it["parts"]:
            mode = part_modes[p["part_id"]]
            w_sec = part_window_sec(mode)
            pw = part_windows[p["part_id"]]
            cands = per_part[p["part_id"]]
            ids = [c.finding_id for c in cands]
            row = {
                "item_id": it["item_id"],
                "part_id": p["part_id"],
                "temporal_mode": mode,
                "temporal_mode_source": part_mode_src[p["part_id"]],
                "window_sec": w_sec,
                "window_before_sec": pw.before,
                "window_after_sec": pw.after,
                "dwell_sec": pw.dwell_sec,
                "dwell_source": pw.dwell_source,
                "window_label": pw.label(),
                "candidate_ids": ids,
                "near_misses": near_misses(p, it["video_filenames"], findings, set(ids), pw),
            }
            if cands:
                row["case_index"] = len(cases)
                cases.append({"case_id": p["part_id"],
                              "reference": render_reference(it, p),
                              "response": render_candidates(cands)})
            part_rows.append(row)
    verdicts = judge(cases) if cases else []
    if len(verdicts) != len(cases):
        raise RuntimeError(f"judge returned {len(verdicts)} verdicts for {len(cases)} cases")
    for row in part_rows:
        w_sec = int(row.get("window_sec") or WINDOW_SEC)
        mode = str(row.get("temporal_mode") or "WINDOW")
        if "case_index" not in row:
            win_txt = row.get("window_label") or f"±{w_sec}s"
            row.update(
                score=0.0,
                explanation=f"代码预筛：同一视频 {win_txt} 内没有任何 AI 条目",
                matched_ids=[],
                timestamp_drift_sec=None,
            )
            continue
        s, expl = verdicts[row.pop("case_index")]
        if s is None:
            raise RuntimeError(f"judge failed on {row['part_id']}: {expl}")
        s = float(s)
        if s not in (0.0, 0.5, 1.0):
            raise RuntimeError(f"judge gave off-scale score {s} on {row['part_id']}")
        matched = [m for m in parse_match(expl) if m in row["candidate_ids"]]
        if s > 0 and not matched:
            raise RuntimeError(f"judge scored {row['part_id']}={s} without citing a candidate: {expl}")
        matched_objs = [by_id[m] for m in matched if m in by_id]
        drift_sec = compute_point_timestamp_drift(
            part_spec_by_id.get(row["part_id"], {}), matched_objs, mode
        ) if s > 0 else None
        row.update(score=s, explanation=expl, matched_ids=matched, timestamp_drift_sec=drift_sec)

    results = []
    confirmed_only_total_pts = 0.0
    all_matched_fids: set[str] = set()
    for it in items:
        parts = [r for r in part_rows if r["item_id"] == it["item_id"]]
        row_score = sum(r["score"] for r in parts) / len(parts)
        matched = [by_id[m] for r in parts for m in r["matched_ids"] if m in by_id]
        for m_obj in matched:
            all_matched_fids.add(m_obj.finding_id)
        confirmed_part_scores = []
        for r in parts:
            has_confirmed = any(
                by_id[m].disposition.strip().upper() == "CONFIRMED"
                for m in r["matched_ids"]
                if m in by_id
            )
            confirmed_part_scores.append(r["score"] if has_confirmed else 0.0)
        confirmed_row_score = sum(confirmed_part_scores) / len(parts) if parts else 0.0
        confirmed_only_total_pts += confirmed_row_score
        sop_cat, sop_cat_src = sop_category_and_source(it)
        results.append({
            "item_id": it["item_id"], "sheet_row": it.get("sheet_row"), "split": it["split"],
            "focus": it["focus"], "outlet_name": it["outlet_name"],
            "audit_clause": it.get("audit_clause", ""),
            "sop_category": sop_cat,
            "sop_category_source": sop_cat_src,
            "finding_verbatim": it["finding_verbatim"],
            "video_filenames": it["video_filenames"],
            "score": row_score,
            "confirmed_only_score": confirmed_row_score,
            "parts": parts,
            "matched": [dataclasses.asdict(m) for m in matched],
        })

    def recall(split: str | None) -> dict[str, Any]:
        rows = [r for r in results if split is None or r["split"] == split]
        pts = sum(r["score"] for r in rows)
        return {"points": pts, "rows": len(rows), "recall": pts / len(rows) if rows else 0.0}

    per_clip: dict[str, int] = {}
    for f in findings:
        per_clip[f.filename] = per_clip.get(f.filename, 0) + 1

    # Drill-down 1: Recall by SOP category -- every category present in the golden set
    sop_category_recall: dict[str, dict[str, Any]] = {}
    for cat in sorted({r["sop_category"] for r in results}):
        cat_rows = [r for r in results if r["sop_category"] == cat]
        if cat_rows:
            pts = sum(r["score"] for r in cat_rows)
            sop_category_recall[cat] = {
                "points": pts,
                "rows": len(cat_rows),
                "recall": pts / len(cat_rows),
            }

    # Drill-down 2: Recall by Outlet & Camera Focus
    outlet_focus_recall: dict[str, dict[str, Any]] = {}
    seen_of_keys: list[str] = []
    for r in results:
        of_key = f"{r['outlet_name']} | {r['focus']}"
        if of_key not in seen_of_keys:
            seen_of_keys.append(of_key)
    for of_key in seen_of_keys:
        of_rows = [r for r in results if f"{r['outlet_name']} | {r['focus']}" == of_key]
        pts = sum(r["score"] for r in of_rows)
        outlet_focus_recall[of_key] = {
            "outlet_name": of_rows[0]["outlet_name"],
            "focus": of_rows[0]["focus"],
            "points": pts,
            "rows": len(of_rows),
            "recall": pts / len(of_rows) if of_rows else 0.0,
        }

    # Drill-down 3: Per-video recall & alert count
    video_breakdown: dict[str, dict[str, Any]] = {}
    all_video_names: list[str] = []
    for r in results:
        for vf in r["video_filenames"]:
            if vf not in all_video_names:
                all_video_names.append(vf)
    for vf in per_clip:
        if vf not in all_video_names:
            all_video_names.append(vf)
    for vf in all_video_names:
        v_rows = [r for r in results if vf in r["video_filenames"]]
        pts = sum(r["score"] for r in v_rows)
        video_breakdown[vf] = {
            "points": pts,
            "rows": len(v_rows),
            "recall": pts / len(v_rows) if v_rows else 0.0,
            "findings_count": per_clip.get(vf, 0),
        }

    # Point-action timestamp drift metrics (computed strictly on POINT parts only; WINDOW exempt)
    point_parts = [p for p in part_rows if p.get("temporal_mode") == "POINT"]
    window_parts = [p for p in part_rows if p.get("temporal_mode") != "POINT"]
    point_drifts = [
        int(p["timestamp_drift_sec"])
        for p in point_parts
        if p.get("timestamp_drift_sec") is not None
    ]
    mean_point_drift = (sum(point_drifts) / len(point_drifts)) if point_drifts else None
    max_point_drift = max(point_drifts) if point_drifts else None

    total_rows = len(results)
    total_findings_count = len(findings)
    matched_findings_count = len(all_matched_fids)
    hit_rate = (matched_findings_count / total_findings_count) if total_findings_count else 0.0

    return {
        "recall": {"all": recall(None), "dev": recall("dev"), "holdout": recall("holdout")},
        "alert_density": {
            "findings": len(findings), "clips": len(per_clip),
            "mean_per_clip": len(findings) / len(per_clip) if per_clip else 0.0,
            "max_per_clip": max(per_clip.values()) if per_clip else 0,
            "per_clip": per_clip,
        },
        "sop_category_recall": sop_category_recall,
        "outlet_focus_recall": outlet_focus_recall,
        "video_breakdown": video_breakdown,
        "quality_metrics": {
            "confirmed_only_points": confirmed_only_total_pts,
            "confirmed_only_recall": (confirmed_only_total_pts / total_rows) if total_rows else 0.0,
            "matched_findings_count": matched_findings_count,
            "total_findings_count": total_findings_count,
            "hit_rate": hit_rate,
            "point_window_sec": POINT_WINDOW_SEC,
            "window_sec": WINDOW_SEC,
            "point_parts_total": len(point_parts),
            "point_parts_matched": len(point_drifts),
            "window_parts_total": len(window_parts),
            "dwell_parts_total": sum(1 for p in part_rows if p.get("dwell_sec")),
            "mean_point_timestamp_drift_sec": mean_point_drift,
            "max_point_timestamp_drift_sec": max_point_drift,
        },
        "items": results,
        "judge_cases": len(cases),
    }


def load_json(path: str) -> dict[str, Any]:
    if path.startswith("gs://"):
        from eval.golden_set import _read_bytes

        raw = _read_bytes(path)
        if raw is None:
            raise FileNotFoundError(f"GCS JSON not found: {path}")
        return json.loads(raw.decode("utf-8"))
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def render_markdown(run_label: str, rep: dict[str, Any]) -> str:
    r = rep["recall"]
    ad = rep["alert_density"]
    qm = rep.get("quality_metrics") or {}
    drift_str = (
        f"平均 {qm['mean_point_timestamp_drift_sec']:.1f}s（最大 {qm['max_point_timestamp_drift_sec']}s，"
        f"仅统计 {qm.get('point_parts_matched', 0)}/{qm.get('point_parts_total', 0)} 个 POINT 瞬时动作，±{qm.get('point_window_sec', POINT_WINDOW_SEC)}s 窗口；"
        f"{qm.get('window_parts_total', 0)} 个 WINDOW 持续过程动作豁免偏移统计）"
        if qm.get("mean_point_timestamp_drift_sec") is not None
        else f"无命中的 POINT 瞬时动作（POINT ±{qm.get('point_window_sec', POINT_WINDOW_SEC)}s / WINDOW ±{qm.get('window_sec', WINDOW_SEC)}s）"
    )
    mark = {1.0: "✓ 命中", 0.5: "◐ 半对", 0.0: "✗ 漏掉"}
    lines = [
        f"# 尺子打分：{run_label}",
        "",
        f"- 黄金集：{(rep.get('golden') or {}).get('golden_version', '未记录')}（{r['all']['rows']} 题）",
        f"- 加权召回（全部 {r['all']['rows']} 行）：**{r['all']['points']:.1f} / {r['all']['rows']} = {r['all']['recall']:.1%}**",
        f"- 仅 CONFIRMED 召回率：{qm.get('confirmed_only_points', 0.0):.1f} / {r['all']['rows']} = {qm.get('confirmed_only_recall', 0.0):.1%}",
        f"- 告警密度：{ad['findings']} 条 / {ad['clips']} 段 = 平均 {ad['mean_per_clip']:.1f} 条/段，最多 {ad['max_per_clip']} 条/段（有效命中率 Hit Rate = {qm.get('hit_rate', 0.0):.1%}）",
        f"- 瞬时动作时间戳定位误差（POINT ±{qm.get('point_window_sec', POINT_WINDOW_SEC)}s）：{drift_str}",
        f"- 裁判调用：{rep['judge_cases']} 个 part；judge = {rep.get('judge_model', '?')}",
        "",
        "| 行 | 分组 | 时间窗模式 | 客户标注 | 判分 | 裁判理由 | 命中的 AI 条目 | 窗口外近邻（仅供校准，不计分） |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for it in rep["items"]:
        grp = f"{it['focus'].split()[0]} · {it['outlet_name']}"
        modes = "/".join(
            f"{p.get('temporal_mode', 'WINDOW')}({p.get('window_label') or '±' + str(p.get('window_sec', WINDOW_SEC)) + 's'}"
            + (f",偏移{p['timestamp_drift_sec']}s)" if p.get("timestamp_drift_sec") is not None else ")")
            for p in it["parts"]
        )
        reasons = " / ".join(f"{p['part_id']}: {p['explanation']}" for p in it["parts"])
        hits = "<br>".join(
            f"{m['finding_id']} {m['osd']} {m['rule_id']}：{m['evidence'][:80]}" for m in it["matched"]
        ) or "—"
        near = "<br>".join(n for p in it["parts"] for n in p.get("near_misses", [])) or "—"
        label = it["finding_verbatim"].replace("|", "/")
        lines.append(
            f"| {it.get('sheet_row') or it['item_id']} | {grp} | {modes} | {label} | {mark.get(it['score'], it['score'])} | "
            f"{reasons.replace('|', '/')} | {hits.replace('|', '/')} | {near.replace('|', '/')} |"
        )
    return "\n".join(lines) + "\n"


def resolve_judge_model(project: str, credentials: Any = None) -> str:
    """Newest Gemini Pro visible in the project, listed live (never hardcoded)."""
    from google import genai
    gc = genai.Client(vertexai=True, project=project, location="global", credentials=credentials)
    names = [m.name.split("/")[-1] for m in gc.models.list(config={"page_size": 200, "query_base": True})]
    return pick_judge_model(names)


def score_run(*, run_dir: str | os.PathLike[str], golden_path: str | os.PathLike[str], project: str,
              run_label: str, judge_model: str = "", judge_passes: int = 3, credentials: Any = None,
              judge_fn: Callable[[list[dict[str, str]]], list[tuple[float | None, str]]] | None = None,
              ) -> dict[str, Any]:
    """Score every ``job_*.json`` in ``run_dir`` against the golden JSONL.

    Library entry point shared by the CLI (``main``) and ``eval/run_gcp_round.py``.
    ``judge_fn`` replaces the live SDK judge (tests only)."""
    if judge_passes < 1 or judge_passes % 2 == 0:
        raise ValueError("judge_passes must be an odd number >= 1")
    job_paths = sorted(Path(run_dir).glob("job_*.json"))
    if not job_paths:
        raise FileNotFoundError(f"no job_*.json in {run_dir}")
    jobs = [load_json(str(p)) for p in job_paths]
    from eval.golden_set import load_golden

    golden = load_golden(str(golden_path))
    items = golden.items
    findings = flatten_findings(jobs)

    sdk_result: list[Any] = []
    if judge_fn is None:
        judge_model = judge_model or resolve_judge_model(project, credentials)

        def judge_fn(cases):
            passes = []
            for _ in range(judge_passes):
                verdicts, res = sdk_judge(cases, project, judge_model, credentials)
                sdk_result.append(res)
                passes.append(verdicts)
            return vote(passes)

    rep = score(items, findings, judge_fn)
    rep["golden"] = golden.summary()
    rep.update(
        judge_passes=judge_passes, run_label=run_label, judge_model=judge_model or "injected",
        job_ids=[j.get("job_id") for j in jobs],
        scored_at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        point_window_sec=POINT_WINDOW_SEC,
        window_sec=WINDOW_SEC,
    )
    rep["_sdk_result"] = sdk_result[0] if sdk_result else None
    return rep


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--job", action="append", required=True, help="job JSON path or gs:// URI")
    ap.add_argument("--run-label", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument(
        "--project",
        default=os.environ.get("GCP_PROJECT", ""),
        required=not bool(os.environ.get("GCP_PROJECT")),
        help="Project that runs the judge model (or set GCP_PROJECT)",
    )
    ap.add_argument("--judge-model", default="", help="default: newest Gemini Pro, listed live")
    ap.add_argument("--judge-passes", type=int, default=3, help="odd number; median vote per part")
    args = ap.parse_args(argv)
    if args.judge_passes < 1 or args.judge_passes % 2 == 0:
        ap.error("--judge-passes must be an odd number >= 1")

    credentials = None
    if os.environ.get("ACCESS_TOKEN"):
        from google.oauth2.credentials import Credentials
        credentials = Credentials(os.environ["ACCESS_TOKEN"], quota_project_id=args.project)

    # Stage the --job inputs (local or gs://) as job_*.json so the CLI uses the same path as score_run.
    with tempfile.TemporaryDirectory() as tmp:
        for i, p in enumerate(args.job):
            with open(os.path.join(tmp, f"job_{i:02d}.json"), "w", encoding="utf-8") as fh:
                json.dump(load_json(p), fh, ensure_ascii=False)
        rep = score_run(run_dir=tmp, golden_path=args.dataset, project=args.project,
                        run_label=args.run_label, judge_model=args.judge_model,
                        judge_passes=args.judge_passes, credentials=credentials)
    sdk_res = rep.pop("_sdk_result", None)
    os.makedirs(args.out_dir, exist_ok=True)
    with open(os.path.join(args.out_dir, "score.json"), "w", encoding="utf-8") as fh:
        json.dump(rep, fh, ensure_ascii=False, indent=1)
    with open(os.path.join(args.out_dir, "score.md"), "w", encoding="utf-8") as fh:
        fh.write(render_markdown(args.run_label, rep))
    if sdk_res is not None:
        with open(os.path.join(args.out_dir, "sdk_eval_result.json"), "w", encoding="utf-8") as fh:
            fh.write(sdk_res.model_dump_json(fallback=str))
    r = rep["recall"]
    print(f"{args.run_label}: all {r['all']['points']:.1f}/{r['all']['rows']} = {r['all']['recall']:.1%} | "
          f"dev {r['dev']['recall']:.1%} | holdout {r['holdout']['recall']:.1%} | judge {rep['judge_model']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
