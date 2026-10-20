"""Unit and integration tests for REQ-009 Daily Scheduled Auto-Audit Engine & Smart Append."""

from __future__ import annotations

import asyncio
import datetime as dt
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

from cctv_audit.agentic_auditor import TokenLedgerRow
from cctv_audit.audit_service import AuditService
from cctv_audit.gcp import GoogleWorkspaceGateway
from cctv_audit.gcs_gateway import GcsStorageGateway, read_xlsx_sheets
from cctv_audit.jobs import (
    AuditJob,
    JobState,
    SegmentCheckpoint,
    UserScopedJobStore,
)
from cctv_audit.prompt_manager import (
    PromptModelConfig,
    ScheduledAuditRule,
    parse_scheduled_rules_from_rows,
)
from cctv_audit.scheduled_auditor import (
    FileLedgerEntry,
    FileLedgerState,
    ScheduledAuditEngine,
    ScheduledStateStore,
    discover_and_route_drive_videos_sync,
    discover_and_route_gcs_videos_sync,
    is_rule_due,
    resolve_canonical_enabled_rules,
)
from cctv_audit.workspace_reporter import EVIDENCE_SUBFOLDER_NAME, ViolationSheetRow


def _make_sample_rule(
    *,
    row_index: int = 2,
    rule_id: str = "RULE-VN-01",
    rule_name: str = "越南大区每日凌晨巡检",
    enabled: bool = True,
    root_folder_url: str = "https://drive.google.com/drive/folders/folder_vn_root",
    root_folder_id: str = "folder_vn_root",
    supervisor_email: str = "vn-sup@chagee.com",
    trigger_time: str = "02:00",
    timezone: str = "Asia/Ho_Chi_Minh",
    lookback_hours: float = 24.0,
) -> ScheduledAuditRule:
    return ScheduledAuditRule(
        row_index=row_index,
        rule_id=rule_id,
        rule_name=rule_name,
        enabled=enabled,
        root_folder_url=root_folder_url,
        root_folder_id=root_folder_id,
        supervisor_email=supervisor_email,
        trigger_time=trigger_time,
        timezone=timezone,
        lookback_hours=lookback_hours,
    )


def test_parse_scheduled_rules_and_timezone_trigger_window() -> None:
    rows = [
        [
            "Rule_ID",
            "Rule_Name",
            "Root_Folder_URL",
            "Trigger_Time",
            "Timezone",
            "Lookback_Hours",
            "Supervisor_Email",
            "Enabled",
            "Last_Run_Status",
        ],
        [
            "RULE-VN-01",
            "越南胡志明大区巡检",
            "https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOpQrStUvWxYz123456",
            "2:05",
            "Asia/Ho_Chi_Minh",
            "24",
            "sup.vn@chagee.com",
            "ENABLED",
            "",
        ],
        [
            "RULE-SG-02",
            "新加坡GCS桶巡检",
            "gs://chagee-sg-cctv/stores/orchard",
            "04:30",
            "Asia/Singapore",
            "48",
            "sup.sg@chagee.com",
            "TRUE",
            "",
        ],
        [
            "RULE-DISABLED",
            "停用规则",
            "https://drive.google.com/drive/folders/1ZzYyXxWwVvUuTtSsRrQqPpOoNnMmLlKk",
            "03:00",
            "Asia/Singapore",
            "24",
            "sup.disabled@chagee.com",
            "DISABLED",
            "",
        ],
    ]
    parsed = parse_scheduled_rules_from_rows(rows)
    assert len(parsed) == 3
    vn_rule, sg_rule, dis_rule = parsed

    assert vn_rule.row_index == 2
    assert vn_rule.enabled is True
    assert vn_rule.root_folder_id == "1AbCdEfGhIjKlMnOpQrStUvWxYz123456"
    assert vn_rule.trigger_time == "02:05"
    assert vn_rule.timezone == "Asia/Ho_Chi_Minh"
    assert vn_rule.lookback_hours == 24.0

    assert sg_rule.row_index == 3
    assert sg_rule.enabled is True
    assert sg_rule.root_folder_id == "gs://chagee-sg-cctv/stores/orchard"
    assert sg_rule.lookback_hours == 48.0

    assert dis_rule.enabled is False

    # Timezone evaluation: 02:05 Asia/Ho_Chi_Minh (UTC+7) == 19:05 UTC previous day
    before_window = dt.datetime(2026, 10, 14, 19, 0, tzinfo=dt.timezone.utc)
    due_before, local_date = is_rule_due(vn_rule, before_window)
    assert due_before is False
    assert local_date == "2026-10-15"

    in_window = dt.datetime(2026, 10, 14, 19, 20, tzinfo=dt.timezone.utc)
    due_in, local_date = is_rule_due(vn_rule, in_window)
    assert due_in is True
    assert local_date == "2026-10-15"

    after_window = dt.datetime(2026, 10, 14, 21, 30, tzinfo=dt.timezone.utc)
    due_after, _ = is_rule_due(vn_rule, after_window)
    assert due_after is False

    due_disabled, _ = is_rule_due(dis_rule, in_window)
    assert due_disabled is False

    # Midnight boundary test: rule scheduled at 23:30 Asia/Ho_Chi_Minh (UTC+7),
    # evaluated at 00:15 local time on 2026-10-16 (17:15 UTC on 2026-10-15) -> 45m into 120m grace window!
    late_night_rule = _make_sample_rule(
        row_index=5,
        rule_id="RULE-LATE",
        trigger_time="23:30",
        timezone="Asia/Ho_Chi_Minh",
    )
    after_midnight_utc = dt.datetime(2026, 10, 15, 17, 15, tzinfo=dt.timezone.utc)
    due_midnight, cycle_date = is_rule_due(late_night_rule, after_midnight_utc)
    assert due_midnight is True
    assert cycle_date == "2026-10-15"  # Binds to the 2026-10-15 23:30 cycle date!


def test_workspace_credentials_custom_subject_cache_no_thrashing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verifies `workspace_credentials(impersonate_user)` caches per-supervisor credentials
    without thrashing even when default `_workspace_creds` was never initialized (`None`).
    """
    from cctv_audit import gcp

    build_calls: List[Tuple[str, str]] = []

    class _FakeCreds:
        valid = True

    def _fake_build(subject: str, dwd_sa: str) -> _FakeCreds:
        build_calls.append((subject, dwd_sa))
        return _FakeCreds()

    monkeypatch.setenv("WORKSPACE_IMPERSONATE_USER", "bot@chagee.com")
    monkeypatch.setenv("WORKSPACE_DWD_SERVICE_ACCOUNT", "worker@proj.iam.gserviceaccount.com")
    monkeypatch.setattr(gcp, "_workspace_creds", None)
    monkeypatch.setattr(gcp, "_workspace_creds_identity", None)
    monkeypatch.setattr(gcp, "_workspace_creds_dwd_sa", None)
    monkeypatch.setattr(gcp, "_workspace_creds_by_identity", {})
    monkeypatch.setattr(gcp, "_build_workspace_credentials", _fake_build)

    c1 = gcp.workspace_credentials("sup1@chagee.com")
    c2 = gcp.workspace_credentials("sup1@chagee.com")
    c3 = gcp.workspace_credentials("sup2@chagee.com")
    c4 = gcp.workspace_credentials("sup1@chagee.com")

    assert c1 is c2 is c4
    assert c1 is not c3
    assert len(build_calls) == 2


def test_smallest_row_index_tie_breaker_for_same_root_folder() -> None:
    r_row2 = _make_sample_rule(
        row_index=2,
        rule_id="RULE-A",
        root_folder_id="folder_same_root",
        supervisor_email="sup_a@chagee.com",
    )
    r_row5 = _make_sample_rule(
        row_index=5,
        rule_id="RULE-B",
        root_folder_id="folder_same_root",
        supervisor_email="sup_b@chagee.com",
    )
    canonical, shadowed = resolve_canonical_enabled_rules([r_row5, r_row2])
    assert [r.rule_id for r in canonical] == ["RULE-A"]
    assert len(shadowed) == 1
    dup, winner = shadowed[0]
    assert dup.rule_id == "RULE-B"
    assert winner.rule_id == "RULE-A"


class _FakeDriveService:
    """Fake Drive v3 service supporting bottom-up `parents` traversal and folder caching."""

    def __init__(
        self,
        *,
        video_files: List[Dict[str, Any]],
        folders: Dict[str, Dict[str, Any]],
    ) -> None:
        self._video_files = video_files
        self._folders = folders
        self.folder_get_calls: Dict[str, int] = {}
        self.list_queries: List[str] = []

    def files(self) -> "_FakeDriveService":
        return self

    def list(self, **kwargs: Any) -> "_FakeDriveRequest":
        self.list_queries.append(str(kwargs.get("q") or ""))
        return _FakeDriveRequest({"files": list(self._video_files)})

    def get(self, *, fileId: str, **kwargs: Any) -> "_FakeDriveRequest":
        self.folder_get_calls[fileId] = self.folder_get_calls.get(fileId, 0) + 1
        meta = self._folders.get(fileId)
        if meta is None:
            raise RuntimeError(f"Folder {fileId} not found")
        return _FakeDriveRequest(meta)


class _FakeDriveRequest:
    def __init__(self, payload: Dict[str, Any]) -> None:
        self._payload = payload

    def execute(self, num_retries: int = 0) -> Dict[str, Any]:
        return self._payload


def test_bottom_up_drive_discovery_and_time_agnostic_closest_ancestor_carve_out() -> None:
    """Verifies that:
    1. A parent regional rule firing at 02:00 NEVER steals videos inside a child flagship store
       folder that has its own ENABLED rule scheduled for 04:00 (Time-Agnostic Carve-Out).
    2. Videos inside `📁 违规证据切片_Evidence` are ignored.
    3. Folder parent metadata is cached across multiple videos in the same store folder.
    """
    now_utc = dt.datetime(2026, 10, 15, 2, 5, tzinfo=dt.timezone.utc)
    recent_iso = "2026-10-15T01:00:00Z"

    # Rule 1: Regional Root (Trigger 02:00)
    rule_region = _make_sample_rule(
        row_index=2,
        rule_id="RULE-REGION",
        root_folder_id="folder_vn_root",
        trigger_time="02:00",
        lookback_hours=24.0,
    )
    # Rule 2: Flagship Store subfolder under Regional Root (Trigger 04:00, not due at 02:05!)
    rule_flagship = _make_sample_rule(
        row_index=3,
        rule_id="RULE-FLAGSHIP",
        root_folder_id="folder_store_flagship",
        trigger_time="04:00",
        lookback_hours=24.0,
    )

    folders = {
        "folder_vn_root": {"id": "folder_vn_root", "name": "VN_Root", "parents": [], "trashed": False},
        "folder_district_1": {
            "id": "folder_district_1",
            "name": "District_1",
            "parents": ["folder_vn_root"],
            "trashed": False,
        },
        "folder_store_normal": {
            "id": "folder_store_normal",
            "name": "Store_Bau_Cat",
            "parents": ["folder_district_1"],
            "trashed": False,
        },
        "folder_store_flagship": {
            "id": "folder_store_flagship",
            "name": "Store_Cantavil_Flagship",
            "parents": ["folder_district_1"],
            "trashed": False,
        },
        "folder_evidence": {
            "id": "folder_evidence",
            "name": EVIDENCE_SUBFOLDER_NAME,
            "parents": ["folder_store_normal"],
            "trashed": False,
        },
    }

    videos = [
        {
            "id": "vid_normal_1",
            "name": "cam01_normal.mp4",
            "mimeType": "video/mp4",
            "size": "10485760",
            "parents": ["folder_store_normal"],
            "createdTime": recent_iso,
            "videoMediaMetadata": {"width": 1920, "height": 1080, "durationMillis": 300000},
        },
        {
            "id": "vid_normal_2",
            "name": "cam02_normal.mp4",
            "mimeType": "video/mp4",
            "size": "10485760",
            "parents": ["folder_store_normal"],
            "createdTime": recent_iso,
            "videoMediaMetadata": {"width": 1920, "height": 1080, "durationMillis": 300000},
        },
        {
            "id": "vid_flagship_1",
            "name": "cam01_flagship.mp4",
            "mimeType": "video/mp4",
            "size": "10485760",
            "parents": ["folder_store_flagship"],
            "createdTime": recent_iso,
            "videoMediaMetadata": {"width": 1920, "height": 1080, "durationMillis": 300000},
        },
        {
            "id": "vid_evidence_clip",
            "name": "F-001_evidence.mp4",
            "mimeType": "video/mp4",
            "size": "1048576",
            "parents": ["folder_evidence"],
            "createdTime": recent_iso,
            "videoMediaMetadata": {"width": 1920, "height": 1080, "durationMillis": 12000},
        },
    ]

    fake_drive = _FakeDriveService(video_files=videos, folders=folders)
    routed = discover_and_route_drive_videos_sync(
        fake_drive,
        canonical_enabled_rules=[rule_region, rule_flagship],
        max_lookback_hours=24.0,
        now_utc=now_utc,
    )

    # Regional rule ONLY gets Store_Bau_Cat's 2 videos; Store_Cantavil_Flagship is carved out to RULE-FLAGSHIP!
    assert [c.file_id for c in routed["RULE-REGION"]] == ["vid_normal_1", "vid_normal_2"]
    assert all(c.leaf_folder_name == "Store_Bau_Cat" for c in routed["RULE-REGION"])
    assert [c.file_id for c in routed["RULE-FLAGSHIP"]] == ["vid_flagship_1"]
    assert routed["RULE-FLAGSHIP"][0].leaf_folder_name == "Store_Cantavil_Flagship"

    # Folder cache ensures `folder_store_normal` was fetched only once across 2 videos + 1 evidence subfolder
    assert fake_drive.folder_get_calls["folder_store_normal"] == 1


def test_method_1_smart_append_preserves_history_in_google_sheets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verifies Method 1 (`方式一：同表智能追加`) in `GoogleWorkspaceGateway.create_dual_tab_report_sheet`:
    - When Day 2 audits `cam02_day2.mp4`, existing rows in Tab 1 & Tab 2 for `cam01_day1.mp4` are preserved.
    - When `cam01_day1.mp4` is re-audited, only `cam01_day1.mp4` rows are replaced while `cam02_day2.mp4` stays intact.
    """

    async def _run() -> None:
        gw = GoogleWorkspaceGateway(impersonate_user="sup@chagee.com")
        tab1_title = "违规事件 3 秒复核台"
        tab2_title = "本次视频 Token 消耗与耗时账单"

        # In-memory simulated Google Sheet state
        sheet_store: Dict[str, List[List[Any]]] = {
            tab1_title: [],
            tab2_title: [],
        }
        drive_created: List[str] = []

        class _FakeDriveFilesForSheet:
            def list(self, **kwargs: Any) -> _FakeDriveRequest:
                if not drive_created:
                    return _FakeDriveRequest({"files": []})
                return _FakeDriveRequest(
                    {
                        "files": [
                            {
                                "id": "sheet_existing_123",
                                "name": "📊 AI稽核报告与Token账单",
                                "webViewLink": "https://docs.google.com/spreadsheets/d/sheet_existing_123/edit",
                            }
                        ]
                    }
                )

            def create(self, **kwargs: Any) -> _FakeDriveRequest:
                drive_created.append("sheet_existing_123")
                return _FakeDriveRequest(
                    {
                        "id": "sheet_existing_123",
                        "webViewLink": "https://docs.google.com/spreadsheets/d/sheet_existing_123/edit",
                    }
                )

        class _FakeDriveSvc:
            def files(self) -> _FakeDriveFilesForSheet:
                return _FakeDriveFilesForSheet()

        class _FakeValuesApi:
            def get(self, *, spreadsheetId: str, range: str) -> _FakeDriveRequest:
                tab_name = range.split("!")[0].strip("'")
                rows = sheet_store.get(tab_name, [])
                return _FakeDriveRequest({"values": [list(r) for r in rows[1:]]})

            def batchClear(self, *, spreadsheetId: str, body: Dict[str, Any]) -> _FakeDriveRequest:
                for r_spec in body.get("ranges", []):
                    tab_name = r_spec.split("!")[0].strip("'")
                    sheet_store[tab_name] = []
                return _FakeDriveRequest({})

            def batchUpdate(self, *, spreadsheetId: str, body: Dict[str, Any]) -> _FakeDriveRequest:
                for entry in body.get("data", []):
                    tab_name = entry["range"].split("!")[0].strip("'")
                    sheet_store[tab_name] = [list(r) for r in entry.get("values", [])]
                return _FakeDriveRequest({})

        class _FakeSpreadsheetsApi:
            def get(self, *, spreadsheetId: str, fields: str = "") -> _FakeDriveRequest:
                return _FakeDriveRequest(
                    {
                        "sheets": [
                            {"properties": {"sheetId": 10, "title": tab1_title}},
                            {"properties": {"sheetId": 20, "title": tab2_title}},
                        ]
                    }
                )

            def batchUpdate(self, *, spreadsheetId: str, body: Dict[str, Any]) -> _FakeDriveRequest:
                return _FakeDriveRequest({})

            def values(self) -> _FakeValuesApi:
                return _FakeValuesApi()

        class _FakeSheetsService:
            def spreadsheets(self) -> _FakeSpreadsheetsApi:
                return _FakeSpreadsheetsApi()

        monkeypatch.setattr(gw, "_drive_service", lambda: _FakeDriveSvc())
        monkeypatch.setattr(gw, "_sheets_service", lambda: _FakeSheetsService())

        # Day 1: Audit cam01_day1.mp4
        day1_finding = ViolationSheetRow(
            audit_id="job_day1",
            video_filename="cam01_day1.mp4",
            timestamp_in_clip="01:15",
            rule_id="A1",
            disposition="CONFIRMED",
            severity="RED_LINE",
            confidence=0.95,
            evidence_description="Day 1 violation on cam01",
            evidence_drive_url="https://drive.google.com/file/d/ev1/view",
        )
        day1_ledger = TokenLedgerRow(
            audit_id="job_day1",
            auditor_folder_id="folder_store_1",
            video_filename="cam01_day1.mp4",
            video_duration_sec=300.0,
            resolution="1920x1080",
            model_version_used="gemini-3-flash-preview",
            prompt_version_used="v2.5",
            prompt_token_count=10000,
            thoughts_token_count=700,
            candidates_token_count=300,
            total_token_count=11000,
            estimated_cost_usd=0.0085,
            e2e_latency_ms=45000,
            flagged_events_count=1,
        )
        await gw.create_dual_tab_report_sheet(
            parent_folder_id="folder_store_1",
            title="📊 AI稽核报告与Token账单",
            tab1_rows=[day1_finding],
            tab2_rows=[day1_ledger],
        )
        assert len(sheet_store[tab1_title]) == 2  # 1 header + 1 finding
        assert sheet_store[tab1_title][1][1] == "cam01_day1.mp4"

        # Day 2: Incremental audit of cam02_day2.mp4 -> MUST preserve cam01_day1.mp4!
        day2_finding = ViolationSheetRow(
            audit_id="job_day2",
            video_filename="cam02_day2.mp4",
            timestamp_in_clip="03:40",
            rule_id="B2",
            disposition="SUSPECTED",
            severity="NORMAL",
            confidence=0.82,
            evidence_description="Day 2 violation on cam02",
            evidence_drive_url="https://drive.google.com/file/d/ev2/view",
        )
        day2_ledger = TokenLedgerRow(
            audit_id="job_day2",
            auditor_folder_id="folder_store_1",
            video_filename="cam02_day2.mp4",
            video_duration_sec=300.0,
            resolution="1920x1080",
            model_version_used="gemini-3-flash-preview",
            prompt_version_used="v2.5",
            prompt_token_count=12000,
            thoughts_token_count=800,
            candidates_token_count=400,
            total_token_count=13200,
            estimated_cost_usd=0.0098,
            e2e_latency_ms=48000,
            flagged_events_count=1,
        )
        await gw.create_dual_tab_report_sheet(
            parent_folder_id="folder_store_1",
            title="📊 AI稽核报告与Token账单",
            tab1_rows=[day2_finding],
            tab2_rows=[day2_ledger],
        )

        tab1_rows = sheet_store[tab1_title]
        assert len(tab1_rows) == 3  # 1 header + Day 1 row + Day 2 row
        assert [r[1] for r in tab1_rows[1:]] == ["cam01_day1.mp4", "cam02_day2.mp4"]

        tab2_rows = sheet_store[tab2_title]
        assert len(tab2_rows) == 3  # 1 header + Day 1 ledger + Day 2 ledger
        assert [r[2] for r in tab2_rows[1:]] == ["cam01_day1.mp4", "cam02_day2.mp4"]
        assert [r[10] for r in tab2_rows[1:]] == [11000, 13200]

        # Day 3: Re-audit cam01_day1.mp4 (0 violations found on re-run) -> removes old cam01 finding, keeps cam02!
        day3_ledger = day1_ledger.model_copy(
            update={"audit_id": "job_day3_reaudit", "total_token_count": 9500, "flagged_events_count": 0}
        )
        await gw.create_dual_tab_report_sheet(
            parent_folder_id="folder_store_1",
            title="📊 AI稽核报告与Token账单",
            tab1_rows=[],
            tab2_rows=[day3_ledger],
        )
        tab1_after_reaudit = sheet_store[tab1_title]
        assert len(tab1_after_reaudit) == 2  # Header + cam02_day2.mp4
        assert tab1_after_reaudit[1][1] == "cam02_day2.mp4"

    asyncio.run(_run())


def test_method_1_smart_append_preserves_history_in_gcs_xlsx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verifies Method 1 (`方式一：同表智能追加`) in `GcsStorageGateway.create_dual_tab_report_sheet`."""

    async def _run() -> None:
        gcs_gw = GcsStorageGateway()
        uploaded_blobs: Dict[str, bytes] = {}

        def _fake_request(
            method: str,
            url: str,
            *,
            body: Any = None,
            content_type: str = "",
            content_length: Optional[int] = None,
            timeout: float = 30.0,
        ) -> Tuple[int, bytes]:
            if method == "GET":
                if "latest" in uploaded_blobs:
                    return 200, uploaded_blobs["latest"]
                return 404, b""
            if method == "POST":
                raw_bytes = body if isinstance(body, (bytes, bytearray)) else body.read()
                uploaded_blobs["latest"] = bytes(raw_bytes)
                return 200, b'{"name": "store_01/report.xlsx"}'
            return 200, b"{}"

        monkeypatch.setattr(gcs_gw, "_request", _fake_request)

        day1_finding = ViolationSheetRow(
            audit_id="job_gcs_1",
            video_filename="gcs_cam01.mp4",
            timestamp_in_clip="00:45",
            rule_id="A1",
            disposition="CONFIRMED",
            severity="RED_LINE",
            confidence=0.9,
            evidence_description="GCS Day 1 violation",
            evidence_drive_url="https://storage.cloud.google.com/bkt/ev1.mp4",
        )
        day1_ledger = TokenLedgerRow(
            audit_id="job_gcs_1",
            auditor_folder_id="gs://my-bkt/store_01",
            video_filename="gcs_cam01.mp4",
            video_duration_sec=300.0,
            resolution="1920x1080",
            model_version_used="gemini-3-flash-preview",
            prompt_version_used="v2.5",
            prompt_token_count=8000,
            thoughts_token_count=500,
            candidates_token_count=200,
            total_token_count=8700,
            estimated_cost_usd=0.006,
            e2e_latency_ms=35000,
            flagged_events_count=1,
        )
        await gcs_gw.create_dual_tab_report_sheet(
            parent_folder_id="gs://my-bkt/store_01",
            title="📊 AI稽核报告与Token账单",
            tab1_rows=[day1_finding],
            tab2_rows=[day1_ledger],
        )

        day2_finding = day1_finding.model_copy(
            update={
                "audit_id": "job_gcs_2",
                "video_filename": "gcs_cam02.mp4",
                "evidence_description": "GCS Day 2 violation",
            }
        )
        day2_ledger = day1_ledger.model_copy(
            update={
                "audit_id": "job_gcs_2",
                "video_filename": "gcs_cam02.mp4",
                "total_token_count": 9300,
            }
        )
        await gcs_gw.create_dual_tab_report_sheet(
            parent_folder_id="gs://my-bkt/store_01",
            title="📊 AI稽核报告与Token账单",
            tab1_rows=[day2_finding],
            tab2_rows=[day2_ledger],
        )

        raw_xlsx = uploaded_blobs["latest"]
        sheets = read_xlsx_sheets(raw_xlsx)
        rows1 = sheets["违规事件 3 秒复核台"]
        assert [r[1] for r in rows1[1:3]] == ["gcs_cam01.mp4", "gcs_cam02.mp4"]

        rows2 = sheets["本次视频 Token 消耗与耗时账单"]
        assert [r[2] for r in rows2[1:3]] == ["gcs_cam01.mp4", "gcs_cam02.mp4"]
        assert [int(float(r[10])) for r in rows2[1:3]] == [8700, 9300]

    asyncio.run(_run())


def test_scheduled_engine_e2e_multi_store_batch_3state_ledger_and_watchdog_isolation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end verification of `ScheduledAuditEngine`:
    1. Overlapping duplicate rules trigger tie-breaker status write-back.
    2. Bottom-up discovery routes 2 leaf stores (`Store_A` and `Store_B`) while carving out a child
       rule (`Store_Flagship` at 04:00).
    3. Store jobs are created with `execution_backend="cloud_run_job"` and ignored by the 2-minute
       Service watchdog (`list_recoverable_jobs`).
    4. Executing each store job suppresses individual store Chat notifications and sends EXACTLY ONE
       consolidated multi-store Google Chat summary card when the last store completes, updating
       `Tab 2` `Last_Run_Status` and transitioning file ledgers to `COMPLETED`.
    5. Subsequent ticks on the same day or next day skip `COMPLETED` videos, while a video whose
       previous job `FAILED` is automatically re-claimed and re-audited.
    """

    async def _run() -> None:
        job_store = UserScopedJobStore(state_dir=tmp_path / "jobs")
        state_store = ScheduledStateStore(local_dir=tmp_path / "sched_state")
        svc = AuditService(job_store=job_store)

        # Stub preflight setup problem check & active prompt config
        async def _no_setup_problem(folder_id: str, impersonate_user: Optional[str] = None) -> Optional[str]:
            return None

        monkeypatch.setattr(svc, "_workspace_setup_problem", _no_setup_problem)

        fake_prompt_cfg = PromptModelConfig(
            active_prompt_version="v2.5",
            active_model_version="gemini-3-flash-preview",
            system_instruction="Test SOP instruction",
        )
        status_updates: Dict[int, str] = {}

        async def _load_active_cfg(sop_source: Optional[str] = None) -> PromptModelConfig:
            return fake_prompt_cfg

        async def _update_rule_status(row_index: int, status_text: str, sheet_id: Optional[str] = None) -> None:
            status_updates[row_index] = status_text

        monkeypatch.setattr(svc.prompt_manager, "load_active_config", _load_active_cfg)
        monkeypatch.setattr(svc.prompt_manager, "update_scheduled_rule_status", _update_rule_status)

        # Track single-store vs batch notifications
        single_store_notifications: List[str] = []
        batch_summary_notifications: List[Dict[str, Any]] = []

        async def _fake_run_detached_audit(job: AuditJob, prompt_cfg: PromptModelConfig) -> None:
            # Verify that scheduled batch jobs have batch_id set so single-store notification is suppressed
            assert job.batch_id != ""
            assert job.execution_backend == "cloud_run_job"
            ckpt = SegmentCheckpoint(
                segment_key="0_0",
                file_id=job.preflight_report.videos[0].file_id,
                filename=job.preflight_report.videos[0].filename,
                segment_index=1,
                total_segments_for_video=1,
                findings=[],
                ledger_row=TokenLedgerRow(
                    audit_id=job.job_id,
                    auditor_folder_id=job.folder_id,
                    video_filename=job.preflight_report.videos[0].filename,
                    video_duration_sec=300.0,
                    resolution="1920x1080",
                    model_version_used=prompt_cfg.active_model_version,
                    prompt_version_used=prompt_cfg.active_prompt_version,
                    prompt_token_count=5000,
                    thoughts_token_count=300,
                    candidates_token_count=200,
                    total_token_count=5500,
                    estimated_cost_usd=0.0042,
                    e2e_latency_ms=30000,
                    flagged_events_count=1,
                ),
                completed_at=1000.0,
            )
            done_job = job.model_copy(
                update={
                    "state": JobState.DONE,
                    "completed_segments": {"0_0": ckpt},
                    "violations_found": 1,
                    "total_tokens_used": 5500,
                    "report_sheet_url": f"https://docs.google.com/spreadsheets/d/sheet_{job.folder_id}",
                }
            )
            await svc.jobs.save(done_job)

        monkeypatch.setattr(svc, "_run_detached_audit", _fake_run_detached_audit)

        async def _fake_send_batch_summary(**kwargs: Any) -> bool:
            batch_summary_notifications.append(kwargs)
            return True

        monkeypatch.setattr(svc.reporter, "send_batch_summary_notification", _fake_send_batch_summary)

        # Configure 3 rules in Tab 2:
        # - Row 2: Regional rule (02:00)
        # - Row 3: Child flagship rule (04:00) -> Carve-Out target
        # - Row 4: Duplicate regional rule on same folder_vn_root (02:00) -> Shadowed by Row 2
        r_region = _make_sample_rule(
            row_index=2,
            rule_id="RULE-REGION",
            root_folder_id="folder_vn_root",
            trigger_time="02:00",
        )
        r_flagship = _make_sample_rule(
            row_index=3,
            rule_id="RULE-FLAGSHIP",
            root_folder_id="folder_store_flagship",
            trigger_time="04:00",
        )
        r_dup = _make_sample_rule(
            row_index=4,
            rule_id="RULE-DUP",
            root_folder_id="folder_vn_root",
            trigger_time="02:00",
        )

        async def _load_rules(sheet_id: Optional[str] = None) -> List[ScheduledAuditRule]:
            return [r_region, r_flagship, r_dup]

        monkeypatch.setattr(svc.prompt_manager, "load_scheduled_rules", _load_rules)

        folders = {
            "folder_vn_root": {"id": "folder_vn_root", "name": "VN_Root", "parents": [], "trashed": False},
            "folder_store_a": {"id": "folder_store_a", "name": "Store_A", "parents": ["folder_vn_root"], "trashed": False},
            "folder_store_b": {"id": "folder_store_b", "name": "Store_B", "parents": ["folder_vn_root"], "trashed": False},
            "folder_store_flagship": {
                "id": "folder_store_flagship",
                "name": "Store_Flagship",
                "parents": ["folder_vn_root"],
                "trashed": False,
            },
        }
        videos = [
            {
                "id": "vid_a1",
                "name": "storeA_01.mp4",
                "mimeType": "video/mp4",
                "size": "1000000",
                "parents": ["folder_store_a"],
                "createdTime": "2026-10-14T18:00:00Z",
                "videoMediaMetadata": {"width": 1920, "height": 1080, "durationMillis": 300000},
            },
            {
                "id": "vid_b1",
                "name": "storeB_01.mp4",
                "mimeType": "video/mp4",
                "size": "1000000",
                "parents": ["folder_store_b"],
                "createdTime": "2026-10-14T18:05:00Z",
                "videoMediaMetadata": {"width": 1920, "height": 1080, "durationMillis": 300000},
            },
            {
                "id": "vid_flagship",
                "name": "flagship_01.mp4",
                "mimeType": "video/mp4",
                "size": "1000000",
                "parents": ["folder_store_flagship"],
                "createdTime": "2026-10-14T18:10:00Z",
                "videoMediaMetadata": {"width": 1920, "height": 1080, "durationMillis": 300000},
            },
        ]
        fake_drive = _FakeDriveService(video_files=videos, folders=folders)

        dispatched_jobs: List[AuditJob] = []
        engine = ScheduledAuditEngine(
            audit_service=svc,
            state_store=state_store,
            drive_service_factory=lambda email: fake_drive,
            cloud_run_job_dispatcher=lambda j: dispatched_jobs.append(j),
        )

        # Tick 1 at 02:10 Asia/Ho_Chi_Minh (19:10 UTC on 2026-10-14)
        tick_time = dt.datetime(2026, 10, 14, 19, 10, tzinfo=dt.timezone.utc)
        tick_res = await engine.run_scheduled_tick(now_utc=tick_time)

        assert tick_res["status"] == "triggered"
        assert tick_res["shadowed_rules"] == ["RULE-DUP"]
        assert "配置重叠，已由第 2 行规则 [RULE-REGION] 代理执行" in status_updates[4]
        assert len(dispatched_jobs) == 2  # Store_A and Store_B (Store_Flagship was carved out!)

        # Verify 2-minute Service watchdog ignores `execution_backend == "cloud_run_job"`
        recoverable = await job_store.list_recoverable_jobs(stale_timeout_sec=0.0)
        assert recoverable == []

        # Tick 2 five minutes later on the same day -> blocked by daily atomic lock!
        tick2_res = await engine.run_scheduled_tick(
            now_utc=dt.datetime(2026, 10, 14, 19, 15, tzinfo=dt.timezone.utc)
        )
        assert tick2_res["status"] == "idle"
        assert len(dispatched_jobs) == 2

        # Now execute Store 1 via `engine.execute_scheduled_store_job` -> Batch not finished yet (0 summary cards)
        job1, job2 = dispatched_jobs
        await engine.execute_scheduled_store_job(
            job_id=job1.job_id, user_id=job1.user_id, batch_id=job1.batch_id
        )
        assert len(batch_summary_notifications) == 0

        # Execute Store 2 -> Last store in batch finishes, sending EXACTLY ONE consolidated Chat summary card!
        await engine.execute_scheduled_store_job(
            job_id=job2.job_id, user_id=job2.user_id, batch_id=job2.batch_id
        )
        assert len(batch_summary_notifications) == 1
        summary_call = batch_summary_notifications[0]
        assert summary_call["rule_id"] == "RULE-REGION"
        assert len(summary_call["store_summaries"]) == 2
        assert "门店 2/2 家（共 2 支视频）" in status_updates[2]
        assert len(single_store_notifications) == 0

        # Verify both videos' 3-state ledgers are now COMPLETED
        for vid_id in ("vid_a1", "vid_b1"):
            raw_l, _ = state_store.read_text_with_generation(state_store.file_ledger_key(vid_id))
            assert raw_l is not None
            assert FileLedgerEntry.model_validate_json(raw_l).state == FileLedgerState.COMPLETED

        # Next day tick with the SAME videos -> 0 new videos claimed (`no_new_videos`, 0 Token spent)
        next_day_tick = dt.datetime(2026, 10, 15, 19, 10, tzinfo=dt.timezone.utc)
        # Adjust video createdTime so it's within the next day's 24h window but already in COMPLETED ledger
        videos[0]["createdTime"] = "2026-10-15T18:00:00Z"
        videos[1]["createdTime"] = "2026-10-15T18:05:00Z"
        tick3_res = await engine.run_scheduled_tick(now_utc=next_day_tick)
        assert tick3_res["triggered_batches"][0]["status"] == "no_new_videos"
        assert "无新增待检视频（0 Token 消耗）" in status_updates[2]

        # Now simulate a video whose previous job FAILED -> 3-state ledger must allow re-claim on Day 3!
        await engine.mark_file_ledger_terminal(
            "vid_a1", job_id=job1.job_id, succeeded=False, error_message="Simulated transient error"
        )
        day3_tick = dt.datetime(2026, 10, 16, 19, 10, tzinfo=dt.timezone.utc)
        videos[0]["createdTime"] = "2026-10-16T18:00:00Z"
        videos[1]["createdTime"] = "2026-10-16T18:05:00Z"
        dispatched_jobs.clear()
        tick4_res = await engine.run_scheduled_tick(now_utc=day3_tick)
        assert tick4_res["triggered_batches"][0]["status"] == "dispatched"
        assert tick4_res["triggered_batches"][0]["stores_count"] == 1
        assert tick4_res["triggered_batches"][0]["videos_count"] == 1
        assert len(dispatched_jobs) == 1
        assert dispatched_jobs[0].folder_id == "folder_store_a"

    asyncio.run(_run())


def test_gcs_closest_ancestor_carve_out() -> None:
    """Verifies GCS prefix discovery also enforces the Time-Agnostic Closest-Ancestor Carve-Out."""
    now_utc = dt.datetime(2026, 10, 15, 2, 0, tzinfo=dt.timezone.utc)
    r_parent = _make_sample_rule(
        row_index=2,
        rule_id="RULE-GCS-PARENT",
        root_folder_url="gs://chagee-cctv/vn",
        root_folder_id="gs://chagee-cctv/vn",
        trigger_time="02:00",
    )
    r_child = _make_sample_rule(
        row_index=3,
        rule_id="RULE-GCS-CHILD",
        root_folder_url="gs://chagee-cctv/vn/hcmc/flagship",
        root_folder_id="gs://chagee-cctv/vn/hcmc/flagship",
        trigger_time="05:00",
    )

    class _FakeGcsGw:
        def _request(self, method: str, url: str, timeout: float = 20.0) -> Tuple[int, bytes]:
            payload = {
                "items": [
                    {
                        "name": "vn/hcmc/store_normal/cam01.mp4",
                        "size": "5000000",
                        "contentType": "video/mp4",
                        "timeCreated": "2026-10-15T01:00:00Z",
                    },
                    {
                        "name": "vn/hcmc/flagship/cam01.mp4",
                        "size": "5000000",
                        "contentType": "video/mp4",
                        "timeCreated": "2026-10-15T01:00:00Z",
                    },
                ]
            }
            import json as _json

            return 200, _json.dumps(payload).encode("utf-8")

        def _is_video_object(self, item: Dict[str, Any]) -> bool:
            return str(item.get("name") or "").endswith(".mp4")

        def _object_dims(self, bucket: str, item: Dict[str, Any]) -> Tuple[int, int, float]:
            return 1920, 1080, 300.0

    routed = discover_and_route_gcs_videos_sync(
        _FakeGcsGw(),
        canonical_enabled_rules=[r_parent, r_child],
        now_utc=now_utc,
    )
    assert [c.file_id for c in routed["RULE-GCS-PARENT"]] == [
        "gs://chagee-cctv/vn/hcmc/store_normal/cam01.mp4"
    ]
    assert [c.file_id for c in routed["RULE-GCS-CHILD"]] == [
        "gs://chagee-cctv/vn/hcmc/flagship/cam01.mp4"
    ]


def test_main_tf_declares_cloud_run_job_24h_and_scheduled_tick() -> None:
    """Static verification that `main.tf` defines the 24h Cloud Run Job, IAM binding, and Scheduler tick."""
    main_tf = (Path(__file__).resolve().parent.parent / "main.tf").read_text(encoding="utf-8")
    assert 'resource "google_cloud_run_v2_job" "cctv_audit_job_worker"' in main_tf
    assert 'timeout         = "86400s"' in main_tf
    assert 'command = ["python3", "-m", "cctv_audit.job_runner"]' in main_tf
    assert 'resource "google_cloud_run_v2_job_iam_member" "worker_job_executor"' in main_tf
    assert 'role     = "roles/run.jobsExecutorWithOverrides"' in main_tf
    assert 'resource "google_cloud_scheduler_job" "cctv_audit_scheduled_tick"' in main_tf
    assert "/internal/jobs/scheduled-tick" in main_tf


def test_master_sheet_snapshot_and_xlsx_seed_contain_tab2_scheduled_rules() -> None:
    """Verifies that both `sop/master_sheet.json` and `sop/master_sheet.xlsx` include `Tab2_定时巡检配置表`
    with the canonical 9-column header and default-DISABLED example rows."""
    import json
    from cctv_audit.gcs_gateway import read_xlsx_sheets
    from cctv_audit.prompt_manager import SCHEDULED_RULE_HEADERS, TAB2_SCHEDULED_TITLE

    root = Path(__file__).resolve().parent.parent
    snapshot_doc = json.loads((root / "sop" / "master_sheet.json").read_text(encoding="utf-8"))
    tabs_by_title = {t["title"]: t["values"] for t in snapshot_doc["tabs"]}
    assert TAB2_SCHEDULED_TITLE in tabs_by_title
    tab2_rows = tabs_by_title[TAB2_SCHEDULED_TITLE]
    assert tab2_rows[0] == list(SCHEDULED_RULE_HEADERS)

    parsed_json = parse_scheduled_rules_from_rows(tab2_rows)
    assert len(parsed_json) == 2
    assert all(not r.enabled for r in parsed_json)
    assert {r.rule_id for r in parsed_json} == {"SCHED-EXAMPLE-DRIVE", "SCHED-EXAMPLE-GCS"}

    xlsx_sheets = read_xlsx_sheets((root / "sop" / "master_sheet.xlsx").read_bytes())
    assert TAB2_SCHEDULED_TITLE in xlsx_sheets
    assert xlsx_sheets[TAB2_SCHEDULED_TITLE][0] == list(SCHEDULED_RULE_HEADERS)
    parsed_xlsx = parse_scheduled_rules_from_rows(xlsx_sheets[TAB2_SCHEDULED_TITLE])
    assert len(parsed_xlsx) == 2
    assert all(not r.enabled for r in parsed_xlsx)


def test_scheduled_state_store_urllib_gcs_cas_and_412_conflict() -> None:
    """Verifies that `ScheduledStateStore` uses stdlib `urllib` GCS JSON API (no `google.cloud.storage`
    runtime dependency) and correctly passes `ifGenerationMatch` and maps HTTP 412 to `PreconditionFailedError`."""
    import json
    from cctv_audit.scheduled_auditor import PreconditionFailedError

    store = ScheduledStateStore(gcs_bucket_name="my-test-bucket")
    assert store._use_memory is False

    calls: List[Tuple[str, str, Optional[bytes]]] = []
    responses: List[Tuple[int, bytes]] = [
        (404, b"Not Found"),
        (200, json.dumps({"generation": "101"}).encode("utf-8")),
        (200, json.dumps({"generation": "101"}).encode("utf-8")),
        (200, b'{"rule_id": "R1"}'),
        (412, b"Precondition Failed"),
    ]

    def _fake_request_gcs(
        method: str,
        url: str,
        *,
        body: Optional[bytes] = None,
        content_type: str = "",
        timeout: float = 20.0,
    ) -> Tuple[int, bytes]:
        calls.append((method, url, body))
        return responses.pop(0)

    store._request_gcs = _fake_request_gcs  # type: ignore[method-assign]

    # 1. Missing object returns (None, 0)
    raw0, gen0 = store.read_text_with_generation("schedules/locks/R1_2026-10-15.json")
    assert raw0 is None and gen0 == 0
    assert "fields=generation" in calls[-1][1]

    # 2. CAS create with if_generation_match=0
    new_gen = store.write_text_cas(
        "schedules/locks/R1_2026-10-15.json",
        '{"rule_id": "R1"}',
        if_generation_match=0,
    )
    assert new_gen == 101
    assert "ifGenerationMatch=0" in calls[-1][1]

    # 3. Read existing object returns (raw, 101)
    raw1, gen1 = store.read_text_with_generation("schedules/locks/R1_2026-10-15.json")
    assert raw1 == '{"rule_id": "R1"}'
    assert gen1 == 101
    assert "alt=media&generation=101" in calls[-1][1]

    # 4. HTTP 412 maps to PreconditionFailedError
    with pytest.raises(PreconditionFailedError):
        store.write_text_cas(
            "schedules/locks/R1_2026-10-15.json",
            '{"rule_id": "R1", "updated": true}',
            if_generation_match=100,
        )
    assert "ifGenerationMatch=100" in calls[-1][1]


def test_job_runner_default_audit_service_wires_routing_storage_gateway() -> None:
    """Verifies that `cctv_audit.job_runner._build_default_audit_service()` injects
    `RoutingStorageGateway` into both `VideoIngestor` (`_drive_reader`) and `WorkspaceReporter`
    (`_gateway`), matching `cctv_audit.server.audit_service` so Cloud Run Jobs can download
    videos and write reports for both Google Drive and `gs://` store folders."""
    from cctv_audit.gcs_gateway import RoutingStorageGateway
    from cctv_audit.job_runner import _build_default_audit_service

    svc = _build_default_audit_service()
    assert isinstance(svc.ingestor._drive_reader, RoutingStorageGateway)
    assert isinstance(svc.reporter._gateway, RoutingStorageGateway)


def test_req015_portal_hmac_signing_and_composite_key_review_writeback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verifies REQ-015 HTML Supervisor Portal & Opus Round 8 Safeguards:
    - [P0-2] HMAC-SHA256 signature verification accepts valid URLs and rejects expired or tampered signatures.
    - [P0-1] Composite-key row matching (`match_and_apply_tab1_reviews` & `apply_portal_reviews`) updates
      Columns J (`人工复核状态`) and K (`人工复核备注`) accurately even after rows in Tab 1 are reordered.
    - [P1-2] Multi-store `Batch Store Switcher` renders store pills and switches active store via `&store=`.
    """
    import time
    import urllib.parse
    from cctv_audit.gcs_gateway import TAB1_HEADERS, TAB2_HEADERS, build_incident_composite_key
    from cctv_audit.jobs import AuditJob, JobState, UserScopedJobStore
    from cctv_audit.portal import (
        apply_portal_reviews,
        build_portal_url,
        compute_portal_signature,
        load_portal_payload,
        render_supervisor_portal_html,
        verify_portal_signature,
    )
    from cctv_audit.scheduled_auditor import ScheduledBatchRecord, ScheduledStoreTask
    from cctv_audit.video_ingestor import VideoIngestor
    from cctv_audit.workspace_reporter import WorkspaceReporter

    async def _run() -> None:
        monkeypatch.setenv("CLOUD_RUN_WORKER_URL", "https://worker.example.run.app")

        # 1. HMAC signature generation & verification ([P0-2]) — default TTL is 30 days (2,592,000s)
        fixed_now = 1790000000
        url_job = build_portal_url(job_id="job123", user_id="sup@chagee.com", now=fixed_now)
        parsed = urllib.parse.urlparse(url_job)
        qs = urllib.parse.parse_qs(parsed.query)
        exp = int(qs["exp"][0])
        assert exp == fixed_now + 30 * 86400
        sig = qs["sig"][0]
        ok1, _ = verify_portal_signature(
            job_id="job123", user_id="sup@chagee.com", exp=exp, sig=sig, now=exp - 100
        )
        assert ok1 is True
        # Trailing punctuation from chat clients (e.g. `)**`) is stripped before comparison
        ok1_punct, _ = verify_portal_signature(
            job_id="job123", user_id="sup@chagee.com", exp=exp, sig=sig + ")**", now=exp - 100
        )
        assert ok1_punct is True
        # Expired signature rejected
        ok2, _ = verify_portal_signature(
            job_id="job123", user_id="sup@chagee.com", exp=exp, sig=sig, now=exp + 10
        )
        assert ok2 is False
        # Tampered scope_id or sig rejected
        ok3, _ = verify_portal_signature(
            job_id="job999", user_id="sup@chagee.com", exp=exp, sig=sig, now=exp - 100
        )
        assert ok3 is False
        ok4, _ = verify_portal_signature(
            job_id="job123", user_id="sup@chagee.com", exp=exp, sig="0" * 64, now=exp - 100
        )
        assert ok4 is False

        # 2. Prepare simulated Tab 1 & Tab 2 data rows (excluding header row, matching read_report_sheet_data)
        tab1_rows: List[List[Any]] = [
            [
                "job123",
                "cam01_morning.mp4",
                "03:12 - 03:27",
                "C1",
                "CONFIRMED",
                "CRITICAL",
                "0.96",
                "员工接触垃圾桶后未执行6步法洗手",
                "https://drive.google.com/file/d/ev1/view",
                "待复核",
                "",
            ],
            [
                "job123",
                "cam02_noon.mp4",
                "04:05 - 04:18",
                "B1",
                "SUSPECTED",
                "NORMAL",
                "0.85",
                "取冰后冰铲疑似未放回消毒盒",
                "https://drive.google.com/file/d/ev2/view",
                "待复核",
                "",
            ],
        ]
        tab2_rows: List[List[Any]] = [
            [
                "job123",
                "cam01_morning.mp4",
                0,
                300.0,
                "1920x1080",
                "gemini-3.8-flash",
                "v10.0_full",
                120000,
                2500,
                1000,
                123500,
                0.0320,
                180.5,
                2,
            ],
        ]

        class _FakePortalGateway:
            async def read_report_sheet_data(
                self, folder_id: str, report_sheet_url: str = ""
            ) -> Tuple[List[List[Any]], List[List[Any]]]:
                return [list(r) for r in tab1_rows], [list(r) for r in tab2_rows]

            async def update_report_human_reviews(
                self,
                folder_id: str,
                report_sheet_url: str,
                updates: List[Dict[str, str]],
            ) -> int:
                from cctv_audit.gcs_gateway import match_and_apply_tab1_reviews

                updated_rows, patches = match_and_apply_tab1_reviews(tab1_rows, updates)
                tab1_rows[:] = updated_rows
                return len(patches)

        fake_gw = _FakePortalGateway()
        job_store = UserScopedJobStore(state_dir=tmp_path / "jobs")
        svc = AuditService(
            job_store=job_store,
            ingestor=VideoIngestor(drive_reader=fake_gw),  # type: ignore[arg-type]
            reporter=WorkspaceReporter(gateway=fake_gw),  # type: ignore[arg-type]
        )
        state_store = ScheduledStateStore(gcs_bucket_name="")
        svc.state_store = state_store  # type: ignore[attr-defined]

        job1 = await job_store.save(
            AuditJob(
                job_id="job123",
                user_id="sup@chagee.com",
                folder_id="folder_store_1",
                folder_name="Store_Bau_Cat",
                state=JobState.DONE,
                violations_found=2,
                total_tokens_used=123500,
                active_model_version="gemini-3.8-flash",
                active_prompt_version="v10.0_full",
                report_sheet_url="https://docs.google.com/spreadsheets/d/sheet1/edit",
            )
        )
        job2 = await job_store.save(
            AuditJob(
                job_id="job456",
                user_id="sup@chagee.com",
                folder_id="folder_store_2",
                folder_name="Store_Cantavil",
                state=JobState.DONE,
                violations_found=0,
                total_tokens_used=95000,
                active_model_version="gemini-3.8-flash",
                active_prompt_version="v10.0_full",
                report_sheet_url="https://docs.google.com/spreadsheets/d/sheet2/edit",
            )
        )

        # Save multi-store batch record for [P1-2] Batch Store Switcher verification
        batch_rec = ScheduledBatchRecord(
            batch_id="batch_999",
            rule_id="RULE-VN",
            rule_name="越南大区每日巡检",
            row_index=2,
            root_folder_id="root_vn",
            root_folder_url="https://drive.google.com/drive/folders/root_vn",
            supervisor_email="sup@chagee.com",
            local_date="2026-10-15",
            store_tasks={
                "job123": ScheduledStoreTask(
                    job_id="job123",
                    user_id="sup@chagee.com",
                    folder_id="folder_store_1",
                    folder_name="Store_Bau_Cat",
                    video_count=2,
                    state="DONE",
                    violations_found=2,
                    total_tokens=123500,
                    report_sheet_url=job1.report_sheet_url or "",
                ),
                "job456": ScheduledStoreTask(
                    job_id="job456",
                    user_id="sup@chagee.com",
                    folder_id="folder_store_2",
                    folder_name="Store_Cantavil",
                    video_count=1,
                    state="DONE",
                    violations_found=0,
                    total_tokens=95000,
                    report_sheet_url=job2.report_sheet_url or "",
                ),
            },
        )
        state_store.write_text_cas(
            state_store.batch_key("batch_999"),
            batch_rec.model_dump_json(indent=2),
            if_generation_match=0,
        )

        # 3. Load single-store portal & render HTML
        valid_exp = int(time.time()) + 3600
        valid_job_sig = compute_portal_signature(
            job_id="job123", user_id="sup@chagee.com", exp=valid_exp
        )
        payload = await load_portal_payload(
            audit_service=svc,
            gateway=fake_gw,
            job_id="job123",
            user_id="sup@chagee.com",
            exp=valid_exp,
            sig=valid_job_sig,
        )
        assert len(payload["findings"]) == 2
        assert len(payload["sop_rules"]) == 24
        assert payload["sop_rules"][0]["clean_subtitle"]
        assert payload["sop_rules"][0]["module_code"] == "A"
        assert payload["findings"][0]["severity"] == "RED_LINE"
        assert payload["findings"][0]["rule_subtitle"]
        html_out = render_supervisor_portal_html(payload)
        assert "CHAGEE 霸王茶姬 · 门店 AI 视频稽核督导工作台" in html_out
        assert "Store_Bau_Cat" in html_out
        assert "cam01_morning.mp4" in html_out
        assert "function exportXLSX()" in html_out
        assert "function setAuthUser(" in html_out
        assert "async function persistReviewsToSheet(" in html_out
        assert "/api/portal/review" in html_out

        # 4. Load multi-store batch portal & verify Batch Store Switcher ([P1-2])
        valid_batch_sig = compute_portal_signature(batch_id="batch_999", exp=valid_exp)
        batch_payload = await load_portal_payload(
            audit_service=svc,
            gateway=fake_gw,
            batch_id="batch_999",
            store_job_id="job456",
            exp=valid_exp,
            sig=valid_batch_sig,
        )
        assert batch_payload["audit_id"] == "job456"
        assert batch_payload["store_name"] == "Store_Cantavil"
        assert len(batch_payload["batch_stores"]) == 2
        batch_html = render_supervisor_portal_html(batch_payload)
        assert "🏪 批次门店切换" in batch_html
        assert "Store_Bau_Cat" in batch_html and "Store_Cantavil" in batch_html

        # 5. [P0-1] Simulate supervisor reordering/sorting Tab 1 rows in Google Sheets BEFORE clicking review in Portal!
        inc_1_id = payload["findings"][0]["incident_id"]  # C1 on cam01_morning.mp4 (originally row 0)
        inc_2_id = payload["findings"][1]["incident_id"]  # B1 on cam02_noon.mp4 (originally row 1)
        assert inc_1_id == build_incident_composite_key(
            "job123", "cam01_morning.mp4", "03:12 - 03:27", "C1", 0
        )
        # Swap row 0 and row 1 in tab1_rows to simulate Sheet sorting
        tab1_rows[0], tab1_rows[1] = tab1_rows[1], tab1_rows[0]

        res = await apply_portal_reviews(
            audit_service=svc,
            gateway=fake_gw,
            job_id="job123",
            user_id="sup@chagee.com",
            exp=valid_exp,
            sig=valid_job_sig,
            reviews=[
                {
                    "incident_id": inc_1_id,
                    "status": "CONFIRMED_VIOLATION",
                    "note": "已与店长核实，确系未洗手",
                },
                {
                    "incident_id": inc_2_id,
                    "status": "DISMISSED",
                    "note": "冰铲实际在消毒盒边缘",
                },
            ],
        )
        assert res["ok"] is True
        assert res["updated_count"] == 2
        # Row 0 is now cam02_noon.mp4 (inc_2_id -> DISMISSED) and Row 1 is cam01_morning.mp4 (inc_1_id -> CONFIRMED)
        assert tab1_rows[0][1] == "cam02_noon.mp4"
        assert tab1_rows[0][9] == "❌ 误报排除 (FALSE_POSITIVE)"
        assert tab1_rows[0][10] == "冰铲实际在消毒盒边缘"
        assert tab1_rows[1][1] == "cam01_morning.mp4"
        assert tab1_rows[1][9] == "✅ 确认违规 (CONFIRMED)"
        assert tab1_rows[1][10] == "已与店长核实，确系未洗手"

        # 6. Verify browser JS payload contract (`job`/`user`/`updates` via `PortalReviewRequestPayload`)
        from cctv_audit.server import PortalReviewRequestPayload, PortalReviewUpdateItem

        req_payload = PortalReviewRequestPayload(
            job=payload["auth"]["job"],
            user=payload["auth"]["user"],
            batch=payload["auth"]["batch"],
            store=payload["auth"]["store"],
            exp=payload["auth"]["exp"],
            sig=payload["auth"]["sig"],
            updates=[
                PortalReviewUpdateItem(
                    incident_id=inc_1_id,
                    status="FOLLOWUP",
                    note="待调取次日复训录像",
                )
            ],
        )
        res_js = await apply_portal_reviews(
            audit_service=svc,
            gateway=fake_gw,
            job_id=req_payload.job_id or req_payload.job,
            batch_id=req_payload.batch_id or req_payload.batch,
            store_job_id=req_payload.store_job_id or req_payload.store,
            user_id=req_payload.user_id or req_payload.user,
            exp=req_payload.exp,
            sig=req_payload.sig,
            updates=[r.model_dump() for r in req_payload.updates],
        )
        assert res_js["ok"] is True
        assert res_js["updated_count"] == 1
        assert tab1_rows[1][9] == "⚠️ 待调取后续录像 (FOLLOWUP)"
        assert tab1_rows[1][10] == "待调取次日复训录像"

    asyncio.run(_run())


def test_unified_start_and_completion_chat_notifications_and_dedup_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verifies:
    - Single-store `start_audit` sends `send_job_start_notification` (`Job ID` + HTML portal link) once
      (`start_notification_sent=True`) and does NOT re-send on resume (`is_restart=True`, `[P1-1]`).
    - `WorkspaceReporter` formats start & completion Google Chat Webhook payloads with both HTML Portal
      (`🖥️ 打开 HTML 督导复核工作台`) and Google Sheet (`📊 打开原 Google Sheet 账单`) buttons.
    """
    import json
    import urllib.request
    from cctv_audit.config import config
    from cctv_audit.jobs import AuditJob, JobState, UserScopedJobStore
    from cctv_audit.video_ingestor import InspectFolderResponse
    from cctv_audit.workspace_reporter import WorkspaceReporter

    async def _run() -> None:
        monkeypatch.setenv("CLOUD_RUN_WORKER_URL", "https://worker.example.run.app")
        monkeypatch.setattr(config, "enable_google_chat_notification", True)
        monkeypatch.setattr(
            config,
            "google_chat_webhook_url",
            "https://chat.googleapis.com/v1/spaces/TEST/messages?key=k&token=t",
        )

        webhook_posts: List[Dict[str, Any]] = []

        class _FakeHttpResp:
            status_code = 200

            def raise_for_status(self) -> None:
                return None

        class _FakeAsyncClient:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                pass

            async def __aenter__(self) -> "_FakeAsyncClient":
                return self

            async def __aexit__(self, *args: Any) -> bool:
                return False

            async def post(self, url: str, json: Dict[str, Any], **kwargs: Any) -> _FakeHttpResp:
                webhook_posts.append(json)
                return _FakeHttpResp()

        import httpx

        monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)

        reporter = WorkspaceReporter(webhook_url="https://chat.googleapis.com/v1/spaces/TEST/messages?key=k&token=t")
        # 1. Test single-store start & completion webhook payloads directly
        ok_start = await reporter.send_job_start_notification(
            user_email="sup@chagee.com",
            audit_id="job_abc",
            folder_id="folder_1",
            folder_name="Store_01",
            video_count=2,
            total_duration_sec=600.0,
            estimated_minutes=12.0,
            model_version="gemini-3.8-flash",
            prompt_version="v10.0_full",
        )
        assert ok_start is True
        assert "text" not in webhook_posts[-1]  # Single clean cardsV2 card, no duplicate text block
        start_payload_str = json.dumps(webhook_posts[-1], ensure_ascii=False)
        assert "job_abc" in start_payload_str
        assert "/portal" not in start_payload_str  # Start notification does not include HTML dashboard link

        ok_done = await reporter.send_completion_notification(
            user_email="sup@chagee.com",
            audit_id="job_abc",
            sheet_url="https://docs.google.com/spreadsheets/d/s1/edit",
            violations_count=3,
            total_tokens=150000,
        )
        assert ok_done is True
        assert "text" not in webhook_posts[-1]  # Single clean cardsV2 card, no duplicate text block
        done_payload_str = json.dumps(webhook_posts[-1], ensure_ascii=False)
        assert "🖥️ 打开 HTML 督导复核工作台" in done_payload_str
        assert "📊 打开原 Google Sheet 账单" in done_payload_str
        assert "/portal?job=job_abc" in done_payload_str

        # 2. Test batch start & batch completion webhook payloads
        ok_bstart = await reporter.send_batch_start_notification(
            batch_id="batch_xyz",
            rule_id="RULE-01",
            rule_name="华东巡检",
            supervisor_email="sup@chagee.com",
            root_folder_url="https://drive.google.com/drive/folders/root1",
            store_summaries=[
                {"folder_name": "Store_01", "job_id": "j1", "video_count": 2, "state": "RUNNING"}
            ],
        )
        assert ok_bstart is True
        assert "text" not in webhook_posts[-1]
        bstart_str = json.dumps(webhook_posts[-1], ensure_ascii=False)
        assert "batch_xyz" in bstart_str
        assert "/portal" not in bstart_str  # Start notification does not include HTML dashboard link

        ok_bdone = await reporter.send_batch_summary_notification(
            batch_id="batch_xyz",
            rule_id="RULE-01",
            rule_name="华东巡检",
            supervisor_email="sup@chagee.com",
            root_folder_url="https://drive.google.com/drive/folders/root1",
            store_summaries=[
                {
                    "folder_name": "Store_01",
                    "job_id": "j1",
                    "video_count": 2,
                    "state": "DONE",
                    "violations_found": 3,
                    "total_tokens_used": 150000,
                    "report_sheet_url": "https://docs.google.com/spreadsheets/d/s1/edit",
                }
            ],
        )
        assert ok_bdone is True
        assert "text" not in webhook_posts[-1]
        bdone_str = json.dumps(webhook_posts[-1], ensure_ascii=False)
        assert "🌐 打开本批次多门店 HTML 复核工作台" in bdone_str
        assert "/portal?batch=batch_xyz" in bdone_str

        # 3. Verify [P1-1] start_notification_sent deduplication on AuditService.start_audit
        webhook_posts.clear()
        job_store = UserScopedJobStore(state_dir=tmp_path / "dedup_jobs")
        svc = AuditService(job_store=job_store, reporter=reporter)

        async def _noop_dispatch(job: Any, prompt_cfg: Any) -> None:
            return None

        monkeypatch.setattr(svc, "_dispatch_or_run_detached", _noop_dispatch)

        ready_job = await job_store.save(
            AuditJob(
                job_id="dedup1",
                user_id="sup@chagee.com",
                folder_id="folder_1",
                folder_name="Store_Dedup",
                state=JobState.READY,
                preflight_report=InspectFolderResponse(
                    passed=True,
                    folder_id="folder_1",
                    total_videos=1,
                    total_size_mb=20.0,
                    total_duration_sec=300.0,
                    planned_segments_count=1,
                    estimated_tokens=50000,
                    estimated_cost_usd=0.02,
                    estimated_minutes=6.0,
                    message_to_user="ok",
                    videos=[],
                ),
            )
        )
        started = await svc.start_audit(user_id="sup@chagee.com", job_id=ready_job.job_id)
        assert started.start_notification_sent is True
        assert len(webhook_posts) == 1

        # Simulate crash & resume (state=FAILED -> start_audit): must NOT send a second start notification!
        failed_job = await job_store.save(
            started.model_copy(update={"state": JobState.FAILED, "error_message": "transient"})
        )
        resumed = await svc.start_audit(user_id="sup@chagee.com", job_id=failed_job.job_id)
        assert resumed.resume_count == 1
        assert len(webhook_posts) == 1

        # 4. Verify send_eval_start_notification and send_eval_completion_notification
        # resolve gs:// webhook secret URIs via _resolve_webhook_url_sync instead of passing gs:// to httpx
        posted_urls: List[str] = []

        class _UrlCheckingClient(_FakeAsyncClient):
            async def post(self, url: str, json: Dict[str, Any], **kwargs: Any) -> _FakeHttpResp:
                assert url.startswith("https://chat.googleapis.com/"), f"Unexpected webhook URL: {url}"
                posted_urls.append(url)
                webhook_posts.append(json)
                return _FakeHttpResp()

        monkeypatch.setattr(httpx, "AsyncClient", _UrlCheckingClient)
        eval_reporter = WorkspaceReporter(webhook_url="gs://test-bucket/config/google_chat_webhook.txt")
        monkeypatch.setattr(
            eval_reporter,
            "_resolve_webhook_url_sync",
            lambda: "https://chat.googleapis.com/v1/spaces/EVAL/messages?key=k&token=t",
        )
        ok_eval_start = await eval_reporter.send_eval_start_notification(
            eval_id="EVAL-01",
            eval_name="V10 基线评测",
            run_id="eval_EVAL-01_run1",
            prompt_tab_name="Prompt_v2.5_V10全量17条标准版",
            model_version="gemini-3.8-flash",
            supervisor_email="sup@chagee.com",
            golden_videos_folder_url="https://drive.google.com/drive/folders/vid1",
            human_labels_sheet_url="https://docs.google.com/spreadsheets/d/lbl1/edit",
            results_folder_url="https://drive.google.com/drive/folders/res1",
        )
        assert ok_eval_start is True
        ok_eval_done = await eval_reporter.send_eval_completion_notification(
            eval_id="EVAL-01",
            eval_name="V10 基线评测",
            run_id="eval_EVAL-01_run1",
            prompt_tab_name="Prompt_v2.5_V10全量17条标准版",
            model_version="gemini-3.8-flash",
            supervisor_email="sup@chagee.com",
            status="DONE",
            summary={
                "spreadsheet_url": "https://docs.google.com/spreadsheets/d/rep1/edit",
                "overall_recall": 0.421,
                "holdout_recall": 0.444,
                "dev_recall": 0.400,
                "findings_per_clip": 5.2,
                "hit_rate": 0.5625,
                "total_cost_usd": 3.21,
                "total_tokens": 120000,
                "guardrails_passed": True,
            },
        )
        assert ok_eval_done is True
        assert posted_urls == [
            "https://chat.googleapis.com/v1/spaces/EVAL/messages?key=k&token=t",
            "https://chat.googleapis.com/v1/spaces/EVAL/messages?key=k&token=t",
        ]

    asyncio.run(_run())


def test_p0_2_oidc_guard_isolates_internal_endpoints_from_public_portal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verifies [P0-2]: when `ENFORCE_INTERNAL_OIDC_AUTH=true`, unauthenticated calls to
    `/internal/*`, `/a2a`, `/inspect`, and `/execute` are rejected with HTTP 401/403 while
    valid OIDC tokens succeed.
    """
    import cctv_audit.server as srv

    class _DummyReq:
        base_url = "https://worker.example.run.app/"

        def __init__(self, headers: Optional[Dict[str, str]] = None) -> None:
            self.headers = headers or {}

    async def _run() -> None:
        monkeypatch.setenv("ENFORCE_INTERNAL_OIDC_AUTH", "true")

        # 1. Missing Authorization header -> 401
        r1 = await srv.internal_scheduled_tick(_DummyReq())  # type: ignore[arg-type]
        assert getattr(r1, "status_code", None) == 401

        r2 = await srv.internal_sweep_stale_jobs(_DummyReq())  # type: ignore[arg-type]
        assert getattr(r2, "status_code", None) == 401

        # 2. Invalid Bearer token -> 403
        r3 = await srv.internal_scheduled_tick(
            _DummyReq({"Authorization": "Bearer invalid.jwt.token"})  # type: ignore[arg-type]
        )
        assert getattr(r3, "status_code", None) == 403

        # 3. Valid OIDC token -> allowed
        from google.oauth2 import id_token

        monkeypatch.setattr(
            id_token,
            "verify_oauth2_token",
            lambda token, req: {
                "iss": "https://accounts.google.com",
                "email": "chagee-cctv-audit-worker@example-project.iam.gserviceaccount.com",
            },
        )
        monkeypatch.setattr(
            srv.audit_service,
            "run_scheduled_tick",
            lambda: asyncio.sleep(0, result={"tick": "ok"}),
        )
        r4 = await srv.internal_scheduled_tick(
            _DummyReq({"Authorization": "Bearer valid.jwt.token"})  # type: ignore[arg-type]
        )
        assert r4 == {"tick": "ok"}

    asyncio.run(_run())


def test_simplified_utc_schema_parsing_and_lookback_days_conversion() -> None:
    """Verifies the simplified `Tab2_定时巡检配置表` schema (`Trigger_Hour_UTC`, `Trigger_Minute_UTC`,
    `Lookback_Days` dropdown options `0.5` or `1`–`7`, default `UTC` timezone, `ENABLED`/`PAUSED`).
    """
    from cctv_audit.prompt_manager import (
        SCHEDULED_RULE_HEADERS,
        parse_scheduled_rules_with_diagnostics,
    )

    rows = [
        list(SCHEDULED_RULE_HEADERS),
        [
            "SCHED-HALF-DAY",
            "12小时高频巡检",
            "https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOpQrStUvWxYz123456",
            "05",
            "00",
            "0.5",
            "sup1@chagee.com",
            "ENABLED",
            "",
        ],
        [
            "SCHED-WEEKLY",
            "7天周度回溯巡检",
            "gs://chagee-sg-cctv/stores/orchard",
            "9",
            "45",
            "7",
            "sup2@chagee.com",
            "PAUSED",
            "",
        ],
    ]
    valid_rules, errors = parse_scheduled_rules_with_diagnostics(rows)
    assert errors == []
    assert len(valid_rules) == 2

    r1, r2 = valid_rules
    assert r1.rule_id == "SCHED-HALF-DAY"
    assert r1.trigger_time == "05:00"
    assert r1.timezone == "UTC"
    assert r1.lookback_days == 0.5
    assert r1.lookback_hours == 12.0
    assert r1.enabled is True

    assert r2.rule_id == "SCHED-WEEKLY"
    assert r2.trigger_time == "09:45"
    assert r2.timezone == "UTC"
    assert r2.lookback_days == 7.0
    assert r2.lookback_hours == 168.0
    assert r2.enabled is False


def test_tab2_diagnostics_writeback_and_same_day_reschedule_lock(
    tmp_path: Path,
) -> None:
    """Verifies:
    1. Invalid rows in `Tab2_定时巡检配置表` (invalid hour, non-quarter minute, unsupported
       `Lookback_Days`, `/file/d/` single-video link, duplicate `Rule_ID`) produce diagnostic
       errors and `run_scheduled_tick` writes `❌ 配置错误：...` to Column I (`Last_Run_Status`).
    2. When an operator changes `Trigger_Hour_UTC` / `Trigger_Minute_UTC` on the same day,
       `try_acquire_daily_rule_lock` CAS-updates the existing daily lock so the rescheduled
       trigger executes instead of being blocked by an earlier hour's lock.
    """
    from cctv_audit.prompt_manager import (
        SCHEDULED_RULE_HEADERS,
        parse_scheduled_rules_with_diagnostics,
    )

    rows = [
        list(SCHEDULED_RULE_HEADERS),
        [
            "SCHED-OK",
            "正常规则",
            "https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOpQrStUvWxYz123456",
            "01",
            "00",
            "1",
            "sup@chagee.com",
            "ENABLED",
            "",
        ],
        [
            "SCHED-OK",
            "重复Rule_ID",
            "https://drive.google.com/drive/folders/1ZzYyXxWwVvUuTtSsRrQqPpOoNnMmLlKk",
            "02",
            "15",
            "1",
            "sup@chagee.com",
            "ENABLED",
            "",
        ],
        [
            "SCHED-BAD-FILE",
            "误填单视频链接",
            "https://drive.google.com/file/d/1SingleVideoFileId12345/view",
            "03",
            "30",
            "1",
            "sup@chagee.com",
            "ENABLED",
            "",
        ],
        [
            "SCHED-BAD-MIN",
            "非15分钟刻度",
            "https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOpQrStUvWxYz123456",
            "05",
            "10",
            "1",
            "sup@chagee.com",
            "ENABLED",
            "",
        ],
        [
            "SCHED-BAD-DAYS",
            "超范围天数",
            "https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOpQrStUvWxYz123456",
            "06",
            "00",
            "14",
            "sup@chagee.com",
            "ENABLED",
            "",
        ],
    ]
    valid_rules, row_errors = parse_scheduled_rules_with_diagnostics(rows)
    assert len(valid_rules) == 1
    assert valid_rules[0].rule_id == "SCHED-OK"
    assert [e.row_index for e in row_errors] == [3, 4, 5, 6]
    assert "重复" in row_errors[0].error_message
    assert "/file/d/" in row_errors[1].error_message
    assert "Trigger_Minute_UTC" in row_errors[2].error_message
    assert "Lookback_Days" in row_errors[3].error_message

    status_updates: Dict[int, str] = {}

    class _FakePromptMgr:
        last_scheduled_rule_errors = row_errors

        async def ensure_master_sheet_validations(self) -> bool:
            return True

        async def load_scheduled_rules(self):
            return valid_rules

        async def update_scheduled_rule_status(self, row_index: int, text: str) -> None:
            status_updates[row_index] = text

    class _FakeService:
        prompt_manager = _FakePromptMgr()
        jobs = UserScopedJobStore(state_dir=tmp_path / "jobs")

    state_store = ScheduledStateStore(memory_store={})
    engine = ScheduledAuditEngine(
        audit_service=_FakeService(),
        state_store=state_store,
        drive_service_factory=lambda _email: None,
    )

    # First run at 10:00 UTC (outside 01:00 UTC window, so idle, but writes all 4 row errors)
    res_idle = asyncio.run(
        engine.run_scheduled_tick(
            now_utc=dt.datetime(2026, 10, 16, 10, 0, tzinfo=dt.timezone.utc)
        )
    )
    assert res_idle["status"] == "idle"
    assert res_idle["invalid_rows"] == [3, 4, 5, 6]
    for r_idx in (3, 4, 5, 6):
        assert status_updates[r_idx].startswith("❌ 配置错误：")

    # Test same-day reschedule lock: acquire at 01:00 UTC on 2026-10-16, then change trigger_time to 05:15 UTC
    rule_0100 = valid_rules[0]
    t1 = dt.datetime(2026, 10, 16, 1, 5, tzinfo=dt.timezone.utc)
    assert engine.try_acquire_daily_rule_lock(rule_0100, "2026-10-16", t1) is True
    # Calling again with the SAME trigger_time ("01:00") on the same day is blocked
    assert engine.try_acquire_daily_rule_lock(rule_0100, "2026-10-16", t1) is False
    # Operator edits Trigger_Hour_UTC=05, Trigger_Minute_UTC=15 on the same day -> lock is re-acquired!
    rule_0515 = rule_0100.model_copy(update={"trigger_time": "05:15"})
    t2 = dt.datetime(2026, 10, 16, 5, 20, tzinfo=dt.timezone.utc)
    assert engine.try_acquire_daily_rule_lock(rule_0515, "2026-10-16", t2) is True
    # Subsequent tick at 05:30 UTC for the same 05:15 trigger_time is blocked
    assert engine.try_acquire_daily_rule_lock(rule_0515, "2026-10-16", t2) is False


def test_migrate_tab2_rows_and_build_master_sheet_validations() -> None:
    """Verifies legacy `Tab2_定时巡检配置表` rows (`13:00` / `13:30` `Asia/Singapore`, `24` hours)
    migrate cleanly to UTC (`05` / `00` and `05` / `30`, `1` day) and `build_master_sheet_validation_requests`
    emits strict dropdowns for Tab 0, Prompt_*, and Tab 2.
    """
    from cctv_audit.prompt_manager import (
        SCHEDULED_RULE_HEADERS,
        build_master_sheet_validation_requests,
        migrate_tab2_rows_to_utc_schema,
    )

    legacy_rows = [
        [
            "Rule_ID",
            "Rule_Name",
            "Root_Folder_URL",
            "Trigger_Time",
            "Timezone",
            "Lookback_Hours",
            "Supervisor_Email",
            "Enabled",
            "Last_Run_Status",
        ],
        [
            "SCHED-All",
            "全门店巡检",
            "https://drive.google.com/drive/folders/1RootFolderAll12345",
            "13:00",
            "Asia/Singapore",
            "24",
            "supervisor@example.com",
            "ENABLED",
            "ok",
        ],
        [
            "SCHED-Handwashing-Monitoring",
            "洗手专项",
            "https://drive.google.com/drive/folders/1SubFolderHand12345",
            "13:30",
            "Asia/Singapore",
            "12",
            "supervisor@example.com",
            "FALSE",
            "ok",
        ],
    ]
    changed, migrated = migrate_tab2_rows_to_utc_schema(legacy_rows)
    assert changed is True
    assert migrated[0] == list(SCHEDULED_RULE_HEADERS)
    # 13:00 Asia/Singapore (UTC+8) -> 05:00 UTC, 24h -> "1" day
    assert migrated[1][3:6] == ["05", "00", "1"]
    assert migrated[1][7] == "ENABLED"
    # 13:30 Asia/Singapore (UTC+8) -> 05:30 UTC, 12h -> "0.5" day, FALSE -> "PAUSED"
    assert migrated[2][3:6] == ["05", "30", "0.5"]
    assert migrated[2][7] == "PAUSED"

    # Second run on already-migrated rows is idempotent (changed == False)
    changed_again, migrated_again = migrate_tab2_rows_to_utc_schema(migrated)
    assert changed_again is False
    assert migrated_again == migrated

    fake_meta = {
        "sheets": [
            {"properties": {"title": "Tab0_版本总控与回滚开关", "sheetId": 10}},
            {"properties": {"title": "Prompt_v2.5_V10全量17条标准版", "sheetId": 20}},
            {"properties": {"title": "Tab2_定时巡检配置表", "sheetId": 30}},
        ]
    }
    reqs = build_master_sheet_validation_requests(fake_meta)
    # 2 on Tab0 + 3 on Prompt_v2.5 + 7 on Tab2 (1 repeatCell + 6 setDataValidation) = 12 requests
    assert len(reqs) == 12


def test_preflight_rejected_store_preserves_error_message_in_batch_and_portal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When a store's videos fail preflight (`< 720P` resolution gate), `finalize_store_in_batch`
    must preserve the detailed preflight rejection message instead of overwriting it with `""`,
    and `load_portal_payload` must expose it in both `error_message` and `batch_stores[*].error_message`.
    """

    async def _run() -> None:
        job_store = UserScopedJobStore(state_dir=tmp_path / "jobs")
        state_store = ScheduledStateStore(local_dir=tmp_path / "sched_state")
        svc = AuditService(job_store=job_store)

        async def _no_setup_problem_with_name(folder_id: str) -> tuple[str, str]:
            return "", "test03"

        monkeypatch.setattr(svc, "_workspace_setup_problem_with_name", _no_setup_problem_with_name)

        status_updates: Dict[int, str] = {}
        batch_summary_notifications: List[Dict[str, Any]] = []

        async def _update_rule_status(
            row_index: int, status_text: str, sheet_id: Optional[str] = None
        ) -> None:
            status_updates[row_index] = status_text

        async def _fake_send_batch_summary(**kwargs: Any) -> bool:
            batch_summary_notifications.append(kwargs)
            return True

        monkeypatch.setattr(svc.prompt_manager, "update_scheduled_rule_status", _update_rule_status)
        monkeypatch.setattr(
            svc.reporter, "send_batch_summary_notification", _fake_send_batch_summary
        )

        rule = _make_sample_rule(
            row_index=2,
            rule_id="SCHED-All",
            root_folder_id="folder_root",
            trigger_time="12:45",
            timezone="UTC",
        )

        async def _load_rules(sheet_id: Optional[str] = None) -> List[ScheduledAuditRule]:
            return [rule]

        monkeypatch.setattr(svc.prompt_manager, "load_scheduled_rules", _load_rules)

        folders = {
            "folder_root": {"id": "folder_root", "name": "Root", "parents": [], "trashed": False},
            "folder_test03": {
                "id": "folder_test03",
                "name": "test03",
                "parents": ["folder_root"],
                "trashed": False,
            },
        }
        videos = [
            {
                "id": "vid_low1",
                "name": "low_res_clip1.mp4",
                "mimeType": "video/mp4",
                "size": "1000000",
                "parents": ["folder_test03"],
                "createdTime": "2026-10-16T12:40:00Z",
                "videoMediaMetadata": {"width": 960, "height": 540, "durationMillis": 300000},
            },
            {
                "id": "vid_low2",
                "name": "low_res_clip2.mp4",
                "mimeType": "video/mp4",
                "size": "1000000",
                "parents": ["folder_test03"],
                "createdTime": "2026-10-16T12:41:00Z",
                "videoMediaMetadata": {"width": 960, "height": 540, "durationMillis": 300000},
            },
        ]
        fake_drive = _FakeDriveService(video_files=videos, folders=folders)
        engine = ScheduledAuditEngine(
            audit_service=svc,
            state_store=state_store,
            drive_service_factory=lambda email: fake_drive,
            cloud_run_job_dispatcher=lambda j: None,
        )

        tick_res = await engine.run_scheduled_tick(
            now_utc=dt.datetime(2026, 10, 16, 12, 45, tzinfo=dt.timezone.utc)
        )
        assert tick_res["status"] == "triggered"
        batch_id = tick_res["triggered_batches"][0]["batch_id"]
        assert len(batch_summary_notifications) == 1
        st_summary = batch_summary_notifications[0]["store_summaries"][0]
        assert st_summary["state"] == "REJECTED"
        assert "REJECTED_LOW_RESOLUTION" in st_summary["error_message"]
        assert "low_res_clip1.mp4" in st_summary["error_message"]

        import time
        from cctv_audit.portal import compute_portal_signature, load_portal_payload

        exp = int(time.time()) + 3600
        sig = compute_portal_signature(batch_id=batch_id, exp=exp)
        payload = await load_portal_payload(
            audit_service=svc,
            job_store=job_store,
            state_store=state_store,
            batch_id=batch_id,
            exp=exp,
            sig=sig,
        )
        assert payload["job_state"] == "REJECTED"
        assert "REJECTED_LOW_RESOLUTION" in payload["error_message"]
        assert "low_res_clip1.mp4" in payload["error_message"]
        assert "REJECTED_LOW_RESOLUTION" in payload["batch_stores"][0]["error_message"]
        assert "未通过原因：[test03]" in status_updates[2]

        # Verify both low-res videos are permanently marked REJECTED_LOW_RES and NOT re-claimed on Day 2!
        for vid_id in ("vid_low1", "vid_low2"):
            raw_l, _ = state_store.read_text_with_generation(state_store.file_ledger_key(vid_id))
            assert raw_l is not None
            assert (
                FileLedgerEntry.model_validate_json(raw_l).state
                == FileLedgerState.REJECTED_LOW_RES
            )

        videos[0]["createdTime"] = "2026-10-17T12:40:00Z"
        videos[1]["createdTime"] = "2026-10-17T12:41:00Z"
        tick_day2 = await engine.run_scheduled_tick(
            now_utc=dt.datetime(2026, 10, 17, 12, 45, tzinfo=dt.timezone.utc)
        )
        assert tick_day2["triggered_batches"][0]["status"] == "no_new_videos"

        # Option A: Now upload 1 HD (1080P) video + 1 new low-res (540P) video into the same folder ->
        # the 540P video is skipped (REJECTED_LOW_RES) while the 1080P video passes preflight and dispatches!
        videos.extend(
            [
                {
                    "id": "vid_hd_new",
                    "name": "hd_clip3.mp4",
                    "mimeType": "video/mp4",
                    "size": "2000000",
                    "parents": ["folder_test03"],
                    "createdTime": "2026-10-18T12:40:00Z",
                    "videoMediaMetadata": {"width": 1920, "height": 1080, "durationMillis": 300000},
                },
                {
                    "id": "vid_low_new",
                    "name": "low_res_clip4.mp4",
                    "mimeType": "video/mp4",
                    "size": "1000000",
                    "parents": ["folder_test03"],
                    "createdTime": "2026-10-18T12:41:00Z",
                    "videoMediaMetadata": {"width": 960, "height": 540, "durationMillis": 300000},
                },
            ]
        )
        dispatched: List[AuditJob] = []
        engine._cloud_run_job_dispatcher = lambda j: dispatched.append(j)
        tick_day3 = await engine.run_scheduled_tick(
            now_utc=dt.datetime(2026, 10, 18, 12, 45, tzinfo=dt.timezone.utc)
        )
        assert tick_day3["triggered_batches"][0]["status"] == "dispatched"
        assert len(dispatched) == 1
        assert dispatched[0].preflight_report.total_videos == 1
        assert dispatched[0].preflight_report.videos[0].filename == "hd_clip3.mp4"
        raw_low_new, _ = state_store.read_text_with_generation(
            state_store.file_ledger_key("vid_low_new")
        )
        assert raw_low_new is not None
        assert (
            FileLedgerEntry.model_validate_json(raw_low_new).state
            == FileLedgerState.REJECTED_LOW_RES
        )

    asyncio.run(_run())


def test_tab3_eval_workbench_parsing_and_dropdown_validations() -> None:
    """Verifies Tab3_黄金集评测工作台 10-col row parsing, legacy 9-col migration, error diagnostics, and native dropdown validations."""
    from cctv_audit.prompt_manager import (
        EVAL_WORKBENCH_HEADERS,
        build_master_sheet_validation_requests,
        migrate_tab3_rows_to_3path_schema,
        parse_scheduled_eval_rules_with_diagnostics,
    )

    assert len(EVAL_WORKBENCH_HEADERS) == 10
    rows = [
        list(EVAL_WORKBENCH_HEADERS),
        [
            "EVAL-01",
            "V10 基线评测",
            "Prompt_v2.5_V10全量17条标准版",
            "gemini-3.1-flash-preview",
            "https://drive.google.com/drive/folders/1tAXA-HbRGFjyKxpvG0Pnm0d6CUlbnUQY",
            "https://docs.google.com/spreadsheets/d/1n_1m8gkN_R8MkzsUMqmvm7hNJbbRAhyoGERAOF0Clw0/edit",
            "https://drive.google.com/drive/folders/1ky221gNbzgIT83981ghVpFc03and4s-g",
            "admin@percyhan.altostrat.com",
            "START_EVAL",
            "",
        ],
        [
            "EVAL-BAD-TAB",
            "无效 SOP 标签页",
            "Prompt_NonExistent",
            "gemini-3.1-flash-preview",
            "https://drive.google.com/drive/folders/1tAXA-HbRGFjyKxpvG0Pnm0d6CUlbnUQY",
            "https://docs.google.com/spreadsheets/d/1n_1m8gkN_R8MkzsUMqmvm7hNJbbRAhyoGERAOF0Clw0/edit",
            "https://drive.google.com/drive/folders/1ky221gNbzgIT83981ghVpFc03and4s-g",
            "admin@percyhan.altostrat.com",
            "START_EVAL",
            "",
        ],
    ]
    valid_rules, row_errors = parse_scheduled_eval_rules_with_diagnostics(
        rows,
        available_prompt_tabs=["Prompt_v2.5_V10全量17条标准版", "Prompt_v2.6_r02"],
    )
    assert len(valid_rules) == 1
    assert valid_rules[0].eval_id == "EVAL-01"
    assert valid_rules[0].trigger_action == "START_EVAL"
    assert valid_rules[0].golden_videos_folder_id == "1tAXA-HbRGFjyKxpvG0Pnm0d6CUlbnUQY"
    assert valid_rules[0].golden_source_id == "1n_1m8gkN_R8MkzsUMqmvm7hNJbbRAhyoGERAOF0Clw0"
    assert valid_rules[0].results_folder_id == "1ky221gNbzgIT83981ghVpFc03and4s-g"
    assert len(row_errors) == 1
    assert row_errors[0].row_index == 3
    assert "Prompt_NonExistent" in row_errors[0].error_message

    # Legacy 9-column migration check
    legacy_rows = [
        [
            "Eval_ID",
            "Eval_Name",
            "Prompt_Tab_Name",
            "Model_Version",
            "Golden_Sheet_URL",
            "Results_Folder_URL",
            "Supervisor_Email",
            "Trigger_Action",
            "Last_Eval_Report",
        ],
        [
            "EVAL-LEGACY",
            "旧版9列评测",
            "Prompt_v2.5_V10全量17条标准版",
            "gemini-3.1-flash-preview",
            "https://docs.google.com/spreadsheets/d/1n_1m8gkN_R8MkzsUMqmvm7hNJbbRAhyoGERAOF0Clw0/edit",
            "https://drive.google.com/drive/folders/1ky221gNbzgIT83981ghVpFc03and4s-g",
            "admin@percyhan.altostrat.com",
            "IDLE",
            "",
        ],
    ]
    changed, migrated = migrate_tab3_rows_to_3path_schema(legacy_rows)
    assert changed is True
    assert migrated[0] == list(EVAL_WORKBENCH_HEADERS)
    assert len(migrated[1]) == 10
    assert migrated[1][4] == ""
    assert migrated[1][5] == "https://docs.google.com/spreadsheets/d/1n_1m8gkN_R8MkzsUMqmvm7hNJbbRAhyoGERAOF0Clw0/edit"
    assert migrated[1][7] == "admin@percyhan.altostrat.com"
    assert migrated[1][8] == "IDLE"

    sheet_meta = {
        "sheets": [
            {"properties": {"title": "Tab0_版本总控与回滚开关", "sheetId": 10}},
            {"properties": {"title": "Prompt_v2.5_V10全量17条标准版", "sheetId": 11}},
            {"properties": {"title": "Prompt_v2.6_r02", "sheetId": 12}},
            {"properties": {"title": "Tab2_定时巡检配置表", "sheetId": 20}},
            {"properties": {"title": "Tab3_黄金集评测工作台", "sheetId": 30}},
        ]
    }
    reqs = build_master_sheet_validation_requests(sheet_meta)
    tab3_reqs = [
        r["setDataValidation"]
        for r in reqs
        if "setDataValidation" in r
        and r["setDataValidation"]["range"]["sheetId"] == 30
    ]
    # 5 requests: Prompt_Tab_Name (Col C), Model_Version (Col D), Clear E:G, Supervisor_Email (Col H), Trigger_Action (Col I)
    assert len(tab3_reqs) == 5
    trigger_req = [r for r in tab3_reqs if r["range"]["startColumnIndex"] == 8][0]
    assert trigger_req["range"]["endColumnIndex"] == 9


def test_tab3_eval_workbench_tick_lifecycle_and_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verifies Tab3_黄金集评测工作台 15m tick:
    1. Row with missing Results_Folder_URL (and empty EVAL_RESULTS_FOLDER_ID) fails 0-Token preflight -> Trigger_Action = 'FAILED'.
    2. Valid START_EVAL row transitions START_EVAL -> RUNNING -> DONE, writes 5-tab Sheet URL back to Tab3,
       releases/marks the eval lock DONE, and sends Google Chat completion notification.
    """
    from cctv_audit.prompt_manager import ScheduledEvalRule

    async def _run() -> None:
        monkeypatch.delenv("EVAL_RESULTS_FOLDER_ID", raising=False)
        job_store = UserScopedJobStore(state_dir=tmp_path / "jobs")
        state_store = ScheduledStateStore(local_dir=tmp_path / "state")
        svc = AuditService(job_store=job_store)

        tab3_updates: List[Tuple[int, str, str]] = []
        eval_start_notifications: List[Dict[str, Any]] = []
        eval_chat_notifications: List[Dict[str, Any]] = []

        async def _update_eval_row(
            row_index: int,
            *,
            trigger_action: Optional[str] = None,
            last_eval_report: Optional[str] = None,
            sheet_id: Optional[str] = None,
        ) -> bool:
            tab3_updates.append((row_index, str(trigger_action or ""), str(last_eval_report or "")))
            return True

        async def _load_config_for_tab(
            prompt_tab_name: str,
            model_version: str,
            sheet_id: Optional[str] = None,
        ) -> Any:
            return await svc.prompt_manager.load_active_config()

        async def _send_eval_start(**kwargs: Any) -> bool:
            eval_start_notifications.append(kwargs)
            return True

        async def _send_eval_chat(**kwargs: Any) -> bool:
            eval_chat_notifications.append(kwargs)
            return True

        monkeypatch.setattr(svc.prompt_manager, "update_scheduled_eval_row", _update_eval_row)
        monkeypatch.setattr(svc.prompt_manager, "load_config_for_tab", _load_config_for_tab)
        monkeypatch.setattr(svc.reporter, "send_eval_start_notification", _send_eval_start)
        monkeypatch.setattr(svc.reporter, "send_eval_completion_notification", _send_eval_chat)
        monkeypatch.setattr(svc.prompt_manager, "load_scheduled_rules", lambda: asyncio.sleep(0, result=[]))

        rule_bad_folder = ScheduledEvalRule(
            row_index=2,
            eval_id="EVAL-NO-FOLDER",
            eval_name="缺少归档目录测试",
            prompt_tab_name="Prompt_v2.5_V10全量17条标准版",
            model_version="gemini-3.1-flash-preview",
            golden_videos_folder_url="https://drive.google.com/drive/folders/1tAXA-HbRGFjyKxpvG0Pnm0d6CUlbnUQY",
            golden_videos_folder_id="1tAXA-HbRGFjyKxpvG0Pnm0d6CUlbnUQY",
            human_labels_sheet_url="https://docs.google.com/spreadsheets/d/1n_1m8gkN_R8MkzsUMqmvm7hNJbbRAhyoGERAOF0Clw0/edit",
            golden_sheet_url="https://docs.google.com/spreadsheets/d/1n_1m8gkN_R8MkzsUMqmvm7hNJbbRAhyoGERAOF0Clw0/edit",
            golden_source_id="1n_1m8gkN_R8MkzsUMqmvm7hNJbbRAhyoGERAOF0Clw0",
            results_folder_url="",
            results_folder_id="",
            supervisor_email="admin@percyhan.altostrat.com",
            trigger_action="START_EVAL",
            last_eval_report="",
        )
        rule_ok = ScheduledEvalRule(
            row_index=3,
            eval_id="EVAL-OK-01",
            eval_name="V10.2 候选评测",
            prompt_tab_name="Prompt_v2.6_r02",
            model_version="gemini-3.1-flash-preview",
            golden_videos_folder_url="https://drive.google.com/drive/folders/1tAXA-HbRGFjyKxpvG0Pnm0d6CUlbnUQY",
            golden_videos_folder_id="1tAXA-HbRGFjyKxpvG0Pnm0d6CUlbnUQY",
            human_labels_sheet_url="https://docs.google.com/spreadsheets/d/1n_1m8gkN_R8MkzsUMqmvm7hNJbbRAhyoGERAOF0Clw0/edit",
            golden_sheet_url="https://docs.google.com/spreadsheets/d/1n_1m8gkN_R8MkzsUMqmvm7hNJbbRAhyoGERAOF0Clw0/edit",
            golden_source_id="1n_1m8gkN_R8MkzsUMqmvm7hNJbbRAhyoGERAOF0Clw0",
            results_folder_url="https://drive.google.com/drive/folders/1ky221gNbzgIT83981ghVpFc03and4s-g",
            results_folder_id="1ky221gNbzgIT83981ghVpFc03and4s-g",
            supervisor_email="admin@percyhan.altostrat.com",
            trigger_action="START_EVAL",
            last_eval_report="",
        )

        async def _load_eval_rules(sheet_id: Optional[str] = None) -> List[ScheduledEvalRule]:
            return [rule_bad_folder, rule_ok]

        monkeypatch.setattr(svc.prompt_manager, "load_scheduled_eval_rules", _load_eval_rules)

        seen_task_recs: List[Any] = []

        async def _fake_eval_runner(task_rec: Any) -> Dict[str, Any]:
            seen_task_recs.append(task_rec)
            return {
                "eval_id": task_rec.eval_id,
                "eval_name": task_rec.eval_name,
                "round_id": "eval_ok_01",
                "run_id": task_rec.eval_task_id,
                "prompt_tab_name": task_rec.prompt_tab_name,
                "model_version": task_rec.model_version,
                "overall_recall": 0.421,
                "holdout_recall": 0.444,
                "dev_recall": 0.400,
                "findings_per_clip": 5.5,
                "hit_rate": 0.5625,
                "guardrails_passed": True,
                "total_cost_usd": 3.45,
                "total_tokens": 128000,
                "spreadsheet_url": "https://docs.google.com/spreadsheets/d/eval_sheet_999/edit",
                "spreadsheet_id": "eval_sheet_999",
                "sheet_error": None,
            }

        engine = ScheduledAuditEngine(
            audit_service=svc,
            state_store=state_store,
            eval_runner=_fake_eval_runner,
        )
        res = await engine.run_scheduled_tick(
            now_utc=dt.datetime(2026, 10, 16, 14, 0, tzinfo=dt.timezone.utc)
        )
        assert res["status"] == "triggered"
        assert len(res["triggered_evals"]) == 2
        assert res["triggered_evals"][0]["status"] == "preflight_failed"
        assert res["triggered_evals"][1]["status"] == "dispatched"
        assert len(seen_task_recs) == 1
        assert seen_task_recs[0].golden_videos_folder_id == "1tAXA-HbRGFjyKxpvG0Pnm0d6CUlbnUQY"

        # Row 2 was marked FAILED on preflight
        row2_updates = [u for u in tab3_updates if u[0] == 2]
        assert len(row2_updates) == 1
        assert row2_updates[0][1] == "FAILED"
        assert "Results_Folder_URL" in row2_updates[0][2]

        # Row 3 transitioned START_EVAL -> RUNNING -> DONE with report link
        row3_updates = [u for u in tab3_updates if u[0] == 3]
        assert [u[1] for u in row3_updates] == ["RUNNING", "DONE"]
        assert "https://docs.google.com/spreadsheets/d/eval_sheet_999/edit" in row3_updates[-1][2]
        assert "42.1%" in row3_updates[-1][2]
        assert len(eval_start_notifications) == 1
        assert eval_start_notifications[0]["eval_id"] == "EVAL-OK-01"
        assert len(eval_chat_notifications) == 1
        assert eval_chat_notifications[0]["status"] == "DONE"

    asyncio.run(_run())







