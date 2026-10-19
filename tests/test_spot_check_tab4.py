"""Unit & integration tests for Fixed `Tab4_专项测试表` Spot-Check & New SOP Testing via Gemini Enterprise."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest

from cctv_audit.agentic_auditor import (
    Disposition,
    Finding,
    Severity,
    Status,
    TokenLedgerRow,
)
from cctv_audit.audit_service import AuditService
from cctv_audit.config import config
from cctv_audit.gcs_gateway import GcsStorageGateway, read_xlsx_sheets
from cctv_audit.jobs import AuditJob, JobState, UserScopedJobStore
from cctv_audit.portal import compute_portal_signature, load_portal_payload
from cctv_audit.prompt_manager import (
    PromptManager,
    SHEET_RULE_HEADER,
    SopRuleItem,
    TAB4_SPOT_CHECK_TITLE,
    build_master_sheet_validation_requests,
    parse_rules_from_sheet_rows,
    render_v25_system_instruction,
)
from cctv_audit.server import (
    InternalJobExecuteRequest,
    internal_execute_job,
)
from cctv_audit.turn import TurnAction, TurnDecision, classify_turn_with_llm, detect_spot_check_tab, verify_url_verbatim
from cctv_audit.video_ingestor import VideoIngestor, VideoMetadataItem
from cctv_audit.workspace_reporter import ViolationSheetRow, WorkspaceReporter


def test_detect_spot_check_tab_strictly_inspects_latest_user_utterance() -> None:
    assert (
        detect_spot_check_tab(
            "请帮忙稽核视频：https://drive.google.com/drive/folders/1--hbVk8xE5i-aZrph7Pgy4vZwZdp9-NJ"
        )
        == ""
    )
    assert (
        detect_spot_check_tab(
            "@CHAGEE 门店视频 AI 稽核专家 请帮忙稽核视频：https://drive.google.com/drive/folders/1--hbVk8xE5i-aZrph7Pgy4vZwZdp9-NJ"
        )
        == ""
    )
    assert (
        detect_spot_check_tab(
            "【仅测 Tab4_专项测试表（非全量）】请帮忙稽核视频：https://drive.google.com/drive/folders/1--hbVk8xE5i-aZrph7Pgy4vZwZdp9-NJ"
        )
        == TAB4_SPOT_CHECK_TITLE
    )
    assert (
        detect_spot_check_tab(
            "@CHAGEE 门店视频 AI 稽核专家 【仅测 Tab4_专项测试表（非全量）】请帮忙稽核视频：https://drive.google.com/drive/folders/1--hbVk8xE5i-aZrph7Pgy4vZwZdp9-NJ"
        )
        == TAB4_SPOT_CHECK_TITLE
    )
    assert (
        detect_spot_check_tab(
            "帮我用 Tab4 专项抽检这个视频目录：gs://my-bucket/store01"
        )
        == TAB4_SPOT_CHECK_TITLE
    )
    multi_turn = (
        "[user] 【仅测 Tab4_专项测试表（非全量）】请帮忙稽核视频：<历史链接>\n"
        "[assistant] ⚠️ 【当前为专项抽检 / 新 SOP 测试模式（非全量稽核）】\n"
        "[user] @CHAGEE 门店视频 AI 稽核专家 请帮忙稽核视频，https://drive.google.com/drive/folders/1--hbVk8xE5i-aZrph7Pgy4vZwZdp9-NJ"
    )
    assert detect_spot_check_tab(multi_turn) == ""


def test_classify_turn_populates_target_prompt_tab_and_anti_confusion_fallback() -> None:
    async def _run() -> None:
        folder_url = "https://drive.google.com/drive/folders/1--hbVk8xE5i-aZrph7Pgy4vZwZdp9-NJ"

        d_full = verify_url_verbatim(
            TurnDecision(action=TurnAction.INSPECT, drive_url=folder_url, target_prompt_tab="Tab4_专项测试表"),
            f"请帮忙稽核视频，{folder_url}",
        )
        assert d_full.action == TurnAction.INSPECT
        assert d_full.drive_url == folder_url
        assert d_full.target_prompt_tab == ""

        d_spot = verify_url_verbatim(
            TurnDecision(action=TurnAction.INSPECT, drive_url=folder_url, target_prompt_tab=""),
            f"【仅测 Tab4_专项测试表（非全量）】请帮忙稽核视频：{folder_url}",
        )
        assert d_spot.action == TurnAction.INSPECT
        assert d_spot.drive_url == folder_url
        assert d_spot.target_prompt_tab == TAB4_SPOT_CHECK_TITLE

        d_unclear = await classify_turn_with_llm("")
        assert d_unclear.action == TurnAction.UNCLEAR
        assert "@CHAGEE 门店视频 AI 稽核专家" in d_unclear.reply_summary
        assert "请帮忙稽核视频" in d_unclear.reply_summary
        assert "Tab4_专项测试表" in d_unclear.reply_summary

        d_missing_url = verify_url_verbatim(
            TurnDecision(action=TurnAction.INSPECT, drive_url=folder_url),
            "我忘了贴链接",
        )
        assert d_missing_url.action == TurnAction.UNCLEAR
        assert "@CHAGEE 门店视频 AI 稽核专家" in d_missing_url.reply_summary
        assert "请帮忙稽核视频" in d_missing_url.reply_summary
        assert "Tab4_专项测试表" in d_missing_url.reply_summary

    asyncio.run(_run())


def test_prompt_manager_tab4_scope_lock_and_empty_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _run() -> None:
        monkeypatch.setattr(config, "master_prompt_sheet_id", "1yPHZgVtOsmrfVaC8aqM3GZ9ftm3T8cep9MqTRDroo8w")
        custom_rule = SopRuleItem(
            rule_id="C9",
            name="C9 · 新增果糖机喷嘴清洁测试",
            category="【C】新SOP测试",
            severity="NORMAL",
            check_mode="absence",
            requires_carryover=False,
            check_instruction="每次取用果糖后 15 秒内必须用蓝边毛巾擦拭果糖机喷嘴。",
            pass_criteria="取果糖后 15 秒内用蓝边毛巾擦拭喷嘴",
            fail_criteria="取果糖后超过 15 秒未擦拭果糖机喷嘴",
        )
        sys_inst = render_v25_system_instruction(
            TAB4_SPOT_CHECK_TITLE,
            [custom_rule],
            is_spot_check=True,
        )
        assert "## 原则一：宁可误报，绝不漏报" in sys_inst
        assert "多人同屏注意力解耦（Anti-Attention-Masking）" in sys_inst
        assert "【专项抽检 / 新 SOP 测试范围锁定（Scope Lock）】" in sys_inst
        assert "`C9`" in sys_inst
        assert "严禁上报不在本次专项清单（`C9`）内的其他任何 SOP 条款" in sys_inst

        class _FakeSheetsClient:
            def __init__(self) -> None:
                self.tab4_rows: List[List[str]] = [
                    list(SHEET_RULE_HEADER),
                    [
                        "C9",
                        "C9 · 新增果糖机喷嘴清洁测试",
                        "【C】新SOP测试",
                        "NORMAL",
                        "absence",
                        "FALSE",
                        "每次取用果糖后 15 秒内擦拭喷嘴",
                        "15秒内擦拭",
                        "超15秒未擦拭",
                    ],
                ]
                self.ensured = False

            async def read_tab0_pointers(self, sheet_id: str) -> Dict[str, str]:
                return {
                    "Active_Prompt_Version": "Prompt_v2.5_V10全量17条标准版",
                    "Active_Model_Version": "gemini-3.8-flash",
                    "Fallback_Model_Version": "gemini-2.5-flash",
                }

            async def read_prompt_tab_rules(self, sheet_id: str, tab_name: str) -> List[SopRuleItem]:
                assert tab_name == TAB4_SPOT_CHECK_TITLE
                return parse_rules_from_sheet_rows(self.tab4_rows)

            async def ensure_master_sheet_validations(self, sheet_id: str, *, force: bool = False) -> bool:
                self.ensured = True
                return True

        fake_client = _FakeSheetsClient()
        pm = PromptManager(sheet_client=fake_client, known_valid_models={"gemini-3.8-flash", "gemini-2.5-flash"})
        cfg1 = await pm.load_config_for_tab(TAB4_SPOT_CHECK_TITLE, probe_model=False)
        assert cfg1.active_model_version == "gemini-3.8-flash"
        assert [r.rule_id for r in cfg1.rules] == ["C9"]
        assert "【专项抽检 / 新 SOP 测试范围锁定（Scope Lock）】" in cfg1.system_instruction

        fake_client.tab4_rows.append(
            [
                "B4",
                "B4 · 红边毛巾抽检",
                "【B】制冰机",
                "RED_LINE",
                "presence",
                "FALSE",
                "核查红边毛巾是否从消毒桶取出",
                "从消毒桶取出",
                "未从消毒桶取出",
            ]
        )
        cfg2 = await pm.load_config_for_tab(TAB4_SPOT_CHECK_TITLE, probe_model=False)
        assert [r.rule_id for r in cfg2.rules] == ["C9", "B4"]

        fake_client.tab4_rows = [list(SHEET_RULE_HEADER)]
        with pytest.raises(ValueError, match="Tab4_专项测试表.*当前为空"):
            await pm.load_config_for_tab(TAB4_SPOT_CHECK_TITLE, probe_model=False)

    asyncio.run(_run())


def test_master_sheet_validation_requests_include_tab4() -> None:
    meta = {
        "sheets": [
            {"properties": {"title": "Tab0_版本总控与回滚开关", "sheetId": 1}},
            {"properties": {"title": "Prompt_v2.5_V10全量17条标准版", "sheetId": 2}},
            {"properties": {"title": TAB4_SPOT_CHECK_TITLE, "sheetId": 4}},
        ]
    }
    reqs = build_master_sheet_validation_requests(meta)
    tab4_sheet_ids = {
        r["setDataValidation"]["range"]["sheetId"]
        for r in reqs
        if "setDataValidation" in r
    }
    assert 4 in tab4_sheet_ids


def test_two_step_spot_check_preflight_confirm_and_worker_execute(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def _run() -> None:
        monkeypatch.setattr(config, "master_prompt_sheet_id", "1yPHZgVtOsmrfVaC8aqM3GZ9ftm3T8cep9MqTRDroo8w")
        store = UserScopedJobStore(state_dir=tmp_path / "jobs", gcs_bucket="")
        video = VideoMetadataItem(
            file_id="vid_1",
            filename="Footage 1.mp4",
            width=1920,
            height=1080,
            duration_sec=120.0,
        )

        class _FakeReader:
            async def list_folder_videos(self, folder_id: str) -> List[VideoMetadataItem]:
                return [video]

            async def download_video_to_path(self, file_id: str, dest_path: Path) -> Path:
                dest_path.write_bytes(b"fake")
                return dest_path

        class _FakeSheetsClient:
            def __init__(self, empty: bool = False) -> None:
                self.empty = empty

            async def read_tab0_pointers(self, sheet_id: str) -> Dict[str, str]:
                return {
                    "Active_Prompt_Version": "Prompt_v2.5_V10全量17条标准版",
                    "Active_Model_Version": "gemini-3.8-flash",
                    "Fallback_Model_Version": "gemini-2.5-flash",
                }

            async def read_prompt_tab_rules(self, sheet_id: str, tab_name: str) -> List[SopRuleItem]:
                if tab_name == TAB4_SPOT_CHECK_TITLE:
                    if self.empty:
                        return []
                    return parse_rules_from_sheet_rows(
                        [
                            list(SHEET_RULE_HEADER),
                            [
                                "B4",
                                "B4 · 红边毛巾消毒桶溯源专项抽检",
                                "【B】制冰机",
                                "RED_LINE",
                                "presence",
                                "FALSE",
                                "核查红边毛巾是否从消毒桶取出",
                                "从消毒桶取出",
                                "未从消毒桶取出",
                            ],
                        ]
                    )
                return parse_rules_from_sheet_rows(
                    [
                        list(SHEET_RULE_HEADER),
                        [
                            "A1",
                            "A1 · 强制洗手",
                            "【A】洗手",
                            "RED_LINE",
                            "presence",
                            "TRUE",
                            "洗手指令",
                            "合规",
                            "违规",
                        ],
                    ]
                )

        pm = PromptManager(
            sheet_client=_FakeSheetsClient(empty=False),
            known_valid_models={"gemini-3.8-flash", "gemini-2.5-flash"},
        )
        svc = AuditService(
            job_store=store,
            ingestor=VideoIngestor(drive_reader=_FakeReader()),
            prompt_manager=pm,
            reporter=WorkspaceReporter(gateway=None, enable_notification=False),
        )

        folder_url = "https://drive.google.com/drive/folders/1--hbVk8xE5i-aZrph7Pgy4vZwZdp9-NJ"

        job_spot = await svc.preflight(
            user_id="sup@chagee.com",
            drive_url=folder_url,
            session_id="sess_spot",
            target_prompt_tab=TAB4_SPOT_CHECK_TITLE,
        )
        assert job_spot.state == JobState.READY
        assert job_spot.target_prompt_tab == TAB4_SPOT_CHECK_TITLE
        assert job_spot.audited_rule_ids == ["B4"]
        assert job_spot.active_prompt_version == "Tab4_专项测试表[B4]"
        assert job_spot.preflight_report is not None
        assert "【当前为专项抽检 / 新 SOP 测试模式（非全量稽核）】" in job_spot.preflight_report.message_to_user
        assert f"@{config.agent_display_name} 请帮忙稽核视频" in job_spot.preflight_report.message_to_user

        monkeypatch.setattr(config, "agent_display_name", "Custom Tenant 稽核专家")
        custom_unclear = verify_url_verbatim(
            TurnDecision(action=TurnAction.INSPECT, drive_url="https://not-present.example/folder"),
            "请帮我稽核视频",
        )
        assert "@Custom Tenant 稽核专家" in custom_unclear.reply_summary
        monkeypatch.setattr(config, "agent_display_name", "CHAGEE 门店视频 AI 稽核专家")

        job_full = await svc.preflight(
            user_id="sup@chagee.com",
            drive_url=folder_url,
            session_id="sess_full",
            target_prompt_tab="",
        )
        assert job_full.state == JobState.READY
        assert job_full.target_prompt_tab == ""
        assert job_full.audited_rule_ids == []
        assert job_full.preflight_report is not None
        assert "【日常全量稽核】" in job_full.preflight_report.message_to_user

        svc_empty = AuditService(
            job_store=store,
            ingestor=VideoIngestor(drive_reader=_FakeReader()),
            prompt_manager=PromptManager(
                sheet_client=_FakeSheetsClient(empty=True),
                known_valid_models={"gemini-3.8-flash", "gemini-2.5-flash"},
            ),
            reporter=WorkspaceReporter(gateway=None, enable_notification=False),
        )
        job_rejected = await svc_empty.preflight(
            user_id="sup@chagee.com",
            drive_url=folder_url,
            session_id="sess_empty",
            target_prompt_tab=TAB4_SPOT_CHECK_TITLE,
        )
        assert job_rejected.state == JobState.REJECTED
        assert "Tab4_专项测试表" in (job_rejected.error_message or "")

        captured_prompt_cfgs: List[Any] = []

        async def _fake_run_detached(j: AuditJob, p_cfg: Any) -> None:
            captured_prompt_cfgs.append((j, p_cfg))

        monkeypatch.setattr(svc, "_run_detached_audit", _fake_run_detached)
        monkeypatch.setattr("cctv_audit.server.audit_service", svc)

        res = await internal_execute_job(
            InternalJobExecuteRequest(user_id="sup@chagee.com", job_id=job_spot.job_id, hold_connection=True)
        )
        assert res["completed"] is True
        assert len(captured_prompt_cfgs) == 1
        _, loaded_cfg = captured_prompt_cfgs[0]
        assert loaded_cfg.active_prompt_version == "Tab4_专项测试表[B4]"
        assert [r.rule_id for r in loaded_cfg.rules] == ["B4"]

    asyncio.run(_run())


def test_rule_scoped_smart_append_preserves_other_rules_on_same_video() -> None:
    async def _run() -> None:
        gw = GcsStorageGateway()
        stored_objects: Dict[str, bytes] = {}

        def _fake_request(
            method: str,
            url: str,
            *,
            body: Any = None,
            content_type: str = "",
            content_length: int | None = None,
            timeout: float = 60.0,
        ) -> tuple[int, bytes]:
            if method == "GET" and "alt=media" in url:
                data = stored_objects.get("report.xlsx", b"")
                return (200, data) if data else (404, b"")
            if method == "POST" and "uploadType=media" in url:
                assert isinstance(body, (bytes, bytearray))
                stored_objects["report.xlsx"] = bytes(body)
                return 200, b'{"generation":"1"}'
            return 200, b"{}"

        gw._request = _fake_request  # type: ignore[method-assign]

        full_tab1 = [
            ViolationSheetRow(
                audit_id="full01",
                video_filename="Footage 1.mp4",
                timestamp_in_clip="00:01:10",
                rule_id="A2",
                disposition="CONFIRMED",
                severity="RED_LINE",
                confidence=0.95,
                evidence_description="污染后未洗手直接戴手套",
                evidence_drive_url="https://drive.google.com/file/d/ev_a2/view",
            ),
            ViolationSheetRow(
                audit_id="full01",
                video_filename="Footage 1.mp4",
                timestamp_in_clip="00:02:20",
                rule_id="B4",
                disposition="CONFIRMED",
                severity="RED_LINE",
                confidence=0.91,
                evidence_description="旧版 B4 记录",
                evidence_drive_url="https://drive.google.com/file/d/ev_b4_old/view",
            ),
            ViolationSheetRow(
                audit_id="full01",
                video_filename="Footage 1.mp4",
                timestamp_in_clip="00:03:30",
                rule_id="C4",
                disposition="CONFIRMED",
                severity="RED_LINE",
                confidence=0.98,
                evidence_description="员工使用私人手机",
                evidence_drive_url="https://drive.google.com/file/d/ev_c4/view",
            ),
        ]
        full_tab2 = [
            TokenLedgerRow(
                audit_id="full01",
                auditor_folder_id="gs://customer-bucket/store01",
                video_filename="Footage 1.mp4",
                video_duration_sec=300.0,
                resolution="1920x1080",
                model_version_used="gemini-3.8-flash",
                prompt_version_used="Prompt_v2.5_V10全量17条标准版",
                prompt_token_count=10000,
                thoughts_token_count=1000,
                candidates_token_count=500,
                total_token_count=11500,
                estimated_cost_usd=0.05,
                e2e_latency_ms=12000,
                flagged_events_count=3,
            )
        ]
        await gw.create_dual_tab_report_sheet(
            "gs://customer-bucket/store01",
            "📊 AI稽核报告与Token账单",
            full_tab1,
            full_tab2,
        )

        spot_tab1 = [
            ViolationSheetRow(
                audit_id="spot02",
                video_filename="Footage 1.mp4",
                timestamp_in_clip="00:02:25",
                rule_id="B4",
                disposition="CONFIRMED",
                severity="RED_LINE",
                confidence=0.99,
                evidence_description="【专项抽检更新】红边毛巾未从消毒桶取出且漏擦白色外盖四周侧面边缘",
                evidence_drive_url="https://drive.google.com/file/d/ev_b4_new/view",
            )
        ]
        spot_tab2 = [
            TokenLedgerRow(
                audit_id="spot02",
                auditor_folder_id="gs://customer-bucket/store01",
                video_filename="Footage 1.mp4",
                video_duration_sec=300.0,
                resolution="1920x1080",
                model_version_used="gemini-3.8-flash",
                prompt_version_used="Tab4_专项测试表[B4]",
                prompt_token_count=9800,
                thoughts_token_count=800,
                candidates_token_count=300,
                total_token_count=10900,
                estimated_cost_usd=0.04,
                e2e_latency_ms=9500,
                flagged_events_count=1,
            )
        ]
        await gw.create_dual_tab_report_sheet(
            "gs://customer-bucket/store01",
            "📊 AI稽核报告与Token账单",
            spot_tab1,
            spot_tab2,
            audited_rule_ids=["B4"],
        )

        sheets = read_xlsx_sheets(stored_objects["report.xlsx"])
        t1_rows = sheets["违规事件 3 秒复核台"][1:]
        t2_rows = sheets["本次视频 Token 消耗与耗时账单"][1:]

        assert [(r[0], r[3], r[7]) for r in t1_rows] == [
            ("full01", "A2", "污染后未洗手直接戴手套"),
            ("full01", "C4", "员工使用私人手机"),
            ("spot02", "B4", "【专项抽检更新】红边毛巾未从消毒桶取出且漏擦白色外盖四周侧面边缘"),
        ]
        assert [(r[0], r[6]) for r in t2_rows] == [
            ("full01", "Prompt_v2.5_V10全量17条标准版"),
            ("spot02", "Tab4_专项测试表[B4]"),
        ]

    asyncio.run(_run())


def test_portal_renders_custom_new_sop_rule_from_tab4(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def _run() -> None:
        monkeypatch.setattr(config, "master_prompt_sheet_id", "1yPHZgVtOsmrfVaC8aqM3GZ9ftm3T8cep9MqTRDroo8w")
        store = UserScopedJobStore(state_dir=tmp_path / "jobs", gcs_bucket="")
        job = AuditJob(
            job_id="ab12cd",
            user_id="sup@chagee.com",
            folder_id="folder_xyz",
            folder_name="1-Cantavil D2",
            drive_url="https://drive.google.com/drive/folders/folder_xyz",
            state=JobState.DONE,
            target_prompt_tab=TAB4_SPOT_CHECK_TITLE,
            audited_rule_ids=["C9"],
            active_model_version="gemini-3.8-flash",
            active_prompt_version="Tab4_专项测试表[C9]",
            report_sheet_url="https://docs.google.com/spreadsheets/d/sheet_report/edit",
            violations_found=1,
        )
        await store.save(job)

        class _FakeSheetsClient:
            async def read_tab0_pointers(self, sheet_id: str) -> Dict[str, str]:
                return {"Active_Model_Version": "gemini-3.8-flash"}

            async def read_prompt_tab_rules(self, sheet_id: str, tab_name: str) -> List[SopRuleItem]:
                return parse_rules_from_sheet_rows(
                    [
                        list(SHEET_RULE_HEADER),
                        [
                            "C9",
                            "C9 · 新增测试规则：自动果糖机喷嘴擦拭",
                            "【C】新SOP测试",
                            "NORMAL",
                            "absence",
                            "FALSE",
                            "每次取用果糖后 15 秒内擦拭喷嘴",
                            "取果糖后 15 秒内用蓝边毛巾擦拭喷嘴",
                            "取果糖后超过 15 秒未擦拭果糖机喷嘴",
                        ],
                    ]
                )

        class _FakeGateway:
            async def read_report_sheet_data(self, folder_id: str, sheet_url: str = "") -> tuple[list, list]:
                return (
                    [
                        [
                            "ab12cd",
                            "Footage 1.mp4",
                            "00:01:15",
                            "C9",
                            "CONFIRMED",
                            "NORMAL",
                            "0.94",
                            "员工A取用果糖后满 30 秒未擦拭果糖机喷嘴",
                            "https://drive.google.com/file/d/clip_c9_12345/view",
                            "⏳ 待人工复核 (PENDING_HUMAN_REVIEW)",
                            "",
                        ]
                    ],
                    [],
                )

        svc = MagicMock()
        svc.jobs = store
        svc.state_store = None
        svc.prompt_manager = PromptManager(
            sheet_client=_FakeSheetsClient(),
            known_valid_models={"gemini-3.8-flash"},
        )

        exp = 4102444800
        sig = compute_portal_signature(job_id="ab12cd", user_id="sup@chagee.com", exp=exp)
        payload = await load_portal_payload(
            audit_service=svc,
            gateway=_FakeGateway(),
            job_id="ab12cd",
            user_id="sup@chagee.com",
            exp=exp,
            sig=sig,
        )
        assert len(payload["findings"]) == 1
        f0 = payload["findings"][0]
        assert f0["rule_id"] == "C9"
        assert f0["rule_name"] == "C9 · 新增测试规则：自动果糖机喷嘴擦拭"
        assert f0["sop_pass_criteria"] == "取果糖后 15 秒内用蓝边毛巾擦拭喷嘴"
        assert f0["sop_fail_criteria"] == "取果糖后超过 15 秒未擦拭果糖机喷嘴"

    asyncio.run(_run())


def test_engine_prompt_chips_canned_queries_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    from deploy import deploy_reasoning_engine as dre

    agent_name = "霸王茶姬 (CHAGEE) 门店监控 AI 稽核专家"
    folder_url = "https://drive.google.com/drive/folders/1--hbVk8xE5i-aZrph7Pgy4vZwZdp9-NJ"
    chips = dre.build_engine_prompt_chips(agent_name, folder_url)
    chip_ids = [cid for cid, _ in chips]
    assert chip_ids == ["cctv-full-audit", "cctv-tab4-spot-check"]
    chip_map = dict(chips)
    assert chip_map["cctv-full-audit"]["displayName"] == "门店视频日常全量稽核"
    assert chip_map["cctv-full-audit"]["defaultTexts"]["suggestedPrompts"] == [
        {"promptText": "请帮忙稽核视频：<门店监控 Google Drive 文件夹链接或 gs:// 目录>"}
    ]
    assert chip_map["cctv-tab4-spot-check"]["displayName"] == "专项抽检与新SOP测试"
    assert chip_map["cctv-tab4-spot-check"]["defaultTexts"]["suggestedPrompts"] == [
        {"promptText": "【仅测 Tab4_专项测试表（非全量）】请帮忙稽核视频：<门店监控 Google Drive 文件夹链接>"}
    ]
    for _, body in chips:
        assert body["enabled"] is True
        assert body["defaultTexts"]["prefix"] == "请根据你要稽核的内容选择如下的气泡"

    calls: list[tuple[str, str, dict | None]] = []

    def _fake_call(
        method: str,
        url: str,
        body: dict | None = None,
        project_id: str = "",
        ignore_errors: bool = False,
    ) -> dict:
        calls.append((method, url, body))
        if method == "GET" and url.endswith("/cannedQueries?pageSize=100"):
            return {
                "cannedQueries": [
                    {
                        "name": "projects/p/locations/global/collections/default_collection/engines/eng1/assistants/default_assistant/cannedQueries/goog_start_my_day",
                        "displayName": "Start my day",
                        "googleDefined": True,
                        "enabled": True,
                    },
                    {
                        "name": "projects/p/locations/global/collections/default_collection/engines/eng1/assistants/default_assistant/cannedQueries/videocheck",
                        "displayName": "视频稽核SOP",
                        "enabled": True,
                    },
                    {
                        "name": "projects/p/locations/global/collections/default_collection/engines/eng1/assistants/default_assistant/cannedQueries/cctv-confirm-and-status",
                        "displayName": "确认开始稽核与进度查询",
                        "enabled": True,
                    },
                    {
                        "name": "projects/p/locations/global/collections/default_collection/engines/eng1/assistants/default_assistant/cannedQueries/cctv-full-audit",
                        "displayName": "门店视频日常全量稽核",
                        "enabled": True,
                    },
                ]
            }
        return {"name": url, "displayName": (body or {}).get("displayName", "")}

    monkeypatch.setattr(dre, "_call", _fake_call)
    de_base = "https://discoveryengine.googleapis.com/v1alpha/projects/p/locations/global/collections/default_collection"
    synced = dre.ensure_engine_prompt_chips(
        "p",
        de_base,
        "eng1",
        agent_display_name=agent_name,
        example_folder_url=folder_url,
    )
    assert len(synced) == 2

    # Google-defined default chip is disabled; deprecated custom chips (`videocheck`, `cctv-confirm-and-status`) are deleted
    disabled_urls = [
        url for m, url, b in calls if m == "PATCH" and b and b.get("enabled") is False
    ]
    deleted_urls = [url for m, url, _ in calls if m == "DELETE"]
    assert any("goog_start_my_day?updateMask=enabled" in u for u in disabled_urls)
    assert any(u.endswith("/cannedQueries/videocheck") for u in deleted_urls)
    assert any(u.endswith("/cannedQueries/cctv-confirm-and-status") for u in deleted_urls)

    # Existing `cctv-full-audit` is PATCHed, while `cctv-tab4-spot-check` is POSTed
    patched_custom = [
        url for m, url, b in calls if m == "PATCH" and b and b.get("enabled") is True
    ]
    posted_custom = [url for m, url, _ in calls if m == "POST"]
    assert len(patched_custom) == 1
    assert "cctv-full-audit?updateMask=displayName,defaultTexts,enabled" in patched_custom[0]
    assert len(posted_custom) == 1
    assert any("cannedQueryId=cctv-tab4-spot-check" in u for u in posted_custom)

