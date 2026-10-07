"""Round 38: token-free Workspace identity (keyless domain-wide delegation) + fail-fast Drive setup.

Production must never depend on a Cloudtop, a stored user refresh token, or a human re-login. These
tests pin the contract:
1. DWD credentials are built through IAM `signJwt` (google-auth impersonated credentials with
   `subject=`), never from a key file or refresh token.
2. Every Drive/Sheets setup problem (DWD not authorised, TokenCreator missing, bot without Drive
   storage, folder viewer-only / unshared, SOP Sheet unshared) becomes a supervisor-facing Chinese
   message BEFORE any Gemini spend.
3. The report Sheet is only ever reused when it is this audit's own Sheet -- never an unrelated
   spreadsheet that happens to sit in the folder.
4. A job parked on a setup problem is not retried every 2 minutes by the watchdog, and every restart
   counts toward MAX_AUTO_RESUMES.
5. Terraform, the ReasoningEngine deploy script, and the code agree on the identity wiring.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import google.auth
import google.auth.exceptions
import httplib2
import pytest
from google.auth import impersonated_credentials
from googleapiclient.errors import HttpError

from cctv_audit import audit_service as audit_service_module
from cctv_audit import gcp
from cctv_audit import prompt_manager as prompt_manager_module
from cctv_audit.audit_service import AuditService
from cctv_audit.gcp import (
    GoogleWorkspaceGateway,
    WorkspaceAccessError,
    WorkspaceAuthError,
    WorkspaceStorageQuotaError,
)
from cctv_audit.jobs import AuditJob, JobState, UserScopedJobStore
from cctv_audit.prompt_manager import GoogleSheetsConfigClient, PromptManager, PromptModelConfig
from cctv_audit.video_ingestor import VideoMetadataItem
from cctv_audit.workspace_reporter import WorkspaceReporter

ROOT = Path(__file__).resolve().parent.parent
BOT = "cctv-bot@customer.example.com"
SA = "my-stack-worker@my-project.iam.gserviceaccount.com"
FOLDER_META_OK = {
    "id": "F1",
    "name": "店铺A-0926",
    "mimeType": gcp._FOLDER_MIME,
    "capabilities": {"canAddChildren": True},
}


@pytest.fixture(autouse=True)
def _isolated_identity(monkeypatch):
    """Each test starts in service-account mode with an empty credential cache."""
    monkeypatch.delenv("WORKSPACE_IMPERSONATE_USER", raising=False)
    monkeypatch.delenv("WORKSPACE_DWD_SERVICE_ACCOUNT", raising=False)
    monkeypatch.setattr(gcp, "_workspace_creds", None)
    monkeypatch.setattr(gcp, "_workspace_creds_identity", None)


def _dwd_mode(monkeypatch) -> None:
    monkeypatch.setenv("WORKSPACE_IMPERSONATE_USER", BOT)
    monkeypatch.setenv("WORKSPACE_DWD_SERVICE_ACCOUNT", SA)


def _http_error(status: int, message: str, reason: str) -> HttpError:
    body = {"error": {"code": status, "message": message, "errors": [{"reason": reason, "message": message}]}}
    return HttpError(httplib2.Response({"status": str(status)}), json.dumps(body).encode("utf-8"))


class _Req:
    def __init__(self, result: Any = None, exc: Optional[BaseException] = None) -> None:
        self._result, self._exc = result, exc

    def execute(self, num_retries: int = 0) -> Any:
        if self._exc is not None:
            raise self._exc
        return self._result


class _FakeFiles:
    def __init__(self) -> None:
        self.calls: List[tuple[str, Dict[str, Any]]] = []
        self.handlers: Dict[str, Callable[..., _Req]] = {}

    def _call(self, name: str, **kwargs: Any) -> _Req:
        self.calls.append((name, kwargs))
        handler = self.handlers.get(name)
        return handler(**kwargs) if handler else _Req({})

    def get(self, **kw: Any) -> _Req:
        return self._call("get", **kw)

    def list(self, **kw: Any) -> _Req:
        return self._call("list", **kw)

    def create(self, **kw: Any) -> _Req:
        return self._call("create", **kw)

    def delete(self, **kw: Any) -> _Req:
        return self._call("delete", **kw)

    def update(self, **kw: Any) -> _Req:
        return self._call("update", **kw)

    def names(self) -> List[str]:
        return [name for name, _ in self.calls]


class _FakeDrive:
    def __init__(self) -> None:
        self.api = _FakeFiles()

    def files(self) -> _FakeFiles:
        return self.api


class _FakeSheets:
    """Records every spreadsheets()/values() call as (op, kwargs)."""

    def __init__(self, meta: Optional[dict] = None, values_result: Optional[dict] = None) -> None:
        self.log: List[tuple[str, Dict[str, Any]]] = []
        self.meta = meta or {"sheets": [{"properties": {"sheetId": 0, "title": "Sheet1"}}]}
        self.values_result = values_result or {"values": []}

    def spreadsheets(self) -> "_FakeSheets":
        return self

    def values(self) -> "_FakeSheets._Values":
        return _FakeSheets._Values(self)

    def get(self, **kw: Any) -> _Req:
        self.log.append(("get", kw))
        return _Req(self.meta)

    def batchUpdate(self, **kw: Any) -> _Req:
        self.log.append(("batchUpdate", kw))
        return _Req({})

    class _Values:
        def __init__(self, outer: "_FakeSheets") -> None:
            self.outer = outer

        def _record(self, op: str, kw: Dict[str, Any], result: Any = None) -> _Req:
            self.outer.log.append((f"values.{op}", kw))
            return _Req({} if result is None else result)

        def get(self, **kw: Any) -> _Req:
            return self._record("get", kw, self.outer.values_result)

        def batchClear(self, **kw: Any) -> _Req:
            return self._record("batchClear", kw)

        def batchUpdate(self, **kw: Any) -> _Req:
            return self._record("batchUpdate", kw)

        def update(self, **kw: Any) -> _Req:
            return self._record("update", kw)

        def append(self, **kw: Any) -> _Req:
            return self._record("append", kw)

    def ops(self) -> List[str]:
        return [op for op, _ in self.log]


def _gateway(drive: _FakeDrive, sheets: Optional[_FakeSheets] = None) -> GoogleWorkspaceGateway:
    gw = GoogleWorkspaceGateway()
    gw._drive_service = lambda: drive  # type: ignore[method-assign]
    gw._sheets_service = lambda: sheets  # type: ignore[method-assign]
    return gw


# 1. Keyless DWD credentials -------------------------------------------------------------------


def test_dwd_mode_builds_keyless_signjwt_impersonation(monkeypatch):
    class _Source:
        universe_domain = "googleapis.com"

        def __init__(self) -> None:
            self.quota_project_stripped = False

        def with_quota_project(self, quota_project_id):
            assert quota_project_id is None
            self.quota_project_stripped = True
            return self

    source = _Source()
    requested: Dict[str, List[str]] = {}

    def _fake_default(scopes=None, **_kwargs):
        requested["scopes"] = list(scopes or [])
        return source, "my-project"

    monkeypatch.setattr(google.auth, "default", _fake_default)
    creds = gcp._build_workspace_credentials(BOT, SA)

    assert isinstance(creds, impersonated_credentials.Credentials)
    assert creds._subject == BOT  # DWD path: IAM signJwt + jwt-bearer grant, no key file
    assert creds._target_principal == SA
    assert list(creds._target_scopes) == list(gcp._WORKSPACE_SCOPES)
    assert requested["scopes"] == ["https://www.googleapis.com/auth/cloud-platform"]
    assert source.quota_project_stripped


def test_dwd_mode_requires_the_signing_service_account():
    with pytest.raises(WorkspaceAuthError, match="WORKSPACE_DWD_SERVICE_ACCOUNT"):
        gcp._build_workspace_credentials(BOT, "")


class _RefreshFails:
    valid = False

    def __init__(self, raw: str) -> None:
        self.raw = raw

    def refresh(self, request) -> None:
        raise google.auth.exceptions.RefreshError(self.raw)


@pytest.mark.parametrize(
    "raw, expected",
    [
        (
            "unauthorized_client: Client is unauthorized to retrieve access tokens using this method, "
            "or client not authorized for any of the scopes requested.",
            "域级委派尚未生效",
        ),
        ("invalid_grant: Invalid email or User ID", "无法使用"),
        (
            "Unable to acquire impersonated credentials {\"error\": {\"status\": \"PERMISSION_DENIED\", "
            "\"message\": \"Permission 'iam.serviceAccounts.signJwt' denied\"}}",
            "serviceAccountTokenCreator",
        ),
        (
            "Unable to acquire impersonated credentials {\"error\": {\"status\": \"PERMISSION_DENIED\", "
            "\"details\": [{\"reason\": \"SERVICE_DISABLED\"}]}}",
            "iamcredentials.googleapis.com",
        ),
    ],
)
def test_token_failures_become_operator_actionable(monkeypatch, raw, expected):
    _dwd_mode(monkeypatch)
    monkeypatch.setattr(gcp, "_build_workspace_credentials", lambda subject, sa: _RefreshFails(raw))
    with pytest.raises(WorkspaceAuthError) as info:
        gcp.workspace_credentials()
    assert expected in str(info.value)


def test_credentials_are_cached_per_identity(monkeypatch):
    built: List[tuple[str, str]] = []

    class _Valid:
        valid = True

    def _build(subject: str, sa: str):
        built.append((subject, sa))
        return _Valid()

    monkeypatch.setattr(gcp, "_build_workspace_credentials", _build)
    first = gcp.workspace_credentials()
    assert gcp.workspace_credentials() is first
    _dwd_mode(monkeypatch)
    assert gcp.workspace_credentials() is not first  # identity switch rebuilds once
    assert built == [("", ""), (BOT, SA)]


# 2. Drive failures -> supervisor-facing messages ------------------------------------------------


def test_write_errors_map_to_config_problems(monkeypatch):
    quota = _http_error(403, "Service Accounts do not have storage quota.", "storageQuotaExceeded")
    sa_mode = gcp._classify_write_error(quota, "F1")
    assert isinstance(sa_mode, WorkspaceStorageQuotaError)
    assert "workspace_impersonate_user" in str(sa_mode)

    _dwd_mode(monkeypatch)
    dwd_mode = gcp._classify_write_error(quota, "F1")
    assert isinstance(dwd_mode, WorkspaceStorageQuotaError) and BOT in str(dwd_mode)

    missing = gcp._classify_write_error(_http_error(404, "File not found: F1.", "notFound"), "F1")
    assert isinstance(missing, WorkspaceAccessError)
    assert "F1" in str(missing) and BOT in str(missing) and "编辑者" in str(missing)

    viewer = gcp._classify_write_error(
        _http_error(403, "The user does not have sufficient permissions for this file.", "insufficientFilePermissions"),
        "F1",
    )
    assert isinstance(viewer, WorkspaceAccessError) and "只有查看权限" in str(viewer)

    transient = _http_error(503, "Backend Error", "backendError")
    assert gcp._classify_write_error(transient, "F1") is transient


@pytest.mark.parametrize(
    "meta, dwd, error_type, needle",
    [
        ({**FOLDER_META_OK, "mimeType": "video/mp4"}, True, WorkspaceAccessError, "不是文件夹"),
        ({**FOLDER_META_OK, "capabilities": {"canAddChildren": False}}, True, WorkspaceAccessError, "只有查看权限"),
        (FOLDER_META_OK, False, WorkspaceStorageQuotaError, "服务账号没有云端硬盘空间"),
    ],
)
def test_folder_check_rejects_unusable_folders(monkeypatch, meta, dwd, error_type, needle):
    if dwd:
        _dwd_mode(monkeypatch)
    drive = _FakeDrive()
    drive.api.handlers["get"] = lambda **kw: _Req(meta)
    with pytest.raises(error_type, match=needle):
        GoogleWorkspaceGateway._check_folder_meta(drive, "F1")


def test_folder_check_accepts_shared_drive_for_sa_and_my_drive_for_bot(monkeypatch):
    drive = _FakeDrive()
    drive.api.handlers["get"] = lambda **kw: _Req({**FOLDER_META_OK, "driveId": "0AShared"})
    assert GoogleWorkspaceGateway._check_folder_meta(drive, "F1") == "店铺A-0926"

    _dwd_mode(monkeypatch)
    drive.api.handlers["get"] = lambda **kw: _Req(FOLDER_META_OK)  # personal My Drive
    assert GoogleWorkspaceGateway._check_folder_meta(drive, "F1") == "店铺A-0926"


def test_write_probe_creates_then_deletes_a_tiny_file(monkeypatch):
    _dwd_mode(monkeypatch)
    drive = _FakeDrive()
    drive.api.handlers["get"] = lambda **kw: _Req(FOLDER_META_OK)
    drive.api.handlers["create"] = lambda **kw: _Req({"id": "PROBE1"})

    assert asyncio.run(_gateway(drive).probe_write_access("F1")) == "店铺A-0926"
    assert drive.api.names() == ["get", "create", "delete"]
    assert drive.api.calls[1][1]["body"] == {"name": gcp._WRITE_PROBE_NAME, "parents": ["F1"]}
    assert drive.api.calls[2][1]["fileId"] == "PROBE1"


def test_write_probe_cleanup_failure_never_fails_the_job(monkeypatch):
    _dwd_mode(monkeypatch)
    drive = _FakeDrive()
    drive.api.handlers["get"] = lambda **kw: _Req(FOLDER_META_OK)
    drive.api.handlers["create"] = lambda **kw: _Req({"id": "PROBE1"})
    drive.api.handlers["delete"] = lambda **kw: _Req(exc=_http_error(403, "Insufficient", "insufficientFilePermissions"))

    assert asyncio.run(_gateway(drive).probe_write_access("F1")) == "店铺A-0926"
    assert drive.api.names() == ["get", "create", "delete", "update"]
    assert drive.api.calls[3][1]["body"] == {"trashed": True}


def test_write_probe_reports_bot_without_storage(monkeypatch):
    _dwd_mode(monkeypatch)
    drive = _FakeDrive()
    drive.api.handlers["get"] = lambda **kw: _Req(FOLDER_META_OK)
    drive.api.handlers["create"] = lambda **kw: _Req(
        exc=_http_error(403, "The user's Drive storage quota has been exceeded.", "storageQuotaExceeded")
    )
    with pytest.raises(WorkspaceStorageQuotaError, match=BOT):
        asyncio.run(_gateway(drive).probe_write_access("F1"))


def test_unshared_sop_sheet_is_reported_not_silently_ignored(monkeypatch):
    _dwd_mode(monkeypatch)
    drive = _FakeDrive()
    drive.api.handlers["get"] = lambda **kw: _Req(exc=_http_error(404, "File not found: SOP.", "notFound"))
    with pytest.raises(WorkspaceAccessError, match="SOP 总控表"):
        asyncio.run(_gateway(drive).check_sheet_readable("SOP123"))


# 3. Report Sheet ownership -------------------------------------------------------------------

TAB1 = "违规事件 3 秒复核台"
TAB2 = "本次视频 Token 消耗与耗时账单"


def test_report_sheet_reuses_only_this_audits_own_sheet():
    drive = _FakeDrive()
    drive.api.handlers["list"] = lambda **kw: _Req(
        {
            "files": [
                {"id": "ROSTER", "name": "门店排班表"},
                {"id": "S1", "name": "📊 AI稽核报告与Token账单_2026-09-25_abc123"},
            ]
        }
    )
    sheets = _FakeSheets(
        meta={"sheets": [{"properties": {"sheetId": 0, "title": TAB1}}, {"properties": {"sheetId": 1, "title": TAB2}}]}
    )
    sid, url = asyncio.run(
        _gateway(drive, sheets).create_dual_tab_report_sheet(
            "F1", "📊 AI稽核报告与Token账单_2026-09-26_abc123", [], [], reuse_suffix="_abc123"
        )
    )
    assert sid == "S1" and url.endswith("/spreadsheets/d/S1/edit")
    assert "create" not in drive.api.names()
    ops = sheets.ops()
    assert ops.index("values.batchClear") < ops.index("values.batchUpdate")  # stale rows wiped first
    assert all(kw.get("spreadsheetId") == "S1" for _, kw in sheets.log)


def test_report_sheet_never_writes_into_an_unrelated_spreadsheet():
    drive = _FakeDrive()
    drive.api.handlers["list"] = lambda **kw: _Req({"files": [{"id": "ROSTER", "name": "门店排班表"}]})
    drive.api.handlers["create"] = lambda **kw: _Req({"id": "NEW1"})
    sheets = _FakeSheets()
    sid, _ = asyncio.run(
        _gateway(drive, sheets).create_dual_tab_report_sheet(
            "F1", "📊 AI稽核报告与Token账单_2026-09-26_abc123", [], [], reuse_suffix="_abc123"
        )
    )
    assert sid == "NEW1"
    create_body = dict(drive.api.calls)["create"]["body"]
    assert create_body["mimeType"] == gcp._SPREADSHEET_MIME and create_body["parents"] == ["F1"]
    assert "values.batchClear" not in sheets.ops()
    assert all(kw.get("spreadsheetId") == "NEW1" for _, kw in sheets.log)


def test_fixed_report_sheet_and_evidence_folder_names():
    from cctv_audit.workspace_reporter import EVIDENCE_SUBFOLDER_NAME, REPORT_SHEET_TITLE

    assert EVIDENCE_SUBFOLDER_NAME == "📁 违规证据切片_Evidence"
    assert REPORT_SHEET_TITLE == "📊 AI稽核报告与Token账单"

    drive = _FakeDrive()
    drive.api.handlers["list"] = lambda **kw: _Req(
        {
            "files": [
                {"id": "ROSTER", "name": "门店排班表"},
                {"id": "FIXED_S1", "name": REPORT_SHEET_TITLE},
            ]
        }
    )
    sheets = _FakeSheets(
        meta={"sheets": [{"properties": {"sheetId": 0, "title": TAB1}}, {"properties": {"sheetId": 1, "title": TAB2}}]}
    )
    gw = _gateway(drive, sheets)
    reporter = WorkspaceReporter(gateway=gw, webhook_url="")
    art = asyncio.run(
        reporter.publish_in_folder_report(
            audit_id="job_999",
            user_email="auditor@chagee.com",
            parent_folder_id="F1",
            findings_by_video=[],
            ledger_rows=[],
        )
    )
    assert art.report_sheet_id == "FIXED_S1"
    assert "create" not in drive.api.names()
    ops = sheets.ops()
    assert ops.index("values.batchClear") < ops.index("values.batchUpdate")


def test_report_sheet_creation_failure_surfaces_as_setup_problem():
    drive = _FakeDrive()
    drive.api.handlers["list"] = lambda **kw: _Req({"files": []})
    drive.api.handlers["create"] = lambda **kw: _Req(
        exc=_http_error(403, "Service Accounts do not have storage quota.", "storageQuotaExceeded")
    )
    with pytest.raises(WorkspaceStorageQuotaError):
        asyncio.run(
            _gateway(drive, _FakeSheets()).create_dual_tab_report_sheet(
                "F1", "t_abc123", [], [], reuse_suffix="_abc123"
            )
        )


# 4. Zero-token rejection + watchdog behaviour --------------------------------------------------


class _GatewayStub:
    def __init__(self, probe_exc: Optional[BaseException] = None, sheet_exc: Optional[BaseException] = None):
        self.probe_exc, self.sheet_exc = probe_exc, sheet_exc
        self.probed: List[str] = []

    async def probe_write_access(self, folder_id: str) -> str:
        self.probed.append(folder_id)
        if self.probe_exc is not None:
            raise self.probe_exc
        return "店铺A-0926"

    async def check_sheet_readable(self, sheet_id: str) -> str:
        if self.sheet_exc is not None:
            raise self.sheet_exc
        return "SOP"

    async def ensure_subfolder(self, parent_folder_id: str, name: str) -> str:
        return "SUB1"

    async def upload_evidence_mp4(self, subfolder_id: str, local_path: Path) -> str:
        return ""

    async def create_dual_tab_report_sheet(self, *args: Any, **kwargs: Any) -> tuple[str, str]:
        return "S1", "https://docs.google.com/spreadsheets/d/S1/edit"


class _FastPromptManager:
    async def load_active_config(self, sheet_id: Optional[str] = None) -> PromptModelConfig:
        return PromptModelConfig(system_instruction="test")


def _service(tmp_path: Path, gateway: _GatewayStub) -> AuditService:
    return AuditService(
        job_store=UserScopedJobStore(state_dir=tmp_path, gcs_bucket=""),
        prompt_manager=_FastPromptManager(),  # type: ignore[arg-type]
        reporter=WorkspaceReporter(gateway=gateway, webhook_url=""),
    )


HD_ITEMS = [VideoMetadataItem(file_id="v1", filename="cam01.mp4", width=1920, height=1080, duration_sec=600.0)]


def test_preflight_rejects_setup_problem_with_zero_tokens(tmp_path):
    gateway = _GatewayStub(probe_exc=WorkspaceStorageQuotaError(gcp._storage_quota_message()))
    job = asyncio.run(
        _service(tmp_path, gateway).preflight(
            user_id="auditor@chagee.com",
            drive_url="https://drive.google.com/drive/folders/FOLDER_A",
            preloaded_items=HD_ITEMS,
        )
    )
    assert job.state == JobState.REJECTED
    assert job.preflight_report is not None and job.preflight_report.passed is False
    assert "0 Token" in job.preflight_report.message_to_user
    assert "服务账号没有云端硬盘空间" in job.preflight_report.message_to_user
    assert gateway.probed == ["FOLDER_A"]


def test_preflight_rejects_unreadable_sop_sheet(tmp_path, monkeypatch):
    monkeypatch.setattr(audit_service_module.config, "master_prompt_sheet_id", "SOP1234567890ABCDEF")
    gateway = _GatewayStub(sheet_exc=WorkspaceAccessError("读不到 SOP 总控表 `SOP1234567890ABCDEF`"))
    job = asyncio.run(
        _service(tmp_path, gateway).preflight(
            user_id="auditor@chagee.com",
            drive_url="https://drive.google.com/drive/folders/FOLDER_A",
            preloaded_items=HD_ITEMS,
        )
    )
    assert job.state == JobState.REJECTED
    assert "SOP 总控表" in job.preflight_report.message_to_user


def test_preflight_passes_when_setup_is_healthy(tmp_path):
    job = asyncio.run(
        _service(tmp_path, _GatewayStub()).preflight(
            user_id="auditor@chagee.com",
            drive_url="https://drive.google.com/drive/folders/FOLDER_A",
            preloaded_items=HD_ITEMS,
        )
    )
    assert job.state == JobState.READY


def test_setup_failure_is_parked_for_the_operator_not_retried_by_watchdog(tmp_path):
    gateway = _GatewayStub(probe_exc=WorkspaceAccessError("找不到文件夹 `F1`，请共享给 bot"))
    svc = _service(tmp_path, gateway)
    parked = AuditJob(user_id="auditor@chagee.com", folder_id="F1", state=JobState.RUNNING)
    crashed = AuditJob(user_id="auditor@chagee.com", folder_id="F2", state=JobState.FAILED)

    async def _run() -> None:
        await svc.jobs.save(parked)
        await svc.jobs.save(crashed)
        await svc._run_detached_audit(parked, PromptModelConfig(system_instruction="test"))
        failed = await svc.jobs.get("auditor@chagee.com", parked.job_id)
        assert failed is not None and failed.state == JobState.FAILED
        assert failed.needs_operator_fix is True
        assert "找不到文件夹" in (failed.error_message or "")

        # Watchdog still recovers genuine crashes, but leaves the parked job alone.
        recoverable = await svc.jobs.list_recoverable_jobs()
        assert [j.job_id for j in recoverable] == [crashed.job_id]

        # Supervisor fixes the sharing and replies 确认开始: the flag clears and the restart counts.
        gateway.probe_exc = None
        restarted = await svc.start_audit(
            user_id="auditor@chagee.com", job_id=parked.job_id, wait_for_completion=True
        )
        assert restarted.state == JobState.DONE
        assert restarted.needs_operator_fix is False
        assert restarted.resume_count == 1  # counted even though no slice had completed

    asyncio.run(_run())


# 5. Sheets config client + deploy/IaC parity ---------------------------------------------------


def test_sop_sheet_client_reads_via_workspace_identity_with_ttl_cache(monkeypatch):
    prompt_manager_module._SHEET_RANGE_CACHE.clear()
    fake = _FakeSheets(
        values_result={
            "values": [
                ["Key", "Value"],
                ["Active_Prompt_Version", "Prompt_v2.5_V10全量17条标准版"],
                ["Active_Model_Version", "gemini-3.8-flash"],
            ]
        }
    )
    monkeypatch.setattr(GoogleSheetsConfigClient, "_sheets_service", staticmethod(lambda: fake))
    client = GoogleSheetsConfigClient()
    try:
        first = asyncio.run(client.read_tab0_pointers("SHEET1"))
        second = asyncio.run(client.read_tab0_pointers("SHEET1"))
        assert first["Active_Prompt_Version"] == "Prompt_v2.5_V10全量17条标准版"
        assert first == second
        assert fake.ops().count("values.get") == 1  # second read served from the TTL cache

        asyncio.run(client.append_available_models("SHEET1", ["gemini-4.0-flash"]))
        append_kw = next(kw for op, kw in fake.log if op == "values.append")
        assert append_kw["range"] == "'Tab0_版本总控与回滚开关'!A1"
        assert "gemini-4.0-flash" in append_kw["body"]["values"][0][1]
    finally:
        prompt_manager_module._SHEET_RANGE_CACHE.clear()


def test_sop_sheet_pointers_are_never_rewritten_and_retired_models_fall_back(monkeypatch):
    """REQ-013: the runtime never overwrites the customer's Tab 0 pointer cells (even as Editor).

    A retired model name is probed and replaced in memory only by the configured fallback
    (FALLBACK_MODEL_VERSION), with a warning the supervisor sees -- no hardcoded version.
    """
    prompt_manager_module._SHEET_RANGE_CACHE.clear()
    fake = _FakeSheets(
        values_result={
            "values": [
                ["Key", "Value"],
                ["Active_Model_Version", "gemini-1.5-flash-002"],
                ["Fallback_Model_Version", "gemini-1.5-flash-002"],
            ]
        }
    )
    monkeypatch.setattr(GoogleSheetsConfigClient, "_sheets_service", staticmethod(lambda: fake))
    try:
        pointers = asyncio.run(GoogleSheetsConfigClient().read_tab0_pointers("SHEET1"))
        assert pointers["Active_Model_Version"] == "gemini-1.5-flash-002"  # returned verbatim
        assert fake.ops() == ["values.get"]  # no update/append on the customer's control Sheet

        probed: List[str] = []

        class _Models:
            def generate_content(self, model: str, contents: str) -> None:
                probed.append(model)
                raise RuntimeError("404 NOT_FOUND: model retired")

        class _Client:
            models = _Models()

        async def _fake_client() -> _Client:
            return _Client()

        monkeypatch.setattr(prompt_manager_module, "get_genai_client", _fake_client)
        monkeypatch.setattr(prompt_manager_module.config, "fallback_model_version", "cfg-fallback-flash")
        model, warning = asyncio.run(
            PromptManager(sheet_client=None).resolve_and_probe_model(
                pointers["Active_Model_Version"], pointers["Fallback_Model_Version"]
            )
        )
        assert model == "cfg-fallback-flash"
        assert warning is not None and "gemini-1.5-flash-002" in warning
        assert probed == ["gemini-1.5-flash-002"]  # the configured fallback itself is not re-probed
    finally:
        prompt_manager_module._SHEET_RANGE_CACHE.clear()


def _load_deploy_module():
    spec = importlib.util.spec_from_file_location(
        "deploy_reasoning_engine_under_test", ROOT / "deploy" / "deploy_reasoning_engine.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _re_env(**overrides: str) -> Dict[str, str]:
    body = _load_deploy_module().build_reasoning_engine_body(
        project_id="my-project",
        location="us-central1",
        image_uri="europe-west4-docker.pkg.dev/my-project/my-stack-images/cctv-audit-worker:latest",
        master_prompt_sheet_id="1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789_-abcd",
        service_account=SA,
        display_name="my-stack-agent",
        gcp_location="europe-west4",
        **overrides,
    )
    return {e["name"]: e["value"] for e in body["spec"]["deploymentSpec"]["env"]}


def test_reasoning_engine_gets_the_same_workspace_identity_as_cloud_run():
    dwd = _re_env(workspace_impersonate_user=BOT, workspace_dwd_service_account=SA)
    assert dwd["WORKSPACE_IMPERSONATE_USER"] == BOT
    assert dwd["WORKSPACE_DWD_SERVICE_ACCOUNT"] == SA
    assert dwd["ENABLE_BACKGROUND_WATCHDOG"] == "false"  # ADR-003
    assert dwd["GCP_LOCATION"] == "europe-west4"  # the stack's region, never a code default

    sa_mode = _re_env(workspace_impersonate_user="", workspace_dwd_service_account=SA)
    assert "WORKSPACE_IMPERSONATE_USER" not in sa_mode
    assert sa_mode["WORKSPACE_DWD_SERVICE_ACCOUNT"] == SA


def _hcl_block(text: str, header: str) -> str:
    """The HCL block that starts with `header`, up to its matching closing brace."""
    start = text.index(header)
    depth = 0
    for i in range(text.index("{", start), len(text)):
        depth += {"{": 1, "}": -1}.get(text[i], 0)
        if depth == 0:
            return text[start : i + 1]
    raise AssertionError(f"unterminated block {header!r}")


def test_terraform_matches_code_for_workspace_identity():
    tf = (ROOT / "main.tf").read_text(encoding="utf-8")
    boot = (ROOT / "bootstrap" / "main.tf").read_text(encoding="utf-8")
    scopes = re.search(r'workspace_dwd_scopes\s*=\s*"([^"]+)"', tf)
    assert scopes is not None and scopes.group(1).split(",") == list(gcp._WORKSPACE_SCOPES + gcp._CHAT_MENTION_SCOPES)
    assert 'name  = "WORKSPACE_DWD_SERVICE_ACCOUNT"' in tf
    assert 'name  = "WORKSPACE_IMPERSONATE_USER"' in tf
    assert '--workspace-impersonate-user "${var.workspace_impersonate_user}"' in tf
    # Both tiers sign DWD JWTs as the runtime SA. bootstrap/ creates it; main.tf only reads it, and
    # both roots must name the same account.
    assert '--workspace-dwd-service-account "${data.google_service_account.audit_worker_sa.email}"' in tf
    data_sa = _hcl_block(tf, 'data "google_service_account" "audit_worker_sa"')
    assert re.search(r"account_id\s*=\s*local\.worker_account_id\s", data_sa)
    derivation = re.compile(r'worker_account_id\s*=\s*"\$\{var\.name_prefix\}-worker"')
    assert derivation.search(tf) and derivation.search(boot)
    # Keyless DWD: TokenCreator on the SA itself, and only on itself (granted by bootstrap/).
    grant = _hcl_block(boot, 'resource "google_service_account_iam_member" "worker_self_token_creator"')
    assert re.search(r"service_account_id\s*=\s*google_service_account\.worker\.name\s", grant)
    assert re.search(r'role\s*=\s*"roles/iam\.serviceAccountTokenCreator"', grant)
    assert re.search(r'member\s*=\s*"serviceAccount:\$\{google_service_account\.worker\.email\}"', grant)
    assert '"iamcredentials.googleapis.com"' in tf
    assert re.search(r'name\s*=\s*"ENABLE_BACKGROUND_WATCHDOG"\s*\n\s*value\s*=\s*"false"', tf)
    assert "GOOGLE_WORKSPACE_OAUTH_JSON" not in tf + boot  # no stored user token anywhere in IaC


def test_resolve_chat_user_id_and_completion_notification_mention(monkeypatch):
    r"""When DWD is configured, `resolve_chat_user_id` resolves the initiating supervisor's email
    to their 21-digit OIDC `sub` (cached per process) and `send_completion_notification` formats
    `<users/{sub}> (\`{email}\`)`; on any failure or without DWD it falls back to `\`{email}\``."""
    import io
    import json
    import urllib.request
    from unittest import mock

    from cctv_audit import gcp as gcp_mod
    from cctv_audit.workspace_reporter import WorkspaceReporter

    gcp_mod._chat_user_id_cache.clear()
    monkeypatch.setenv("WORKSPACE_IMPERSONATE_USER", BOT)
    monkeypatch.setenv("WORKSPACE_DWD_SERVICE_ACCOUNT", SA)

    impersonated_calls = []

    class _FakeImpersonated:
        def __init__(self, *, source_credentials, target_principal, target_scopes, subject):
            impersonated_calls.append((target_principal, tuple(target_scopes), subject))
            self.token = "tok-sub"

        def refresh(self, _req):
            pass

    urlopen_calls = []

    class _FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def read(self):
            return json.dumps({"sub": "104120710022716991580", "name": "test-1 user"}).encode("utf-8")

    def _fake_urlopen(req, timeout=8.0):
        urlopen_calls.append((req.full_url, req.headers.get("Authorization")))
        return _FakeResp()

    with (
        mock.patch("google.auth.default", return_value=(mock.MagicMock(), "proj")),
        mock.patch("google.auth.impersonated_credentials.Credentials", _FakeImpersonated),
        mock.patch.object(urllib.request, "urlopen", side_effect=_fake_urlopen),
    ):
        uid1 = gcp_mod.resolve_chat_user_id("Test-1@example.com ")
        uid2 = gcp_mod.resolve_chat_user_id("test-1@example.com")
        assert uid1 == "104120710022716991580"
        assert uid2 == "104120710022716991580"
        assert len(impersonated_calls) == 1  # second call hit cache
        assert impersonated_calls[0] == (
            SA,
            ("https://www.googleapis.com/auth/userinfo.profile",),
            "test-1@example.com",
        )
        assert len(urlopen_calls) == 1

        # Service account or invalid email returns "" without network calls
        assert gcp_mod.resolve_chat_user_id("worker@proj.iam.gserviceaccount.com") == ""
        assert gcp_mod.resolve_chat_user_id("") == ""
        assert len(urlopen_calls) == 1

    # Webhook payload includes <users/104120710022716991580> (`test-1@example.com`)
    posted = []

    class _FakeAsyncClient:
        def __init__(self, **_kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def post(self, url, json):
            posted.append((url, json))
            r = mock.MagicMock()
            r.raise_for_status = lambda: None
            return r

    reporter = WorkspaceReporter(gateway=None, webhook_url="https://chat.googleapis.com/v1/spaces/X/messages?key=k&token=t")
    with mock.patch("httpx.AsyncClient", _FakeAsyncClient):
        ok = asyncio.run(
            reporter.send_completion_notification(
                user_email="test-1@example.com",
                audit_id="42e5d3",
                sheet_url="https://docs.google.com/spreadsheets/d/S/edit",
                violations_count=29,
                total_tokens=2022031,
            )
        )
        assert ok is True
        assert "<users/104120710022716991580> (`test-1@example.com`)" in posted[-1][1]["text"]

        # When DWD is absent or fails for an unknown user, falls back gracefully to plain email
        monkeypatch.delenv("WORKSPACE_DWD_SERVICE_ACCOUNT", raising=False)
        ok2 = asyncio.run(
            reporter.send_completion_notification(
                user_email="other@example.com",
                audit_id="999999",
                sheet_url="https://docs.google.com/spreadsheets/d/S/edit",
                violations_count=0,
                total_tokens=100,
            )
        )
        assert ok2 is True
        assert "• **发起督导**：`other@example.com`" in posted[-1][1]["text"]
        assert "<users/" not in posted[-1][1]["text"]

        # When ENABLE_GOOGLE_CHAT_NOTIFICATION=false, send_completion_notification skips posting
        # even when webhook_url is configured.
        monkeypatch.setenv("ENABLE_GOOGLE_CHAT_NOTIFICATION", "false")
        from cctv_audit.config import AuditConfig, config as app_cfg
        assert AuditConfig().enable_google_chat_notification is False
        with mock.patch.object(app_cfg, "enable_google_chat_notification", False):
            ok3 = asyncio.run(
                reporter.send_completion_notification(
                    user_email="other@example.com",
                    audit_id="999998",
                    sheet_url="https://docs.google.com/spreadsheets/d/S/edit",
                    violations_count=0,
                    total_tokens=100,
                )
            )
            assert ok3 is False
            assert len(posted) == 2

        monkeypatch.setenv("ENABLE_GOOGLE_CHAT_NOTIFICATION", "true")
        assert AuditConfig().enable_google_chat_notification is True

    gcp_mod._chat_user_id_cache.clear()
