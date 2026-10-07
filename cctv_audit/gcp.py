"""Vertex AI & Google Workspace client plumbing (adapted from percy-han/cctv-audit).

Preserves the hard-won production fixes from `percy-han/cctv-audit/cctv_audit/gcp.py`:
1. Uses standard ADC (`google.auth.default`) so access tokens auto-refresh past 1 hour.
2. Strips `quota_project` (`credentials_without_quota_project()`) to prevent false
   `403 serviceusage.services.use` errors when accessing same-project GCS/Sheets.
3. Uses the deployment's own project and model location (`GCP_PROJECT`, `VERTEX_MODEL_LOCATION`;
   no code defaults) and an explicit HTTPX timeout in milliseconds (`config.gemini_timeout_ms`, default 750000 = 12.5 min, Axiom 3).
4. Wraps Gemini API calls in exponential backoff using tenacity.
"""

from __future__ import annotations

import asyncio
import math
import logging
import os
import threading
from typing import Any, NoReturn, Optional, List

from pathlib import Path


import google.auth
import google.auth.exceptions
import tenacity
from google import genai
from google.genai import types

from .config import config

logger = logging.getLogger("cctv_audit.gcp")

_SCOPES = (
    "https://www.googleapis.com/auth/cloud-platform",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
)

_client_lock = asyncio.Lock()
_client: Optional[genai.Client] = None


def credentials_without_quota_project():
    """Returns ADC with quota_project stripped off to avoid false 403s on GCS/Drive."""
    try:
        creds, _ = google.auth.default(scopes=list(_SCOPES))
    except Exception as exc:
        logger.warning("ADC resolution failed (%s); falling back to client default.", exc)
        return None
    strip = getattr(creds, "with_quota_project", None)
    return strip(None) if callable(strip) else creds


def _create_client_sync() -> genai.Client:
    if not config.gcp_project:
        raise RuntimeError("GCP_PROJECT is not set: the Gemini client needs the stack's project ID.")
    creds = credentials_without_quota_project()
    return genai.Client(
        vertexai=True,
        project=config.gcp_project,
        location=config.vertex_model_location,
        credentials=creds,
        http_options=types.HttpOptions(
            api_version="v1beta1",
            timeout=config.gemini_timeout_ms,
        ),
    )


async def get_genai_client() -> genai.Client:
    """Singleton Vertex AI client (project GCP_PROJECT, location VERTEX_MODEL_LOCATION)."""
    global _client
    if _client is None:
        async with _client_lock:
            if _client is None:
                _client = await asyncio.to_thread(_create_client_sync)
    return _client


def _is_transient_error(exc: BaseException) -> bool:
    msg = str(exc)
    return any(
        code in msg
        for code in ("429", "500", "502", "503", "504", "RESOURCE_EXHAUSTED", "UNAVAILABLE", "DeadlineExceeded")
    )


# Per-call Gemini timeout scales with the slice length: 2.5 s per second of video (the user's rule:
# 5-min clip -> 12.5 min), never below `config.gemini_timeout_ms`. A 10-min clip therefore gets
# 25 min instead of a fixed 12.5 min it might never finish in (code_review_log.md Round 60).
GEMINI_TIMEOUT_SEC_PER_VIDEO_SEC: float = 2.5


def gemini_timeout_ms_for_slice(slice_duration_sec: float) -> int:
    """Timeout (ms) for one Gemini call on a slice of `slice_duration_sec` seconds."""
    scaled_ms = int(math.ceil(max(0.0, float(slice_duration_sec)) * GEMINI_TIMEOUT_SEC_PER_VIDEO_SEC * 1000.0))
    return max(int(config.gemini_timeout_ms), scaled_ms)


async def generate_content_with_retry(
    *,
    model: str,
    contents: list[Any],
    gen_config: types.GenerateContentConfig,
    max_attempts: int = 4,
    base_delay_sec: float = 2.0,
    max_delay_sec: float = 30.0,
    client: Optional[genai.Client] = None,
    timeout_ms: Optional[int] = None,
) -> Any:
    """Calls `client.aio.models.generate_content` with tenacity exponential backoff.

    `timeout_ms` (default `config.gemini_timeout_ms`, the client-level timeout) bounds each attempt:
    a different value is sent as a per-request `http_options.timeout`, which the SDK patches over the
    client's options, and the `asyncio.wait_for` guard follows it.
    """
    active_client = client or await get_genai_client()
    effective_timeout_ms = int(timeout_ms) if timeout_ms else int(config.gemini_timeout_ms)
    if effective_timeout_ms != int(config.gemini_timeout_ms):
        base_opts = gen_config.http_options or types.HttpOptions()
        if isinstance(base_opts, dict):
            base_opts = types.HttpOptions.model_validate(base_opts)
        gen_config = gen_config.model_copy(
            update={"http_options": base_opts.model_copy(update={"timeout": effective_timeout_ms})}
        )

    @tenacity.retry(
        stop=tenacity.stop_after_attempt(max_attempts),
        wait=tenacity.wait_random_exponential(multiplier=base_delay_sec, max=max_delay_sec),
        retry=tenacity.retry_if_exception(_is_transient_error),
        reraise=True,
        before_sleep=tenacity.before_sleep_log(logger, logging.WARNING),
    )
    async def _do_call():
        return await asyncio.wait_for(
            active_client.aio.models.generate_content(
                model=model,
                contents=contents,
                config=gen_config,
            ),
            timeout=(effective_timeout_ms / 1000.0) + 15.0,
        )

    return await _do_call()


# ---------------------------------------------------------------------------------------------
# Google Workspace (Drive / Sheets) identity -- token-free in every mode.
#
# A service account has zero Drive storage, so Drive refuses to let it *own* new files in anybody's
# My Drive (403 storageQuotaExceeded: "Service Accounts do not have storage quota. Leverage shared
# drives, or use OAuth delegation instead."). Empty folders are the only exception. The mode is
# selected purely by environment variables (set by Terraform):
#
#   1. WORKSPACE_IMPERSONATE_USER=<bot@customer-domain>  -> keyless domain-wide delegation (DWD).
#      The runtime SA asks the IAM Credentials API to sign a JWT with `sub=<bot>` on behalf of
#      WORKSPACE_DWD_SERVICE_ACCOUNT and trades it at oauth2.googleapis.com for a 1-hour access
#      token; google-auth re-signs automatically before expiry. No key file, no refresh token, no
#      human login, nothing to rotate. New files are owned by the bot and use the bot's storage.
#      One-time prerequisite: a Workspace super admin authorises the SA's OAuth client ID for
#      `_WORKSPACE_SCOPES` (admin.google.com -> Security -> API controls -> Domain-wide delegation).
#   2. unset -> the runtime SA acts as itself (reads shared folders, edits existing files, creates
#      folders; cannot create Sheets or upload clips inside a personal My Drive).
#
# A user refresh token (`gcloud auth application-default login`) is deliberately NOT supported:
# organisation re-authentication policies invalidate it within about a day, so it cannot run
# unattended. See code_review_log.md Round 38.
# ---------------------------------------------------------------------------------------------

_WORKSPACE_SCOPES = (
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
)
_IAM_SIGNER_SCOPES = ("https://www.googleapis.com/auth/cloud-platform",)
# Separate optional DWD scope used only by `resolve_chat_user_id` to map a supervisor's email
# to their 21-digit Google user `sub` ID for Google Chat `<users/{id}>` @-mentions. Kept out of
# `_WORKSPACE_SCOPES` so Drive/Sheets access still works even if a Workspace admin has not
# authorised `userinfo.profile` yet.
_CHAT_MENTION_SCOPES = ("https://www.googleapis.com/auth/userinfo.profile",)
_OIDC_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"


class WorkspaceConfigError(RuntimeError):
    """Drive/Sheets setup problem that only an operator can fix; retrying will not help.

    Raised before any Gemini spend (preflight and Tier-2 start) so a misconfiguration never burns
    tokens. The message is supervisor-facing Chinese and names exactly what to change.
    """


class WorkspaceAuthError(WorkspaceConfigError):
    """The Drive/Sheets identity cannot obtain a token; the message says exactly what to fix."""


class WorkspaceStorageQuotaError(WorkspaceConfigError):
    """The Drive writer identity has no storage for new files (e.g. a bare SA inside My Drive)."""


class WorkspaceAccessError(WorkspaceConfigError):
    """The identity cannot see / edit the target folder or read the SOP Sheet (sharing problem)."""


def workspace_identity() -> tuple[str, str]:
    """(impersonated_user, dwd_service_account) from env; ("", "") selects service-account mode."""
    return (
        os.environ.get("WORKSPACE_IMPERSONATE_USER", "").strip(),
        os.environ.get("WORKSPACE_DWD_SERVICE_ACCOUNT", "").strip(),
    )


def _build_workspace_credentials(subject: str, dwd_sa: str) -> Any:
    if not subject:
        creds = credentials_without_quota_project()
        if creds is None:
            raise WorkspaceAuthError("无法获取运行时服务账号凭证（ADC 不可用）")
        return creds
    if not dwd_sa:
        raise WorkspaceAuthError(
            "已设置 WORKSPACE_IMPERSONATE_USER，但缺少 WORKSPACE_DWD_SERVICE_ACCOUNT"
            "（被授权域级委派的服务账号邮箱）"
        )
    from google.auth import impersonated_credentials

    source, _ = google.auth.default(scopes=list(_IAM_SIGNER_SCOPES))
    strip = getattr(source, "with_quota_project", None)
    if callable(strip):
        source = strip(None)
    return impersonated_credentials.Credentials(
        source_credentials=source,
        target_principal=dwd_sa,
        target_scopes=list(_WORKSPACE_SCOPES),
        subject=subject,
    )


def _explain_refresh_error(exc: Exception, subject: str, dwd_sa: str) -> str:
    raw = str(exc)
    if not subject:
        return f"服务账号获取 Workspace 访问令牌失败：{raw}"
    if "unauthorized_client" in raw:
        return (
            "域级委派尚未生效：请 Workspace 超级管理员在 admin.google.com → 安全 → API 控制 → "
            f"全网域委派 中，为服务账号 {dwd_sa} 的客户端 ID 授权 scopes "
            f"{','.join(_WORKSPACE_SCOPES)}（原始错误：{raw}）"
        )
    if "invalid_grant" in raw:
        return (
            f"被代表账号 {subject} 无法使用：账号不存在、已停用，或不属于授权域级委派的 Workspace 域"
            f"（原始错误：{raw}）"
        )
    if "SERVICE_DISABLED" in raw or "has not been used in project" in raw:
        return (
            "项目未启用 IAM Service Account Credentials API（iamcredentials.googleapis.com），"
            f"无法无密钥签名：请启用后重试（原始错误：{raw}）"
        )
    if "signJwt" in raw or "PERMISSION_DENIED" in raw:
        return (
            f"运行时服务账号没有 {dwd_sa} 的 roles/iam.serviceAccountTokenCreator 权限，无法签名"
            f"（原始错误：{raw}）"
        )
    return f"以 {subject} 身份获取 Workspace 访问令牌失败：{raw}"


_workspace_creds_lock = threading.Lock()
_workspace_creds: Any = None
_workspace_creds_identity: Optional[tuple[str, str]] = None


def workspace_credentials() -> Any:
    """Returns the single process-wide credentials object used for every Drive / Sheets call.

    Built once per identity and refreshed in place under a lock, so DWD costs one signJwt + token
    exchange per hour instead of per API call. Raises `WorkspaceAuthError` with an operator-
    actionable message when a token cannot be obtained.
    """
    global _workspace_creds, _workspace_creds_identity
    identity = workspace_identity()
    with _workspace_creds_lock:
        if _workspace_creds is None or _workspace_creds_identity != identity:
            _workspace_creds = _build_workspace_credentials(*identity)
            _workspace_creds_identity = identity
        creds = _workspace_creds
        if not creds.valid:
            from google.auth.transport.requests import Request as AuthRequest

            try:
                creds.refresh(AuthRequest())
            except google.auth.exceptions.RefreshError as exc:
                raise WorkspaceAuthError(_explain_refresh_error(exc, *identity)) from exc
        return creds


_chat_user_id_lock = threading.Lock()
_chat_user_id_cache: dict[str, str] = {}


def resolve_chat_user_id(user_email: str) -> str:
    """Resolves a Workspace user email (e.g. `test-1@domain.com`) to its numeric Google user ID (`sub`)
    for Google Chat incoming webhook `<users/{sub}>` @-mentions.

    Uses keyless DWD (`WORKSPACE_DWD_SERVICE_ACCOUNT` signing a JWT with `sub=user_email` and scope
    `https://www.googleapis.com/auth/userinfo.profile`) and queries OIDC userinfo. Best-effort and
    cached per process: returns `""` on any error or when DWD is not configured, so notifications
    always fall back to displaying the plain email without failing the job.
    """
    email = (user_email or "").strip().lower()
    if not email or "@" not in email or email.endswith(".gserviceaccount.com"):
        return ""
    with _chat_user_id_lock:
        cached = _chat_user_id_cache.get(email)
    if cached is not None:
        return cached

    _, dwd_sa = workspace_identity()
    if not dwd_sa:
        return ""
    try:
        import json
        import urllib.request
        from google.auth import impersonated_credentials
        from google.auth.transport.requests import Request as AuthRequest

        source, _ = google.auth.default(scopes=list(_IAM_SIGNER_SCOPES))
        strip = getattr(source, "with_quota_project", None)
        if callable(strip):
            source = strip(None)
        creds = impersonated_credentials.Credentials(
            source_credentials=source,
            target_principal=dwd_sa,
            target_scopes=list(_CHAT_MENTION_SCOPES),
            subject=email,
        )
        creds.refresh(AuthRequest())
        req = urllib.request.Request(
            _OIDC_USERINFO_URL,
            headers={"Authorization": f"Bearer {creds.token}"},
        )
        with urllib.request.urlopen(req, timeout=8.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        sub = str(data.get("sub") or "").strip()
        if sub.isdigit():
            with _chat_user_id_lock:
                _chat_user_id_cache[email] = sub
            return sub
    except Exception as exc:
        logger.warning("Could not resolve Google Chat user ID for %s: %s", email, exc)
    return ""


def workspace_principal() -> str:
    """The account every audit folder (Editor) and the SOP Sheet (Viewer) must be shared with."""
    subject, dwd_sa = workspace_identity()
    if subject:
        return subject
    if dwd_sa:
        return dwd_sa
    email = str(getattr(_workspace_creds, "service_account_email", "") or "")
    return email if "@" in email else "运行时服务账号"


def _http_status(exc: BaseException) -> Optional[int]:
    status = getattr(getattr(exc, "resp", None), "status", None)
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


def _http_error_text(exc: BaseException) -> str:
    content = getattr(exc, "content", b"")
    if isinstance(content, bytes):
        content = content.decode("utf-8", "ignore")
    return f"{exc} {content}"


def _is_storage_quota_error(exc: BaseException) -> bool:
    """True for Drive's 403 storageQuotaExceeded (bare SA in My Drive, or a full bot account)."""
    text = _http_error_text(exc)
    return _http_status(exc) == 403 and (
        "storageQuotaExceeded" in text or "do not have storage quota" in text
    )


def _storage_quota_message() -> str:
    subject, _ = workspace_identity()
    if subject:
        return f"Drive 拒绝写入：被代表账号 {subject} 的云端硬盘空间已满或未开通 Drive，请清理或扩容后重试"
    return (
        "Drive 拒绝写入：服务账号没有云端硬盘空间（Google 规定），不能在个人 My Drive 里新建表格"
        "或上传视频。请管理员配置域级委派（Terraform 变量 workspace_impersonate_user）后重试"
    )


def _classify_write_error(exc: BaseException, folder_id: str) -> BaseException:
    """Maps a Drive failure on `folder_id` to an operator-actionable WorkspaceConfigError.

    Returns `exc` unchanged when it is not a configuration problem (transient / unknown).
    """
    if isinstance(exc, WorkspaceConfigError):
        return exc
    if _is_storage_quota_error(exc):
        return WorkspaceStorageQuotaError(_storage_quota_message())
    status = _http_status(exc)
    who = workspace_principal()
    if status == 404:
        return WorkspaceAccessError(
            f"找不到文件夹 `{folder_id}`，或 `{who}` 没有访问权限："
            f"请在 Google Drive 里把该文件夹以「编辑者」身份共享给 `{who}`"
        )
    if status == 403 and "insufficient" in _http_error_text(exc).lower():
        return WorkspaceAccessError(
            f"`{who}` 对文件夹 `{folder_id}` 只有查看权限，无法写入报告和证据视频："
            "请把共享权限改为「编辑者」"
        )
    return exc


def _reraise_as_config_error(exc: BaseException, folder_id: str) -> NoReturn:
    """Call from an `except` block: re-raises `exc`, upgraded to a WorkspaceConfigError if it is one."""
    mapped = _classify_write_error(exc, folder_id)
    if mapped is exc:
        raise exc
    raise mapped from exc


_FOLDER_MIME = "application/vnd.google-apps.folder"
_SPREADSHEET_MIME = "application/vnd.google-apps.spreadsheet"
_WRITE_PROBE_NAME = ".cctv_audit_write_probe"


from .video_ingestor import VideoMetadataItem


class GoogleWorkspaceGateway:
    """All Drive / Sheets I/O. Credentials resolve lazily per call, never at import time."""

    def _drive_service(self):
        from googleapiclient.discovery import build

        return build("drive", "v3", credentials=workspace_credentials(), cache_discovery=False)

    def _sheets_service(self):
        from googleapiclient.discovery import build

        return build("sheets", "v4", credentials=workspace_credentials(), cache_discovery=False)

    @staticmethod
    def _check_folder_meta(drive: Any, folder_id: str) -> str:
        """Folder exists, is a folder, is editable by the identity, and the identity can own files."""
        who = workspace_principal()
        try:
            meta = (
                drive.files()
                .get(
                    fileId=folder_id,
                    fields="id, name, mimeType, driveId, capabilities(canAddChildren)",
                    supportsAllDrives=True,
                )
                .execute(num_retries=3)
            )
        except Exception as exc:
            _reraise_as_config_error(exc, folder_id)
        name = str(meta.get("name") or folder_id)
        if meta.get("mimeType") != _FOLDER_MIME:
            raise WorkspaceAccessError(
                f"链接指向的是文件「{name}」，不是文件夹：请发送存放监控视频的 Google Drive 文件夹链接"
            )
        if not (meta.get("capabilities") or {}).get("canAddChildren"):
            raise WorkspaceAccessError(
                f"`{who}` 对文件夹「{name}」只有查看权限，无法写入报告和证据视频："
                "请把共享权限改为「编辑者」"
            )
        if not workspace_identity()[0] and not meta.get("driveId"):
            # A bare SA can never own a non-folder file in My Drive; say so before trying.
            raise WorkspaceStorageQuotaError(_storage_quota_message())
        return name

    async def probe_write_access(self, folder_id: str) -> str:
        """Proves the Workspace identity can create and own a file in `folder_id`; returns its name.

        Runs before any Gemini spend (preflight and Tier-2 start). Creates a 2-byte probe file and
        deletes it again, which is the only reliable way to detect a missing DWD authorisation, a
        bot without Drive storage, or viewer-only sharing. Raises a WorkspaceConfigError subclass.
        """

        def _probe() -> str:
            from googleapiclient.http import MediaInMemoryUpload

            drive = self._drive_service()
            name = self._check_folder_meta(drive, folder_id)
            try:
                created = (
                    drive.files()
                    .create(
                        body={"name": _WRITE_PROBE_NAME, "parents": [folder_id]},
                        media_body=MediaInMemoryUpload(b"ok", mimetype="text/plain"),
                        fields="id",
                        supportsAllDrives=True,
                    )
                    .execute(num_retries=3)
                )
            except Exception as exc:
                _reraise_as_config_error(exc, folder_id)
            probe_id = created["id"]
            try:
                drive.files().delete(fileId=probe_id, supportsAllDrives=True).execute(num_retries=3)
            except Exception as exc:
                # Shared-Drive content managers may trash but not delete; never fail the job on cleanup.
                try:
                    drive.files().update(
                        fileId=probe_id, body={"trashed": True}, supportsAllDrives=True
                    ).execute(num_retries=3)
                except Exception:
                    logger.warning("Write probe %s left in folder %s: %s", probe_id, folder_id, exc)
            return name

        return await asyncio.to_thread(_probe)

    async def check_sheet_readable(self, sheet_id: str) -> str:
        """The SOP master Sheet is visible to the Workspace identity; returns its title.

        Without this check an unshared SOP Sheet silently degrades every audit to the bundled
        baseline prompt (`PromptManager.load_active_config` falls back on any read error).
        """

        def _check() -> str:
            drive = self._drive_service()
            try:
                meta = (
                    drive.files()
                    .get(fileId=sheet_id, fields="id, name", supportsAllDrives=True)
                    .execute(num_retries=3)
                )
            except Exception as exc:
                if _http_status(exc) == 404:
                    who = workspace_principal()
                    raise WorkspaceAccessError(
                        f"读不到 SOP 总控表 `{sheet_id}`：请把它以「查看者」（或以上）身份共享给 `{who}`"
                    ) from exc
                raise
            return str(meta.get("name") or sheet_id)

        return await asyncio.to_thread(_check)

    @staticmethod
    def _ffprobe_drive_stream(file_id: str) -> tuple[int, int, float]:
        """Fallback when Drive's async media indexer has not populated `videoMediaMetadata` yet."""
        import json
        import shutil
        import subprocess

        ffprobe = shutil.which("ffprobe")
        if not ffprobe or not file_id:
            return 0, 0, 0.0
        try:
            token = workspace_credentials().token
            if not token:
                return 0, 0, 0.0
            url = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media&supportsAllDrives=true"
            proc = subprocess.run(
                [
                    ffprobe,
                    "-v",
                    "error",
                    "-headers",
                    f"Authorization: Bearer {token}\r\n",
                    "-print_format",
                    "json",
                    "-show_format",
                    "-show_streams",
                    url,
                ],
                capture_output=True,
                text=True,
                timeout=15.0,
                check=False,
            )
            if proc.returncode != 0:
                return 0, 0, 0.0
            info = json.loads(proc.stdout or "{}")
            vstreams = [s for s in info.get("streams", []) if s.get("codec_type") == "video"]
            if not vstreams:
                return 0, 0, 0.0
            vs = vstreams[0]
            w = int(vs.get("width") or 0)
            h = int(vs.get("height") or 0)
            dur = float(info.get("format", {}).get("duration") or vs.get("duration") or 0.0)
            return w, h, dur
        except Exception as exc:
            logger.debug("ffprobe fallback on Drive file %s failed: %s", file_id, exc)
            return 0, 0, 0.0

    async def list_folder_videos(self, folder_id: str) -> List[VideoMetadataItem]:
        loop = asyncio.get_running_loop()

        def _fetch():
            drive = self._drive_service()
            q = (
                f"'{folder_id}' in parents and trashed=false and "
                f"(mimeType contains 'video/mp4' or mimeType contains 'video/quicktime' or mimeType contains 'video/')"
            )
            res = (
                drive.files()
                .list(
                    q=q,
                    fields="files(id, name, videoMediaMetadata)",
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                )
                .execute(num_retries=3)
            )
            raw_files = res.get("files", [])
            missing_ids: List[str] = []
            for f in raw_files:
                meta = f.get("videoMediaMetadata", {})
                w = int(meta.get("width", 0) or 0)
                h = int(meta.get("height", 0) or 0)
                dur = float(meta.get("durationMillis", 0) or 0) / 1000.0
                if w <= 0 or h <= 0 or dur <= 0.0:
                    missing_ids.append(f["id"])

            probed_by_id: Dict[str, tuple[int, int, float]] = {}
            if len(missing_ids) == 1:
                probed_by_id[missing_ids[0]] = self._ffprobe_drive_stream(missing_ids[0])
            elif len(missing_ids) > 1:
                from concurrent.futures import ThreadPoolExecutor

                with ThreadPoolExecutor(max_workers=min(4, len(missing_ids))) as pool:
                    for fid, result in zip(
                        missing_ids, pool.map(self._ffprobe_drive_stream, missing_ids)
                    ):
                        probed_by_id[fid] = result

            items = []
            for f in raw_files:
                meta = f.get("videoMediaMetadata", {})
                w = int(meta.get("width", 0) or 0)
                h = int(meta.get("height", 0) or 0)
                dur = float(meta.get("durationMillis", 0) or 0) / 1000.0
                if f["id"] in probed_by_id:
                    pw, ph, pdur = probed_by_id[f["id"]]
                    w = w or pw
                    h = h or ph
                    dur = dur if dur > 0.0 else pdur
                items.append(
                    VideoMetadataItem(
                        file_id=f["id"],
                        filename=f["name"],
                        duration_sec=dur,
                        width=w,
                        height=h,
                    )
                )
            from cctv_audit.video_ingestor import natural_video_sort_key

            items.sort(key=lambda v: natural_video_sort_key(v.filename))
            return items

        return await loop.run_in_executor(None, _fetch)

    async def download_video_to_path(self, file_id: str, dest_path: Path) -> Path:
        import httpx

        # Refreshed under the credential lock; raises WorkspaceAuthError with a fix-it message.
        creds = await asyncio.to_thread(workspace_credentials)
        token = creds.token

        url = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media&supportsAllDrives=true"
        headers = {"Authorization": f"Bearer {token}"}

        # Download securely to a .tmp file first so aborted tasks don't leave partial fragments
        tmp_path = dest_path.with_name(dest_path.name + ".tmp")

        async with httpx.AsyncClient(timeout=600.0) as client:
            async with client.stream("GET", url, headers=headers) as response:
                response.raise_for_status()
                with open(tmp_path, "wb") as f:
                    async for chunk in response.aiter_bytes(chunk_size=8192 * 1024):
                        f.write(chunk)

        # Atomically commit the file once fully downloaded
        tmp_path.rename(dest_path)
        return dest_path

    async def ensure_subfolder(self, parent_folder_id: str, name: str) -> str:
        loop = asyncio.get_running_loop()

        def _ensure() -> str:
            drive = self._drive_service()
            q = (
                f"'{parent_folder_id}' in parents and "
                f"(name='{name}' or name contains '违规证据切片_Evidence') and "
                f"mimeType='application/vnd.google-apps.folder' and trashed=false"
            )
            res = (
                drive.files()
                .list(
                    q=q,
                    fields="files(id, name)",
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                )
                .execute(num_retries=3)
            )
            files = res.get("files", [])
            if files:
                return files[0]["id"]
            metadata = {
                "name": name,
                "parents": [parent_folder_id],
                "mimeType": _FOLDER_MIME,
            }
            try:
                return (
                    drive.files()
                    .create(
                        body=metadata,
                        fields="id",
                        supportsAllDrives=True,
                    )
                    .execute(num_retries=3)["id"]
                )
            except Exception as exc:
                _reraise_as_config_error(exc, parent_folder_id)

        return await loop.run_in_executor(None, _ensure)

    async def upload_evidence_mp4(self, subfolder_id: str, local_path: Path) -> str:
        loop = asyncio.get_running_loop()

        def _up() -> str:
            from googleapiclient.http import MediaFileUpload

            drive = self._drive_service()
            try:
                # Avoid duplicate uploads if a retry already created the same clip file in subfolder_id
                q = (
                    f"'{subfolder_id}' in parents and name='{local_path.name}' "
                    f"and trashed=false"
                )
                existing = (
                    drive.files()
                    .list(
                        q=q,
                        fields="files(id)",
                        supportsAllDrives=True,
                        includeItemsFromAllDrives=True,
                    )
                    .execute(num_retries=3)
                    .get("files", [])
                )
                if existing:
                    return f"https://drive.google.com/file/d/{existing[0]['id']}/view"

                metadata = {"name": local_path.name, "parents": [subfolder_id]}
                media = MediaFileUpload(
                    str(local_path), mimetype="video/mp4", resumable=True
                )
                f = (
                    drive.files()
                    .create(
                        body=metadata,
                        media_body=media,
                        fields="id, webViewLink",
                        supportsAllDrives=True,
                    )
                    .execute(num_retries=3)
                )
                fid = f.get("id")
                if fid:
                    return f"https://drive.google.com/file/d/{fid}/view"
                link = f.get("webViewLink", "")
                if link:
                    return link
            except Exception as e:
                mapped = _classify_write_error(e, subfolder_id)
                if isinstance(mapped, WorkspaceConfigError):
                    if mapped is e:
                        raise
                    raise mapped from e
                logger.warning(
                    "Failed to upload evidence clip %s to Drive: %s",
                    local_path.name,
                    e,
                )
            return ""

        return await loop.run_in_executor(None, _up)

    async def create_dual_tab_report_sheet(
        self,
        parent_folder_id: str,
        title: str,
        tab1_rows: List,
        tab2_rows: List,
        *,
        reuse_suffix: str = "",
    ) -> tuple[str, str]:
        """Creates the dual-tab report Sheet in `parent_folder_id` (owned by the Workspace identity).

        `reuse_suffix` (e.g. `_<audit_id>`) makes a watchdog-resumed job re-publish into the Sheet it
        already created instead of leaving a duplicate. Only this job's own Sheet is ever reused --
        never an unrelated spreadsheet that happens to sit in the folder.
        """
        loop = asyncio.get_running_loop()

        def _create_or_update() -> tuple[str, str]:
            drive = self._drive_service()
            sheets = self._sheets_service()
            sid = ""
            surl = ""
            tab1_title = "违规事件 3 秒复核台"
            tab2_title = "本次视频 Token 消耗与耗时账单"
            created_new_sheet = False

            # 1. Resume / fixed-title idempotency: reuse the folder's report Sheet whose name
            #    equals `title` (e.g. `📊 AI稽核报告与Token账单`) or ends with `reuse_suffix`.
            #    Unrelated spreadsheets in the folder (e.g. "门店排班表") never match.
            page_token = None
            while not sid:
                page = (
                    drive.files()
                    .list(
                        q=(
                            f"'{parent_folder_id}' in parents and "
                            f"mimeType='{_SPREADSHEET_MIME}' and trashed=false"
                        ),
                        fields="nextPageToken, files(id, name, webViewLink)",
                        pageSize=1000,
                        pageToken=page_token,
                        supportsAllDrives=True,
                        includeItemsFromAllDrives=True,
                    )
                    .execute(num_retries=3)
                )
                for f in page.get("files", []):
                    fname = str(f.get("name", ""))
                    if fname == title or (reuse_suffix and fname.endswith(reuse_suffix)):
                        sid = f["id"]
                        surl = (
                            f.get("webViewLink")
                            or f"https://docs.google.com/spreadsheets/d/{sid}/edit"
                        )
                        break
                page_token = page.get("nextPageToken")
                if not page_token:
                    break

            # 2. Otherwise create a brand-new Sheet. A failure here is a setup problem (no DWD, bot
            #    storage full, folder not editable) and must surface, not be papered over.
            if not sid:
                metadata = {
                    "name": title,
                    "mimeType": _SPREADSHEET_MIME,
                    "parents": [parent_folder_id],
                }
                try:
                    res = (
                        drive.files()
                        .create(
                            body=metadata,
                            fields="id, webViewLink",
                            supportsAllDrives=True,
                        )
                        .execute(num_retries=3)
                    )
                except Exception as exc:
                    _reraise_as_config_error(exc, parent_folder_id)
                sid = res["id"]
                surl = res.get("webViewLink") or f"https://docs.google.com/spreadsheets/d/{sid}/edit"
                created_new_sheet = True

            # 3. Ensure both target tabs exist on spreadsheet `sid`
            meta = sheets.spreadsheets().get(spreadsheetId=sid).execute(num_retries=3)
            existing_sheets = meta.get("sheets", [])
            existing_titles = {
                s.get("properties", {}).get("title", "") for s in existing_sheets
            }
            requests_body = []

            if created_new_sheet and existing_sheets:
                first_sheet_id = (
                    existing_sheets[0].get("properties", {}).get("sheetId", 0)
                )
                requests_body.append(
                    {
                        "updateSheetProperties": {
                            "properties": {
                                "sheetId": first_sheet_id,
                                "title": tab1_title,
                            },
                            "fields": "title",
                        }
                    }
                )
                requests_body.append(
                    {"addSheet": {"properties": {"title": tab2_title}}}
                )
            else:
                for target_tab in (tab1_title, tab2_title):
                    if target_tab not in existing_titles:
                        requests_body.append(
                            {"addSheet": {"properties": {"title": target_tab}}}
                        )

            if requests_body:
                sheets.spreadsheets().batchUpdate(
                    spreadsheetId=sid, body={"requests": requests_body}
                ).execute(num_retries=3)

            # A reused Sheet still holds the previous attempt's rows; wipe them so a shorter
            # re-publish cannot leave stale rows underneath.
            if not created_new_sheet:
                sheets.spreadsheets().values().batchClear(
                    spreadsheetId=sid,
                    body={"ranges": [f"'{tab1_title}'", f"'{tab2_title}'"]},
                ).execute(num_retries=3)

            # 4. Clear & write Tab 1 (`违规事件 3 秒复核台`) and Tab 2 (`本次视频 Token 消耗与耗时账单`)
            tab1_values = [
                [
                    "稽核单号 (Audit_ID)",
                    "监控原片 (Video_Filename)",
                    "违规时间点 (Timestamp)",
                    "SOP条款 (Rule_ID)",
                    "判定类型 (Disposition)",
                    "严重等级 (Severity)",
                    "置信度 (Confidence)",
                    "AI稽核证据描述 (Evidence_Description)",
                    "20秒证据视频链接 (Evidence_Drive_URL)",
                    "人工复核状态 (Human_Review_Status)",
                ]
            ]
            for r in tab1_rows:
                tab1_values.append(
                    [
                        str(getattr(r, "audit_id", "")),
                        str(getattr(r, "video_filename", "")),
                        str(getattr(r, "timestamp_in_clip", "")),
                        str(getattr(r, "rule_id", "")),
                        str(getattr(r, "disposition", "")),
                        str(getattr(r, "severity", "")),
                        float(getattr(r, "confidence", 0.0)),
                        str(getattr(r, "evidence_description", "")),
                        str(getattr(r, "evidence_drive_url", "")),
                        str(getattr(r, "human_review_status", "")),
                    ]
                )

            tab2_values = [
                [
                    "稽核单号 (Audit_ID)",
                    "文件夹ID (Folder_ID)",
                    "监控原片 (Video_Filename)",
                    "切片时长秒 (Duration_Sec)",
                    "分辨率 (Resolution)",
                    "实际调用模型 (Model_Version)",
                    "实际调用Prompt (Prompt_Version)",
                    "输入Token (Prompt_Tokens)",
                    "思考Token (Thoughts_Tokens)",
                    "输出Token (Candidates_Tokens)",
                    "总Token (Total_Tokens)",
                    "预估成本USD (Cost_USD)",
                    "端到端耗时ms (Latency_ms)",
                    "检出事件数 (Flagged_Events)",
                    "降级告警 (Fallback_Warning)",
                ]
            ]
            for r in tab2_rows:
                tab2_values.append(
                    [
                        str(getattr(r, "audit_id", "")),
                        str(getattr(r, "auditor_folder_id", "")),
                        str(getattr(r, "video_filename", "")),
                        float(getattr(r, "video_duration_sec", 0.0)),
                        str(getattr(r, "resolution", "")),
                        str(getattr(r, "model_version_used", "")),
                        str(getattr(r, "prompt_version_used", "")),
                        int(getattr(r, "prompt_token_count", 0)),
                        int(getattr(r, "thoughts_token_count", 0)),
                        int(getattr(r, "candidates_token_count", 0)),
                        int(getattr(r, "total_token_count", 0)),
                        float(getattr(r, "estimated_cost_usd", 0.0)),
                        int(getattr(r, "e2e_latency_ms", 0)),
                        int(getattr(r, "flagged_events_count", 0)),
                        str(getattr(r, "fallback_warning", "") or ""),
                    ]
                )

            sheets.spreadsheets().values().batchUpdate(
                spreadsheetId=sid,
                body={
                    "valueInputOption": "USER_ENTERED",
                    "data": [
                        {"range": f"'{tab1_title}'!A1", "values": tab1_values},
                        {"range": f"'{tab2_title}'!A1", "values": tab2_values},
                    ],
                },
            ).execute(num_retries=3)
            return sid, surl

        return await loop.run_in_executor(None, _create_or_update)

