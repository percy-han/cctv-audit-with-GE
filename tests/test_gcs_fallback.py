"""Round 69: Zero-GWS GCS fallback mode (`gcs_gateway.py` + routing through the audit pipeline).

All GCS traffic goes to an in-memory fake bucket by overriding `GcsStorageGateway._request`,
`_download`, `_ffprobe_gcs_stream` and `_get_access_token`; nothing touches the network.
"""

from __future__ import annotations

import asyncio
import io
import json
import re
import urllib.parse
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

import cctv_audit.gcs_gateway as gg
from cctv_audit.agentic_auditor import Disposition, Finding, TokenLedgerRow
from cctv_audit.audit_service import AuditService
from cctv_audit.gcp import WorkspaceAccessError
from cctv_audit.jobs import LOCAL_PLACEHOLDER_BUCKET, JobState, UserScopedJobStore
from cctv_audit.prompt_manager import PromptManager
from cctv_audit.video_ingestor import VideoIngestor, extract_drive_id
from cctv_audit.workspace_reporter import (
    EVIDENCE_SUBFOLDER_NAME,
    REPORT_SHEET_TITLE,
    WorkspaceReporter,
)

STAGING = "proj-cctv-staging"


class FakeGcs(gg.GcsStorageGateway):
    """GcsStorageGateway whose transport is an in-memory bucket map."""

    def __init__(self, forbidden: Tuple[str, ...] = ()) -> None:
        super().__init__()
        self.objects: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self.forbidden = set(forbidden)
        self.ffprobe_calls: List[str] = []
        self.calls: List[Tuple[str, str]] = []
        self.history: Dict[Tuple[str, str, str], bytes] = {}  # (bucket, name, generation) -> data
        self.versioning: Dict[str, bool] = {}
        self.bucket_patch_status = 200
        self._next_gen = 1728547200000000

    def _new_gen(self, bucket: str, name: str, data: bytes) -> str:
        self._next_gen += 1
        gen = str(self._next_gen)
        self.history[(bucket, name, gen)] = data
        return gen

    def put(self, bucket: str, name: str, data: bytes = b"v", ctype: str = "video/mp4") -> str:
        gen = self._new_gen(bucket, name, data)
        self.objects[(bucket, name)] = {"data": data, "contentType": ctype, "metadata": {}, "generation": gen}
        return gen

    def _get_access_token(self) -> str:
        return "fake-token"

    def _ffprobe_gcs_stream(self, bucket: str, obj: str) -> Tuple[int, int, float]:
        self.ffprobe_calls.append(obj)
        return 1920, 1080, 300.0

    def _download(self, url: str, dest_path: Path) -> int:
        bucket, name = self._parse_object_url(url)
        obj = self.objects.get((bucket, name))
        if obj is None:
            return 404
        dest_path.write_bytes(obj["data"])
        return 200

    @staticmethod
    def _parse_object_url(url: str) -> Tuple[str, str]:
        m = re.search(r"/b/([^/]+)/o/([^?]+)", url)
        assert m, url
        return urllib.parse.unquote(m.group(1)), urllib.parse.unquote(m.group(2))

    def _request(self, method, url, *, body=None, content_type="", content_length=None, timeout=60.0):
        self.calls.append((method, url))
        parsed = urllib.parse.urlparse(url)
        qs = urllib.parse.parse_qs(parsed.query)
        bucket_only = re.fullmatch(r".*/storage/v1/b/([^/]+)", parsed.path)
        if bucket_only:  # bucket-level PATCH (Object Versioning)
            if self.bucket_patch_status >= 400:
                return self.bucket_patch_status, b'{"error": "storage.buckets.update denied"}'
            self.versioning[urllib.parse.unquote(bucket_only.group(1))] = json.loads(body)["versioning"]["enabled"]
            return 200, b'{"versioning": {"enabled": true}}'
        bucket = urllib.parse.unquote(re.search(r"/b/([^/]+)/o", parsed.path).group(1))
        if bucket in self.forbidden:
            return 403, b'{"error": "forbidden"}'
        if "/upload/" in parsed.path:
            data = body.read() if hasattr(body, "read") else (body or b"")
            name = qs["name"][0]
            prev = self.objects.get((bucket, name), {})
            gen = self._new_gen(bucket, name, data)
            self.objects[(bucket, name)] = {
                "data": data,
                "contentType": content_type,
                "metadata": prev.get("metadata", {}),
                "generation": gen,
            }
            return 200, json.dumps({"name": name, "generation": gen}).encode()
        m = re.search(r"/o/(.+)$", parsed.path)
        if m:
            name = urllib.parse.unquote(m.group(1))
            obj = self.objects.get((bucket, name))
            if "generation" in qs:  # pinned version read (Object Versioning)
                data = self.history.get((bucket, name, qs["generation"][0]))
                if data is None:
                    return 404, b"{}"
                if qs.get("alt") == ["media"]:
                    return 200, data
                return 200, json.dumps({"name": name, "size": str(len(data)), "generation": qs["generation"][0]}).encode()
            if obj is None:
                return 404, b"{}"
            if method == "DELETE":
                del self.objects[(bucket, name)]
                return 204, b""
            if method == "PATCH":
                obj["metadata"].update(json.loads(body)["metadata"])
                return 200, b"{}"
            if qs.get("alt") == ["media"]:
                return 200, obj["data"]
            return 200, json.dumps(
                {"name": name, "size": str(len(obj["data"])), "generation": obj.get("generation", "")}
            ).encode()
        # list
        prefix = qs.get("prefix", [""])[0]
        delim = qs.get("delimiter", [""])[0]
        items = []
        for (b, name), obj in sorted(self.objects.items()):
            if b != bucket or not name.startswith(prefix):
                continue
            if delim and delim in name[len(prefix):]:
                continue
            items.append(
                {
                    "name": name,
                    "size": str(len(obj["data"])),
                    "contentType": obj["contentType"],
                    "metadata": dict(obj["metadata"]),
                }
            )
        if "maxResults" in qs:
            items = items[: int(qs["maxResults"][0])]
        return 200, json.dumps({"items": items}).encode()


@pytest.fixture(autouse=True)
def _staging_bucket(monkeypatch):
    monkeypatch.setattr(gg.config, "staging_bucket", f"gs://{STAGING}")


# ---------------------------------------------------------------------------------------------
# Guardrail 5: input normalisation (Drive parsing unchanged)
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("gs://my-bkt/stores/A/", "gs://my-bkt/stores/A"),
        ("GS://My-Bkt/stores", "gs://my-bkt/stores"),
        ("gs://my-bkt", "gs://my-bkt"),
        (
            "https://console.cloud.google.com/storage/browser/my-bkt/stores/%E9%97%A8%E5%BA%97A?project=p1",
            "gs://my-bkt/stores/门店A",
        ),
        (
            "https://console.cloud.google.com/storage/browser/_details/my-bkt/stores/a.mp4;tab=live_object",
            "gs://my-bkt/stores/a.mp4",  # console `;tab=` params dropped
        ),
        ("https://storage.cloud.google.com/my-bkt/x/y", "gs://my-bkt/x/y"),
        ("https://storage.googleapis.com/my-bkt/x/", "gs://my-bkt/x"),
        ("https://drive.google.com/drive/folders/1AbCDefGhIjKlMnOpQrStUvWxYz?usp=sharing", "1AbCDefGhIjKlMnOpQrStUvWxYz"),
        ("https://drive.google.com/file/d/FILE123/view", "FILE123"),
        ("https://drive.google.com/open?id=OPEN42", "OPEN42"),
        ("1AbCDefGhIjKlMnOpQrStUvWxYz", "1AbCDefGhIjKlMnOpQrStUvWxYz"),
    ],
)
def test_extract_drive_id_normalises_gcs_and_keeps_drive(raw, expected):
    assert extract_drive_id(raw) == expected


def test_extract_drive_id_rejects_bad_gcs():
    with pytest.raises(ValueError):
        extract_drive_id("gs://")
    with pytest.raises(ValueError):
        extract_drive_id("gs://Bad_Bucket!/x")
    with pytest.raises(ValueError):
        extract_drive_id("https://console.cloud.google.com/home/dashboard")  # not storage -> Drive parser


# ---------------------------------------------------------------------------------------------
# Guardrails 2 & 3: links and slugs
# ---------------------------------------------------------------------------------------------


def test_build_source_video_url_and_slug():
    drive_id = "1AbCDefGhIjKlMnOpQrStUvWxYz"
    assert gg.build_source_video_url(drive_id) == f"https://drive.google.com/file/d/{drive_id}/view"
    assert gg.build_source_video_url("") == ""
    assert (
        gg.build_source_video_url("gs://my-bkt/stores/门店A/cam 1.mp4")
        == "https://console.cloud.google.com/storage/browser/_details/my-bkt/stores/%E9%97%A8%E5%BA%97A/cam%201.mp4"
    )
    assert gg.build_gcs_console_folder_url("gs://my-bkt/stores/A") == (
        "https://console.cloud.google.com/storage/browser/my-bkt/stores/A/"
    )

    assert gg.safe_storage_id_slug(drive_id) == drive_id
    uri = "gs://my-bkt/stores/门店A/" + "very-long camera name " * 4 + ".mp4"
    slug = gg.safe_storage_id_slug(uri)
    stem, digest = slug.rsplit("_", 1)
    assert re.fullmatch(r"[A-Za-z0-9_-]{1,40}", stem) and re.fullmatch(r"[0-9a-f]{10}", digest)
    assert "/" not in slug and slug == gg.safe_storage_id_slug(uri)
    assert gg.safe_storage_id_slug("gs://b1/a/x.mp4") != gg.safe_storage_id_slug("gs://b1/b/x.mp4")


def test_reporter_and_auditor_fallback_links_use_storage_aware_url():
    reporter = WorkspaceReporter()
    finding = Finding(
        rule_id="A1",
        disposition=Disposition.CONFIRMED,
        severity="红线",
        timestamp_in_clip="00:10",
        evidence="x",
    )
    rows = reporter.build_tab1_rows("J1", "v.mp4", [finding], source_video_file_id="gs://b-1/s/v.mp4")
    assert rows[0].evidence_drive_url == "https://console.cloud.google.com/storage/browser/_details/b-1/s/v.mp4"
    rows = reporter.build_tab1_rows("J1", "v.mp4", [finding], source_video_file_id="DRIVEID")
    assert rows[0].evidence_drive_url == "https://drive.google.com/file/d/DRIVEID/view"


# ---------------------------------------------------------------------------------------------
# Guardrail 1: internal staging prefixes are never accepted
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "target",
    [
        f"gs://{STAGING}",
        f"gs://{STAGING}/jobs",
        f"gs://{STAGING}/jobs/user_x",
        f"gs://{STAGING}/eval/run1",
        f"gs://{STAGING}/smoke",
        f"gs://{STAGING}/agent_platform_eval/a",
    ],
)
def test_internal_state_prefixes_blocked(target):
    gw = FakeGcs()
    with pytest.raises(WorkspaceAccessError, match="内部暂存"):
        asyncio.run(gw.probe_write_access(target))
    with pytest.raises(WorkspaceAccessError):
        asyncio.run(gw.list_folder_videos(target))
    with pytest.raises(WorkspaceAccessError):
        asyncio.run(gw.create_dual_tab_report_sheet(target, "r", [], []))
    assert gw.calls == []  # rejected before any GCS request


def test_whole_staging_bucket_blocked_customer_buckets_allowed():
    """The staging bucket has a 30-day auto-delete lifecycle rule: no customer data may live there."""
    gw = FakeGcs()
    for target in (f"gs://{STAGING}/stores/A", f"gs://{STAGING}/jobs_archive", f"gs://{STAGING}/sop"):
        with pytest.raises(WorkspaceAccessError, match="30 天后自动删除"):
            asyncio.run(gw.probe_write_access(target))
        with pytest.raises(WorkspaceAccessError, match="内部暂存"):
            asyncio.run(gw.list_folder_videos(target))
    assert gw.calls == []
    assert asyncio.run(gw.probe_write_access("gs://customer-cctv-bucket/stores/store_01")) == (
        "gs://customer-cctv-bucket/stores/store_01"
    )
    assert asyncio.run(gw.probe_write_access("gs://other-bkt")) == "gs://other-bkt"
    assert not gw.objects  # the write probe object was deleted again


def test_forbidden_bucket_maps_to_access_error_with_iam_hint():
    gw = FakeGcs(forbidden=("locked-bkt",))
    with pytest.raises(WorkspaceAccessError, match="roles/storage.objectAdmin"):
        asyncio.run(gw.probe_write_access("gs://locked-bkt/stores"))


# ---------------------------------------------------------------------------------------------
# Guardrail 6: styled .xlsx
# ---------------------------------------------------------------------------------------------


def test_xlsx_writer_structure_and_styles():
    class R:  # duck-typed ViolationSheetRow
        def __init__(self, disp: str, url: str) -> None:
            self.audit_id, self.video_filename, self.timestamp_in_clip = "J1", "v.mp4", "00:10"
            self.rule_id, self.disposition, self.severity = "A1", disp, "RED_LINE"
            self.confidence, self.evidence_description = 0.9, "<b>&\x01"
            self.evidence_drive_url, self.human_review_status = url, "⏳"

    url = "https://console.cloud.google.com/storage/browser/_details/b/e.mp4"
    data = gg.build_dual_tab_xlsx_bytes([R("CONFIRMED", url), R("SUSPECTED", ""), R("COMPLIANT", "")], [])
    sheets = gg.read_xlsx_sheets(data)
    assert list(sheets) == [gg.TAB1_TITLE, gg.TAB2_TITLE]
    assert sheets[gg.TAB1_TITLE][0] == gg.TAB1_HEADERS
    assert sheets[gg.TAB1_TITLE][1][7] == "<b>&"  # escaped, control char stripped
    assert sheets[gg.TAB2_TITLE] == [gg.TAB2_HEADERS]
    zf = zipfile.ZipFile(io.BytesIO(data))
    styles = zf.read("xl/styles.xml").decode()
    for color in ("FF1C2D42", "FFFCE8E6", "FFFEF7E0", "FFE6F4EA", "FFFFFFFF"):
        assert color in styles
    s1 = zf.read("xl/worksheets/sheet1.xml").decode()
    assert '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>' in s1
    assert '<col min="8" max="8" width="60" customWidth="1"/>' in s1
    assert '<c r="E2" t="inlineStr" s="3">' in s1 and '<c r="E3" t="inlineStr" s="4">' in s1
    assert '<c r="E4" t="inlineStr" s="5">' in s1 and '<c r="A1" t="inlineStr" s="1">' in s1
    assert '<hyperlink ref="I2" r:id="rId1"/>' in s1 and "I3" not in s1.split("<hyperlinks>")[1]
    rels = zf.read("xl/worksheets/_rels/sheet1.xml.rels").decode()
    assert url in rels and 'TargetMode="External"' in rels
    assert "xl/worksheets/_rels/sheet2.xml.rels" not in zf.namelist()


# ---------------------------------------------------------------------------------------------
# End-to-end routing: preflight -> download -> evidence + .xlsx report, Drive untouched
# ---------------------------------------------------------------------------------------------


class _ExplodingDrive:
    def __getattr__(self, name):
        raise AssertionError(f"Drive gateway must not be used for gs:// targets (called {name})")


def test_routing_end_to_end_gcs_flow(tmp_path):
    fake = FakeGcs()
    bucket, prefix = "store-videos", "stores/门店A"
    fake.put(bucket, f"{prefix}/cam10.mp4", b"ten")
    fake.put(bucket, f"{prefix}/cam2.mp4", b"two")
    fake.put(bucket, f"{prefix}/notes.txt", b"n", ctype="text/plain")
    fake.put(bucket, f"{prefix}/", b"", ctype="application/x-directory")
    fake.put(bucket, f"{prefix}/sub/deep.mp4", b"deep")
    fake.put(bucket, f"{prefix}/{EVIDENCE_SUBFOLDER_NAME}/old.mp4", b"old")
    router = gg.RoutingStorageGateway(drive_gateway=_ExplodingDrive(), gcs_gateway=fake)

    svc = AuditService(
        job_store=UserScopedJobStore(
            state_dir=tmp_path / "jobs", gcs_bucket=LOCAL_PLACEHOLDER_BUCKET, gcs_store={}
        ),
        ingestor=VideoIngestor(drive_reader=router),
        reporter=WorkspaceReporter(gateway=router, enable_notification=False),
    )
    console_url = f"https://console.cloud.google.com/storage/browser/{bucket}/stores/%E9%97%A8%E5%BA%97A?project=p"

    async def _run() -> None:
        job = await svc.preflight(user_id="sup@example.com", drive_url=console_url)
        assert job.folder_id == f"gs://{bucket}/{prefix}"
        assert job.state == JobState.READY, job.preflight_report.message_to_user
        videos = job.preflight_report.videos
        assert [v.filename for v in videos] == ["cam2.mp4", "cam10.mp4"]  # natural order, direct children only
        assert videos[0].file_id == f"gs://{bucket}/{prefix}/cam2.mp4"
        assert (videos[0].width, videos[0].height, videos[0].duration_sec) == (1920, 1080, 300.0)
        assert "Excel" in job.preflight_report.message_to_user
        # Dimensions were cached in object metadata: a second listing runs no ffprobe.
        n_probe = len(fake.ffprobe_calls)
        await router.list_folder_videos(job.folder_id)
        assert len(fake.ffprobe_calls) == n_probe == 2

        dest = tmp_path / "dl" / "cam2.mp4"
        dest.parent.mkdir()
        got = await svc.ingestor.materialise_source(videos[0], dest)
        assert got.read_bytes() == b"two" and not dest.with_name("cam2.mp4.tmp").exists()

        clip = tmp_path / "clip_A1.mp4"
        clip.write_bytes(b"clip")
        finding = Finding(
            rule_id="A1",
            disposition=Disposition.CONFIRMED,
            severity="红线",
            timestamp_in_clip="00:10",
            evidence="未洗手",
            evidence_clip_local_path=str(clip),
        )
        ledger = TokenLedgerRow(
            audit_id=job.job_id,
            auditor_folder_id=job.folder_id,
            video_filename="cam2.mp4",
            video_duration_sec=300.0,
            resolution="1920x1080",
            model_version_used="m",
            prompt_version_used="p",
            total_token_count=10,
        )
        art = await svc.reporter.publish_in_folder_report(
            audit_id=job.job_id,
            user_email="sup@example.com",
            parent_folder_id=job.folder_id,
            findings_by_video=[("cam2.mp4", [finding])],
            ledger_rows=[ledger],
        )
        ev_obj = f"{prefix}/{EVIDENCE_SUBFOLDER_NAME}/clip_A1.mp4"
        assert fake.objects[(bucket, ev_obj)]["data"] == b"clip"
        assert not clip.exists()
        report_obj = f"{prefix}/{REPORT_SHEET_TITLE}.xlsx"
        assert art.report_sheet_id == f"gs://{bucket}/{report_obj}"
        assert art.report_sheet_url == gg.build_gcs_console_object_url(art.report_sheet_id)
        assert art.evidence_subfolder_id == f"gs://{bucket}/{prefix}/{EVIDENCE_SUBFOLDER_NAME}"
        stored = fake.objects[(bucket, report_obj)]
        assert stored["contentType"] == gg.XLSX_CONTENT_TYPE
        sheets = gg.read_xlsx_sheets(stored["data"])
        row = sheets[gg.TAB1_TITLE][1]
        assert row[8] == gg.build_gcs_console_object_url(f"gs://{bucket}/{ev_obj}")
        assert sheets[gg.TAB2_TITLE][1][1] == job.folder_id

    asyncio.run(_run())


def test_routing_dispatches_drive_ids_to_workspace_gateway():
    seen: List[str] = []

    class _Drive:
        async def probe_write_access(self, folder_id):
            seen.append(f"probe:{folder_id}")
            return "Drive 文件夹"

        async def check_sheet_readable(self, sheet_id):
            seen.append(f"sheet:{sheet_id}")
            return "SOP"

        def extra(self):
            return "delegated"

    router = gg.RoutingStorageGateway(drive_gateway=_Drive(), gcs_gateway=_ExplodingDrive())
    assert asyncio.run(router.probe_write_access("1AbCDefGhIjKlMnOpQrStUvWxYz")) == "Drive 文件夹"
    assert asyncio.run(router.check_sheet_readable("SHEET1")) == "SOP"
    assert asyncio.run(router.check_sheet_readable("")) == ""
    assert router.extra() == "delegated"
    assert seen == ["probe:1AbCDefGhIjKlMnOpQrStUvWxYz", "sheet:SHEET1"]


def test_gcs_preflight_skips_sop_check_in_builtin_rules_mode(tmp_path, monkeypatch):
    import cctv_audit.audit_service as audit_service_module

    monkeypatch.setattr(audit_service_module.config, "master_prompt_sheet_id", "")
    fake = FakeGcs()
    fake.put("store-videos", "s/cam.mp4")

    class _Drive:
        async def check_sheet_readable(self, sheet_id):
            raise AssertionError("no SOP source configured: nothing must be probed")

    router = gg.RoutingStorageGateway(drive_gateway=_Drive(), gcs_gateway=fake)
    svc = AuditService(
        job_store=UserScopedJobStore(state_dir=tmp_path, gcs_bucket=LOCAL_PLACEHOLDER_BUCKET, gcs_store={}),
        ingestor=VideoIngestor(drive_reader=router),
        reporter=WorkspaceReporter(gateway=router, enable_notification=False),
    )
    job = asyncio.run(svc.preflight(user_id="u@example.com", drive_url="gs://store-videos/s"))
    assert job.state == JobState.READY


def test_gcs_empty_prefix_message_mentions_iam(tmp_path):
    router = gg.RoutingStorageGateway(drive_gateway=_ExplodingDrive(), gcs_gateway=FakeGcs())
    res = asyncio.run(VideoIngestor(drive_reader=router).inspect_drive_videos("gs://empty-bkt/s"))
    assert not res.passed
    assert "roles/storage.objectViewer" in res.message_to_user and "gs://empty-bkt/s/" in res.message_to_user


# ---------------------------------------------------------------------------------------------
# Guardrail 4: empty MASTER_PROMPT_SHEET_ID -> bundled V25 rules
# ---------------------------------------------------------------------------------------------


def test_empty_master_prompt_sheet_id_uses_bundled_rules(monkeypatch, caplog):
    import cctv_audit.prompt_manager as pm_module

    class _NoSheet:
        def __getattr__(self, name):
            raise AssertionError(f"Sheets client must not be called in Zero-GWS mode ({name})")

    monkeypatch.setattr(pm_module.config, "master_prompt_sheet_id", "")
    caplog.set_level("INFO", logger="cctv_audit.prompt_manager")
    cfg = asyncio.run(PromptManager(sheet_client=_NoSheet()).load_active_config())
    assert len(cfg.rules) == 24
    assert cfg.active_prompt_version == pm_module.config.default_prompt_version
    assert "Zero-GWS" in caplog.text


# ---------------------------------------------------------------------------------------------
# Server wording
# ---------------------------------------------------------------------------------------------


def test_historical_gs_urls_are_masked():
    import cctv_audit.server as srv

    inner = {
        "message": {"parts": [{"text": "确认开始"}]},
        "events": [{"author": "user", "content": {"parts": [{"text": "稽核 gs://store-videos/stores/A"}]}}],
    }
    text = srv._extract_adk_turn_text(inner)
    assert "gs://store-videos" not in text and "<历史链接>" in text


def test_server_done_and_confirm_messages_for_gcs_jobs(monkeypatch):
    import cctv_audit.server as srv
    from cctv_audit.jobs import AuditJob
    from cctv_audit.turn import TurnAction, TurnDecision

    assert isinstance(srv._gw, gg.RoutingStorageGateway)
    folder = "gs://store-videos/stores/A"
    done_job = AuditJob(
        user_id="u@example.com",
        folder_id=folder,
        drive_url=folder,
        state=JobState.DONE,
        report_sheet_url="https://console.cloud.google.com/storage/browser/_details/store-videos/stores/A/r.xlsx",
    )
    drive_job = done_job.model_copy(update={"folder_id": "DRIVEFOLDER", "report_sheet_url": "https://docs.google.com/x"})

    async def _decide(_text):
        return TurnDecision(action=TurnAction.STATUS)

    monkeypatch.setattr(srv, "classify_turn_with_llm", _decide)

    async def _status(_u, _j):
        return current[0]

    monkeypatch.setattr(srv.audit_service, "get_status", _status)
    current = [done_job]
    msg = asyncio.run(srv._handle_conversation_turn("u@example.com", "s", "好了么"))
    assert "📊 **稽核报告（Excel .xlsx）**：" in msg and done_job.report_sheet_url in msg
    assert "[点击打开 Google Cloud Storage 目录](https://console.cloud.google.com/storage/browser/store-videos/stores/A/)" in msg
    current = [drive_job]
    msg = asyncio.run(srv._handle_conversation_turn("u@example.com", "s", "好了么"))
    assert "• 专属报告 Sheet：https://docs.google.com/x" in msg and "Excel" not in msg

    async def _confirm(_text):
        return TurnDecision(action=TurnAction.CONFIRM)

    async def _start(**_kw):
        return done_job.model_copy(update={"state": JobState.RUNNING})

    monkeypatch.setattr(srv, "classify_turn_with_llm", _confirm)
    monkeypatch.setattr(srv.audit_service, "start_audit", _start)
    msg = asyncio.run(srv._handle_conversation_turn("u@example.com", "s", "确认开始"))
    assert "GCS 目录" in msg and ".xlsx" in msg and "Google Drive" not in msg


# ---------------------------------------------------------------------------------------------
# Round 70: GCS-hosted Master SOP workbook (gs://<bucket>/sop/master_sheet.xlsx | .json)
# ---------------------------------------------------------------------------------------------

import importlib.util  # noqa: E402

from cctv_audit.config import extract_spreadsheet_id  # noqa: E402
import cctv_audit.prompt_manager as pm_module  # noqa: E402
from cctv_audit.prompt_manager import GoogleSheetsConfigClient  # noqa: E402

_ROOT = Path(__file__).resolve().parent.parent
SOP_URI = "gs://store-videos/sop/master_sheet.xlsx"


def _load_init_script():
    spec = importlib.util.spec_from_file_location("init_sop_sheet_r70", _ROOT / "scripts" / "init_sop_sheet.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _clear_gcs_workbook_cache():
    pm_module._GCS_WORKBOOK_CACHE.clear()
    yield
    pm_module._GCS_WORKBOOK_CACHE.clear()


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("gs://store-videos/sop/master_sheet.xlsx", "gs://store-videos/sop/master_sheet.xlsx"),
        ("  gs://Store-Videos/sop/Master.XLSX ", "gs://store-videos/sop/Master.XLSX"),
        ("gs://store-videos/sop/master_sheet.json", "gs://store-videos/sop/master_sheet.json"),
        (
            "https://console.cloud.google.com/storage/browser/_details/store-videos/sop/master_sheet.xlsx;tab=live_object?project=p",
            "gs://store-videos/sop/master_sheet.xlsx",
        ),
        ("https://storage.cloud.google.com/store-videos/sop/master_sheet.json", "gs://store-videos/sop/master_sheet.json"),
        # Google Sheet handling unchanged
        ("https://docs.google.com/spreadsheets/d/1AbCdEfGhIjKlMnOpQrStUvWxYz/edit#gid=0", "1AbCdEfGhIjKlMnOpQrStUvWxYz"),
        ("1AbCdEfGhIjKlMnOpQrStUvWxYz", "1AbCdEfGhIjKlMnOpQrStUvWxYz"),
    ],
)
def test_extract_spreadsheet_id_accepts_gcs_sop_workbook(raw, expected):
    assert extract_spreadsheet_id(raw) == expected


@pytest.mark.parametrize(
    "bad",
    [
        "gs://store-videos",
        "gs://store-videos/",
        "gs://store-videos/sop/",
        "gs://store-videos/sop/master_sheet.csv",
        "https://console.cloud.google.com/storage/browser/store-videos/sop",
        "https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOpQrStUvWxYz",
    ],
)
def test_extract_spreadsheet_id_rejects_non_workbook_gcs(bad):
    with pytest.raises(ValueError):
        extract_spreadsheet_id(bad)


def _excel_style_xlsx() -> bytes:
    """Workbook shaped like Microsoft Excel / WPS output: sharedStrings, sparse cells/rows, abs Target."""
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    rns = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
    pkg = "http://schemas.openxmlformats.org/package/2006/relationships"
    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    sst = (
        f'<sst {ns} count="5" uniqueCount="5">'
        "<si><t>Config_Key</t></si><si><t>Active_Prompt_Version</t></si>"
        '<si><r><t>Prompt_</t></r><r><rPr><b/></rPr><t xml:space="preserve">v9 </t></r><rPh><t>ignored</t></rPh></si>'
        "<si><t>右侧说明</t></si><si><t>I列</t></si></sst>"
    )
    sheet = (
        f"<worksheet {ns}><sheetData>"
        '<row r="1"><c r="A1" t="s"><v>0</v></c></row>'
        '<row r="2"><c r="A2" t="s"><v>1</v></c><c r="C2" t="s"><v>2</v></c><c r="D2" t="s"><v>3</v></c></row>'
        '<row r="5"><c r="B5" t="b"><v>1</v></c><c r="C5"><v>42</v></c><c r="E5" t="inlineStr"><is><t>内联</t></is></c>'
        '<c r="I5" t="s"><v>4</v></c></row>'
        '<row r="6"><c r="AA6" t="b"><v>0</v></c></row>'
        "</sheetData></worksheet>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "xl/workbook.xml",
            f'<workbook {ns} {rns}><sheets><sheet name="Tab0_版本总控与回滚开关" sheetId="1" r:id="rId3"/></sheets></workbook>',
        )
        zf.writestr(
            "xl/_rels/workbook.xml.rels",
            f'<Relationships xmlns="{pkg}"><Relationship Id="rId3" Type="{rel}/worksheet" '
            'Target="/xl/worksheets/sheet7.xml"/></Relationships>',
        )
        zf.writestr("xl/sharedStrings.xml", sst)
        zf.writestr("xl/worksheets/sheet7.xml", sheet)
    return buf.getvalue()


def test_read_xlsx_sheets_excel_shared_strings_and_sparse_cells():
    rows = gg.read_xlsx_sheets(_excel_style_xlsx())["Tab0_版本总控与回滚开关"]
    assert rows[0] == ["Config_Key"]
    assert rows[1] == ["Active_Prompt_Version", "", "Prompt_v9 ", "右侧说明"]  # C2 lands in column C
    assert rows[2] == [] and rows[3] == []  # rows 3-4 omitted by Excel
    assert rows[4] == ["", "TRUE", "42", "", "内联", "", "", "", "I列"]  # B5 / E5 / I5 aligned
    assert len(rows[5]) == 27 and rows[5][26] == "FALSE"  # AA -> index 26
    # inlineStr workbooks written by this module still round-trip
    data = gg.build_xlsx_bytes([{"name": "T", "rows": [["a", "b"], ["", "c"]]}])
    assert gg.read_xlsx_sheets(data) == {"T": [["a", "b"], ["", "c"]]}


@pytest.mark.parametrize("target", ["worksheets/sheet1.xml", "xl/worksheets/sheet1.xml", "/xl/worksheets/sheet1.xml"])
def test_read_xlsx_sheets_accepts_all_target_forms(target):
    data = gg.build_xlsx_bytes([{"name": "T", "rows": [["x"]]}])
    src = zipfile.ZipFile(io.BytesIO(data))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for n in src.namelist():
            body = src.read(n)
            if n == "xl/_rels/workbook.xml.rels":
                body = body.replace(b'Target="worksheets/sheet1.xml"', f'Target="{target}"'.encode())
            zf.writestr(n, body)
    assert gg.read_xlsx_sheets(buf.getvalue()) == {"T": [["x"]]}


def _edit_workbook(raw: bytes, edit) -> bytes:
    """Simulates a customer editing the downloaded .xlsx in Excel and re-uploading it."""
    tabs = gg.read_xlsx_sheets(raw)
    edit(tabs)
    return gg.build_xlsx_bytes([{"name": k, "rows": v} for k, v in tabs.items()])


def test_init_script_upload_then_prompt_manager_reads_gcs_workbook(tmp_path, monkeypatch):
    mod = _load_init_script()
    xlsx_path = tmp_path / "master_sheet.xlsx"
    assert mod.main(["--export-xlsx", str(xlsx_path)]) == 0

    fake = FakeGcs()
    monkeypatch.setattr(mod, "make_gcs_gateway", lambda sa="": fake)
    console = "https://console.cloud.google.com/storage/browser/_details/store-videos/sop/master_sheet.xlsx"
    assert mod.main(["--gcs-uri", console]) == 0  # Console URL accepted, read-back verified
    stored = fake.objects[("store-videos", "sop/master_sheet.xlsx")]
    assert stored["contentType"] == gg.XLSX_CONTENT_TYPE
    assert gg.read_xlsx_sheets(stored["data"]) == gg.read_xlsx_sheets(xlsx_path.read_bytes())
    assert mod.main(["--sheet-id", SOP_URI, "--dry-run"]) == 0  # gs:// via --sheet-id is detected too
    with pytest.raises(ValueError):
        mod.main(["--gcs-uri", "gs://store-videos/sop/master_sheet.json"])

    client = GoogleSheetsConfigClient(gcs_gateway=fake)
    pm = PromptManager(sheet_client=client, known_valid_models={"gemini-3.8-flash", "gemini-2.5-flash"})

    async def _load():
        return await pm.load_active_config(SOP_URI)

    downloads_before = sum(1 for m, u in fake.calls if "alt=media" in u)
    cfg = asyncio.run(_load())
    assert cfg.active_prompt_version == "Prompt_v2.5_V10全量17条标准版"
    assert cfg.active_model_version == "gemini-3.8-flash"
    assert len(cfg.rules) == 24 and cfg.rules[0].rule_id == "A1"
    assert sum(1 for m, u in fake.calls if "alt=media" in u) == downloads_before + 1  # Tab0 + rules: one download
    original_a1 = cfg.rules[0].check_instruction

    # Customer edits in Excel: rewrite A1, drop (disable) the D2 row, switch the model pointer.
    def _edit(tabs):
        rules = tabs["Prompt_v2.5_V10全量17条标准版"]
        rules[1][6] = "【已修改】A1 新的检查要求"
        tabs["Prompt_v2.5_V10全量17条标准版"] = [r for r in rules if not r or r[0] != "D2"]
        for row in tabs["Tab0_版本总控与回滚开关"]:
            if row and row[0] == "Active_Model_Version":
                row[1] = "gemini-2.5-flash"

    stored["data"] = _edit_workbook(stored["data"], _edit)
    cached = asyncio.run(_load())  # within the 60s TTL: still the old workbook
    assert cached.rules[0].check_instruction == original_a1
    pm_module._GCS_WORKBOOK_CACHE.clear()  # TTL elapsed
    cfg2 = asyncio.run(_load())
    assert cfg2.rules[0].check_instruction == "【已修改】A1 新的检查要求"
    assert len(cfg2.rules) == 23 and "D2" not in {r.rule_id for r in cfg2.rules}
    assert cfg2.active_model_version == "gemini-2.5-flash"
    assert "【已修改】A1 新的检查要求" in cfg2.system_instruction

    # Runtime never writes to a customer-owned GCS workbook.
    n_calls = len(fake.calls)
    asyncio.run(client.append_available_models(SOP_URI, ["gemini-9-flash"]))
    assert len(fake.calls) == n_calls


def test_json_snapshot_workbook_and_default_config_path(monkeypatch):
    fake = FakeGcs()
    snapshot = (_ROOT / "sop" / "master_sheet.json").read_bytes()
    fake.put("store-videos", "sop/master_sheet.json", snapshot, ctype="application/json")
    monkeypatch.setattr(pm_module.config, "master_prompt_sheet_id", "gs://store-videos/sop/master_sheet.json")
    pm = PromptManager(sheet_client=GoogleSheetsConfigClient(gcs_gateway=fake), known_valid_models={"gemini-3.8-flash"})
    cfg = asyncio.run(pm.load_active_config())
    assert cfg.active_prompt_version == "Prompt_v2.5_V10全量17条标准版" and len(cfg.rules) == 24


def test_gcs_sop_object_guardrails():
    gw = FakeGcs()
    with pytest.raises(WorkspaceAccessError, match="具体的 .xlsx 或 .json"):
        gw.download_object_bytes("gs://store-videos")
    with pytest.raises(WorkspaceAccessError, match="内部暂存"):
        gw.download_object_bytes(f"gs://{STAGING}/jobs/master_sheet.xlsx")
    with pytest.raises(WorkspaceAccessError, match="内部暂存"):
        gw.upload_object_bytes(f"gs://{STAGING}/eval/x.xlsx", b"x", gg.XLSX_CONTENT_TYPE)
    # Even the staging bucket's sop/ prefix is refused (30-day auto-delete); customer buckets are fine.
    with pytest.raises(WorkspaceAccessError, match="30 天后自动删除"):
        gw.upload_object_bytes(f"gs://{STAGING}/sop/master_sheet.xlsx", b"x", gg.XLSX_CONTENT_TYPE)
    with pytest.raises(WorkspaceAccessError, match="30 天后自动删除"):
        gw.download_object_bytes(f"gs://{STAGING}/sop/master_sheet.xlsx")
    url = gw.upload_object_bytes("gs://customer-cctv-bucket/sop/master_sheet.xlsx", b"x", gg.XLSX_CONTENT_TYPE)
    assert url.endswith("/_details/customer-cctv-bucket/sop/master_sheet.xlsx")
    assert gw.download_object_bytes("gs://customer-cctv-bucket/sop/master_sheet.xlsx") == b"x"
    with pytest.raises(WorkspaceAccessError, match="init_sop_sheet.py --gcs-uri"):
        gw.download_object_bytes("gs://store-videos/sop/missing.xlsx")
    with pytest.raises(WorkspaceAccessError, match="roles/storage.objectViewer"):
        FakeGcs(forbidden=("locked",)).download_object_bytes("gs://locked/sop/master_sheet.xlsx")


def _preflight_service(tmp_path, router):
    return AuditService(
        job_store=UserScopedJobStore(state_dir=tmp_path, gcs_bucket=LOCAL_PLACEHOLDER_BUCKET, gcs_store={}),
        ingestor=VideoIngestor(drive_reader=router),
        reporter=WorkspaceReporter(gateway=router, enable_notification=False),
    )


def test_gcs_preflight_verifies_gcs_sop_workbook(tmp_path, monkeypatch):
    import cctv_audit.audit_service as audit_service_module

    mod = _load_init_script()
    fake = FakeGcs()
    fake.put("store-videos", "store1/cam.mp4")
    monkeypatch.setattr(audit_service_module.config, "master_prompt_sheet_id", SOP_URI)
    router = gg.RoutingStorageGateway(drive_gateway=_ExplodingDrive(), gcs_gateway=fake)
    svc = _preflight_service(tmp_path / "a", router)

    # 1. SOP workbook not uploaded yet -> preflight rejected with the fix-it hint, no listing done.
    job = asyncio.run(svc.preflight(user_id="u@example.com", drive_url="gs://store-videos/store1"))
    assert job.state == JobState.REJECTED
    assert "init_sop_sheet.py --gcs-uri" in job.preflight_report.message_to_user
    assert job.preflight_report.total_videos == 0

    # 2. Workbook without the Tab0 pointer tab -> rejected.
    fake.put("store-videos", "sop/master_sheet.xlsx", gg.build_xlsx_bytes([{"name": "Sheet1", "rows": [["x"]]}]))
    job = asyncio.run(svc.preflight(user_id="u@example.com", drive_url="gs://store-videos/store1"))
    assert job.state == JobState.REJECTED and "Tab0_版本总控与回滚开关" in job.preflight_report.message_to_user

    # 3. Not a workbook at all -> rejected.
    fake.put("store-videos", "sop/master_sheet.xlsx", b"not a zip")
    job = asyncio.run(svc.preflight(user_id="u@example.com", drive_url="gs://store-videos/store1"))
    assert job.state == JobState.REJECTED and "无法解析" in job.preflight_report.message_to_user

    # 4. Proper workbook -> READY.
    fake.put("store-videos", "sop/master_sheet.xlsx", mod.build_xlsx_bytes(mod.load_snapshot(mod.DEFAULT_SNAPSHOT)))
    job = asyncio.run(svc.preflight(user_id="u@example.com", drive_url="gs://store-videos/store1"))
    assert job.state == JobState.READY, job.preflight_report.message_to_user


def test_hybrid_gcs_videos_with_google_sheet_sop(tmp_path, monkeypatch):
    """Videos in gs://, SOP in a Google Sheet: write probe -> GCS, Sheet readability -> Drive gateway."""
    import cctv_audit.audit_service as audit_service_module
    from cctv_audit.gcp import WorkspaceAccessError as WAE

    fake = FakeGcs()
    fake.put("store-videos", "store1/cam.mp4")
    seen: List[str] = []

    class _Drive:
        def __init__(self, fail: bool) -> None:
            self.fail = fail

        async def check_sheet_readable(self, sheet_id):
            seen.append(sheet_id)
            if self.fail:
                raise WAE(f"读不到 SOP 总控表 `{sheet_id}`：请共享给机器人账号")
            return "SOP"

        async def probe_write_access(self, folder_id):
            raise AssertionError("gs:// write probe must go to GCS")

    monkeypatch.setattr(audit_service_module.config, "master_prompt_sheet_id", "SOP1234567890ABCDEF")
    ok = gg.RoutingStorageGateway(drive_gateway=_Drive(fail=False), gcs_gateway=fake)
    job = asyncio.run(
        _preflight_service(tmp_path / "b", ok).preflight(user_id="u@example.com", drive_url="gs://store-videos/store1")
    )
    assert job.state == JobState.READY and seen == ["SOP1234567890ABCDEF"]
    assert any(m == "POST" and "cctv_audit_write_probe" in u for m, u in fake.calls)  # GCS write probe ran

    bad = gg.RoutingStorageGateway(drive_gateway=_Drive(fail=True), gcs_gateway=fake)
    job = asyncio.run(
        _preflight_service(tmp_path / "c", bad).preflight(user_id="u@example.com", drive_url="gs://store-videos/store1")
    )
    assert job.state == JobState.REJECTED and "读不到 SOP 总控表" in job.preflight_report.message_to_user

    # The router sends a gs:// SOP id to GCS, never to Drive.
    router = gg.RoutingStorageGateway(drive_gateway=_ExplodingDrive(), gcs_gateway=fake)
    assert asyncio.run(router.check_sheet_readable("")) == ""
    with pytest.raises(WorkspaceAccessError):
        asyncio.run(router.check_sheet_readable(SOP_URI))  # missing object -> GCS gateway, not Drive


def _billion_laughs_xlsx() -> bytes:
    data = gg.build_xlsx_bytes([{"name": "Tab0_版本总控与回滚开关", "rows": [["k", "v"]]}])
    src = zipfile.ZipFile(io.BytesIO(data))
    bomb = (
        b'<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;&lol;">]>'
        b'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
        b'<row r="1"><c r="A1" t="inlineStr"><is><t>&lol2;</t></is></c></row></sheetData></worksheet>'
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for n in src.namelist():
            zf.writestr(n, bomb if n == "xl/worksheets/sheet1.xml" else src.read(n))
    return buf.getvalue()


def test_xml_entity_and_zip_bomb_guards():
    with pytest.raises(ValueError, match="DOCTYPE/ENTITY"):
        gg.read_xlsx_sheets(_billion_laughs_xlsx())
    with pytest.raises(ValueError, match="limit"):
        gg.read_xlsx_sheets(b"x" * (gg.SOP_MAX_BYTES + 1))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:  # tiny on disk, 21 MiB uncompressed
        zf.writestr("xl/workbook.xml", b"\0" * (21 * 1024 * 1024))
    assert len(buf.getvalue()) < 1024 * 1024
    with pytest.raises(ValueError, match="uncompressed"):
        gg.read_xlsx_sheets(buf.getvalue())
    with pytest.raises(ValueError, match="无法解析"):
        gg.load_gcs_sop_tabs(_billion_laughs_xlsx(), SOP_URI)


def test_hostile_or_oversized_sop_rejected_by_check_and_preflight(tmp_path, monkeypatch):
    import cctv_audit.audit_service as audit_service_module

    fake = FakeGcs()
    fake.put("store-videos", "store1/cam.mp4")
    fake.put("store-videos", "sop/master_sheet.xlsx", _billion_laughs_xlsx())
    with pytest.raises(WorkspaceAccessError, match="DOCTYPE/ENTITY"):
        asyncio.run(fake.check_sheet_readable(SOP_URI))

    fake.put("store-videos", "sop/master_sheet.xlsx", b"x" * (gg.SOP_MAX_BYTES + 1))
    media_before = sum(1 for _, u in fake.calls if "alt=media" in u)
    with pytest.raises(WorkspaceAccessError, match="20 MiB"):
        asyncio.run(fake.check_sheet_readable(SOP_URI))
    assert sum(1 for _, u in fake.calls if "alt=media" in u) == media_before  # refused from metadata size
    with pytest.raises(WorkspaceAccessError, match="1 MiB"):
        fake.put("store-videos", "sop/small.xlsx", b"y" * (2 * 1024 * 1024))
        fake.download_object_bytes("gs://store-videos/sop/small.xlsx", max_bytes=1024 * 1024)

    monkeypatch.setattr(audit_service_module.config, "master_prompt_sheet_id", SOP_URI)
    router = gg.RoutingStorageGateway(drive_gateway=_ExplodingDrive(), gcs_gateway=fake)
    job = asyncio.run(
        _preflight_service(tmp_path, router).preflight(user_id="u@example.com", drive_url="gs://store-videos/store1")
    )
    assert job.state == JobState.REJECTED and "20 MiB" in job.preflight_report.message_to_user


def test_download_size_cap_applies_when_metadata_size_missing():
    class _NoSizeMeta(FakeGcs):
        def _request(self, method, url, **kw):
            status, body = super()._request(method, url, **kw)
            if "fields=size" in url and status == 200:
                return 200, b"{}"
            return status, body

    gw = _NoSizeMeta()
    gw.put("store-videos", "sop/master_sheet.xlsx", b"z" * 2048)
    with pytest.raises(WorkspaceAccessError, match="上限"):
        gw.download_object_bytes(SOP_URI, max_bytes=1024)


# ---------------------------------------------------------------------------------------------
# Round 70c: GCS Object Versioning / generation pinning for the Master SOP workbook
# ---------------------------------------------------------------------------------------------

GEN = "1728547200123456"


@pytest.mark.parametrize(
    "raw, expected",
    [
        (f"gs://store-videos/sop/master_sheet.xlsx#{GEN}", f"gs://store-videos/sop/master_sheet.xlsx#{GEN}"),
        (f"gs://store-videos/sop/master_sheet.json#{GEN}", f"gs://store-videos/sop/master_sheet.json#{GEN}"),
        (
            "https://console.cloud.google.com/storage/browser/_details/store-videos/sop/master_sheet.xlsx"
            f";tab=live_object?project=p&generation={GEN}",
            f"gs://store-videos/sop/master_sheet.xlsx#{GEN}",
        ),
        (
            f"https://storage.googleapis.com/store-videos/sop/master_sheet.xlsx?generation={GEN}",
            f"gs://store-videos/sop/master_sheet.xlsx#{GEN}",
        ),
        # non-numeric fragments / generations are UI anchors: stripped as before
        ("gs://store-videos/sop/master_sheet.xlsx#tab=live_object", "gs://store-videos/sop/master_sheet.xlsx"),
        (
            "https://storage.cloud.google.com/store-videos/sop/master_sheet.xlsx?generation=abc",
            "gs://store-videos/sop/master_sheet.xlsx",
        ),
    ],
)
def test_generation_pinning_preserved_by_extract_spreadsheet_id(raw, expected):
    assert extract_spreadsheet_id(raw) == expected


def test_generation_suffix_requires_workbook_object_and_parse_helpers():
    with pytest.raises(ValueError):
        extract_spreadsheet_id(f"gs://store-videos/sop/master_sheet.csv#{GEN}")
    with pytest.raises(ValueError):
        extract_spreadsheet_id(f"gs://store-videos#{GEN}")
    assert gg.parse_gcs_uri(f"gs://b-1/sop/m.xlsx#{GEN}") == ("b-1", "sop/m.xlsx")
    assert gg.parse_gcs_uri_with_generation(f"gs://b-1/sop/m.xlsx#{GEN}") == ("b-1", "sop/m.xlsx", GEN)
    assert gg.parse_gcs_uri_with_generation("gs://b-1/sop/m.xlsx") == ("b-1", "sop/m.xlsx", None)


def test_pinned_generation_download_and_prompt_manager(tmp_path):
    mod = _load_init_script()
    fake = FakeGcs()
    v1 = mod.build_xlsx_bytes(mod.load_snapshot(mod.DEFAULT_SNAPSHOT))
    gen1 = fake.put("store-videos", "sop/master_sheet.xlsx", v1, ctype=gg.XLSX_CONTENT_TYPE)

    def _v2(tabs):
        tabs["Prompt_v2.5_V10全量17条标准版"][1][6] = "【v2】A1 新规则"

    gen2 = fake.put("store-videos", "sop/master_sheet.xlsx", _edit_workbook(v1, _v2), ctype=gg.XLSX_CONTENT_TYPE)
    assert gen1 != gen2

    fake.calls.clear()
    assert fake.download_object_bytes(f"{SOP_URI}#{gen1}") == v1
    assert [u for _, u in fake.calls] and all(f"generation={gen1}" in u for _, u in fake.calls)
    assert "fields=size,generation" in fake.calls[0][1] and "alt=media" in fake.calls[1][1]
    with pytest.raises(WorkspaceAccessError, match="历史版本"):
        fake.download_object_bytes(f"{SOP_URI}#999")

    pm = PromptManager(sheet_client=GoogleSheetsConfigClient(gcs_gateway=fake), known_valid_models={"gemini-3.8-flash"})
    live = asyncio.run(pm.load_active_config(SOP_URI))
    assert live.rules[0].check_instruction == "【v2】A1 新规则"
    console_pinned = (
        "https://console.cloud.google.com/storage/browser/_details/store-videos/sop/master_sheet.xlsx"
        f"?generation={gen1}"
    )
    fake.calls.clear()
    pinned = asyncio.run(pm.load_active_config(console_pinned))
    assert pinned.rules[0].check_instruction != "【v2】A1 新规则" and len(pinned.rules) == 24
    assert any("alt=media" in u and f"generation={gen1}" in u for _, u in fake.calls)
    # pinned SOP passes the preflight readability check too
    assert asyncio.run(fake.check_sheet_readable(f"{SOP_URI}#{gen1}")) == ""


def test_init_script_enables_versioning_and_prints_pinned_uri(monkeypatch, capsys):
    mod = _load_init_script()
    fake = FakeGcs()
    monkeypatch.setattr(mod, "make_gcs_gateway", lambda sa="": fake)
    assert mod.main(["--gcs-uri", SOP_URI]) == 0
    out = capsys.readouterr().out
    gen = fake.objects[("store-videos", "sop/master_sheet.xlsx")]["generation"]
    assert fake.versioning == {"store-videos": True}
    assert f"live URI (tracks the latest version): {SOP_URI}" in out
    assert f"pinned URI (this exact version, immutable): {SOP_URI}#{gen}" in out
    assert "Version history" in out
    # read-back verified the exact uploaded generation
    assert any("alt=media" in u and f"generation={gen}" in u for _, u in fake.calls)


def test_init_script_continues_when_versioning_forbidden(monkeypatch, capsys):
    mod = _load_init_script()
    fake = FakeGcs()
    fake.bucket_patch_status = 403  # caller has objectAdmin but not storage.buckets.update
    monkeypatch.setattr(mod, "make_gcs_gateway", lambda sa="": fake)
    assert mod.main(["--gcs-uri", f"{SOP_URI}#{GEN}"]) == 0  # a pinned URI still uploads the live object
    captured = capsys.readouterr()
    assert "gcloud storage buckets update gs://store-videos --versioning" in captured.err
    assert ("store-videos", "sop/master_sheet.xlsx") in fake.objects
    assert fake.versioning == {}

    fake2 = FakeGcs()
    monkeypatch.setattr(mod, "make_gcs_gateway", lambda sa="": fake2)
    assert mod.main(["--gcs-uri", SOP_URI, "--no-enable-versioning"]) == 0
    assert not any(m == "PATCH" for m, _ in fake2.calls)
    with pytest.raises(WorkspaceAccessError, match="30 天后自动删除"):
        mod.main(["--gcs-uri", f"gs://{STAGING}/sop/master_sheet.xlsx"])


# ---------------------------------------------------------------------------------------------
# Round 71: Terraform-provisioned workspace bucket -> GCS_SOP_URI -> effective_sop_source(folder_id)
# ---------------------------------------------------------------------------------------------

from cctv_audit.config import AuditConfig  # noqa: E402

WS_SOP = "gs://proj-cctv-workspace/sop/master_sheet.xlsx"
SHEET_ID = "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789_-abcd"


def test_gcs_sop_uri_config_normalisation():
    assert AuditConfig(gcs_sop_uri="").gcs_sop_uri == ""
    assert AuditConfig(gcs_sop_uri=f" {WS_SOP} ").gcs_sop_uri == WS_SOP
    assert AuditConfig(gcs_sop_uri=f"{WS_SOP}#{GEN}").gcs_sop_uri == f"{WS_SOP}#{GEN}"
    console = "https://console.cloud.google.com/storage/browser/_details/proj-cctv-workspace/sop/master_sheet.xlsx"
    assert AuditConfig(gcs_sop_uri=console).gcs_sop_uri == WS_SOP
    for bad in (SHEET_ID, "gs://proj-cctv-workspace", "gs://proj-cctv-workspace/sop/x.csv"):
        with pytest.raises(ValueError):
            AuditConfig(gcs_sop_uri=bad)


@pytest.mark.parametrize(
    "primary, fallback, folder, expected",
    [
        # explicit gs:// master_prompt_sheet_id always wins (incl. a generation pin)
        (f"gs://cust/sop/m.xlsx#{GEN}", WS_SOP, "gs://cust/stores/a", f"gs://cust/sop/m.xlsx#{GEN}"),
        (f"gs://cust/sop/m.xlsx#{GEN}", WS_SOP, "DRIVEFOLDER", f"gs://cust/sop/m.xlsx#{GEN}"),
        # dual-mode: gs:// folder uses the workspace SOP, Drive folder uses the Google Sheet
        (SHEET_ID, WS_SOP, "gs://proj-cctv-workspace/stores/a", WS_SOP),
        (SHEET_ID, WS_SOP, "DRIVEFOLDER", SHEET_ID),
        (SHEET_ID, WS_SOP, "", SHEET_ID),
        # no Google Sheet: the workspace SOP serves every folder
        ("", WS_SOP, "DRIVEFOLDER", WS_SOP),
        ("", WS_SOP, "gs://x/stores/a", WS_SOP),
        # no workspace SOP: unchanged behaviour
        (SHEET_ID, "", "gs://x/stores/a", SHEET_ID),
        ("", "", "gs://x/stores/a", ""),
    ],
)
def test_effective_sop_source(primary, fallback, folder, expected):
    cfg = AuditConfig(master_prompt_sheet_id=primary, gcs_sop_uri=fallback)
    assert cfg.effective_sop_source(folder) == expected


def test_dual_mode_gcs_preflight_and_start_use_workspace_sop(tmp_path, monkeypatch):
    """Sheet-ID deployment + Terraform workspace bucket: gs:// audits read the GCS SOP, never Sheets."""
    import cctv_audit.audit_service as audit_service_module

    mod = _load_init_script()
    fake = FakeGcs()
    fake.put("proj-cctv-workspace", "stores/a/cam.mp4")
    monkeypatch.setattr(audit_service_module.config, "master_prompt_sheet_id", SHEET_ID)
    monkeypatch.setattr(audit_service_module.config, "gcs_sop_uri", WS_SOP)

    class _NoSheets:
        async def check_sheet_readable(self, sheet_id):
            raise AssertionError("gs:// audit must not touch Google Sheets")

    router = gg.RoutingStorageGateway(drive_gateway=_NoSheets(), gcs_gateway=fake)

    seen_sheet_ids: List[Optional[str]] = []

    class _RecordingPM:
        async def load_active_config(self, sheet_id=None):
            seen_sheet_ids.append(sheet_id)
            from cctv_audit.prompt_manager import PromptModelConfig

            return PromptModelConfig(
                active_model_version="m", active_prompt_version="p", system_instruction="s"
            )

    svc = AuditService(
        job_store=UserScopedJobStore(state_dir=tmp_path, gcs_bucket=LOCAL_PLACEHOLDER_BUCKET, gcs_store={}),
        ingestor=VideoIngestor(drive_reader=router),
        prompt_manager=_RecordingPM(),  # type: ignore[arg-type]
        reporter=WorkspaceReporter(gateway=router, enable_notification=False),
    )
    # Workspace SOP not seeded yet -> preflight rejected with the GCS hint (Sheets never asked).
    job = asyncio.run(svc.preflight(user_id="u@example.com", drive_url="gs://proj-cctv-workspace/stores/a"))
    assert job.state == JobState.REJECTED and "init_sop_sheet.py --gcs-uri" in job.preflight_report.message_to_user

    fake.put(
        "proj-cctv-workspace",
        "sop/master_sheet.xlsx",
        mod.build_xlsx_bytes(mod.load_snapshot(mod.DEFAULT_SNAPSHOT)),
        ctype=gg.XLSX_CONTENT_TYPE,
    )

    async def _flow():
        j = await svc.preflight(user_id="u@example.com", drive_url="gs://proj-cctv-workspace/stores/a")
        assert j.state == JobState.READY, j.preflight_report.message_to_user
        await asyncio.gather(*list(svc._warmup_tasks))  # background warm-up uses the GCS SOP
        return j

    asyncio.run(_flow())
    assert seen_sheet_ids == [WS_SOP]
