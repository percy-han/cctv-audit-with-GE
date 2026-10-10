"""Comprehensive Unit & Contract Tests (`test_units.py`) for Chagee CCTV AI Audit.

Covers all Phase 2 SDD contracts and Phase 3 Test Plan cases (`TC-001` through `TC-008`):
1. `TC-001` / `REQ-011` / `CON-006`: `<720P` low-resolution preflight rejection (`0 Token` spend)
   and `>=720P` sliding-window (30m + 2m overlap) slicing calculation.
2. `TC-002` / `REQ-013`: Google Sheets `[Tab 0]` dual pointers (`Active_Prompt_Version` & `Active_Model_Version`),
   0.1s rollback switching, `AUTO_LATEST_FLASH` silent auto-switch blocking, and candidate catalog sync.
3. `TC-003` / `REQ-008`: Multi-user isolation in `UserScopedJobStore` (Supervisor A cannot read Supervisor B's jobs)
   and `Folder-as-a-Workspace` in-folder Sheet & Evidence subfolder creation.
4. `TC-004` / `CON-007` / `SC-004`: High-Recall 5-tier `Disposition` (`CONFIRMED`/`SUSPECTED`/`UNVERIFIED`)
   auto-sanitization to `Status.VIOLATION` and mandatory `⏳ 待人工复核 (PENDING_HUMAN_REVIEW)` initial status.
5. `TC-005` / `REQ-012`: `Tab 2` Token & latency ledger recording both `model_version_used` and `prompt_version_used`.
6. `TC-006`: Anti-hallucination timestamp normalization (`Finding._normalise_timestamp`) and verbatim URL check.
"""

from __future__ import annotations

import asyncio
import unittest
from typing import List

from cctv_audit.agentic_auditor import (
    Disposition,
    Finding,
    Severity,
    Status,
    TokenLedgerRow,
    WindowResult,
)
from cctv_audit.audit_service import AuditService
import cctv_audit.audit_service as audit_service_module
from cctv_audit.jobs import LOCAL_PLACEHOLDER_BUCKET, JobState, UserScopedJobStore
from cctv_audit.prompt_manager import PromptManager, SopRuleItem
from cctv_audit.turn import TurnAction, TurnDecision, verify_url_verbatim
from cctv_audit.video_ingestor import (
    VideoIngestor,
    VideoMetadataItem,
    calculate_sliding_windows,
    extract_drive_id,
)
from cctv_audit.workspace_reporter import (
    INITIAL_REVIEW_STATUS,
    ViolationSheetRow,
    WorkspaceReporter,
)


class FakeSheetClient:
    """In-memory mock of the Master Prompt & Model Config Google Sheet."""

    def __init__(self, active_prompt: str, active_model: str) -> None:
        self.pointers = {
            "Active_Prompt_Version": active_prompt,
            "Active_Model_Version": active_model,
            "Fallback_Model_Version": "gemini-3.8-flash",
        }
        self.appended_models: List[str] = []

    async def read_tab0_pointers(self, sheet_id: str) -> dict[str, str]:
        return dict(self.pointers)

    async def read_prompt_tab_rules(
        self, sheet_id: str, tab_name: str
    ) -> List[SopRuleItem]:
        return [
            SopRuleItem(
                rule_id="A1",
                name=f"规则来自_{tab_name}",
                check_instruction="洗手20秒检查",
            )
        ]

    async def append_available_models(
        self, sheet_id: str, new_models: List[str]
    ) -> None:
        self.appended_models.extend(new_models)


def test_extract_drive_id_and_sliding_windows():
    """TC-001: Verifies standard urllib.parse Drive ID extraction and 30m/2m sliding windows."""
    folder_url = "https://drive.google.com/drive/folders/1AbCDefGhIjKlMnOpQrStUvWxYz?usp=sharing"
    assert extract_drive_id(folder_url) == "1AbCDefGhIjKlMnOpQrStUvWxYz"

    # 60-minute (3600s) video with 1800s segment and 120s overlap -> 0..1800, 1680..3480, 3360..3600
    windows = calculate_sliding_windows(3600.0, segment_sec=1800, overlap_sec=120)
    assert len(windows) == 3
    assert windows[0] == (0.0, 1800.0)
    assert windows[1] == (1680.0, 3480.0)
    assert windows[2] == (3360.0, 3600.0)


def test_preflight_rejects_below_720p_and_accepts_hd():
    """TC-001 / CON-006: Videos < 720P are immediately rejected with 0 token spend."""
    ingestor = VideoIngestor()

    low_res_items = [
        VideoMetadataItem(
            file_id="vid_low",
            filename="store_360p.mp4",
            width=640,
            height=360,
            duration_sec=1500.0,
        )
    ]
    res_low = asyncio.run(
        ingestor.inspect_drive_videos(
            "https://drive.google.com/drive/folders/folder_low",
            preloaded_items=low_res_items,
        )
    )
    assert res_low.passed is False
    assert res_low.estimated_tokens == 0
    assert "store_360p.mp4" in res_low.rejected_videos[0]

    hd_items = [
        VideoMetadataItem(
            file_id="vid_hd",
            filename="store_1080p.mp4",
            width=1920,
            height=1080,
            duration_sec=1800.0,
        )
    ]
    res_hd = asyncio.run(
        ingestor.inspect_drive_videos(
            "https://drive.google.com/drive/folders/folder_hd",
            preloaded_items=hd_items,
        )
    )
    assert res_hd.passed is True
    assert res_hd.planned_segments_count == 4
    assert res_hd.estimated_tokens == 0
    # 1800s video -> 4 slices of (0..600, 540..1140, 1080..1680, 1620..1800) = 1980s sliced duration
    # 1980s * (361s / 300s) / 60 = 39.71 -> 39.7 min (sequential carryover, not divided by gemini_concurrency)
    assert res_hd.estimated_minutes == 39.7
    assert "预估消耗" not in res_hd.message_to_user
    assert "Tab 2 真实账单" in res_hd.message_to_user
    assert "361 秒" in res_hd.message_to_user
    # One 30-min video IS sliced -> "切片计划" names the long video count and the total slice count.
    assert "**切片计划**" in res_hd.message_to_user
    assert "`1` 段超过 10 分钟的长视频" in res_hd.message_to_user
    assert "合计 **`4` 个无音分片**" in res_hd.message_to_user
    assert "处理计划" not in res_hd.message_to_user

    # 5 x 5-min clips are NOT sliced -> must not claim 10-minute slicing.
    short_items = [
        VideoMetadataItem(
            file_id=f"vid_s{i}",
            filename=f"Footage{i}.mov",
            width=1920,
            height=1080,
            duration_sec=302.0,
        )
        for i in range(1, 6)
    ]
    res_short = asyncio.run(
        ingestor.inspect_drive_videos(
            "https://drive.google.com/drive/folders/folder_short",
            preloaded_items=short_items,
        )
    )
    assert res_short.passed is True
    assert res_short.planned_segments_count == 5
    assert "**处理计划**：共 `5` 段视频，单段均不超过 10 分钟，无需切分" in res_short.message_to_user
    assert "切片计划" not in res_short.message_to_user
    assert "分钟/段" not in res_short.message_to_user

    from cctv_audit.video_ingestor import progress_unit_label

    assert progress_unit_label(short_items) == "段视频"
    assert progress_unit_label(hd_items) == "个分片"
    assert progress_unit_label([]) == "个分片"
    assert progress_unit_label(None) == "个分片"

    # Regression (referee R58 P1): a 600.1s video (2 windows) + a 0s video (0 windows) sum to 2 == len,
    # which must NOT be reported as "no slicing".
    tricky_items = [
        VideoMetadataItem(file_id="long", filename="a.mov", width=1920, height=1080, duration_sec=600.1),
        VideoMetadataItem(file_id="zero", filename="b.mov", width=1920, height=1080, duration_sec=0.0),
    ]
    assert progress_unit_label(tricky_items) == "个分片"
    res_tricky = asyncio.run(
        ingestor.inspect_drive_videos(
            "https://drive.google.com/drive/folders/folder_tricky",
            preloaded_items=tricky_items,
        )
    )
    assert res_tricky.planned_segments_count == len(tricky_items)
    assert "**切片计划**" in res_tricky.message_to_user
    assert "处理计划" not in res_tricky.message_to_user
    # Exactly 600s is not sliced.
    assert progress_unit_label(
        [VideoMetadataItem(file_id="e", filename="e.mov", width=1920, height=1080, duration_sec=600.0)]
    ) == "段视频"

    from unittest import mock
    from cctv_audit.config import config as app_cfg
    from cctv_audit.video_ingestor import estimate_wall_minutes

    # 1 x 5-min (300s) clip -> 361s = 6.0 min; 5 x 5-min (1500s) folder -> 1805s = 30.1 min (never 3.0 min)
    assert estimate_wall_minutes(300.0, 1, media_processing="agentic") == 6.0
    assert estimate_wall_minutes(1500.0, 5, media_processing="agentic") == 30.1
    assert estimate_wall_minutes(300.0, 1, media_processing="static") == 1.5
    assert estimate_wall_minutes(0.0, 0) == 0.0

    with mock.patch.object(app_cfg, "video_media_processing", "static"):
        res_static = asyncio.run(
            ingestor.inspect_drive_videos(
                "https://drive.google.com/drive/folders/folder_hd",
                preloaded_items=hd_items,
            )
        )
        assert res_static.estimated_minutes == 9.9
        assert "90 秒" in res_static.message_to_user
        assert "361 秒" not in res_static.message_to_user


def test_prompt_and_model_sheet_rollback_and_no_silent_autoswitch():
    """TC-002 / REQ-013: Verifies 0.1s rollback in Tab 0 and blocks AUTO_LATEST_FLASH silent switching."""
    sheet = FakeSheetClient(
        active_prompt="Prompt_v1.1_新增抹茶效期版",
        active_model="gemini-3.8-flash",
    )
    pm = PromptManager(sheet_client=sheet)

    cfg_v11 = asyncio.run(pm.load_active_config("sheet_master"))
    assert cfg_v11.active_prompt_version == "Prompt_v1.1_新增抹茶效期版"
    assert cfg_v11.active_model_version == "gemini-3.8-flash"
    assert "Prompt_v1.1_新增抹茶效期版" in cfg_v11.system_instruction

    # Simulate 0.1s supervisor dropdown rollback in Tab 0 back to Prompt_v1.0
    sheet.pointers["Active_Prompt_Version"] = "Prompt_v1.0_基准73%版"
    cfg_v10 = asyncio.run(pm.load_active_config("sheet_master"))
    assert cfg_v10.active_prompt_version == "Prompt_v1.0_基准73%版"

    # Verify AUTO_LATEST_FLASH is blocked from silently changing the production model
    sheet.pointers["Active_Model_Version"] = "AUTO_LATEST_FLASH"
    cfg_blocked = asyncio.run(pm.load_active_config("sheet_master"))
    assert cfg_blocked.active_model_version == "gemini-3.8-flash"
    assert cfg_blocked.model_fallback_warning is not None
    assert "AUTO_LATEST_FLASH" in cfg_blocked.model_fallback_warning

    # Verify candidate catalog sync only appends new models without changing Active_Model_Version
    sheet.pointers["Active_Model_Version"] = "gemini-3.8-flash"
    added = asyncio.run(
        pm.sync_available_models_to_catalog(
            "sheet_master",
            existing_catalog=["gemini-3.8-flash"],
            discovered_models=["gemini-3.8-flash", "gemini-4.0-flash"],
        )
    )
    assert added == ["gemini-4.0-flash"]
    assert sheet.appended_models == ["gemini-4.0-flash"]
    assert sheet.pointers["Active_Model_Version"] == "gemini-3.8-flash"


def test_multi_user_isolation_and_in_folder_reporting():
    """TC-003 / TC-004 / TC-005: Enforces user_id isolation and PENDING_HUMAN_REVIEW in Tab 1 + dual versions in Tab 2."""
    store = UserScopedJobStore()
    service = AuditService(job_store=store)

    items = [
        VideoMetadataItem(
            file_id="v1",
            filename="kitchen_cam01.mp4",
            width=1280,
            height=720,
            duration_sec=600.0,
        )
    ]
    job_a = asyncio.run(
        service.preflight(
            user_id="auditor_a@chagee.com",
            drive_url="https://drive.google.com/drive/folders/folder_auditor_A",
            preloaded_items=items,
        )
    )
    assert job_a.state == JobState.READY

    # Auditor B MUST NOT be able to access Auditor A's job
    cross_read = asyncio.run(store.get("auditor_b@chagee.com", job_a.job_id))
    assert cross_read is None

    # Verify Disposition -> Status sanitization and PENDING_HUMAN_REVIEW default (CON-007 / SC-004)
    reporter = WorkspaceReporter()
    raw_findings = [
        Finding(
            rule_id="A1",
            disposition=Disposition.UNVERIFIED,
            timestamp_in_clip="01:05:20",
            evidence="应做未见：未观察到员工洗手",
        ),
        Finding(
            rule_id="A2",
            disposition=Disposition.CONFIRMED,
            timestamp_in_clip="04:15",
            evidence="员工触碰垃圾抽屉把手后直接拿取雪克杯",
        ),
        Finding(
            rule_id="B1",
            disposition=Disposition.OUT_OF_SCOPE,
            timestamp_in_clip="00:00",
            evidence="制冰机不在画面内",
        ),
    ]
    tab1_rows = reporter.build_tab1_rows(
        "job123",
        "kitchen_cam01.mp4",
        raw_findings,
        source_video_file_id="v1_source_id",
    )
    assert len(tab1_rows) == 2  # CONFIRMED and UNVERIFIED enter violation table; OUT_OF_SCOPE excluded
    assert tab1_rows[0].timestamp_in_clip == "65:20"
    assert all(r.human_review_status == INITIAL_REVIEW_STATUS for r in tab1_rows)
    assert all(
        r.evidence_drive_url == "https://drive.google.com/file/d/v1_source_id/view"
        for r in tab1_rows
    )
    assert all("见原片" not in r.evidence_drive_url for r in tab1_rows)
    assert all("/drive/folders/" not in r.evidence_drive_url for r in tab1_rows)


def test_verbatim_url_guardrail():
    """TC-006: Prevents hallucinated Drive URLs not present in the user's message."""
    hallucinated = TurnDecision(
        action=TurnAction.INSPECT,
        drive_url="https://drive.google.com/drive/folders/hallucinated_999",
        reply_summary="准备预检",
    )
    checked = verify_url_verbatim(hallucinated, "帮我稽核一下今天新加坡门店的视频")
    assert checked.action == TurnAction.UNCLEAR
    assert checked.drive_url == ""


def test_overlap_dedup_and_severity_normalization() -> None:
    """TC-007: Verifies severity alias normalization, N/A timestamp handling, global offset enrichment, and 60s overlap dedup."""
    from cctv_audit.agentic_auditor import deduplicate_overlapping_findings

    # 1. Severity alias ('HIGH' -> RED_LINE, 'MEDIUM' -> NORMAL) & 'N/A' timestamp normalization
    f_omission = Finding(
        rule_id="3.3 制冰机清洁顺序",
        disposition=Disposition.UNVERIFIED,
        severity="HIGH",  # type: ignore[arg-type]
        timestamp_in_clip="N/A",
        evidence="全片30分钟未见拆卸水挡板",
    ).sanitise()
    assert f_omission.severity == Severity.RED_LINE
    assert f_omission.timestamp_in_clip == "00:00"

    # 2. Global offset enrichment across Slice #0 (0..600s) and Slice #1 (540..1140s)
    # Same violation occurring at 570s (09:30 in Slice #0, and 00:30 in Slice #1)
    f_slice0 = (
        Finding(
            rule_id="1.5.3 洗手揉搓不足20秒",
            disposition=Disposition.CONFIRMED,
            confidence=0.88,
            severity="RED_LINE",
            timestamp_in_clip="09:30",
            evidence="Slice 0 末尾 09:30 洗手仅 3 秒",
        )
        .sanitise()
        .with_segment_context(segment_index=0, start_offset_sec=0.0)
    )
    f_slice1_dup = (
        Finding(
            rule_id="1.5.3 洗手揉搓不足20秒",
            disposition=Disposition.CONFIRMED,
            confidence=0.95,
            severity="HIGH",  # type: ignore[arg-type]
            timestamp_in_clip="00:30",
            evidence="Slice 1 开头 00:30 (即全局 09:30) 洗手仅 3 秒",
            evidence_clip_local_path="/tmp/evidence_570s.mp4",
        )
        .sanitise()
        .with_segment_context(segment_index=1, start_offset_sec=540.0)
    )
    f_slice1_distinct = (
        Finding(
            rule_id="1.5.5 玩手机交叉污染",
            disposition=Disposition.CONFIRMED,
            confidence=0.92,
            severity="RED_LINE",
            timestamp_in_clip="08:49",
            evidence="Slice 1 08:49 (全局 17:49) 玩手机未洗手",
        )
        .sanitise()
        .with_segment_context(segment_index=1, start_offset_sec=540.0)
    )

    assert f_slice0.global_offset_sec == 570.0
    assert f_slice1_dup.global_offset_sec == 570.0
    assert f_slice1_distinct.global_offset_sec == 1069.0
    assert "17:49 (Slice#1 @08:49)" in f_slice1_distinct.timestamp_in_clip

    deduped = deduplicate_overlapping_findings(
        [f_slice0, f_slice1_dup, f_slice1_distinct], overlap_window_sec=60.0
    )
    assert len(deduped) == 2
    assert deduped[0].confidence == 0.95
    assert deduped[0].evidence_clip_local_path == "/tmp/evidence_570s.mp4"

    # 3. Within the SAME slice (segment_index=0), distinct events sharing a rule_id within 60s
    # (e.g. C4 hat violation @ 00:33 + C4 apron on counter @ 00:56, or two separate <20s handwashes)
    # MUST NOT be swallowed by cross-slice overlap deduplication.
    f_c4_hat = (
        Finding(
            rule_id="C4",
            disposition=Disposition.CONFIRMED,
            confidence=0.95,
            severity="RED_LINE",
            timestamp_in_clip="00:33",
            on_screen_clock="12:10:33",
            evidence="员工A未佩戴工作帽及发网",
        )
        .sanitise()
        .with_segment_context(segment_index=0, start_offset_sec=0.0)
    )
    f_c4_apron = (
        Finding(
            rule_id="C4",
            disposition=Disposition.CONFIRMED,
            confidence=0.92,
            severity="NORMAL",
            timestamp_in_clip="00:56",
            on_screen_clock="12:10:56",
            evidence="员工A脱下围裙后直接堆放在后厨不锈钢操作台面上，未规范收纳至指定个人物品区域",
        )
        .sanitise()
        .with_segment_context(segment_index=0, start_offset_sec=0.0)
    )
    same_slice_deduped = deduplicate_overlapping_findings(
        [f_c4_hat, f_c4_apron], overlap_window_sec=60.0
    )
    assert len(same_slice_deduped) == 2
    assert "围裙" in same_slice_deduped[1].evidence

    # 4. Natural chronological ordering of multi-clip folders (Footage 1..12)
    from cctv_audit.video_ingestor import natural_video_sort_key

    unordered = ["Footage 12.mp4", "Footage 2.mp4", "Footage 10.mp4", "Footage 1.mp4", "Footage 9.mp4"]
    assert sorted(unordered, key=natural_video_sort_key) == [
        "Footage 1.mp4",
        "Footage 2.mp4",
        "Footage 9.mp4",
        "Footage 10.mp4",
        "Footage 12.mp4",
    ]


def test_same_slice_dedup_needs_same_event_text_and_keeps_red_line() -> None:
    """TC-007b (Round 49/49c): only repeats of one event are merged; distinct events are always kept.

    Job cc8307 (Bau Cat handwashing, Footage 6): a C4 "phone use" RED_LINE at OSD 12:14:58 was merged
    into a C4 "no hat" NORMAL finding of the same second, and the red-line row vanished from the report.
    The Round 49 fix (0.85 text similarity) still merged "员工A未戴帽子" with "员工B未戴帽子" (ratio
    0.857) and let cross-slice dedup fold a RED_LINE into a NORMAL. Round 49c: within a slice only
    verbatim repeats merge; across slices severity must match, readable OSD clocks must agree, pairs
    are taken closest-first and an event holds at most one report per slice.
    """
    import itertools

    from cctv_audit.agentic_auditor import (
        CROSS_SLICE_OSD_TOLERANCE_SEC,
        _osd_clocks_disagree,
        deduplicate_overlapping_findings,
    )

    def _in_slice(segment_index: int, start_offset_sec: float, rule_id: str = "C4", **fields) -> Finding:
        return (
            Finding(rule_id=rule_id, disposition=Disposition.CONFIRMED, **fields)
            .sanitise()
            .with_segment_context(segment_index=segment_index, start_offset_sec=start_offset_sec)
        )

    # (a) Same slice, same rule, same OSD second, different behaviour -> two events, both kept.
    # The NORMAL one comes first with the higher confidence, as in cc8307.
    no_hat = _in_slice(
        0, 0.0,
        confidence=0.95,
        severity="NORMAL",
        timestamp_in_clip="04:58",
        on_screen_clock="12:14:58",
        evidence="员工B在后厨备料区未佩戴工作帽，头发外露",
    )
    phone = _in_slice(
        0, 0.0,
        confidence=0.85,
        severity="RED_LINE",
        timestamp_in_clip="04:58",
        on_screen_clock="12:14:58",
        evidence="员工B戴着手套低头操作手机，随后未洗手直接返回吧台",
    )
    kept = deduplicate_overlapping_findings([no_hat, phone])
    assert len(kept) == 2
    assert [f.severity for f in kept] == [Severity.NORMAL, Severity.RED_LINE]
    assert "手机" in kept[1].evidence

    # Two people with the same violation in the same second: the texts differ only in the person
    # label (difflib ratio 0.857, which the Round 49 threshold of 0.85 merged) -> both kept.
    staff_a = _in_slice(
        0, 0.0,
        confidence=0.9,
        severity="NORMAL",
        timestamp_in_clip="03:20",
        on_screen_clock="12:13:20",
        evidence="员工A未戴帽子",
    )
    staff_b = staff_a.model_copy(update={"evidence": "员工B未戴帽子"})
    assert len(deduplicate_overlapping_findings([staff_a, staff_b])) == 2
    # Digits keep their separators: 1.5s and 15s of rubbing are different findings.
    rub_short = _in_slice(
        0, 0.0,
        rule_id="A3",
        confidence=0.9,
        severity="NORMAL",
        timestamp_in_clip="01:10",
        evidence="员工A按洗手液后搓手1.5秒即冲水",
    )
    rub_long = rub_short.model_copy(update={"evidence": "员工A按洗手液后搓手15秒即冲水"})
    assert len(deduplicate_overlapping_findings([rub_short, rub_long])) == 2

    # (b) Same slice, one event reported twice 3s apart; the texts differ only in whitespace and
    # punctuation -> merged into one. The RED_LINE copy is kept even though the NORMAL copy has the
    # higher confidence AND a cut evidence clip, whichever order the model listed them in.
    normal_copy = _in_slice(
        0, 0.0,
        confidence=0.95,
        severity="NORMAL",
        timestamp_in_clip="02:10",
        on_screen_clock="12:12:10",
        evidence="员工A 戴手套触摸手机后，未洗手直接接触杯盖。",
        evidence_clip_local_path="/tmp/c4_normal_copy.mp4",
    )
    red_copy = _in_slice(
        0, 0.0,
        confidence=0.60,
        severity="RED_LINE",
        timestamp_in_clip="02:13",
        on_screen_clock="12:12:13",
        evidence="员工A戴手套触摸手机后 未洗手直接接触杯盖",
    )
    for listed_order in ([normal_copy, red_copy], [red_copy, normal_copy]):
        merged = deduplicate_overlapping_findings(listed_order)
        assert len(merged) == 1
        assert merged[0].severity == Severity.RED_LINE
        assert merged[0].confidence == 0.60
        assert merged[0].evidence_clip_local_path is None
    # Full-width letters and punctuation fold to the same text.
    full_width = red_copy.model_copy(update={"evidence": "员工Ａ戴手套触摸手机后，未洗手直接接触杯盖！"})
    assert len(deduplicate_overlapping_findings([red_copy, full_width])) == 1

    # The 5s window still applies: the same text 6s later in the same slice is a second event
    # (e.g. two separate short handwashes).
    red_copy_6s_later = red_copy.model_copy(update={"global_offset_sec": red_copy.global_offset_sec + 6.0})
    assert len(deduplicate_overlapping_findings([red_copy, red_copy_6s_later])) == 2

    # Two findings without evidence text 2s apart in one slice are one event (the Round 49 code
    # reported both); an empty text never matches a non-empty one.
    blank = _in_slice(0, 0.0, confidence=0.70, severity="NORMAL", timestamp_in_clip="01:00", evidence="")
    blank_repeat = _in_slice(0, 0.0, confidence=0.80, severity="NORMAL", timestamp_in_clip="01:02", evidence="  ")
    merged_blank = deduplicate_overlapping_findings([blank, blank_repeat])
    assert len(merged_blank) == 1
    assert merged_blank[0].confidence == 0.80
    described = blank.model_copy(update={"evidence": "员工A未戴帽子"})
    assert len(deduplicate_overlapping_findings([blank, described])) == 2

    # (c) Across slices (slice 0 = 0..600s, slice 1 = 540..1140s) the two calls word the same moment
    # differently and label people independently, so the texts are not compared.
    assert CROSS_SLICE_OSD_TOLERANCE_SEC == 15.0
    overlap_s0 = _in_slice(
        0, 0.0,
        confidence=0.70,
        severity="NORMAL",
        timestamp_in_clip="09:40",
        on_screen_clock="12:14:40",
        evidence="员工C未戴发网，头发外露",
        evidence_clip_local_path="/tmp/c4_overlap_s0.mp4",
    )
    overlap_s1 = _in_slice(
        1, 540.0,
        confidence=0.97,
        severity="NORMAL",
        timestamp_in_clip="00:45",
        on_screen_clock="12:14:45",
        evidence="员工A头发外露且未戴帽",
    )
    for listed_order in ([overlap_s0, overlap_s1], [overlap_s1, overlap_s0]):
        merged = deduplicate_overlapping_findings(listed_order, overlap_window_sec=60.0)
        assert len(merged) == 1
        assert merged[0].evidence_clip_local_path == "/tmp/c4_overlap_s0.mp4"
    # A RED_LINE is never folded into a NORMAL (or the reverse) across slices.
    phone_s1 = _in_slice(
        1, 540.0,
        confidence=0.85,
        severity="RED_LINE",
        timestamp_in_clip="00:41",
        on_screen_clock="12:14:41",
        evidence="员工B戴手套看手机后未洗手返回吧台",
    )
    kept = deduplicate_overlapping_findings([overlap_s0, phone_s1], overlap_window_sec=60.0)
    assert [f.severity for f in kept] == [Severity.NORMAL, Severity.RED_LINE]
    # Readable OSD clocks 40s apart are two moments, although the 60s overlap window would allow a
    # merge; when one clock is unreadable, the 60s window decides, as before.
    early_s0 = _in_slice(
        0, 0.0,
        confidence=0.9,
        severity="NORMAL",
        timestamp_in_clip="09:05",
        on_screen_clock="12:14:05",
        evidence="员工C未戴发网",
    )
    late_s1 = _in_slice(
        1, 540.0,
        confidence=0.9,
        severity="NORMAL",
        timestamp_in_clip="00:45",
        on_screen_clock="12:14:45",
        evidence="员工A未戴帽子",
    )
    assert late_s1.global_offset_sec - early_s0.global_offset_sec == 40.0
    assert len(deduplicate_overlapping_findings([early_s0, late_s1], overlap_window_sec=60.0)) == 2
    unreadable = late_s1.model_copy(update={"on_screen_clock": "看不清"})
    assert len(deduplicate_overlapping_findings([early_s0, unreadable], overlap_window_sec=60.0)) == 1
    assert not _osd_clocks_disagree("12:14:30", "12:14:45")
    assert _osd_clocks_disagree("12:14:29", "12:14:45")
    assert not _osd_clocks_disagree("23:59:58", "00:00:03")
    assert not _osd_clocks_disagree("2026-09-12 12:14:05", "12:14:05 (左上角)")
    assert not _osd_clocks_disagree("12:14:05", "")

    # (d) One slice's two different findings never end up in one event through the other slice.
    # Slice 0 saw one no-hat event (员工C); slice 1 saw it again (its own label: 员工A) plus a second
    # person without a hat 3s later. In every listing order, and whichever copy has the clip, the
    # result is two events and the second person's row survives.
    first_s0 = _in_slice(
        0, 0.0,
        confidence=0.9,
        severity="NORMAL",
        timestamp_in_clip="09:40",
        on_screen_clock="12:14:40",
        evidence="员工C未戴发网，头发外露",
    )
    first_s1 = _in_slice(
        1, 540.0,
        confidence=0.9,
        severity="NORMAL",
        timestamp_in_clip="00:41",
        on_screen_clock="12:14:41",
        evidence="员工A头发外露且未戴帽",
    )
    second_s1 = _in_slice(
        1, 540.0,
        confidence=0.9,
        severity="NORMAL",
        timestamp_in_clip="00:44",
        on_screen_clock="12:14:44",
        evidence="员工B未戴帽子",
    )
    for with_clip in (first_s0, first_s1):
        variants = [
            f.model_copy(update={"evidence_clip_local_path": "/tmp/c4_clip.mp4"}) if f is with_clip else f
            for f in (first_s0, first_s1, second_s1)
        ]
        for listed_order in itertools.permutations(variants):
            result = deduplicate_overlapping_findings(list(listed_order), overlap_window_sec=60.0)
            assert len(result) == 2
            assert any(f.evidence == "员工B未戴帽子" for f in result)

    # (e) An empty rule id used to raise IndexError in dedup, after every slice had been analysed, so
    # the job failed without a report (again on each automatic resume). Empty ids now share one key.
    no_rule = _in_slice(
        0, 0.0,
        rule_id="  ",
        confidence=0.5,
        severity="NORMAL",
        timestamp_in_clip="03:20",
        evidence="规则编号缺失",
    )
    no_rule_repeat = no_rule.model_copy(update={"rule_id": ""})
    assert len(deduplicate_overlapping_findings([no_rule, staff_a, no_rule_repeat])) == 2


def test_agentic_media_processing_is_set_on_the_video_part() -> None:
    """TC-014 (Round 49/51): agentic video mode is `types.Part.media_processing` in google-genai 2.25.0.

    The old code did `setattr(gen_config, "media_processing", ...)` on `GenerateContentConfig`, which
    raises `ValueError: ... has no field "media_processing"`; the error was swallowed, so no production
    audit ever ran in agentic mode. Agentic is the default via `VIDEO_MEDIA_PROCESSING` (default
    "agentic"): Gemini 3.x gets AGENTIC on the video Part (gs:// URI and inline branches) by default;
    other models, or when the switch is set to "static", use static sampling.
    """
    import os
    import shutil
    import tempfile
    from pathlib import Path
    from types import SimpleNamespace
    from typing import Optional
    from unittest import mock

    from google.genai import types
    from pydantic import BaseModel, ValidationError

    from cctv_audit import agentic_auditor
    from cctv_audit.config import AuditConfig
    from cctv_audit.prompt_manager import PromptModelConfig
    from cctv_audit.video_ingestor import VideoSliceSegment

    calls: list = []

    async def _fake_generate_content_with_retry(*, model, contents, gen_config, **_kwargs):
        calls.append(SimpleNamespace(model=model, contents=contents, gen_config=gen_config))
        return SimpleNamespace(
            parsed=WindowResult(findings=[]),
            text="",
            usage_metadata=SimpleNamespace(
                prompt_token_count=100,
                thoughts_token_count=20,
                candidates_token_count=10,
                total_token_count=130,
            ),
        )

    tmp_dir = Path(tempfile.mkdtemp(prefix="chagee_agentic_part_"))
    try:
        inline_bytes = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64
        inline_clip = tmp_dir / "slice.mp4"
        inline_clip.write_bytes(inline_bytes)
        gcs_segment = VideoSliceSegment(
            source_file_id="vid_gcs",
            source_filename="Footage4.mov",
            segment_index=0,
            start_offset_sec=0.0,
            end_offset_sec=302.0,
            local_path=tmp_dir / "not_downloaded.mp4",
            gcs_uri="gs://bucket/jobs/media/job/vid_gcs/seg_0.mp4",
        )
        inline_segment = gcs_segment.model_copy(update={"gcs_uri": "", "local_path": inline_clip})
        runs = [
            # (VIDEO_MEDIA_PROCESSING switch, model, segment)
            ("agentic", "gemini-3.8-flash", gcs_segment),
            ("agentic", "gemini-3.8-flash", inline_segment),
            ("agentic", "gemini-2.5-flash", gcs_segment),
            ("agentic", "gemini-2.5-flash", inline_segment),
            ("static", "gemini-3.8-flash", gcs_segment),
            ("static", "gemini-3.8-flash", inline_segment),
        ]

        async def _run() -> None:
            auditor = agentic_auditor.AgenticAuditor()
            for mode, model, segment in runs:
                with mock.patch.object(agentic_auditor.config, "video_media_processing", mode):
                    await auditor.analyze_segment(
                        audit_id="r49",
                        folder_id="folder",
                        segment=segment,
                        prompt_cfg=PromptModelConfig(active_model_version=model, system_instruction="SYS"),
                    )

        with mock.patch.object(
            agentic_auditor, "generate_content_with_retry", _fake_generate_content_with_retry
        ):
            asyncio.run(_run())
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    assert [c.model for c in calls] == [model for _, model, _ in runs]
    gcs_3x, inline_3x, gcs_25, inline_25, gcs_3x_static, inline_3x_static = (c.contents[0] for c in calls)
    assert all(
        isinstance(p, types.Part)
        for p in (gcs_3x, inline_3x, gcs_25, inline_25, gcs_3x_static, inline_3x_static)
    )

    # Switch on (the default) + Gemini 3.x: AGENTIC on the Part, on both the gs:// and the inline-bytes branch.
    assert gcs_3x.file_data.file_uri == "gs://bucket/jobs/media/job/vid_gcs/seg_0.mp4"
    assert gcs_3x.media_processing == types.MediaProcessing.AGENTIC
    assert inline_3x.inline_data.data == inline_bytes
    assert inline_3x.media_processing == types.MediaProcessing.AGENTIC
    # Switch on + other models: left unset (static sampling).
    assert gcs_25.media_processing is None
    assert inline_25.media_processing is None
    # Switch "static": static sampling even for Gemini 3.x.
    assert gcs_3x_static.file_data.file_uri == "gs://bucket/jobs/media/job/vid_gcs/seg_0.mp4"
    assert gcs_3x_static.media_processing is None
    assert inline_3x_static.inline_data.data == inline_bytes
    assert inline_3x_static.media_processing is None

    # The config never carries the switch (it has no such field) and is identical for every call.
    assert "media_processing" not in types.GenerateContentConfig.model_fields
    for call in calls:
        assert "media_processing" not in call.gen_config.model_dump(exclude_none=True)
        assert call.gen_config.system_instruction == "SYS"
        assert call.gen_config.response_schema is WindowResult
    for other in (calls[2], calls[4]):
        assert calls[0].gen_config.model_dump(exclude={"response_schema"}) == other.gen_config.model_dump(
            exclude={"response_schema"}
        )

    # The switch defaults to "agentic", is read from VIDEO_MEDIA_PROCESSING (trimmed, any case), and a
    # typo fails at start-up instead of silently picking a mode.
    with mock.patch.dict(os.environ):
        os.environ.pop("VIDEO_MEDIA_PROCESSING", None)
        os.environ.pop("GEMINI_TIMEOUT_MS", None)
        cfg = AuditConfig()
        assert cfg.video_media_processing == "agentic"
        assert cfg.gemini_timeout_ms == 750_000
        # Per-call timeout = 2.5 x slice length, never below the config floor; the in-container
        # watchdog follows it (+120 s headroom).
        from cctv_audit import gcp as gcp_module
        with mock.patch.object(gcp_module.config, "gemini_timeout_ms", cfg.gemini_timeout_ms):
            assert gcp_module.gemini_timeout_ms_for_slice(300.0) == 750_000
            assert gcp_module.gemini_timeout_ms_for_slice(302.0) == 755_000
            assert gcp_module.gemini_timeout_ms_for_slice(120.0) == 750_000  # floor
            assert gcp_module.gemini_timeout_ms_for_slice(600.0) == 1_500_000
            assert gcp_module.gemini_timeout_ms_for_slice(0.0) == 750_000
            assert audit_service_module._slice_stall_timeout_sec() == 870.0
            assert audit_service_module._slice_stall_timeout_sec(300.0) == 870.0
            assert audit_service_module._slice_stall_timeout_sec(600.0) == 1620.0
    with mock.patch.dict(os.environ, {"VIDEO_MEDIA_PROCESSING": " Static "}):
        assert AuditConfig().video_media_processing == "static"
    with mock.patch.dict(os.environ, {"VIDEO_MEDIA_PROCESSING": " Agentic "}):
        assert AuditConfig().video_media_processing == "agentic"
    with mock.patch.dict(os.environ, {"VIDEO_MEDIA_PROCESSING": "agnetic"}):
        with unittest.TestCase().assertRaises(ValidationError):
            AuditConfig()
    # An explicit mode argument wins over the config (used by offline A/B harnesses).
    with mock.patch.object(agentic_auditor.config, "video_media_processing", "static"):
        assert agentic_auditor.wants_agentic_video("gemini-3.8-flash", "agentic") is True
        assert agentic_auditor.wants_agentic_video("gemini-3.8-flash") is False
    with mock.patch.object(agentic_auditor.config, "video_media_processing", "agentic"):
        assert agentic_auditor.wants_agentic_video("gemini-3.8-flash", "static") is False
        assert agentic_auditor.wants_agentic_video("gemini-3.8-flash") is True

    # With the pinned SDK the switch applies without a warning; an SDK whose Part lacks the field
    # gets a WARNING instead of a silent pass.
    with unittest.TestCase().assertNoLogs("cctv_audit.agentic_auditor", level="WARNING"):
        assert agentic_auditor.enable_agentic_media_processing(
            types.Part.from_uri(file_uri="gs://bucket/x.mp4", mime_type="video/mp4")
        ) is True

    class _PartFromOldSdk(BaseModel):
        file_data: Optional[types.FileData] = None

    old_sdk_part = _PartFromOldSdk()
    with unittest.TestCase().assertLogs("cctv_audit.agentic_auditor", level="WARNING") as captured:
        assert agentic_auditor.enable_agentic_media_processing(old_sdk_part) is False  # type: ignore[arg-type]
    assert "media_processing" in captured.output[0]
    assert not hasattr(old_sdk_part, "media_processing")

    # A Part class that is not a pydantic model at all (no `model_fields`) gets the same WARNING
    # instead of an AttributeError.
    class _PlainPart:
        pass

    plain_part = _PlainPart()
    with unittest.TestCase().assertLogs("cctv_audit.agentic_auditor", level="WARNING") as captured:
        assert agentic_auditor.enable_agentic_media_processing(plain_part) is False  # type: ignore[arg-type]
    assert "media_processing" in captured.output[0]
    assert not hasattr(plain_part, "media_processing")


def test_ledger_counts_tool_use_prompt_tokens_as_input() -> None:
    """TC-016 (Round 49b): agentic video mode bills the frames it pulls in as
    `tool_use_prompt_token_count` (198k-247k per 302 s slice in the Round 49 A/B, vs 9.6k prompt
    tokens). The Tab 2 ledger must count them as input tokens and price them, so its token columns
    add up to `total_token_count`; static-mode responses (field is None) are unchanged.
    """
    from pathlib import Path
    from types import SimpleNamespace
    from unittest import mock

    from cctv_audit import agentic_auditor
    from cctv_audit.config import config
    from cctv_audit.prompt_manager import PromptModelConfig
    from cctv_audit.video_ingestor import VideoSliceSegment

    usages = [
        # Round 49 r4-B (agentic), usage_metadata as returned by Vertex.
        SimpleNamespace(
            prompt_token_count=9_591,
            tool_use_prompt_token_count=225_724,
            thoughts_token_count=6_116,
            candidates_token_count=2_523,
            total_token_count=243_954,
        ),
        # Round 49 r2-A (static): the SDK reports tool_use_prompt_token_count=None.
        SimpleNamespace(
            prompt_token_count=29_286,
            tool_use_prompt_token_count=None,
            thoughts_token_count=3_596,
            candidates_token_count=1_001,
            total_token_count=33_883,
        ),
    ]
    pending = list(usages)

    async def _fake_generate_content_with_retry(*, model, contents, gen_config, **_kwargs):
        return SimpleNamespace(parsed=WindowResult(findings=[]), text="", usage_metadata=pending.pop(0))

    segment = VideoSliceSegment(
        source_file_id="vid_gcs",
        source_filename="Footage4.mov",
        segment_index=0,
        start_offset_sec=0.0,
        end_offset_sec=302.0,
        local_path=Path("/nonexistent/chagee_ledger_slice.mp4"),
        gcs_uri="gs://bucket/jobs/media/job/vid_gcs/seg_0.mp4",
    )

    async def _run() -> list:
        auditor = agentic_auditor.AgenticAuditor()
        rows = []
        for _ in usages:
            _, row = await auditor.analyze_segment(
                audit_id="r49b",
                folder_id="folder",
                segment=segment,
                prompt_cfg=PromptModelConfig(active_model_version="gemini-3.8-flash", system_instruction="SYS"),
            )
            rows.append(row)
        return rows

    with mock.patch.object(
        agentic_auditor, "generate_content_with_retry", _fake_generate_content_with_retry
    ):
        agentic_row, static_row = asyncio.run(_run())

    # The expected costs below are for the Gemini 3.8 Flash Standard Paid Tier rates ($0.75/M input, $3.75/M output).
    assert (config.input_cost_per_million_usd, config.output_cost_per_million_usd) == (0.75, 3.75)

    assert agentic_row.prompt_token_count == 9_591 + 225_724
    assert agentic_row.total_token_count == 243_954
    assert (
        agentic_row.prompt_token_count
        + agentic_row.thoughts_token_count
        + agentic_row.candidates_token_count
        == agentic_row.total_token_count
    )
    # 235,315 input x $0.75/M + 8,639 output x $3.75/M = 0.17648625 + 0.03239625 = $0.2089.
    assert agentic_row.estimated_cost_usd == 0.2089

    assert static_row.prompt_token_count == 29_286
    assert static_row.total_token_count == 33_883
    # 29,286 x $0.75/M + 4,597 x $3.75/M = 0.0219645 + 0.01723875 = $0.0392.
    assert static_row.estimated_cost_usd == 0.0392


def test_full_v10_and_chagee_v2_yaml_prompt_loading() -> None:
    """TC-008: Verifies the 24-section SOP matrix (A1-A5, B0-B5, C1-C8, D1-D4) and Step 0-4 rules."""
    from cctv_audit.prompt_manager import load_rules_from_yaml

    scan_targets, rules = load_rules_from_yaml()
    assert len(scan_targets) == 10
    assert len(rules) == 24
    rule_ids = {r.rule_id for r in rules}
    for expected_id in (
        "A1", "A2", "A3", "A4", "A5", "B0", "B1-1", "B1-2", "B2", "B3", "B4", "B5", "C1", "C7", "C8", "D1", "D2", "D4",
    ):
        assert expected_id in rule_ids
    assert len(rule_ids) == len(rules), "rule ids must be unique"

    pm = PromptManager(sheet_client=None)
    cfg = asyncio.run(pm.load_active_config(sheet_id=""))
    assert len(cfg.rules) == 24
    assert "疑似接触不再豁免" in cfg.system_instruction
    assert "Partner enter work, on the switch" in cfg.system_instruction
    assert "B1-1 · Langtuo 完整流程核对（15 项，SOP 21）" in cfg.system_instruction
    assert "interior surface of ice maker machine grid" in cfg.system_instruction
    assert "side grid" in cfg.system_instruction
    assert "Pour sanitiser solution into water trough" in cfg.system_instruction
    assert "Close back ice maker machine" in cfg.system_instruction
    assert "B1-2 · Manitowoc 完整流程核对（13 项，SOP 8）" in cfg.system_instruction
    assert "官方 SOP 两大制冰机机型结构与可拆部件视觉锚定" in cfg.system_instruction
    assert "可拆部件「四周边框与侧面厚度边缘」全覆盖原则（Wipe the sides and edges thoroughly）" in cfg.system_instruction
    assert "毛巾取用起点逆向溯源原则" in cfg.system_instruction
    assert "步骤先后时序与首尾 3 秒慢放核查" in cfg.system_instruction
    assert "D2 · IPLH 计算（Week 3 必交）" in cfg.system_instruction
    assert "跨切片/跨视频状态接力校准" in cfg.system_instruction
    assert "多人同屏注意力解耦（Anti-Attention-Masking）" in cfg.system_instruction
    assert "多人并发与同秒掩蔽回扫（关键）" in cfg.system_instruction
    assert "原则六：一行为一记录与复合违规正交拆解（严禁高显著性事件吞并伴随违规）" in cfg.system_instruction
    assert "单事件吸干与多维正交漏报回扫（关键）" in cfg.system_instruction
    assert "spit guard" in cfg.system_instruction
    assert "apply soap before wet hand" in cfg.system_instruction
    assert "top surface of the ice maker" in cfg.system_instruction
    assert "water distribution tube" in cfg.system_instruction
    assert "yellow bottle" in cfg.system_instruction
    assert "apron not kept at designated storage area" in cfg.system_instruction
    assert "步骤 4：漏检自查（强制执行，不可跳过）" in cfg.system_instruction

    # C8 = customer SOP 6.2 "Filter Tea into an Ice-Prepared Container" (the HC5 standard).
    c8 = next(r for r in rules if r.rule_id == "C8")
    assert c8.category == next(r for r in rules if r.rule_id == "C7").category  # same 【C】 block, runs every week
    assert c8.severity == "NORMAL"
    # The customer's two sentences, verbatim.
    assert (
        "Pour the filtered tea into a container with ice, prepared 2-3 minutes beforehand "
        "(but not more than 3 minutes in advance)."
    ) in c8.check_instruction
    assert (
        "Stir the tea within 30 seconds of pouring to ensure even cooling. "
        "(30s from the machine prompt: “Tea is ready” / dripping stage)"
    ) in c8.check_instruction
    # Thresholds and the timer start point must survive into the rendered prompt.
    assert "超过 3 分钟" in c8.check_instruction
    assert "30 秒内搅拌" in c8.check_instruction
    assert "滴滤（dripping）阶段" in c8.check_instruction
    # The video is silent, so the start point must be tied to something visible, not the voice prompt.
    assert "视频没有声音" in c8.check_instruction
    assert "出茶口的茶汤从连续流出变成滴落" in c8.check_instruction
    # Ice added / tea ready before the clip starts and not in the carryover: can't time it and can't see an
    # earlier stir, so it must be OUT_OF_SCOPE (not in the violation table), never a guessed violation.
    assert c8.check_instruction.count("`OUT_OF_SCOPE`（不进违规表）") == 2
    assert "已出茶还没搅拌）写进接力摘要" in c8.check_instruction
    assert c8.pass_criteria and c8.fail_criteria

    sys_text = cfg.system_instruction
    c7_at = sys_text.index("### C7 · 顾客服务（9.x）")
    c8_at = sys_text.index("### C8 · 泡茶机出茶入冰与 30 秒内搅拌（6.2 Automated Tea Maker Machine）")
    d_block_at = sys_text.index("## 【D】运营与人效（Week 3 主模块）")
    assert c7_at < c8_at < d_block_at, "C8 must render inside the 【C】 block (explicit status required)"
    assert sys_text.count("## 【C】常态红线扫描（所有周次都跑）") == 1, "C8 must not open a second 【C】 heading"
    # C6 still covers the tea maker in general but hands the timing checks to C8 (no double report).
    c6 = next(r for r in rules if r.rule_id == "C6")
    assert "单独按 C8 核查，不在 C6 重复报" in c6.check_instruction


def test_sop_sheet_url_extraction_and_hot_switch() -> None:
    """TC-008: Verifies Terraform / Cloud Run env MASTER_PROMPT_SHEET_ID URL/ID normalization (immutable at runtime)."""
    from cctv_audit.config import AuditConfig, extract_spreadsheet_id

    sheet_id = "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789_-abcd"
    raw_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit#gid=0"
    assert extract_spreadsheet_id(raw_url) == sheet_id

    cfg = AuditConfig(master_prompt_sheet_id=raw_url)
    assert cfg.master_prompt_sheet_id == sheet_id

    # Multi-account URL form (`/u/<N>/d/`) that Google emits when signed into several accounts.
    multi_account_url = f"https://docs.google.com/spreadsheets/u/1/d/{sheet_id}/edit?usp=sharing"
    assert extract_spreadsheet_id(multi_account_url) == sheet_id
    assert AuditConfig(master_prompt_sheet_id=multi_account_url).master_prompt_sheet_id == sheet_id

    # A bare opaque token is passed through untouched (no scheme, no path separator).
    assert extract_spreadsheet_id("  sheet_master  ") == "sheet_master"

    # A URL-shaped value we cannot parse (e.g. a Drive *folder* link) must fail fast at
    # config construction, never reach the Sheets API, and never silently degrade the prompt.
    folder_url = "https://drive.google.com/drive/folders/1yJW83oOINAdFcWEIIOwmFAFiy2Sr2fLE"
    for bad_value in (folder_url, ""):
        try:
            extract_spreadsheet_id(bad_value)
        except ValueError:
            pass
        else:
            raise AssertionError(f"expected ValueError for {bad_value!r}")


def test_ge_a2a_and_adk_wire_protocols_and_is_busy_probe() -> None:
    """TC-009: Verifies both Gemini Enterprise invocation wire protocols (Cloud Run A2A + ReasoningEngine ADK + /is_busy)."""
    import json
    from unittest.mock import AsyncMock, patch
    import cctv_audit.server as srv

    class _FakeRequest:
        def __init__(
            self,
            base_url: str,
            payload: dict | None = None,
            headers: dict | None = None,
            raise_on_json: bool = False,
        ) -> None:
            self.base_url = base_url
            self._payload = payload if payload is not None else {}
            self.headers = headers or {}
            self._raise_on_json = raise_on_json

        async def json(self):
            if self._raise_on_json:
                raise ValueError("Expecting value: line 1 column 1 (char 0)")
            return self._payload

    def _send(text: str = "查询当前稽核状态", **metadata) -> dict:
        return {
            "jsonrpc": "2.0",
            "id": "req-ge-a2a-001",
            "method": "message/send",
            "params": {
                "message": {
                    "messageId": "m-1",
                    "role": "user",
                    "parts": [{"kind": "text", "text": text}],
                },
                "metadata": dict(metadata),
            },
        }

    async def _run_checks() -> None:
        mock_decision = TurnDecision(
            action=TurnAction.STATUS,
            reply_summary="查询当前稽核状态",
        )
        with patch.object(srv, "classify_turn_with_llm", new=AsyncMock(return_value=mock_decision)):
            # 1. GET /.well-known/agent-card.json (A2A discovery on Cloud Run)
            card = await srv.get_agent_card(
                _FakeRequest("https://my-stack-worker-xxx.a.run.app/")  # type: ignore[arg-type]
            )
            assert card["url"] == "https://my-stack-worker-xxx.a.run.app/a2a"
            assert any(s["id"] == "chagee-store-cctv-audit" for s in card["skills"])
            # The card must not advertise a streaming capability the /a2a handler does not serve.
            assert card["capabilities"]["streaming"] is False
            assert "message/stream" not in srv.SUPPORTED_A2A_METHODS

            # 2. POST /a2a (JSON-RPC 2.0 message/send used by Gemini Enterprise a2aAgentDefinition)
            a2a_body = await srv.a2a_jsonrpc_endpoint(
                _FakeRequest(  # type: ignore[arg-type]
                    "https://my-stack-worker-xxx.a.run.app/",
                    _send(user_email="auditor_sg@chagee.com"),
                )
            )
            assert a2a_body["jsonrpc"] == "2.0"
            assert a2a_body["id"] == "req-ge-a2a-001"
            assert a2a_body["result"]["status"]["state"] == "completed"
            assert "暂无正在运行或已完成的稽核任务" in a2a_body["result"]["status"]["message"]["parts"][0]["text"]

            # 3. POST /api/stream_reasoning_engine (double-encoded request_json used by GE Console Agents dropdown -> ReasoningEngine)
            adk_req = srv.StreamReasoningRequest(
                input={
                    "request_json": json.dumps(
                        {
                            "user_id": "auditor_sg@chagee.com",
                            "session_id": "sess-001",
                            "message": {"role": "user", "parts": [{"text": "查询当前稽核状态"}]},
                            "events": [
                                {
                                    "author": "user",
                                    "content": {"parts": [{"text": "之前发过文件夹链接"}]},
                                }
                            ],
                        },
                        ensure_ascii=False,
                    )
                }
            )
            stream_resp = await srv.ge_stream_endpoint(adk_req)
            assert stream_resp.media_type == "application/json"
            chunks = [chunk async for chunk in stream_resp.body_iterator]
            adk_envelope = json.loads(b"".join(chunks).decode("utf-8").strip().splitlines()[0])
            assert adk_envelope["session_id"] == "sess-001"
            assert adk_envelope["events"][0]["content"]["role"] == "model"
            assert "暂无正在运行或已完成的稽核任务" in adk_envelope["events"][0]["content"]["parts"][0]["text"]

            # 4. GET /is_busy keepAliveProbe when idle
            idle_busy = await srv.is_busy()
            assert idle_busy["busy"] is False
            assert idle_busy["active_jobs"] == 0

            # 5. Unsupported JSON-RPC methods must NOT be coerced into a live (billable) audit turn.
            for bad_method in ("tasks/cancel", "message/stream", "", "notifications/subscribe"):
                req = _send()
                req["method"] = bad_method
                err = await srv.a2a_jsonrpc_endpoint(
                    _FakeRequest("https://x.a.run.app/", req)  # type: ignore[arg-type]
                )
                assert "result" not in err, bad_method
                assert err["error"]["code"] == srv.JSONRPC_METHOD_NOT_FOUND, bad_method
                assert err["id"] == "req-ge-a2a-001"

            # 6. Malformed / non-object envelopes become JSON-RPC errors, never an HTTP 500.
            parse_err = await srv.a2a_jsonrpc_endpoint(
                _FakeRequest("https://x.a.run.app/", raise_on_json=True)  # type: ignore[arg-type]
            )
            assert parse_err["error"]["code"] == srv.JSONRPC_PARSE_ERROR
            batch_err = await srv.a2a_jsonrpc_endpoint(
                _FakeRequest("https://x.a.run.app/", [_send()])  # type: ignore[arg-type]
            )
            assert batch_err["error"]["code"] == srv.JSONRPC_INVALID_REQUEST

            # 7. REQ-008: the un-forgeable Cloud Run IAM / IAP header outranks the request body.
            assert srv.extract_proxy_asserted_user(
                _FakeRequest(  # type: ignore[arg-type]
                    "https://x.a.run.app/",
                    headers={"X-Goog-Authenticated-User-Email": "accounts.google.com:real@chagee.com"},
                )
            ) == ("real@chagee.com", "")
            assert srv.resolve_tenant_identity(
                proxy_email="real@chagee.com", body_identity="victim@chagee.com"
            ) == "real@chagee.com"

            # ...but the Discovery Engine *service agent* is infrastructure, not a supervisor:
            # adopting it would collapse every tenant into one bucket.
            assert srv.extract_proxy_asserted_user(
                _FakeRequest(  # type: ignore[arg-type]
                    "https://x.a.run.app/",
                    headers={
                        "x-goog-authenticated-user-email": (
                            "accounts.google.com:service-123@gcp-sa-discoveryengine.iam.gserviceaccount.com"
                        )
                    },
                )
            ) == ("", "")
            assert srv.resolve_tenant_identity(body_identity="auditor_sg@chagee.com") == "auditor_sg@chagee.com"
            assert srv.resolve_tenant_identity(proxy_user_id="1234567890") == "1234567890"
            assert srv.resolve_tenant_identity() == srv.DEFAULT_TENANT_FALLBACK

        # 8. The supervisor-facing guidance raised by `start_audit` must reach the supervisor as a
        #    reply, not escape as an HTTP 500 / truncated NDJSON stream.
        confirm_decision = TurnDecision(action=TurnAction.CONFIRM, reply_summary="确认稽核")
        with patch.object(srv, "classify_turn_with_llm", new=AsyncMock(return_value=confirm_decision)):
            confirm_body = await srv.a2a_jsonrpc_endpoint(
                _FakeRequest(  # type: ignore[arg-type]
                    "https://x.a.run.app/",
                    _send("确认，开始稽核", user_email="nobody_pending@chagee.com"),
                )
            )
            assert confirm_body["result"]["status"]["state"] == "completed"
            assert "未找到待确认的预检任务" in confirm_body["result"]["status"]["message"]["parts"][0]["text"]

        # 9. TurnAction.STATUS ("好了么") when a job with preflight_report exists (RUNNING, FAILED, DONE)
        #    must never raise AttributeError on preflight_report.planned_segments_count / total_segments.
        from cctv_audit.jobs import AuditJob, JobState
        from cctv_audit.video_ingestor import InspectFolderResponse, VideoMetadataItem as _VMI

        preflight_rep = InspectFolderResponse(
            passed=True,
            folder_id="1--hbVk8xE5i-aZrph7Pgy4vZwZdp9-NJ",
            total_videos=2,
            total_size_mb=950.0,
            total_duration_sec=4170.0,
            planned_segments_count=8,
            estimated_tokens=816000,
            estimated_cost_usd=1.25,
            estimated_minutes=8,
            message_to_user="ok",
            videos=[
                _VMI(file_id="l1", filename="long1.mov", width=1920, height=1080, duration_sec=2085.0),
                _VMI(file_id="l2", filename="long2.mov", width=1920, height=1080, duration_sec=2085.0),
            ],
        )
        assert preflight_rep.total_segments == 8
        # 5 x 5-min clips: nothing is sliced, so the counter unit must be "段视频", never "分钟切片".
        short_rep = preflight_rep.model_copy(
            update={
                "total_videos": 5,
                "planned_segments_count": 5,
                "total_duration_sec": 1508.0,
                "videos": [
                    _VMI(file_id=f"s{i}", filename=f"F{i}.mov", width=1920, height=1080, duration_sec=302.0)
                    for i in range(5)
                ],
            }
        )
        for job_state, rep, expected_marker in (
            (JobState.RUNNING, preflight_rep, "已完成 `0/8` 个分片"),
            (JobState.FAILED, preflight_rep, "执行遇到异常（已完成 `0/8` 个分片）"),
            (JobState.DONE, preflight_rep, "任务 `275db5` 已完成！"),
            (JobState.RUNNING, short_rep, "已完成 `0/5` 段视频"),
            (JobState.FAILED, short_rep, "执行遇到异常（已完成 `0/5` 段视频）"),
            (JobState.DONE, short_rep, "已完成：`0/5` 段视频"),
        ):
            fake_job = AuditJob(
                job_id="275db5",
                user_id="auditor_sg@chagee.com",
                session_id="sess-001",
                folder_id="1--hbVk8xE5i-aZrph7Pgy4vZwZdp9-NJ",
                state=job_state,
                preflight_report=rep,
                completed_segments={},
                violations_found=19,
                total_tokens_used=812345,
                report_sheet_url="https://docs.google.com/spreadsheets/d/sheet_275db5/edit",
                error_message="transient err" if job_state == JobState.FAILED else None,
            )
            with (
                patch.object(srv, "classify_turn_with_llm", new=AsyncMock(return_value=mock_decision)),
                patch.object(srv.audit_service, "get_status", new=AsyncMock(return_value=fake_job)),
            ):
                status_resp = await srv.ge_stream_endpoint(adk_req)
                status_chunks = [c async for c in status_resp.body_iterator]
                status_env = json.loads(b"".join(status_chunks).decode("utf-8").strip().splitlines()[0])
                status_text = status_env["events"][0]["content"]["parts"][0]["text"]
                assert expected_marker in status_text, (job_state, status_text)
                assert "分钟切片" not in status_text, (job_state, status_text)

    asyncio.run(_run_checks())


def test_zero_db_cross_instance_job_store_continuity() -> None:
    """TC-010: Verifies Zero-DB write-through persistence across ReasoningEngine instances (state_dir + GCS)."""
    from pathlib import Path
    import shutil
    import tempfile
    from cctv_audit.jobs import AuditJob, JobState, UserScopedJobStore

    tmp_dir = Path(tempfile.mkdtemp(prefix="chagee_zero_db_store_"))
    try:
        shared_gcs_store: dict[str, str] = {}

        async def _run() -> None:
            # Instance 1 handles Turn 1 (preflight -> READY)
            instance_1 = UserScopedJobStore(
                state_dir=tmp_dir / "instance_1_disk",
                gcs_bucket="my-project-my-stack-staging",
                gcs_store=shared_gcs_store,
            )
            job_turn1 = AuditJob(
                job_id="job_cross_instance_01",
                user_id="auditor_vn@chagee.com",
                session_id="sess-vn-01",
                folder_id="1--hbVk8xE5i-aZrph7Pgy4vZwZdp9-NJ",
                state=JobState.READY,
            )
            await instance_1.save(job_turn1)

            # Instance 2 (completely separate container disk & empty memory) handles Turn 2 ("确认" -> latest_ready_job)
            instance_2 = UserScopedJobStore(
                state_dir=tmp_dir / "instance_2_disk",
                gcs_bucket="my-project-my-stack-staging",
                gcs_store=shared_gcs_store,
            )
            rehydrated = await instance_2.latest_ready_job("auditor_vn@chagee.com", session_id="sess-vn-01")
            assert rehydrated is not None
            assert rehydrated.job_id == "job_cross_instance_01"
            assert rehydrated.state == JobState.READY
            assert rehydrated.folder_id == "1--hbVk8xE5i-aZrph7Pgy4vZwZdp9-NJ"

        asyncio.run(_run())
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_tier1_tier2_crash_checkpoint_and_resume_zero_duplicate_tokens() -> None:
    """TC-011: Verifies that if Tier-2 crashes mid-job (e.g. after completing 2 of 3 slices) and both
    Tier-1 and Tier-2 restart on brand-new instances, resuming the job from GCS skips the 2 completed
    slices (0 duplicate token spend), preserves cross-slice carryover state, and finishes Slice 2.
    """
    from pathlib import Path
    import shutil
    import tempfile
    from cctv_audit.agentic_auditor import (
        Disposition,
        Finding,
        TokenLedgerRow,
        WindowResult,
    )
    from cctv_audit.audit_service import AuditService
    from cctv_audit.jobs import JobState, UserScopedJobStore
    from cctv_audit.prompt_manager import PromptModelConfig
    from cctv_audit.video_ingestor import VideoIngestor, VideoMetadataItem
    from cctv_audit.workspace_reporter import WorkspaceReporter

    tmp_dir = Path(tempfile.mkdtemp(prefix="chagee_crash_resume_"))
    try:
        shared_gcs_store: dict[str, str] = {}
        media_file = tmp_dir / "long_store_video.mp4"
        media_file.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 1024)

        executed_segments: list[int] = []
        seen_carryovers: dict[int, str] = {}

        class _CrashOnSlice2ThenSucceedAuditor:
            def __init__(self, should_crash_on_slice_2: bool) -> None:
                self.should_crash_on_slice_2 = should_crash_on_slice_2

            async def analyze_segment(
                self,
                *,
                audit_id: str,
                folder_id: str,
                segment,
                prompt_cfg,
                prior_carryover_summary: str = "",
                evidence_dir=None,
            ):
                idx = segment.segment_index
                executed_segments.append(idx)
                seen_carryovers[idx] = prior_carryover_summary
                if idx == 2 and self.should_crash_on_slice_2:
                    raise RuntimeError("Simulated Tier-2 Cloud Run container OOM / preemption on slice 2")
                finding = Finding(
                    rule_id=f"A{idx + 1}",
                    disposition=Disposition.CONFIRMED,
                    severity="红线",
                    timestamp_in_clip="05:00",
                    global_offset_sec=float(segment.start_offset_sec + 300),
                    evidence=f"Slice {idx} finding",
                    evidence_drive_url=f"https://drive.google.com/file/d/ev_{idx}/view",
                )
                win_res = WindowResult(
                    findings=[finding],
                    carryover_state_summary=f"Carryover after slice {idx}",
                )
                ledger = TokenLedgerRow(
                    audit_id=audit_id,
                    auditor_folder_id=folder_id,
                    video_filename=segment.source_filename,
                    video_duration_sec=segment.duration_sec,
                    resolution="1920x1080",
                    model_version_used=prompt_cfg.active_model_version,
                    prompt_version_used=prompt_cfg.active_prompt_version,
                    prompt_token_count=100000,
                    thoughts_token_count=10000,
                    candidates_token_count=2000,
                    total_token_count=112000,
                    estimated_cost_usd=0.08,
                    e2e_latency_ms=15000,
                    flagged_events_count=1,
                )
                return win_res, ledger

        class _FastPromptManager:
            async def load_active_config(self, sheet_id=None) -> PromptModelConfig:
                return PromptModelConfig(
                    active_model_version="gemini-1.5-flash-002",
                    active_prompt_version="Prompt_v1.0_基准73%版",
                    system_instruction="CHAGEE SOP V10 Test",
                )

        async def _run() -> None:
            # 1. Worker 1 starts a 1500s (25-min -> 3 slices: 0..600, 540..1140, 1080..1500) video and crashes on slice 2
            store_1 = UserScopedJobStore(
                state_dir=tmp_dir / "worker1_disk",
                gcs_bucket=LOCAL_PLACEHOLDER_BUCKET,
                gcs_store=shared_gcs_store,
            )
            svc_1 = AuditService(
                job_store=store_1,
                ingestor=VideoIngestor(),
                prompt_manager=_FastPromptManager(),  # type: ignore[arg-type]
                auditor=_CrashOnSlice2ThenSucceedAuditor(should_crash_on_slice_2=True),  # type: ignore[arg-type]
                reporter=WorkspaceReporter(),
            )
            item = VideoMetadataItem(
                file_id="vid_3slices",
                filename="long_store_video.mp4",
                width=1920,
                height=1080,
                duration_sec=1500.0,
                local_path=media_file,
            )
            job = await svc_1.preflight(
                user_id="auditor_crash@chagee.com",
                drive_url="https://drive.google.com/drive/folders/1--hbVk8xE5i-aZrph7Pgy4vZwZdp9-NJ",
                preloaded_items=[item],
            )
            crashed_job = await svc_1.start_audit(
                user_id="auditor_crash@chagee.com",
                job_id=job.job_id,
                wait_for_completion=True,
            )
            assert crashed_job.state == JobState.FAILED
            assert len(crashed_job.completed_segments) == 2
            assert executed_segments == [0, 1, 2]

            # 2. Simulate BOTH Tier-1 and Tier-2 restarting on brand-new instances (empty local disk, reading from GCS)
            store_2 = UserScopedJobStore(
                state_dir=tmp_dir / "worker2_fresh_disk",
                gcs_bucket=LOCAL_PLACEHOLDER_BUCKET,
                gcs_store=shared_gcs_store,
            )
            svc_2 = AuditService(
                job_store=store_2,
                ingestor=VideoIngestor(),
                prompt_manager=_FastPromptManager(),  # type: ignore[arg-type]
                auditor=_CrashOnSlice2ThenSucceedAuditor(should_crash_on_slice_2=False),  # type: ignore[arg-type]
                reporter=WorkspaceReporter(),
            )
            resumed_job = await svc_2.start_audit(
                user_id="auditor_crash@chagee.com",
                job_id=job.job_id,
                wait_for_completion=True,
            )
            assert resumed_job.state == JobState.DONE
            assert resumed_job.resume_count == 1
            assert len(resumed_job.completed_segments) == 3
            # Verify slices 0 and 1 were NOT re-executed on the resumed worker!
            assert executed_segments == [0, 1, 2, 2]
            # Verify slice 2 received the exact carryover summary persisted from slice 1!
            assert seen_carryovers[2] == "Carryover after slice 1"
            assert resumed_job.total_tokens_used == 112000 * 3
            assert resumed_job.violations_found == 3

        asyncio.run(_run())
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_unattended_watchdog_auto_resumes_crashed_tier2_after_20_min_without_user_action() -> None:
    """TC-012: Simulates the exact operational scenario where the supervisor sends '确认开始' at t=0
    and closes the browser. At t=1200s (20 minutes later), Tier-2 has finished Slice 0 & Slice 1 and
    is hard-killed (SIGKILL / OOM / host preemption) while still marked `JobState.RUNNING` (or `FAILED`)
    in GCS. With ZERO user action in GE, the unattended Watchdog (`sweep_and_resume_stale_jobs` via
    Cloud Scheduler `POST /internal/jobs/sweep` / Tier-1 loop) detects the stale `heartbeat_at` (>180s)
    and automatically resumes & finishes Slice 2 on a healthy Tier-2 instance with 0 duplicate tokens.
    """
    from pathlib import Path
    import shutil
    import tempfile
    from cctv_audit.agentic_auditor import (
        Disposition,
        Finding,
        TokenLedgerRow,
        WindowResult,
    )
    from cctv_audit.audit_service import AuditService
    from cctv_audit.jobs import JobState, SegmentCheckpoint, UserScopedJobStore
    from cctv_audit.prompt_manager import PromptModelConfig
    from cctv_audit.video_ingestor import VideoIngestor, VideoMetadataItem
    from cctv_audit.workspace_reporter import WorkspaceReporter

    tmp_dir = Path(tempfile.mkdtemp(prefix="chagee_watchdog_20min_"))
    try:
        shared_gcs_store: dict[str, str] = {}
        media_file = tmp_dir / "kitchen_25min.mp4"
        media_file.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 1024)
        executed_on_resumed_worker: list[int] = []
        carryover_received: dict[int, str] = {}

        class _FastPromptManager:
            async def load_active_config(self, sheet_id=None) -> PromptModelConfig:
                return PromptModelConfig(
                    active_model_version="gemini-1.5-flash-002",
                    active_prompt_version="Prompt_v1.0_基准73%版",
                    system_instruction="CHAGEE SOP V10 Watchdog Test",
                )

        class _HealthyResumedAuditor:
            async def analyze_segment(
                self,
                *,
                audit_id: str,
                folder_id: str,
                segment,
                prompt_cfg,
                prior_carryover_summary: str = "",
                evidence_dir=None,
            ):
                idx = segment.segment_index
                executed_on_resumed_worker.append(idx)
                carryover_received[idx] = prior_carryover_summary
                finding = Finding(
                    rule_id=f"B{idx + 1}",
                    disposition=Disposition.CONFIRMED,
                    severity="红线",
                    timestamp_in_clip="03:00",
                    global_offset_sec=float(segment.start_offset_sec + 180),
                    evidence=f"Watchdog resumed slice {idx} finding",
                    evidence_drive_url=f"https://drive.google.com/file/d/wd_{idx}/view",
                )
                win_res = WindowResult(
                    findings=[finding],
                    carryover_state_summary=f"Carryover after slice {idx}",
                )
                ledger = TokenLedgerRow(
                    audit_id=audit_id,
                    auditor_folder_id=folder_id,
                    video_filename=segment.source_filename,
                    video_duration_sec=segment.duration_sec,
                    resolution="1920x1080",
                    model_version_used=prompt_cfg.active_model_version,
                    prompt_version_used=prompt_cfg.active_prompt_version,
                    total_token_count=90000,
                )
                return win_res, ledger

        async def _run() -> None:
            # 1. At t=1200s (20 minutes after confirmation), Worker 1 finished Slice 0 and Slice 1
            #    and then suffered a hard SIGKILL / host preemption while `state=JobState.RUNNING`
            #    with `heartbeat_at=1200.0` frozen in GCS.
            store_1 = UserScopedJobStore(
                state_dir=tmp_dir / "dead_worker_disk",
                gcs_bucket=LOCAL_PLACEHOLDER_BUCKET,
                gcs_store=shared_gcs_store,
            )
            svc_1 = AuditService(
                job_store=store_1,
                ingestor=VideoIngestor(),
                prompt_manager=_FastPromptManager(),  # type: ignore[arg-type]
                auditor=_HealthyResumedAuditor(),  # type: ignore[arg-type]
                reporter=WorkspaceReporter(),
            )
            item = VideoMetadataItem(
                file_id="vid_watchdog_01",
                filename="kitchen_25min.mp4",
                width=1920,
                height=1080,
                duration_sec=1500.0,
                local_path=media_file,
            )
            job = await svc_1.preflight(
                user_id="supervisor_offline@chagee.com",
                drive_url="https://drive.google.com/drive/folders/1--hbVk8xE5i-aZrph7Pgy4vZwZdp9-NJ",
                preloaded_items=[item],
            )
            ckpt_0 = SegmentCheckpoint(
                file_id="vid_watchdog_01",
                filename="kitchen_25min.mp4",
                segment_index=0,
                carryover_state_summary="Carryover after slice 0",
                findings=[
                    Finding(
                        rule_id="B1",
                        disposition=Disposition.CONFIRMED,
                        severity="红线",
                        timestamp_in_clip="02:00",
                        global_offset_sec=120.0,
                        evidence="Slice 0 finding",
                    )
                ],
                ledger_row=TokenLedgerRow(
                    audit_id=job.job_id,
                    auditor_folder_id=job.folder_id,
                    video_filename="kitchen_25min.mp4",
                    video_duration_sec=600.0,
                    resolution="1920x1080",
                    model_version_used="gemini-1.5-flash-002",
                    prompt_version_used="Prompt_v1.0_基准73%版",
                    total_token_count=90000,
                ),
            )
            ckpt_1 = SegmentCheckpoint(
                file_id="vid_watchdog_01",
                filename="kitchen_25min.mp4",
                segment_index=1,
                carryover_state_summary="Carryover after slice 1 (minute 20)",
                findings=[
                    Finding(
                        rule_id="B2",
                        disposition=Disposition.CONFIRMED,
                        severity="红线",
                        timestamp_in_clip="04:00",
                        global_offset_sec=780.0,
                        evidence="Slice 1 finding",
                    )
                ],
                ledger_row=TokenLedgerRow(
                    audit_id=job.job_id,
                    auditor_folder_id=job.folder_id,
                    video_filename="kitchen_25min.mp4",
                    video_duration_sec=600.0,
                    resolution="1920x1080",
                    model_version_used="gemini-1.5-flash-002",
                    prompt_version_used="Prompt_v1.0_基准73%版",
                    total_token_count=90000,
                ),
            )
            stale_running_job = await store_1.save(
                job.model_copy(
                    update={
                        "state": JobState.RUNNING,
                        "completed_segments": {
                            "vid_watchdog_01:0": ckpt_0,
                            "vid_watchdog_01:1": ckpt_1,
                        },
                        "total_tokens_used": 180000,
                    }
                )
            )

            # 2. 3+ minutes after the 20-minute crash (`now = stale_running_job.heartbeat_at + 195.0s`),
            #    WITH ZERO USER ACTION IN GE, Cloud Scheduler / Tier-1 Watchdog calls
            #    `sweep_and_resume_stale_jobs()` on a brand-new Tier-2 instance (`store_watchdog`).
            store_watchdog = UserScopedJobStore(
                state_dir=tmp_dir / "watchdog_worker_disk",
                gcs_bucket=LOCAL_PLACEHOLDER_BUCKET,
                gcs_store=shared_gcs_store,
            )
            svc_watchdog = AuditService(
                job_store=store_watchdog,
                ingestor=VideoIngestor(),
                prompt_manager=_FastPromptManager(),  # type: ignore[arg-type]
                auditor=_HealthyResumedAuditor(),  # type: ignore[arg-type]
                reporter=WorkspaceReporter(),
            )
            resumed_jobs = await svc_watchdog.sweep_and_resume_stale_jobs(
                stale_timeout_sec=180.0,
                now=stale_running_job.heartbeat_at + 195.0,
                wait_for_completion=True,
            )
            assert len(resumed_jobs) == 1
            final_job = resumed_jobs[0]
            assert final_job.state == JobState.DONE
            assert final_job.resume_count == 1
            assert len(final_job.completed_segments) == 3
            # Only slice 2 was executed on the resumed worker (Slices 0 and 1 skipped with 0 duplicate tokens!)
            assert executed_on_resumed_worker == [2]
            assert carryover_received[2] == "Carryover after slice 1 (minute 20)"
            assert final_job.total_tokens_used == 270000
            assert final_job.violations_found == 3

        asyncio.run(_run())
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_tc013_cpuprobe_and_in_container_slice_watchdog() -> None:
    """TC-013: Verifies `cctv_audit.cpuprobe` (`cpu_quota`, `runqueue_wait_seconds`), `/is_busy` telemetry,
    and `_await_with_bounded_cleanup` (the in-container slice watchdog from `percy-han/cctv-audit` that terminates
    a hung slice coroutine within `CLEANUP_TIMEOUT_SEC` so `sweep_and_resume_stale_jobs` can resume it cleanly).
    """
    from cctv_audit.cpuprobe import _parse_cpu_max, cpu_quota, runqueue_wait_seconds

    assert _parse_cpu_max("400000 100000") == 4.0
    assert _parse_cpu_max("max 100000") is None
    assert cpu_quota() > 0.0
    rq_wait = runqueue_wait_seconds()
    assert rq_wait is None or rq_wait >= 0.0

    async def _run() -> None:
        async def _wedged_slice() -> str:
            await asyncio.sleep(60.0)
            return "never"

        with unittest.TestCase().assertRaises(TimeoutError) as ctx:
            await AuditService._await_with_bounded_cleanup(
                _wedged_slice(),
                timeout_sec=0.05,
                label="Job w99 slice vid:0",
            )
        assert "Job w99 slice vid:0" in str(ctx.exception)
        assert "已被容器内看门狗终止" in str(ctx.exception)

        # Verify stale PROBING jobs (>60s) do not poison active_running_count()
        import time
        from cctv_audit.config import config
        from cctv_audit.jobs import AuditJob

        svc = AuditService()
        assert AuditService._slice_stall_timeout_sec() == config.gemini_timeout_ms / 1000.0 + 120.0
        stale_probing = AuditJob(
            job_id="02b29d",
            user_id="supervisor@example.com",
            folder_id="f1",
            drive_url="https://drive.google.com/drive/folders/f1",
            state=JobState.PROBING,
            updated_at=time.time() - 3600.0,
        )
        svc.jobs._by_user.setdefault("supervisor@example.com", {})["02b29d"] = stale_probing
        assert svc.jobs.active_running_count() == 0
        assert svc.has_active_work() is False

        # Verify _dispatch_or_run_detached does NOT launch duplicate local fallback
        # when HTTP connection drops mid-run if remote worker is DONE or actively heartbeating
        import os
        from unittest import mock
        from cctv_audit.prompt_manager import PromptModelConfig

        local_fallback_calls: list[str] = []
        dummy_prompt_cfg = PromptModelConfig(
            prompt_version="v2.5",
            system_instruction="test",
            active_model="gemini-3.8-flash",
            fallback_model="gemini-3.8-flash",
        )

        async def _fake_detached(j: AuditJob, pcfg: PromptModelConfig) -> None:
            local_fallback_calls.append(j.job_id)

        svc._post_to_cloud_run_worker_sync = lambda url, uid, jid: False  # type: ignore[assignment]
        svc._run_detached_audit = _fake_detached  # type: ignore[assignment]

        now_ts = time.time()
        done_job = AuditJob(
            job_id="done01",
            user_id="supervisor@example.com",
            folder_id="f1",
            drive_url="https://drive.google.com/drive/folders/f1",
            state=JobState.DONE,
            heartbeat_at=now_ts,
        )
        await svc.jobs.save(done_job)
        stale_caller_snapshot = done_job.model_copy(
            update={"state": JobState.RUNNING, "heartbeat_at": now_ts - 1200.0}
        )
        with mock.patch.dict(os.environ, {"CLOUD_RUN_WORKER_URL": "https://worker.example.run.app"}):
            await svc._dispatch_or_run_detached(stale_caller_snapshot, dummy_prompt_cfg)
            assert local_fallback_calls == []

            # Remote worker still RUNNING with newer fresh heartbeat -> skip fallback
            active_remote = AuditJob(
                job_id="actv02",
                user_id="supervisor@example.com",
                folder_id="f1",
                drive_url="https://drive.google.com/drive/folders/f1",
                state=JobState.RUNNING,
                heartbeat_at=now_ts,
            )
            await svc.jobs.save(active_remote)
            caller_actv = active_remote.model_copy(update={"heartbeat_at": now_ts - 600.0})
            await svc._dispatch_or_run_detached(caller_actv, dummy_prompt_cfg)
            assert local_fallback_calls == []

            # Remote worker never touched job (same heartbeat_at) -> local fallback runs
            untouched_job = AuditJob(
                job_id="fail03",
                user_id="supervisor@example.com",
                folder_id="f1",
                drive_url="https://drive.google.com/drive/folders/f1",
                state=JobState.RUNNING,
                heartbeat_at=now_ts,
            )
            untouched_job = await svc.jobs.save(untouched_job)
            await svc._dispatch_or_run_detached(untouched_job, dummy_prompt_cfg)
            assert local_fallback_calls == ["fail03"]

    asyncio.run(_run())


def test_tc017_ge_stream_heartbeat_and_turn_latency_optimizations() -> None:
    """TC-017 (Round 55): Verifies GE Web UI stream heartbeat + turn_complete envelope,
    router `thinking_budget=0` + historical URL masking, concurrent preflight setup/ffprobe
    probes + background PromptManager cache warm-up, GCS version-token delta sync, and
    Tier-1 `/is_busy` non-blocking dispatch + Cloud Run scale-out retry.
    """
    import io
    import json
    import os
    from pathlib import Path
    import shutil
    import tempfile
    import time
    from types import SimpleNamespace
    import urllib.error
    import urllib.request
    from unittest import mock

    import cctv_audit.audit_service as audit_svc_mod
    from cctv_audit.gcp import GoogleWorkspaceGateway, WorkspaceAccessError, WorkspaceStorageQuotaError
    from cctv_audit.jobs import AuditJob, JobState, UserScopedJobStore
    from cctv_audit.prompt_manager import PromptModelConfig
    import cctv_audit.server as srv
    import cctv_audit.turn as turn_mod
    from cctv_audit.workspace_reporter import WorkspaceReporter

    # 1. `build_adk_event_envelope` includes `partial=False` and `turn_complete=True`
    env = srv.build_adk_event_envelope("预检完成", "inv-01", "sess-01")
    assert env["events"][0]["partial"] is False
    assert env["events"][0]["turn_complete"] is True

    # 2. `_extract_adk_turn_text` compacts history to <=4 events, truncates multi-line tables,
    #    and masks historical URLs so Turn 2 ("确认开始") cannot pass `verify_url_verbatim`
    #    using Turn 1's Drive URL.
    inner_payload = {
        "message": {"role": "user", "parts": [{"text": "确认开始"}]},
        "events": [
            {"author": "user", "content": {"parts": [{"text": f"Old turn {i}"}]}}
            for i in range(6)
        ]
        + [
            {
                "author": "user",
                "content": {
                    "parts": [
                        {
                            "text": "帮我检查 https://drive.google.com/drive/folders/1--hbVk8xE5i-aZrph7Pgy4vZwZdp9-NJ"
                        }
                    ]
                },
            },
            {
                "author": "model",
                "content": {
                    "parts": [
                        {
                            "text": (
                                "✅ 预检通过（单号 `275db5`） https://drive.google.com/drive/folders/1--hbVk8xE5i-aZrph7Pgy4vZwZdp9-NJ\n"
                                "| 视频 | 分辨率 | 时长 |\n"
                                "| --- | --- | --- |\n"
                                + ("| Footage.mp4 | 1920x1080 | 300s |\n" * 40)
                            )
                        }
                    ]
                },
            },
        ],
    }
    extracted = srv._extract_adk_turn_text(inner_payload)
    lines = extracted.splitlines()
    assert len(lines) == 5  # last 4 historical events + 1 current user line
    assert "Old turn 0" not in extracted and "Old turn 3" not in extracted
    assert "1--hbVk8xE5i-aZrph7Pgy4vZwZdp9-NJ" not in extracted
    assert "<历史链接>" in extracted
    assert "| 视频 | 分辨率 |" not in extracted
    assert lines[-1] == "[user] 确认开始"
    # If the LLM router ever mis-picks INSPECT with Turn 1's URL on Turn 2, verify_url_verbatim blocks it!
    misrouted = TurnDecision(
        action=TurnAction.INSPECT,
        drive_url="https://drive.google.com/drive/folders/1--hbVk8xE5i-aZrph7Pgy4vZwZdp9-NJ",
        reply_summary="误把确认当成预检",
    )
    assert verify_url_verbatim(misrouted, extracted).action == TurnAction.UNCLEAR

    # 3. `classify_turn_with_llm` sets `thinking_config.thinking_budget == 0`
    captured_gen_configs: list = []

    async def _fake_router_generate(*, model, contents, gen_config, **_kw):
        captured_gen_configs.append(gen_config)
        return SimpleNamespace(
            parsed=TurnDecision(
                action=TurnAction.INSPECT,
                drive_url="https://drive.google.com/drive/folders/1--hbVk8xE5i-aZrph7Pgy4vZwZdp9-NJ",
                reply_summary="发起预检",
            ),
            text="",
        )

    async def _run_async_checks() -> None:
        with mock.patch.object(turn_mod, "generate_content_with_retry", _fake_router_generate):
            dec = await turn_mod.classify_turn_with_llm(
                "请预检 https://drive.google.com/drive/folders/1--hbVk8xE5i-aZrph7Pgy4vZwZdp9-NJ"
            )
        assert dec.action == TurnAction.INSPECT
        assert len(captured_gen_configs) == 1
        assert captured_gen_configs[0].thinking_config is not None
        assert captured_gen_configs[0].thinking_config.thinking_budget == 0

        # 4. `ge_stream_endpoint`: slow turns emit `b"{}\n"` heartbeats before the final envelope;
        #    disconnecting mid-stream cancels the underlying turn task immediately.
        async def _slow_turn(**_kw) -> str:
            await asyncio.sleep(0.07)
            return "慢回合完成"

        req = srv.StreamReasoningRequest(
            input={
                "request_json": json.dumps(
                    {
                        "user_id": "auditor_sg@chagee.com",
                        "session_id": "sess-hb",
                        "message": {"role": "user", "parts": [{"text": "确认开始"}]},
                    }
                )
            }
        )
        with (
            mock.patch.object(srv, "STREAM_HEARTBEAT_DELAY_SEC", 0.02),
            mock.patch.object(srv, "STREAM_HEARTBEAT_INTERVAL_SEC", 0.02),
            mock.patch.object(srv, "_handle_conversation_turn", _slow_turn),
        ):
            resp = await srv.ge_stream_endpoint(req)
            raw_chunks = [c async for c in resp.body_iterator]

        assert len(raw_chunks) >= 2
        assert all(c == b"{}\n" for c in raw_chunks[:-1])
        final_obj = json.loads(raw_chunks[-1].decode("utf-8"))
        assert final_obj["session_id"] == "sess-hb"
        assert final_obj["events"][0]["partial"] is False
        assert final_obj["events"][0]["turn_complete"] is True
        assert final_obj["events"][0]["content"]["parts"][0]["text"] == "慢回合完成"

        # Client disconnect mid-stream cancels `turn_task`
        cancelled_flag = {"cancelled": False}

        async def _hang_turn(**_kw) -> str:
            try:
                await asyncio.sleep(10.0)
                return "never"
            except asyncio.CancelledError:
                cancelled_flag["cancelled"] = True
                raise

        with (
            mock.patch.object(srv, "STREAM_HEARTBEAT_DELAY_SEC", 0.01),
            mock.patch.object(srv, "STREAM_HEARTBEAT_INTERVAL_SEC", 0.01),
            mock.patch.object(srv, "_handle_conversation_turn", _hang_turn),
        ):
            resp2 = await srv.ge_stream_endpoint(req)
            agen = resp2.body_iterator
            first_hb = await anext(agen)
            assert first_hb == b"{}\n"
            await agen.aclose()
            await asyncio.sleep(0)
        assert cancelled_flag["cancelled"] is True

        # 5. Concurrent `_workspace_setup_problem` probes + non-blocking warm-up
        class _SlowGateway:
            async def probe_write_access(self, folder_id: str) -> str:
                await asyncio.sleep(0.08)
                return "Folder"

            async def check_sheet_readable(self, sheet_id: str) -> str:
                await asyncio.sleep(0.08)
                return "Sheet"

        warmed = {"calls": 0}

        class _CountingPromptManager:
            async def load_active_config(self, sheet_id=None) -> PromptModelConfig:
                warmed["calls"] += 1
                return PromptModelConfig(system_instruction="warmed")

        class _FakeIngestor:
            async def inspect_drive_videos(self, drive_url: str, preloaded_items=None, job_id=""):
                from cctv_audit.video_ingestor import InspectFolderResponse

                return InspectFolderResponse(
                    folder_id="F1",
                    passed=True,
                    total_videos=1,
                    total_duration_sec=300.0,
                    planned_segments_count=1,
                    estimated_tokens=0,
                    estimated_cost_usd=0.0,
                    estimated_minutes=1.0,
                    message_to_user="ok",
                )

        tmp_dir = Path(tempfile.mkdtemp(prefix="chagee_tc017_"))
        try:
            svc = AuditService(
                job_store=UserScopedJobStore(state_dir=tmp_dir / "jobs", gcs_bucket=""),
                ingestor=_FakeIngestor(),  # type: ignore[arg-type]
                prompt_manager=_CountingPromptManager(),  # type: ignore[arg-type]
                reporter=WorkspaceReporter(gateway=_SlowGateway(), webhook_url=""),  # type: ignore[arg-type]
            )
            t0 = time.monotonic()
            prob = await svc._workspace_setup_problem("F1")
            elapsed = time.monotonic() - t0
            assert prob == ""
            # Sequential would take >= 0.16s; concurrent gather finishes in ~0.08s
            assert elapsed < 0.14, f"Expected concurrent setup probes (<0.14s), took {elapsed:.3f}s"

            # When both probes fail simultaneously, probe_write_access error is returned first and no task exception leaks
            class _BothFailGateway:
                async def probe_write_access(self, folder_id: str) -> str:
                    raise WorkspaceStorageQuotaError("存储空间不足")

                async def check_sheet_readable(self, sheet_id: str) -> str:
                    raise WorkspaceAccessError("SOP总控表未共享")

            svc_fail = AuditService(
                job_store=UserScopedJobStore(state_dir=tmp_dir / "jobs2", gcs_bucket=""),
                reporter=WorkspaceReporter(gateway=_BothFailGateway(), webhook_url=""),  # type: ignore[arg-type]
            )
            assert "存储空间不足" in await svc_fail._workspace_setup_problem("F1")

            # Live preflight (`preloaded_items=None`) triggers background warm-up without making `has_active_work()` True
            job = await svc.preflight(
                user_id="auditor@chagee.com",
                drive_url="https://drive.google.com/drive/folders/F1",
                preloaded_items=None,
            )
            assert job.state == JobState.READY
            assert svc.has_active_work() is False
            if svc._warmup_tasks:
                await asyncio.gather(*list(svc._warmup_tasks))
            assert warmed["calls"] == 1

            # 6. `GoogleWorkspaceGateway.list_folder_videos` runs `_ffprobe_drive_stream` concurrently
            gw = GoogleWorkspaceGateway()

            class _FakeDriveList:
                def files(self):
                    return self

                def list(self, **_kw):
                    return self

                def execute(self, num_retries: int = 0):
                    return {
                        "files": [
                            {"id": f"vid_{i}", "name": f"Footage {i}.mp4", "videoMediaMetadata": {}}
                            for i in (4, 2, 1, 3)
                        ]
                    }

            def _slow_ffprobe(fid: str) -> tuple[int, int, float]:
                time.sleep(0.07)
                return (1920, 1080, 300.0)

            gw._drive_service = lambda: _FakeDriveList()  # type: ignore[method-assign]
            gw._ffprobe_drive_stream = _slow_ffprobe  # type: ignore[method-assign]
            t_ff = time.monotonic()
            vids = await gw.list_folder_videos("F1")
            ff_elapsed = time.monotonic() - t_ff
            assert [v.filename for v in vids] == [
                "Footage 1.mp4",
                "Footage 2.mp4",
                "Footage 3.mp4",
                "Footage 4.mp4",
            ]
            assert all(v.width == 1920 and v.duration_sec == 300.0 for v in vids)
            # 4 videos x 0.07s sequentially would be >= 0.28s; parallel pool finishes in ~0.08s
            assert ff_elapsed < 0.20, f"Expected parallel ffprobe (<0.20s), took {ff_elapsed:.3f}s"

            # 7. `UserScopedJobStore` GCS version-token delta sync skips unchanged blobs on subsequent turns
            media_gets: list[str] = []
            j1 = AuditJob(job_id="job001", user_id="u@chagee.com", folder_id="f1", state=JobState.DONE, updated_at=100.0)
            j2 = AuditJob(job_id="job002", user_id="u@chagee.com", folder_id="f1", state=JobState.DONE, updated_at=200.0)
            j3_v1 = AuditJob(job_id="job003", user_id="u@chagee.com", folder_id="f1", state=JobState.RUNNING, updated_at=300.0)
            j3_v2 = AuditJob(
                job_id="job003",
                user_id="u@chagee.com",
                folder_id="f1",
                state=JobState.DONE,
                violations_found=7,
                updated_at=400.0,
            )
            listing_Gen = {"gen3": "1001", "payload3": j3_v1.model_dump_json()}

            class _HttpResp:
                def __init__(self, body_bytes: bytes, status: int = 200) -> None:
                    self._body = body_bytes
                    self.status = status

                def read(self) -> bytes:
                    return self._body

                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return False

            def _fake_gcs_urlopen(req_obj, timeout: float = 10.0):
                url = req_obj.full_url
                if "?prefix=" in url:
                    body = {
                        "items": [
                            {"name": "jobs/u_chagee_com/job001.json", "generation": "1", "etag": "e1"},
                            {"name": "jobs/u_chagee_com/job002.json", "generation": "2", "etag": "e2"},
                            {"name": "jobs/u_chagee_com/job003.json", "generation": listing_Gen["gen3"], "etag": "e3"},
                            {"name": "jobs/u_chagee_com/corrupt.json", "generation": "9", "etag": "e9"},
                        ]
                    }
                    return _HttpResp(json.dumps(body).encode("utf-8"))
                media_gets.append(url)
                if "job001.json" in url:
                    return _HttpResp(j1.model_dump_json().encode("utf-8"))
                if "job002.json" in url:
                    return _HttpResp(j2.model_dump_json().encode("utf-8"))
                if "job003.json" in url:
                    return _HttpResp(listing_Gen["payload3"].encode("utf-8"))
                return _HttpResp(b"{not-valid-json")

            class _FakeCreds:
                token = "fake-gcs-token"

                def refresh(self, _req) -> None:
                    pass

            gcs_store = UserScopedJobStore(
                state_dir=tmp_dir / "gcs_delta_disk",
                gcs_bucket="my-project-my-stack-staging",
            )
            with (
                mock.patch("google.auth.default", return_value=(_FakeCreds(), "proj")),
                mock.patch.object(urllib.request, "urlopen", _fake_gcs_urlopen),
            ):
                # Cold sync: downloads all 4 blobs (corrupt one logged & skipped without aborting)
                first_list = await gcs_store.list_for_user("u@chagee.com")
                assert {j.job_id for j in first_list} == {"job001", "job002", "job003"}
                assert len(media_gets) == 4

                # Warm sync with only job003 generation bumped: skips job001 & job002, re-downloads only job003 (+ corrupt)
                media_gets.clear()
                listing_Gen["gen3"] = "1002"
                listing_Gen["payload3"] = j3_v2.model_dump_json()
                refreshed = await gcs_store.get("u@chagee.com", "job003")
                assert refreshed is not None and refreshed.state == JobState.DONE and refreshed.violations_found == 7
                assert not any("job001.json" in u or "job002.json" in u for u in media_gets)
                assert any("job003.json" in u for u in media_gets)

            # 8. Tier-1 `active_jobs_count` does not count outbound `_background_tasks` waiting on Tier-2,
            #    and `_post_to_cloud_run_worker_sync` retries on HTTP 429/503 during Cloud Run scale-out.
            holder = asyncio.create_task(asyncio.sleep(5.0))
            svc._background_tasks.add(holder)
            try:
                with mock.patch.dict(os.environ, {"CLOUD_RUN_WORKER_URL": "https://worker.example.run.app"}):
                    assert svc.active_jobs_count() == 0
                    assert svc.has_active_work() is False
                    svc._in_flight_jobs.add("local_fallback_job")
                    assert svc.active_jobs_count() == 1
                    svc._in_flight_jobs.discard("local_fallback_job")
            finally:
                svc._background_tasks.discard(holder)
                holder.cancel()

            attempts = {"n": 0}

            def _scaleout_urlopen(req_obj, timeout: float = 3600.0):
                attempts["n"] += 1
                if attempts["n"] < 3:
                    raise urllib.error.HTTPError(
                        req_obj.full_url, 429, "Too Many Requests", hdrs=None, fp=io.BytesIO(b"busy")  # type: ignore[arg-type]
                    )
                return _HttpResp(b'{"dispatched": true}', status=200)

            with (
                mock.patch.object(urllib.request, "urlopen", _scaleout_urlopen),
                mock.patch.object(audit_svc_mod.time, "sleep", lambda _s: None),
            ):
                ok = AuditService._post_to_cloud_run_worker_sync(
                    "https://worker.example.run.app", "u@chagee.com", "job003"
                )
            assert ok is True
            assert attempts["n"] == 3
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    asyncio.run(_run_async_checks())


class ChageeUnitTestSuite(unittest.TestCase):
    """Standard-library unittest runner wrapper for all TC-001..TC-017 unit tests."""

    def test_01_extract_drive_id_and_sliding_windows(self) -> None:
        test_extract_drive_id_and_sliding_windows()

    def test_02_preflight_rejects_below_720p_and_accepts_hd(self) -> None:
        test_preflight_rejects_below_720p_and_accepts_hd()

    def test_03_prompt_and_model_sheet_rollback_and_no_silent_autoswitch(self) -> None:
        test_prompt_and_model_sheet_rollback_and_no_silent_autoswitch()

    def test_04_multi_user_isolation_and_in_folder_reporting(self) -> None:
        test_multi_user_isolation_and_in_folder_reporting()

    def test_05_verbatim_url_guardrail(self) -> None:
        test_verbatim_url_guardrail()

    def test_06_overlap_dedup_and_severity_normalization(self) -> None:
        test_overlap_dedup_and_severity_normalization()

    def test_07_full_v10_and_chagee_v2_yaml_prompt_loading(self) -> None:
        test_full_v10_and_chagee_v2_yaml_prompt_loading()

    def test_08_sop_sheet_url_extraction_and_hot_switch(self) -> None:
        test_sop_sheet_url_extraction_and_hot_switch()

    def test_09_ge_a2a_and_adk_wire_protocols_and_is_busy_probe(self) -> None:
        test_ge_a2a_and_adk_wire_protocols_and_is_busy_probe()

    def test_10_zero_db_cross_instance_job_store_continuity(self) -> None:
        test_zero_db_cross_instance_job_store_continuity()

    def test_11_tier1_tier2_crash_checkpoint_and_resume_zero_duplicate_tokens(self) -> None:
        test_tier1_tier2_crash_checkpoint_and_resume_zero_duplicate_tokens()

    def test_12_unattended_watchdog_auto_resumes_crashed_tier2_after_20_min_without_user_action(self) -> None:
        test_unattended_watchdog_auto_resumes_crashed_tier2_after_20_min_without_user_action()

    def test_13_cpuprobe_and_in_container_slice_watchdog(self) -> None:
        test_tc013_cpuprobe_and_in_container_slice_watchdog()

    def test_14_same_slice_dedup_needs_same_event_text_and_keeps_red_line(self) -> None:
        test_same_slice_dedup_needs_same_event_text_and_keeps_red_line()

    def test_15_agentic_media_processing_is_set_on_the_video_part(self) -> None:
        test_agentic_media_processing_is_set_on_the_video_part()

    def test_16_ledger_counts_tool_use_prompt_tokens_as_input(self) -> None:
        test_ledger_counts_tool_use_prompt_tokens_as_input()

    def test_17_ge_stream_heartbeat_and_turn_latency_optimizations(self) -> None:
        test_tc017_ge_stream_heartbeat_and_turn_latency_optimizations()


if __name__ == "__main__":
    unittest.main()







def test_tc018_generate_content_with_retry_per_call_timeout():
    """A non-default `timeout_ms` reaches the SDK as a per-request `http_options.timeout` (the
    caller's config object is not mutated); the default leaves the request config untouched."""
    import os
    from unittest import mock
    from google.genai import types as gtypes
    from cctv_audit import gcp as gcp_module

    seen = []

    class _Models:
        async def generate_content(self, *, model, contents, config):
            seen.append(config)
            return "ok"

    class _Aio:
        models = _Models()

    class _Client:
        aio = _Aio()

    base_cfg = gtypes.GenerateContentConfig(temperature=0.0)
    with mock.patch.object(gcp_module.config, "gemini_timeout_ms", 750_000):
        out = asyncio.run(gcp_module.generate_content_with_retry(
            model="m", contents=["x"], gen_config=base_cfg, client=_Client(), timeout_ms=1_500_000))
        assert out == "ok"
        assert seen[-1].http_options.timeout == 1_500_000
        assert seen[-1].temperature == 0.0
        assert base_cfg.http_options is None  # caller's object untouched

        asyncio.run(gcp_module.generate_content_with_retry(
            model="m", contents=["x"], gen_config=base_cfg, client=_Client()))
        assert seen[-1] is base_cfg  # default: client-level timeout, request unchanged

        asyncio.run(gcp_module.generate_content_with_retry(
            model="m", contents=["x"], gen_config=base_cfg, client=_Client(), timeout_ms=750_000))
        assert seen[-1] is base_cfg
