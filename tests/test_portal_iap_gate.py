"""Round 86A: IAP-protected portal service, per-store Google Drive gate, fail-closed review audit log.

Hermetic: the IAP JWT verifier, the Drive permission checker, the Drive API client, the GCS client
and the report Sheet gateway are all fakes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

from cctv_audit import gcp, portal_auth
from cctv_audit.jobs import AuditJob, JobState, UserScopedJobStore
from cctv_audit.portal import apply_portal_reviews, compute_portal_signature, load_portal_payload
from cctv_audit.portal_auth import IapAuthError, expected_iap_audience, verify_iap_jwt
from cctv_audit.review_audit_log import (
    PHASE_REQUESTED,
    PHASE_SHEET_WRITE_FAILED,
    ReviewAuditLogError,
    write_review_audit_records,
)
from cctv_audit.scheduled_auditor import ScheduledBatchRecord, ScheduledStateStore, ScheduledStoreTask

AUD = "/projects/123456/locations/asia-southeast1/services/p-portal"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch):
    for key in (
        "PORTAL_MODE",
        "PORTAL_SERVICE_URL",
        "PORTAL_IAP_AUDIENCE",
        "PORTAL_PROJECT_NUMBER",
        "PORTAL_REGION",
        "K_SERVICE",
        "AUDIT_LOG_BUCKET",
    ):
        monkeypatch.delenv(key, raising=False)
    portal_auth._reset_audience_warning_for_tests()
    portal_auth.set_iap_verifier(None)
    portal_auth.set_drive_access_checker(None)
    gcp._clear_viewer_drive_cache()
    yield
    portal_auth.set_iap_verifier(None)
    portal_auth.set_drive_access_checker(None)
    gcp._clear_viewer_drive_cache()


def _make_verifier(actual_aud: str = AUD, *, valid_sig: bool = True, email: str = "Alice@Customer.com"):
    calls: List[Optional[str]] = []

    def _verify(token: str, audience: Optional[str]) -> Dict[str, Any]:
        calls.append(audience)
        if not valid_sig or token != "good-token":
            raise ValueError("Could not verify token signature.")
        if audience is not None and audience != actual_aud:
            raise ValueError(f"Token has wrong audience {actual_aud}, expected one of {audience}")
        return {"email": email, "aud": actual_aud, "iss": "https://cloud.google.com/iap"}

    _verify.calls = calls  # type: ignore[attr-defined]
    return _verify


# ---- A1: IAP JWT verification -------------------------------------------------------------


def test_verify_iap_jwt_ok_returns_lowercased_email() -> None:
    assert verify_iap_jwt("good-token", audience=AUD, verifier=_make_verifier()) == "alice@customer.com"


def test_verify_iap_jwt_missing_token_is_401() -> None:
    with pytest.raises(IapAuthError):
        verify_iap_jwt("", audience=AUD, verifier=_make_verifier())


def test_verify_iap_jwt_bad_signature_is_401_without_audience_warning(caplog) -> None:
    caplog.set_level(logging.WARNING, logger="cctv_audit.portal_auth")
    with pytest.raises(IapAuthError):
        verify_iap_jwt("good-token", audience=AUD, verifier=_make_verifier(valid_sig=False))
    assert not [r for r in caplog.records if "audience" in r.getMessage()]


def test_verify_iap_jwt_wrong_audience_logs_actual_aud_once(caplog) -> None:
    caplog.set_level(logging.WARNING, logger="cctv_audit.portal_auth")
    actual = "/projects/999/locations/asia-southeast1/services/other"
    verifier = _make_verifier(actual_aud=actual)
    for _ in range(3):
        with pytest.raises(IapAuthError):
            verify_iap_jwt("good-token", audience=AUD, verifier=verifier)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and actual in r.getMessage()]
    assert len(warnings) == 1
    assert "PORTAL_IAP_AUDIENCE" in warnings[0].getMessage()
    assert None in verifier.calls  # signature re-checked without audience to read `aud`


def test_verify_iap_jwt_rejects_non_iap_issuer() -> None:
    def _verify(token: str, audience: Optional[str]) -> Dict[str, Any]:
        return {"email": "a@b.com", "aud": AUD, "iss": "https://accounts.google.com"}

    with pytest.raises(IapAuthError):
        verify_iap_jwt("t", audience=AUD, verifier=_verify)


def test_expected_audience_computed_and_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    assert expected_iap_audience() == ""
    monkeypatch.setenv("PORTAL_PROJECT_NUMBER", "123456")
    monkeypatch.setenv("GCP_LOCATION", "asia-southeast1")
    monkeypatch.setenv("K_SERVICE", "p-portal")
    assert expected_iap_audience() == AUD
    monkeypatch.setenv("PORTAL_IAP_AUDIENCE", "/projects/1/locations/x/services/y")
    assert expected_iap_audience() == "/projects/1/locations/x/services/y"


def test_authenticate_ignores_unsigned_email_header(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PORTAL_IAP_AUDIENCE", AUD)

    class _Req:
        headers = {"x-goog-authenticated-user-email": "accounts.google.com:evil@x.com"}

    with pytest.raises(IapAuthError):
        portal_auth.authenticate_portal_viewer(_Req(), verifier=_make_verifier())


# ---- A1: route lockdown (iap) and worker redirect (hmac) ------------------------------------


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from cctv_audit import server

    return TestClient(server.app)


def test_iap_mode_locks_down_non_portal_routes(monkeypatch: pytest.MonkeyPatch, client) -> None:
    monkeypatch.setenv("PORTAL_MODE", "iap")
    for method, path in (
        ("post", "/a2a"),
        ("post", "/execute"),
        ("post", "/inspect"),
        ("post", "/internal/jobs/sweep"),
        ("post", "/internal/jobs/execute"),
        ("post", "/api/reasoning_engine"),
        ("post", "/api/stream_reasoning_engine"),
        ("get", "/.well-known/agent-card.json"),
        ("get", "/is_busy"),
    ):
        resp = client.post(path, json={}) if method == "post" else client.get(path)
        assert resp.status_code == 404, path
    assert client.get("/healthz").status_code == 200
    # Portal routes are reachable but require a verified IAP identity.
    assert client.get("/portal?job=x&exp=1&sig=y").status_code == 401
    assert client.get("/api/portal/data?job=x").status_code == 401
    assert client.post("/api/portal/review", json={}).status_code == 401


def test_iap_mode_never_starts_background_watchdog(monkeypatch: pytest.MonkeyPatch) -> None:
    from cctv_audit import server

    monkeypatch.setenv("PORTAL_MODE", "iap")
    monkeypatch.setenv("ENABLE_BACKGROUND_WATCHDOG", "true")

    def _boom(*_a: Any, **_k: Any) -> None:
        raise AssertionError("watchdog must not start in iap mode")

    monkeypatch.setattr(server.asyncio, "create_task", _boom)
    asyncio.run(server._start_unattended_watchdog())


def test_worker_redirects_portal_and_refuses_apis_when_portal_service_url_set(
    monkeypatch: pytest.MonkeyPatch, client
) -> None:
    monkeypatch.setenv("PORTAL_SERVICE_URL", "https://p-portal-123456.asia-southeast1.run.app/")
    resp = client.get("/portal?job=abc&user=s%40x.com&exp=9&sig=ff", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == (
        "https://p-portal-123456.asia-southeast1.run.app/portal?job=abc&user=s%40x.com&exp=9&sig=ff"
    )
    data = client.get("/api/portal/data?job=abc")
    assert data.status_code == 403 and data.json()["ok"] is False
    assert "use the portal URL" in data.json()["error"]
    review = client.post("/api/portal/review", json={"job": "abc"})
    assert review.status_code == 403 and "use the portal URL" in review.json()["error"]


def test_worker_portal_unchanged_without_portal_service_url(client) -> None:
    # Missing signature -> the pre-existing HMAC 403, no redirect.
    resp = client.get("/portal?job=abc", follow_redirects=False)
    assert resp.status_code == 403
    assert client.get("/api/portal/data?job=abc").status_code == 403


def test_review_endpoint_returns_500_when_audit_log_fails(monkeypatch: pytest.MonkeyPatch, client) -> None:
    from cctv_audit import server

    async def _raise(**_kwargs: Any) -> Dict[str, Any]:
        raise ReviewAuditLogError("bucket unavailable")

    monkeypatch.setattr(server, "apply_portal_reviews", _raise)
    resp = client.post("/api/portal/review", json={"job": "abc"})
    assert resp.status_code == 500
    assert resp.json()["ok"] is False and "No change was made" in resp.json()["error"]


def test_tenant_fallback_is_not_a_brand_email() -> None:
    from cctv_audit import server

    assert server.DEFAULT_TENANT_FALLBACK
    assert "@chagee.com" not in server.DEFAULT_TENANT_FALLBACK
    assert server.resolve_tenant_identity() == server.DEFAULT_TENANT_FALLBACK


# ---- A2: Drive gate (gcp.viewer_can_open_drive_file) ----------------------------------------


class _FakeDrive:
    def __init__(self, outcome: Any) -> None:
        self.outcome = outcome
        self.get_calls: List[Dict[str, Any]] = []

    def files(self) -> "_FakeDrive":
        return self

    def get(self, **kwargs: Any) -> "_FakeDrive":
        self.get_calls.append(kwargs)
        return self

    def execute(self, num_retries: int = 0) -> Dict[str, Any]:
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return {"id": "sheetA"}


def _http_error(status: int) -> Exception:
    import httplib2
    from googleapiclient.errors import HttpError

    return HttpError(httplib2.Response({"status": status}), b'{"error": "nope"}')


def test_drive_gate_allow_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    builds: List[str] = []
    drive = _FakeDrive(None)

    def _build(viewer: str) -> _FakeDrive:
        builds.append(viewer)
        return drive

    monkeypatch.setattr(gcp, "_build_viewer_drive_service", _build)
    assert asyncio.run(gcp.viewer_can_open_drive_file("Alice@X.com", "sheetA")) == (True, "")
    assert asyncio.run(gcp.viewer_can_open_drive_file("alice@x.com", "sheetA")) == (True, "")
    assert builds == ["alice@x.com"]
    assert drive.get_calls == [{"fileId": "sheetA", "fields": "id", "supportsAllDrives": True}]


@pytest.mark.parametrize("status", [403, 404])
def test_drive_gate_deny_is_cached(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    builds: List[str] = []

    def _build(viewer: str) -> _FakeDrive:
        builds.append(viewer)
        return _FakeDrive(_http_error(status))

    monkeypatch.setattr(gcp, "_build_viewer_drive_service", _build)
    ok, reason = asyncio.run(gcp.viewer_can_open_drive_file("bob@x.com", "sheetA"))
    assert ok is False and str(status) in reason
    assert asyncio.run(gcp.viewer_can_open_drive_file("bob@x.com", "sheetA"))[0] is False
    assert len(builds) == 1


def test_drive_gate_unauthorized_client_denies_with_clear_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    def _build(viewer: str) -> Any:
        raise gcp.WorkspaceAuthError("Domain-Wide Delegation ... (raw error: unauthorized_client)")

    monkeypatch.setattr(gcp, "_build_viewer_drive_service", _build)
    ok, reason = asyncio.run(gcp.viewer_can_open_drive_file("carol@x.com", "sheetA"))
    assert ok is False and "domain-wide delegation" in reason


def test_drive_gate_outside_domain_denies(monkeypatch: pytest.MonkeyPatch) -> None:
    def _build(viewer: str) -> Any:
        raise gcp.WorkspaceAuthError("Impersonated user ... (raw error: invalid_grant)")

    monkeypatch.setattr(gcp, "_build_viewer_drive_service", _build)
    ok, reason = asyncio.run(gcp.viewer_can_open_drive_file("ext@gmail.com", "sheetA"))
    assert ok is False and "not a user of the Google Workspace domain" in reason


def test_drive_gate_cache_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    builds: List[str] = []

    def _build(viewer: str) -> _FakeDrive:
        builds.append(viewer)
        return _FakeDrive(None)

    monkeypatch.setattr(gcp, "_build_viewer_drive_service", _build)
    monkeypatch.setattr(gcp, "VIEWER_DRIVE_ACCESS_TTL_SEC", 0.0)
    asyncio.run(gcp.viewer_can_open_drive_file("a@x.com", "s1"))
    asyncio.run(gcp.viewer_can_open_drive_file("a@x.com", "s1"))
    assert len(builds) == 2


def test_extract_drive_file_id_variants() -> None:
    from cctv_audit.portal_auth import drive_target_for_store, extract_drive_file_id

    assert extract_drive_file_id("https://docs.google.com/spreadsheets/u/1/d/AbCdEfGhIjKlMnOp/edit") == "AbCdEfGhIjKlMnOp"
    assert extract_drive_file_id("https://docs.google.com/spreadsheets/d/AbCdEfGhIjKlMnOp/edit#gid=0") == "AbCdEfGhIjKlMnOp"
    assert extract_drive_file_id("AbCdEfGhIjKlMnOp") == "AbCdEfGhIjKlMnOp"
    assert extract_drive_file_id("gs://b/x.xlsx") == ""
    assert drive_target_for_store("", "FolderId0123456") == "FolderId0123456"
    assert drive_target_for_store("gs://b/stores/a/r.xlsx", "gs://b/stores/a") is None


# ---- A2: portal payload gating ------------------------------------------------------------


TAB1_ROW = [
    "jobA",
    "cam01.mp4",
    "03:12 - 03:27",
    "C1",
    "CONFIRMED",
    "CRITICAL",
    "0.96",
    "desc",
    "https://drive.google.com/file/d/ev1/view",
    "PENDING",
    "",
]


class _FakeGateway:
    def __init__(self, rows: Optional[List[List[Any]]] = None, *, fail_update: bool = False) -> None:
        self.rows = [list(r) for r in (rows if rows is not None else [TAB1_ROW])]
        self.fail_update = fail_update
        self.update_calls: List[List[Dict[str, Any]]] = []

    async def read_report_sheet_data(self, folder_id: str, report_sheet_url: str = "") -> Tuple[list, list]:
        return [list(r) for r in self.rows], []

    async def update_report_human_reviews(self, folder_id: str, report_sheet_url: str, updates: list) -> int:
        self.update_calls.append(list(updates))
        if self.fail_update:
            raise RuntimeError("Sheets API 500")
        from cctv_audit.gcs_gateway import match_and_apply_tab1_reviews

        updated, patches = match_and_apply_tab1_reviews(self.rows, updates)
        self.rows = updated
        return len(patches)


def _checker(allowed_ids: set):
    calls: List[Tuple[str, str]] = []

    async def _check(viewer: str, file_id: str) -> Tuple[bool, str]:
        calls.append((viewer, file_id))
        return (file_id in allowed_ids), ("" if file_id in allowed_ids else "HTTP 404")

    _check.calls = calls  # type: ignore[attr-defined]
    return _check


async def _seed_single(tmp_path: Path, *, folder_id: str = "FolderA0123456", sheet: str = "") -> UserScopedJobStore:
    store = UserScopedJobStore(state_dir=tmp_path / "jobs", gcs_bucket="")
    await store.save(
        AuditJob(
            job_id="jobA",
            user_id="sup@x.com",
            folder_id=folder_id,
            folder_name="Store A",
            state=JobState.DONE,
            report_sheet_url=sheet or "https://docs.google.com/spreadsheets/d/SheetAAAAAAAAAAAAA/edit",
        )
    )
    return store


def test_single_store_denied_without_drive_access(tmp_path: Path) -> None:
    async def _run() -> None:
        store = await _seed_single(tmp_path)
        exp = int(time.time()) + 3600
        sig = compute_portal_signature(job_id="jobA", user_id="sup@x.com", exp=exp)
        kwargs = dict(
            job_store=store,
            state_store=ScheduledStateStore(memory_store={}),
            gateway=_FakeGateway(),
            job_id="jobA",
            user_id="sup@x.com",
            exp=exp,
            sig=sig,
            viewer_email="viewer@x.com",
        )
        with pytest.raises(PermissionError) as exc:
            await load_portal_payload(**kwargs, drive_checker=_checker(set()))
        assert "don't have Google Drive access" in str(exc.value) and "viewer@x.com" in str(exc.value)
        ok_checker = _checker({"SheetAAAAAAAAAAAAA"})
        payload = await load_portal_payload(**kwargs, drive_checker=ok_checker)
        assert payload["audit_id"] == "jobA"
        assert ok_checker.calls == [("viewer@x.com", "SheetAAAAAAAAAAAAA")]

    asyncio.run(_run())


def test_gcs_store_skips_drive_check(tmp_path: Path) -> None:
    async def _run() -> None:
        store = await _seed_single(
            tmp_path, folder_id="gs://bkt/stores/a", sheet="gs://bkt/stores/a/report.xlsx"
        )
        exp = int(time.time()) + 3600
        sig = compute_portal_signature(job_id="jobA", user_id="sup@x.com", exp=exp)

        async def _never(viewer: str, file_id: str) -> Tuple[bool, str]:
            raise AssertionError("gs:// stores must not be Drive-checked")

        payload = await load_portal_payload(
            job_store=store,
            state_store=ScheduledStateStore(memory_store={}),
            gateway=_FakeGateway(),
            job_id="jobA",
            user_id="sup@x.com",
            exp=exp,
            sig=sig,
            viewer_email="viewer@x.com",
            drive_checker=_never,
        )
        assert payload["audit_id"] == "jobA"

    asyncio.run(_run())


async def _seed_batch(tmp_path: Path) -> Tuple[UserScopedJobStore, ScheduledStateStore]:
    store = UserScopedJobStore(state_dir=tmp_path / "jobs", gcs_bucket="")
    tasks: Dict[str, ScheduledStoreTask] = {}
    for key in ("A", "B", "C"):
        sheet = f"https://docs.google.com/spreadsheets/d/Sheet{key * 14}/edit"
        await store.save(
            AuditJob(
                job_id=f"job{key}",
                user_id="sup@x.com",
                folder_id=f"Folder{key * 10}",
                folder_name=f"Store {key}",
                state=JobState.DONE,
                report_sheet_url=sheet,
            )
        )
        tasks[f"job{key}"] = ScheduledStoreTask(
            job_id=f"job{key}",
            user_id="sup@x.com",
            folder_id=f"Folder{key * 10}",
            folder_name=f"Store {key}",
            video_count=1,
            state="DONE",
            report_sheet_url=sheet,
        )
    state = ScheduledStateStore(memory_store={})
    rec = ScheduledBatchRecord(
        batch_id="b1",
        rule_id="R1",
        rule_name="daily",
        row_index=2,
        root_folder_id="root",
        root_folder_url="https://drive.google.com/drive/folders/root",
        supervisor_email="sup@x.com",
        local_date="2026-10-23",
        store_tasks=tasks,
    )
    state.write_text_cas(state.batch_key("b1"), rec.model_dump_json(), if_generation_match=0)
    return store, state


def test_batch_hides_stores_without_drive_access(tmp_path: Path) -> None:
    async def _run() -> None:
        store, state = await _seed_batch(tmp_path)
        exp = int(time.time()) + 3600
        sig = compute_portal_signature(batch_id="b1", exp=exp)
        base = dict(job_store=store, state_store=state, gateway=_FakeGateway(), batch_id="b1", exp=exp, sig=sig)
        checker = _checker({"Sheet" + "A" * 14, "Sheet" + "B" * 14})
        payload = await load_portal_payload(**base, viewer_email="v@x.com", drive_checker=checker)
        assert [s["job_id"] for s in payload["batch_stores"]] == ["jobA", "jobB"]
        assert payload["audit_id"] in ("jobA", "jobB")
        assert len(checker.calls) == 3
        # Explicitly requesting a hidden store is a 403, not a silent switch.
        with pytest.raises(PermissionError):
            await load_portal_payload(**base, store_job_id="jobC", viewer_email="v@x.com", drive_checker=checker)
        # No visible store -> 403.
        with pytest.raises(PermissionError):
            await load_portal_payload(**base, viewer_email="v@x.com", drive_checker=_checker(set()))
        # Without a viewer (hmac mode) nothing is filtered.
        legacy = await load_portal_payload(**base)
        assert len(legacy["batch_stores"]) == 3

    asyncio.run(_run())


def test_batch_review_api_enforces_drive_check_for_target_store(tmp_path: Path) -> None:
    async def _run() -> None:
        store, state = await _seed_batch(tmp_path)
        exp = int(time.time()) + 3600
        sig = compute_portal_signature(batch_id="b1", exp=exp)
        gw = _FakeGateway()
        writes: List[Any] = []
        with pytest.raises(PermissionError):
            await apply_portal_reviews(
                job_store=store,
                state_store=state,
                gateway=gw,
                batch_id="b1",
                store_job_id="jobC",
                exp=exp,
                sig=sig,
                updates=[{"incident_id": "x", "status": "confirmed"}],
                viewer_email="v@x.com",
                iap_mode=True,
                drive_checker=_checker({"Sheet" + "A" * 14}),
                audit_writer=lambda records, bucket: writes.append(records) or [],
                audit_bucket="bkt",
            )
        assert gw.update_calls == [] and writes == []

    asyncio.run(_run())


# ---- A3: review audit log -----------------------------------------------------------------


class _FakeBlob:
    def __init__(self, sink: List[Dict[str, Any]], name: str) -> None:
        self.sink = sink
        self.name = name

    def upload_from_string(self, data: str, **kwargs: Any) -> None:
        self.sink.append({"name": self.name, "data": data, **kwargs})


class _FakeBucket:
    def __init__(self, sink: List[Dict[str, Any]]) -> None:
        self.sink = sink

    def blob(self, name: str) -> _FakeBlob:
        return _FakeBlob(self.sink, name)


class _FakeGcs:
    def __init__(self) -> None:
        self.sink: List[Dict[str, Any]] = []
        self.buckets: List[str] = []

    def bucket(self, name: str) -> _FakeBucket:
        self.buckets.append(name)
        return _FakeBucket(self.sink)


def test_audit_writer_is_create_only_with_dated_path() -> None:
    gcs = _FakeGcs()
    rec = {"ts_utc": "2026-10-23T08:50:54.123456Z", "job_id": "jobA", "batch_id": "", "phase": "requested"}
    uris = write_review_audit_records([rec], bucket="ws-bucket", client=gcs)
    assert gcs.buckets == ["ws-bucket"]
    put = gcs.sink[0]
    assert put["if_generation_match"] == 0
    assert put["content_type"] == "application/json"
    assert re.fullmatch(
        r"audit_logs/reviews/2026/10/23/jobA/20261023T085054123456Z_[0-9a-f]{8}\.json", put["name"]
    )
    assert json.loads(put["data"]) == rec
    assert uris == [f"gs://ws-bucket/{put['name']}"]


def test_audit_writer_failure_raises_review_audit_log_error() -> None:
    class _Broken:
        def bucket(self, name: str) -> Any:
            raise RuntimeError("403 storage.objects.create denied")

    with pytest.raises(ReviewAuditLogError):
        write_review_audit_records([{"job_id": "j"}], bucket="b", client=_Broken())
    with pytest.raises(ReviewAuditLogError):
        write_review_audit_records([{"job_id": "j"}], bucket="", client=_FakeGcs())


def _incident_id() -> str:
    from cctv_audit.gcs_gateway import build_incident_composite_key

    return build_incident_composite_key("jobA", "cam01.mp4", "03:12 - 03:27", "C1", 0)


async def _apply(tmp_path: Path, gw: _FakeGateway, writer: Any, **extra: Any) -> Dict[str, Any]:
    store = await _seed_single(tmp_path)
    exp = int(time.time()) + 3600
    sig = compute_portal_signature(job_id="jobA", user_id="sup@x.com", exp=exp)
    kwargs: Dict[str, Any] = dict(
        job_store=store,
        state_store=ScheduledStateStore(memory_store={}),
        gateway=gw,
        job_id="jobA",
        user_id="sup@x.com",
        exp=exp,
        sig=sig,
        updates=[{"incident_id": _incident_id(), "status": "confirmed", "note": "checked with manager"}],
        audit_writer=writer,
        audit_bucket="ws-bucket",
        client_ip="203.0.113.7",
        user_agent="pytest-UA",
    )
    kwargs.update(extra)
    return await apply_portal_reviews(**kwargs)


def test_review_writes_audit_record_before_sheet_iap(tmp_path: Path) -> None:
    order: List[str] = []
    batches: List[List[Dict[str, Any]]] = []

    class _OrderedGateway(_FakeGateway):
        async def update_report_human_reviews(self, *a: Any, **k: Any) -> int:
            order.append("sheet")
            return await super().update_report_human_reviews(*a, **k)

    def _writer(records: List[Dict[str, Any]], *, bucket: str) -> List[str]:
        order.append("audit")
        batches.append(records)
        return ["gs://ws-bucket/x.json"]

    gw = _OrderedGateway()
    res = asyncio.run(
        _apply(
            tmp_path,
            gw,
            _writer,
            viewer_email="Viewer@X.com",
            iap_mode=True,
            drive_checker=_checker({"SheetAAAAAAAAAAAAA"}),
        )
    )
    assert order == ["audit", "sheet"]
    assert res["ok"] is True and res["updated_count"] == 1
    assert res["audit_log_objects"] == ["gs://ws-bucket/x.json"]
    rec = batches[0][0]
    assert rec["phase"] == PHASE_REQUESTED
    assert rec["reviewer_email"] == "viewer@x.com" and rec["reviewer_source"] == "iap"
    assert (rec["old_status"], rec["new_status"]) == ("PENDING", "✅ CONFIRMED")
    assert (rec["old_note"], rec["new_note"]) == ("", "checked with manager")
    assert rec["composite_key"] == _incident_id()
    assert rec["rule_id"] == "C1" and rec["video_filename"] == "cam01.mp4"
    assert rec["timestamp_in_clip"] == "03:12 - 03:27"
    assert rec["job_id"] == "jobA" and rec["batch_id"] == "" and rec["store_job_id"] == ""
    assert rec["client_ip"] == "203.0.113.7" and rec["user_agent"] == "pytest-UA"
    assert rec["report_sheet_url"].endswith("/SheetAAAAAAAAAAAAA/edit")
    assert rec["schema_version"] == 2 and rec["ts_utc"].endswith("Z")
    assert gw.rows[0][9] == "✅ CONFIRMED"


def test_review_hmac_mode_has_empty_reviewer(tmp_path: Path) -> None:
    batches: List[List[Dict[str, Any]]] = []
    asyncio.run(_apply(tmp_path, _FakeGateway(), lambda records, bucket: batches.append(records) or []))
    assert batches[0][0]["reviewer_email"] == ""
    assert batches[0][0]["reviewer_source"] == "hmac_link"


def test_review_fails_closed_when_audit_write_fails(tmp_path: Path) -> None:
    def _broken(records: List[Dict[str, Any]], *, bucket: str) -> List[str]:
        raise ReviewAuditLogError("GCS down")

    gw = _FakeGateway()
    with pytest.raises(ReviewAuditLogError):
        asyncio.run(_apply(tmp_path, gw, _broken))
    assert gw.update_calls == []  # no audit log, no change
    assert gw.rows[0][9] == "PENDING"


def test_review_iap_mode_requires_audit_bucket(tmp_path: Path) -> None:
    gw = _FakeGateway()
    with pytest.raises(ReviewAuditLogError):
        asyncio.run(
            _apply(
                tmp_path,
                gw,
                lambda records, bucket: [],
                audit_bucket="",
                viewer_email="v@x.com",
                iap_mode=True,
                drive_checker=_checker({"SheetAAAAAAAAAAAAA"}),
            )
        )
    assert gw.update_calls == []


def test_review_without_bucket_in_hmac_mode_warns_and_proceeds(tmp_path: Path, caplog) -> None:
    from cctv_audit import portal

    monkey_flag = portal._audit_bucket_warned
    portal._audit_bucket_warned = False
    caplog.set_level(logging.WARNING, logger="cctv_audit.portal")
    try:
        gw = _FakeGateway()
        res = asyncio.run(_apply(tmp_path, gw, lambda records, bucket: pytest.fail("no bucket"), audit_bucket=""))
        assert res["updated_count"] == 1
        assert any("AUDIT_LOG_BUCKET is not set" in r.getMessage() for r in caplog.records)
    finally:
        portal._audit_bucket_warned = monkey_flag


def test_review_sheet_failure_writes_follow_up_record(tmp_path: Path) -> None:
    batches: List[List[Dict[str, Any]]] = []
    gw = _FakeGateway(fail_update=True)
    with pytest.raises(RuntimeError, match="Sheets API 500"):
        asyncio.run(_apply(tmp_path, gw, lambda records, bucket: batches.append(records) or []))
    assert [b[0]["phase"] for b in batches] == [PHASE_REQUESTED, PHASE_SHEET_WRITE_FAILED]


def test_review_noop_is_skipped(tmp_path: Path) -> None:
    row = list(TAB1_ROW)
    row[9], row[10] = "✅ CONFIRMED", "checked with manager"
    gw = _FakeGateway([row])
    res = asyncio.run(_apply(tmp_path, gw, lambda records, bucket: pytest.fail("no-op must not be logged")))
    assert res["ok"] is True and res["updated_count"] == 0
    assert gw.update_calls == []


def test_default_audit_client_does_not_need_google_cloud_storage(monkeypatch):
    """Regression (Round 86 live deploy): the worker image has no google-cloud-storage; the default
    audit-log client must use the JSON API via google-auth + urllib with ifGenerationMatch=0."""
    import builtins
    import urllib.request

    from cctv_audit import review_audit_log as ral

    real_import = builtins.__import__

    def _no_storage(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "google.cloud" and fromlist and "storage" in fromlist:
            raise ImportError("cannot import name 'storage' from 'google.cloud'")
        if name == "google.cloud.storage":
            raise ImportError("no google-cloud-storage")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", _no_storage)

    class _Creds:
        token = "tok"

        def refresh(self, _req):
            return None

    import google.auth
    monkeypatch.setattr(google.auth, "default", lambda scopes=None, **kw: (_Creds(), "p"))
    sent = []

    class _Resp:
        def read(self):
            return b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _urlopen(req, timeout=0):
        sent.append(req)
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)
    rec = {"ts_utc": "2026-10-23T12:00:00.000001Z", "job_id": "j1", "batch_id": "", "phase": "requested"}
    out = ral.write_review_audit_records([rec], bucket="bkt")
    assert out and out[0].startswith("gs://bkt/audit_logs/reviews/2026/10/23/j1/")
    assert len(sent) == 1
    url = sent[0].full_url
    assert "ifGenerationMatch=0" in url and "uploadType=media" in url and "/b/bkt/o" in url
    assert sent[0].get_header("Authorization") == "Bearer tok"


def test_audit_record_captures_the_evidence_clip_and_row_snapshot():
    """The decision is about one evidence clip, not the whole source video: record its link/file id
    and the full Tab-1 row (real Sheet layout: link is not at a fixed header index)."""
    from cctv_audit.review_audit_log import AUDIT_LOG_SCHEMA_VERSION, build_review_audit_record

    row = ["8d62e1", "cam.mp4", "00:15 (Slice#0 @00:15)", "A2", "CONFIRMED", "RED_LINE", 0.98,
           "Staff_A did not wash hands", "https://drive.google.com/file/d/1AbCdEfGhIjKlMn_op-Q/view", "PENDING"]
    rec = build_review_audit_record(
        reviewer_email="a@x.com", reviewer_source="iap", job_id="8d62e1", batch_id="b1", store_job_id="8d62e1",
        composite_key="k", row=row, old_status="PENDING", new_status="✅ CONFIRMED", old_note="", new_note="",
        report_sheet_url="", client_ip="", user_agent="")
    assert AUDIT_LOG_SCHEMA_VERSION == 2 and rec["schema_version"] == 2
    assert rec["evidence_clip_url"] == "https://drive.google.com/file/d/1AbCdEfGhIjKlMn_op-Q/view"
    assert rec["evidence_clip_file_id"] == "1AbCdEfGhIjKlMn_op-Q"
    assert rec["sheet_row_snapshot"][7] == "Staff_A did not wash hands"
    assert rec["sheet_row_snapshot"][6] == "0.98"
    empty = build_review_audit_record(
        reviewer_email="", reviewer_source="hmac_link", job_id="j", batch_id="", store_job_id="",
        composite_key="k", row=["j", "v.mp4"], old_status="", new_status="", old_note="", new_note="",
        report_sheet_url="", client_ip="", user_agent="")
    assert empty["evidence_clip_url"] == "" and empty["evidence_clip_file_id"] == ""


def test_reset_reviews_button_removed():
    """Round 86: the bulk "Reset Local Reviews" button wrote PENDING + empty note to EVERY finding in the
    shared Sheet (wiping other supervisors' work) and was removed."""
    from cctv_audit import portal

    html = portal._SUPERVISOR_PORTAL_HTML_TEMPLATE
    assert "btn-reset-review" not in html
    assert "resetReview" not in html
    assert "Reset Local Reviews" not in html and "清空本地复核记录" not in html
    assert "batchConfirm()" in html  # bulk-confirm stays as-is


def test_tab1_headers_match_written_data_columns():
    """Tab-1 header labels must describe the columns build_tab1_values actually writes (A..K)."""
    from types import SimpleNamespace

    from cctv_audit import gcp, gcs_gateway

    assert gcp.TAB1_HEADERS == gcs_gateway.TAB1_HEADERS
    row = SimpleNamespace(audit_id="j1", video_filename="v.mp4", timestamp_in_clip="00:15", rule_id="A2",
                          disposition="CONFIRMED", severity="RED_LINE", confidence=0.9,
                          evidence_description="desc", evidence_drive_url="https://drive.google.com/file/d/X/view",
                          human_review_status="PENDING", human_review_note="n")
    header, data = gcs_gateway.build_tab1_values([row])
    labelled = dict(zip(header, data))
    assert labelled["Audit ID"] == "j1"
    assert labelled["AI Disposition"] == "CONFIRMED"
    assert labelled["Severity"] == "RED_LINE"
    assert labelled["AI Confidence"] == 0.9
    assert labelled["Violation Description & Timestamped Trajectory"] == "desc"
    assert labelled["Evidence Clip Link (Click to Play)"].startswith("https://drive.google.com/file/d/")
    assert header[9] == "Supervisor Review Status (Dropdown)" and data[9] == "PENDING"
    assert header[10] == "Supervisor Review Note" and data[10] == "n"


# ---- Unified portal homepage ------------------------------------------------------------------


def test_portal_home_payload_lists_tasks_newest_first_with_review_status(tmp_path: Path) -> None:
    from cctv_audit.portal import (
        REVIEW_PROGRESS_COMPLETED,
        REVIEW_PROGRESS_IN_PROGRESS,
        REVIEW_PROGRESS_NA,
        REVIEW_PROGRESS_UNREVIEWED,
        load_portal_home_payload,
    )
    from cctv_audit.scheduled_auditor import ScheduledEvalTaskRecord

    store = UserScopedJobStore(state_dir=tmp_path / "jobs")
    now = time.time()
    jobs = [
        AuditJob(job_id="ge0001", user_id="a@x.com", folder_id="1FolderAAAAAAAAAA", state=JobState.DONE,
                 report_sheet_url="https://docs.google.com/spreadsheets/d/1SheetAAAAAAAAAAAA/edit",
                 violations_found=2, created_at=now - 300),
        AuditJob(job_id="sc0002", user_id="b@x.com", folder_id="1FolderBBBBBBBBBB", state=JobState.DONE,
                 report_sheet_url="https://docs.google.com/spreadsheets/d/1SheetBBBBBBBBBBBB/edit",
                 violations_found=2, batch_id="batch-1", created_at=now - 200),
        AuditJob(job_id="ru0003", user_id="a@x.com", folder_id="1FolderCCCCCCCCCC", state=JobState.RUNNING,
                 created_at=now - 100),
        AuditJob(job_id="cl0004", user_id="a@x.com", folder_id="1FolderDDDDDDDDDD", state=JobState.DONE,
                 violations_found=0, created_at=now - 400),
    ]
    for j in jobs:
        asyncio.run(store.save(j))

    class _Gw:
        async def read_report_sheet_data(self, folder_id: str, sheet: str):
            if "AAAA" in sheet:
                return [["ge0001", "v.mp4", "00:01", "A1", "", "", "", "", "", "PENDING", ""],
                        ["ge0001", "v.mp4", "00:02", "A2", "", "", "", "", "", "PENDING", ""]], []
            return [["sc0002", "v.mp4", "00:01", "A1", "", "", "", "", "", "✅ CONFIRMED", ""],
                    ["sc0002", "v.mp4", "00:02", "A2", "", "", "", "", "", "PENDING", ""]], []

    state = ScheduledStateStore(memory_store={})
    rec = ScheduledEvalTaskRecord(
        eval_task_id="ev-1", eval_id="EVAL-1", eval_name="Golden", row_index=2, prompt_tab_name="Prompt_v1.0_Standard",
        model_version="gemini-x", golden_sheet_url="s", golden_source_id="s", supervisor_email="c@x.com",
        state="DONE", created_at=now - 50, summary_text="Recall 90%",
    )
    state._memory[state.eval_task_key("ev-1")] = rec.model_dump_json()  # type: ignore[attr-defined]

    from cctv_audit import portal as portal_mod
    from cctv_audit.portal import REVIEW_PROGRESS_UNKNOWN, load_portal_home_review_status

    portal_mod._JOB_REVIEW_PROGRESS_CACHE.clear()

    class _CountingGw(_Gw):
        calls = 0

        async def read_report_sheet_data(self, folder_id: str, sheet: str):
            _CountingGw.calls += 1
            return await super().read_report_sheet_data(folder_id, sheet)

    gw = _CountingGw()
    payload = asyncio.run(load_portal_home_payload(job_store=store, state_store=state, gateway=gw))
    assert _CountingGw.calls == 0  # the homepage itself never reads report Sheets
    ids = [t["task_id"] for t in payload["tasks"]]
    assert ids == ["ev-1", "ru0003", "sc0002", "ge0001", "cl0004"]
    by_id = {t["task_id"]: t for t in payload["tasks"]}
    assert by_id["ge0001"]["review_progress_status"] == REVIEW_PROGRESS_UNKNOWN
    assert by_id["cl0004"]["review_progress_status"] == REVIEW_PROGRESS_COMPLETED

    live = asyncio.run(
        load_portal_home_review_status(job_ids=["ge0001", "sc0002", "ru0003", "nope"], job_store=store, gateway=gw)
    )["statuses"]
    assert live["ge0001"]["review_progress_status"] == REVIEW_PROGRESS_UNREVIEWED
    assert live["sc0002"]["review_progress_status"] == REVIEW_PROGRESS_IN_PROGRESS
    assert live["sc0002"]["reviewed_count"] == 1 and live["sc0002"]["findings_total"] == 2
    assert live["ru0003"]["review_progress_status"] == REVIEW_PROGRESS_NA
    assert "nope" not in live
    assert _CountingGw.calls == 2
    # Second call is served from the 5-minute cache, and the homepage now shows it directly.
    asyncio.run(load_portal_home_review_status(job_ids=["ge0001", "sc0002"], job_store=store, gateway=gw))
    assert _CountingGw.calls == 2
    payload2 = asyncio.run(load_portal_home_payload(job_store=store, state_store=state, gateway=gw))
    assert {t["task_id"]: t["review_progress_status"] for t in payload2["tasks"]}["sc0002"] == REVIEW_PROGRESS_IN_PROGRESS
    assert by_id["ru0003"]["review_progress_status"] == REVIEW_PROGRESS_NA
    assert by_id["ev-1"]["task_type"] == "GOLDEN_EVAL" and by_id["ev-1"]["result_text"] == "Recall 90%"
    assert by_id["sc0002"]["task_type"] == "SCHEDULED" and "batch=batch-1" in by_id["sc0002"]["portal_url"]
    assert by_id["ge0001"]["task_type"] == "GE" and by_id["ge0001"]["portal_url"].startswith("/portal?job=ge0001")
    assert by_id["ge0001"]["created_at_sgt"].endswith("SGT")
    assert all(l["target_url"].startswith("https://") for l in payload["links"])


def test_portal_home_payload_filters_by_drive_access_in_iap_mode(tmp_path: Path) -> None:
    from cctv_audit.portal import load_portal_home_payload

    store = UserScopedJobStore(state_dir=tmp_path / "jobs")
    for jid, fid in (("ok0001", "1FolderOKOKOKOKOK"), ("no0002", "1FolderNONONONONO")):
        asyncio.run(store.save(AuditJob(job_id=jid, user_id="a@x.com", folder_id=fid, state=JobState.RUNNING)))

    async def _checker(viewer: str, file_id: str) -> Tuple[bool, str]:
        return ("OKOK" in file_id), "denied"

    payload = asyncio.run(
        load_portal_home_payload(
            job_store=store, state_store=ScheduledStateStore(memory_store={}),
            viewer_email="alice@customer.com", drive_checker=_checker,
        )
    )
    assert [t["task_id"] for t in payload["tasks"]] == ["ok0001"]


def test_portal_home_routes(monkeypatch: pytest.MonkeyPatch, client) -> None:
    # HMAC mode without opt-in: homepage is refused (it lists signed links for every task).
    assert client.get("/portal").status_code == 403
    assert client.get("/api/portal/home").status_code == 403
    monkeypatch.setenv("PORTAL_MODE", "iap")
    assert client.get("/portal").status_code == 401
    assert client.get("/api/portal/home").status_code == 401


def test_portal_quick_links_come_from_terraform_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from cctv_audit import portal as portal_mod

    monkeypatch.setattr(portal_mod.config, "master_prompt_sheet_id", "1ExampleMasterSheetId0000000000")
    monkeypatch.setenv(
        "PORTAL_QUICK_LINKS_JSON",
        json.dumps(
            [
                {"id": "ge", "title_zh": "GE", "title_en": "GE", "url": "https://example.com/ge", "description": "d"},
                {"id": "bad", "title_zh": "x", "title_en": "x", "url": "javascript:alert(1)"},
            ]
        ),
    )
    links = portal_mod.load_home_quick_links()
    assert [l["link_id"] for l in links] == ["ge", "sop_sheet"]
    assert links[1]["target_url"] == "https://docs.google.com/spreadsheets/d/1ExampleMasterSheetId0000000000/edit"
    monkeypatch.setenv("PORTAL_QUICK_LINKS_JSON", "{not json")
    assert [l["link_id"] for l in portal_mod.load_home_quick_links()] == ["sop_sheet"]
    monkeypatch.delenv("PORTAL_QUICK_LINKS_JSON")
    assert [l["link_id"] for l in portal_mod.load_home_quick_links()] == ["sop_sheet"]


def test_review_write_records_progress_on_job(tmp_path: Path) -> None:
    from cctv_audit import portal as portal_mod

    store = UserScopedJobStore(state_dir=tmp_path / "jobs")
    job = AuditJob(job_id="rv0001", user_id="a@x.com", folder_id="1FolderRVRVRVRVRV", state=JobState.DONE,
                   report_sheet_url="https://docs.google.com/spreadsheets/d/1SheetRVRVRVRVRVRV/edit", violations_found=2)
    asyncio.run(store.save(job))
    rows = [["rv0001", "v.mp4", "00:01", "A1", "", "", "", "", "", "PENDING", ""],
            ["rv0001", "v.mp4", "00:02", "A2", "", "", "", "", "", "PENDING", ""]]
    from cctv_audit.gcs_gateway import build_incident_composite_key

    key = build_incident_composite_key("rv0001", "v.mp4", "00:01", "A1", 0)
    asyncio.run(
        portal_mod._record_review_progress_after_write(
            job_store=store, job=job, tab1_rows=rows,
            applied_updates=[{"incident_id": key, "human_review_status": "✅ CONFIRMED"}],
        )
    )
    stored = asyncio.run(store.get("a@x.com", "rv0001"))
    assert (stored.review_reviewed_count, stored.review_total_count) == (1, 2)
    portal_mod._JOB_REVIEW_PROGRESS_CACHE.clear()
    payload = asyncio.run(
        portal_mod.load_portal_home_payload(job_store=store, state_store=ScheduledStateStore(memory_store={}))
    )
    row = payload["tasks"][0]
    assert row["review_source"] == "recorded" and row["review_progress_status"] == "IN_PROGRESS"
