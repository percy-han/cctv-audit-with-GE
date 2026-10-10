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

    def put(self, bucket: str, name: str, data: bytes = b"v", ctype: str = "video/mp4") -> None:
        self.objects[(bucket, name)] = {"data": data, "contentType": ctype, "metadata": {}}

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
        bucket = urllib.parse.unquote(re.search(r"/b/([^/]+)/o", parsed.path).group(1))
        if bucket in self.forbidden:
            return 403, b'{"error": "forbidden"}'
        if "/upload/" in parsed.path:
            data = body.read() if hasattr(body, "read") else (body or b"")
            name = qs["name"][0]
            prev = self.objects.get((bucket, name), {})
            self.objects[(bucket, name)] = {
                "data": data,
                "contentType": content_type,
                "metadata": prev.get("metadata", {}),
            }
            return 200, json.dumps({"name": name}).encode()
        m = re.search(r"/o/(.+)$", parsed.path)
        if m:
            name = urllib.parse.unquote(m.group(1))
            obj = self.objects.get((bucket, name))
            if obj is None:
                return 404, b"{}"
            if method == "DELETE":
                del self.objects[(bucket, name)]
                return 204, b""
            if method == "PATCH":
                obj["metadata"].update(json.loads(body)["metadata"])
                return 200, b"{}"
            return 200, json.dumps({"name": name}).encode()
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


def test_non_internal_prefixes_allowed():
    gw = FakeGcs()
    assert asyncio.run(gw.probe_write_access(f"gs://{STAGING}/stores/A")) == f"gs://{STAGING}/stores/A"
    assert asyncio.run(gw.probe_write_access(f"gs://{STAGING}/jobs_archive")) == f"gs://{STAGING}/jobs_archive"
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


def test_gcs_preflight_skips_sop_sheet_probe(tmp_path, monkeypatch):
    import cctv_audit.audit_service as audit_service_module

    monkeypatch.setattr(audit_service_module.config, "master_prompt_sheet_id", "SOP1234567890ABCDEF")
    fake = FakeGcs()
    fake.put("store-videos", "s/cam.mp4")

    class _Drive:
        async def check_sheet_readable(self, sheet_id):
            raise AssertionError("SOP Sheet must not be probed for gs:// targets")

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
