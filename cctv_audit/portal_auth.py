"""Supervisor portal identity (Cloud Run direct IAP) and per-store Google Drive authorization.

Round 86A. The portal can run in two modes, selected by env `PORTAL_MODE`:

* `hmac` (default; the public `<p>-worker` service): the signed `/portal?...&sig=` link is the
  only check. When `PORTAL_SERVICE_URL` is set, the worker no longer serves the portal itself:
  `GET /portal` redirects to the IAP-protected portal service and the portal APIs answer 403.
* `iap` (the dedicated `<p>-portal` service behind Cloud Run direct IAP): every portal request
  must carry a valid `X-Goog-IAP-JWT-Assertion`, verified here against Google's IAP signing keys
  and the service's expected audience. The verified `email` claim is the viewer. The viewer may
  only see a store whose report (Google Sheet, or the store's Drive folder before the report
  exists) the viewer can open in Google Drive; that check impersonates the viewer through the
  existing keyless domain-wide delegation (`gcp.viewer_can_open_drive_file`).

`X-Goog-Authenticated-User-Email` is never trusted on its own: only the signed JWT counts.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger("cctv_audit.portal_auth")

PORTAL_MODE_IAP = "iap"
PORTAL_MODE_HMAC = "hmac"
IAP_JWT_HEADER = "x-goog-iap-jwt-assertion"
IAP_CERTS_URL = "https://www.gstatic.com/iap/verify/public_key"
IAP_ISSUER = "https://cloud.google.com/iap"

# Paths the IAP portal service serves; everything else answers 404 there.
IAP_MODE_ALLOWED_PATHS = frozenset({"/portal", "/api/portal/data", "/api/portal/review", "/healthz"})

DRIVE_ACCESS_DENIED_MESSAGE = (
    "You don't have Google Drive access to this store's report. "
    "Ask the store folder owner to share it with you."
)
DRIVE_CHECK_CONCURRENCY = 8

# (token, audience or None) -> verified claims. `None` audience = verify signature/expiry only.
IapVerifier = Callable[[str, Optional[str]], Dict[str, Any]]
# (viewer_email, drive_file_id) -> (allowed, reason)
DriveAccessChecker = Callable[[str, str], Awaitable[Tuple[bool, str]]]


class IapAuthError(Exception):
    """The request has no valid IAP identity (maps to HTTP 401)."""


def portal_mode() -> str:
    """`iap` or `hmac` (default) from env `PORTAL_MODE`."""
    raw = os.environ.get("PORTAL_MODE", "").strip().lower()
    return PORTAL_MODE_IAP if raw == PORTAL_MODE_IAP else PORTAL_MODE_HMAC


def is_iap_mode() -> bool:
    return portal_mode() == PORTAL_MODE_IAP


def portal_service_url() -> str:
    """URL of the IAP-protected portal service the public worker redirects to ('' = serve locally)."""
    return os.environ.get("PORTAL_SERVICE_URL", "").strip().rstrip("/")


def expected_iap_audience() -> str:
    """Expected `aud` of the IAP JWT for this Cloud Run service.

    Env `PORTAL_IAP_AUDIENCE` always wins. Otherwise
    `/projects/{PORTAL_PROJECT_NUMBER}/locations/{region}/services/{K_SERVICE}` with region from
    `PORTAL_REGION` or `GCP_LOCATION`. Returns '' when any part is missing.
    """
    override = os.environ.get("PORTAL_IAP_AUDIENCE", "").strip()
    if override:
        return override
    number = os.environ.get("PORTAL_PROJECT_NUMBER", "").strip()
    region = (os.environ.get("PORTAL_REGION", "") or os.environ.get("GCP_LOCATION", "")).strip()
    service = os.environ.get("K_SERVICE", "").strip()
    if not (number and region and service):
        return ""
    return f"/projects/{number}/locations/{region}/services/{service}"


def _default_iap_verifier(token: str, audience: Optional[str]) -> Dict[str, Any]:
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token

    return dict(
        id_token.verify_token(
            token,
            google_requests.Request(),
            audience=audience,
            certs_url=IAP_CERTS_URL,
        )
    )


_iap_verifier_hook: Optional[IapVerifier] = None
_aud_mismatch_warned = False
_aud_warn_lock = threading.Lock()


def set_iap_verifier(verifier: Optional[IapVerifier]) -> None:
    """Test seam: replaces the Google IAP JWT verifier (None restores the real one)."""
    global _iap_verifier_hook
    _iap_verifier_hook = verifier


def _reset_audience_warning_for_tests() -> None:
    global _aud_mismatch_warned
    with _aud_warn_lock:
        _aud_mismatch_warned = False


def _warn_audience_mismatch_once(actual_aud: Any, expected: str) -> None:
    global _aud_mismatch_warned
    with _aud_warn_lock:
        if _aud_mismatch_warned:
            return
        _aud_mismatch_warned = True
    logger.warning(
        "IAP JWT signature is valid but its audience %r does not match the expected %r. "
        "Set env PORTAL_IAP_AUDIENCE=%s on the portal service if that is this service's audience. "
        "Requests are rejected with 401 until fixed (logged once per process).",
        actual_aud,
        expected,
        actual_aud,
    )


def verify_iap_jwt(token: str, *, audience: str, verifier: Optional[IapVerifier] = None) -> str:
    """Verifies an IAP JWT and returns the lower-cased `email` claim; raises `IapAuthError`."""
    token = (token or "").strip()
    if not token:
        raise IapAuthError("Missing IAP identity (X-Goog-IAP-JWT-Assertion header).")
    if not audience:
        logger.error(
            "IAP audience is not configured: set PORTAL_IAP_AUDIENCE, or PORTAL_PROJECT_NUMBER + "
            "GCP_LOCATION (K_SERVICE is set by Cloud Run)."
        )
        raise IapAuthError("Portal IAP audience is not configured.")
    verify = verifier or _iap_verifier_hook or _default_iap_verifier
    try:
        claims = verify(token, audience)
    except Exception as exc:  # noqa: BLE001 - any verifier failure is a 401
        try:
            unchecked = verify(token, None)
        except Exception:  # noqa: BLE001 - bad signature / expired / malformed
            logger.info("Rejected invalid IAP JWT: %s", exc)
            raise IapAuthError("Invalid IAP identity token.") from exc
        _warn_audience_mismatch_once(unchecked.get("aud"), audience)
        raise IapAuthError("IAP identity token audience mismatch.") from exc
    issuer = str(claims.get("iss") or "")
    if issuer and issuer != IAP_ISSUER:
        raise IapAuthError(f"Untrusted IAP token issuer {issuer!r}.")
    email = str(claims.get("email") or "").strip().lower()
    if not email or "@" not in email:
        raise IapAuthError("IAP identity token carries no email claim.")
    return email


def _header(request: Any, name: str) -> str:
    headers = getattr(request, "headers", None)
    if headers is None:
        return ""
    getter = getattr(headers, "get", None)
    value = None
    if callable(getter):
        value = getter(name) or getter(name.title())
    if value is None and hasattr(headers, "items"):
        for key, candidate in headers.items():
            if str(key).lower() == name:
                value = candidate
                break
    return str(value).strip() if value else ""


def authenticate_portal_viewer(request: Any, *, verifier: Optional[IapVerifier] = None) -> str:
    """Verified viewer email from the request's IAP JWT; raises `IapAuthError`."""
    return verify_iap_jwt(
        _header(request, IAP_JWT_HEADER),
        audience=expected_iap_audience(),
        verifier=verifier,
    )


# ---- Drive authorization ---------------------------------------------------------------------

_SHEET_ID_RES = (
    re.compile(r"/spreadsheets/(?:u/\d+/)?d/([A-Za-z0-9_-]{10,})"),
    re.compile(r"/(?:file|document|presentation)/(?:u/\d+/)?d/([A-Za-z0-9_-]{10,})"),
    re.compile(r"/folders/([A-Za-z0-9_-]{10,})"),
    re.compile(r"[?&]id=([A-Za-z0-9_-]{10,})"),
)
_BARE_DRIVE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,}$")


def extract_drive_file_id(value: str) -> str:
    """Drive file ID from a Sheets/Drive URL or a bare ID; '' when none (or a gs:// target)."""
    text = (value or "").strip()
    if not text or text.lower().startswith("gs://"):
        return ""
    for pattern in _SHEET_ID_RES:
        match = pattern.search(text)
        if match:
            return match.group(1)
    return text if _BARE_DRIVE_ID_RE.match(text) else ""


def _is_gcs_like(value: str) -> bool:
    from .gcs_gateway import is_gcs_target

    text = (value or "").strip()
    return bool(text) and (text.lower().startswith("gs://") or is_gcs_target(text))


_gcs_skip_logged = False


def _log_gcs_skip_once() -> None:
    global _gcs_skip_logged
    if not _gcs_skip_logged:
        _gcs_skip_logged = True
        logger.info(
            "gs:// store reports have no Google Drive ACL: portal access to them is gated by IAP "
            "membership (portal_allowed_members) only."
        )


def drive_target_for_store(report_sheet_url: str, folder_id: str) -> Optional[str]:
    """Drive file to check for a store: its report Sheet, else its Drive folder.

    Returns None for gs:// stores (no Drive check possible) and '' when nothing is checkable.
    """
    if _is_gcs_like(report_sheet_url) or _is_gcs_like(folder_id):
        return None
    return extract_drive_file_id(report_sheet_url) or extract_drive_file_id(folder_id)


_drive_checker_hook: Optional[DriveAccessChecker] = None


def set_drive_access_checker(checker: Optional[DriveAccessChecker]) -> None:
    """Test seam: replaces the Drive permission checker (None restores the DWD-based one)."""
    global _drive_checker_hook
    _drive_checker_hook = checker


def _resolve_checker(checker: Optional[DriveAccessChecker]) -> DriveAccessChecker:
    if checker is not None:
        return checker
    if _drive_checker_hook is not None:
        return _drive_checker_hook
    from .gcp import viewer_can_open_drive_file

    return viewer_can_open_drive_file


def _denied_message(viewer_email: str, reason: str = "") -> str:
    msg = f"{DRIVE_ACCESS_DENIED_MESSAGE} (Signed in as {viewer_email}.)"
    return f"{msg} Detail: {reason}" if reason else msg


async def store_visible_to_viewer(
    viewer_email: str,
    report_sheet_url: str,
    folder_id: str,
    *,
    checker: Optional[DriveAccessChecker] = None,
) -> Tuple[bool, str]:
    """(allowed, reason) for one store."""
    target = drive_target_for_store(report_sheet_url or "", folder_id or "")
    if target is None:
        _log_gcs_skip_once()
        return True, ""
    if not target:
        return False, "No Google Drive file is associated with this store yet."
    # Permission outcomes (403/404/DWD refusal) come back as (False, reason). Anything raised here is an
    # infrastructure failure (e.g. Drive 5xx): let it propagate so the route returns 5xx (still fail-closed,
    # no data is served) instead of telling the supervisor they lack access.
    return await _resolve_checker(checker)(viewer_email, target)


async def require_store_access(
    viewer_email: str,
    report_sheet_url: str,
    folder_id: str,
    *,
    checker: Optional[DriveAccessChecker] = None,
) -> None:
    """Raises `PermissionError` (HTTP 403) when the viewer cannot open the store's report."""
    allowed, reason = await store_visible_to_viewer(
        viewer_email, report_sheet_url, folder_id, checker=checker
    )
    if not allowed:
        raise PermissionError(_denied_message(viewer_email, reason))


async def filter_visible_stores(
    viewer_email: str,
    stores: Sequence[Tuple[str, str, str]],
    *,
    checker: Optional[DriveAccessChecker] = None,
    concurrency: int = DRIVE_CHECK_CONCURRENCY,
) -> Dict[str, Tuple[bool, str]]:
    """Checks `(store_key, report_sheet_url, folder_id)` tuples with bounded concurrency."""
    sem = asyncio.Semaphore(max(1, int(concurrency)))

    async def _one(key: str, sheet: str, folder: str) -> Tuple[str, Tuple[bool, str]]:
        async with sem:
            return key, await store_visible_to_viewer(viewer_email, sheet, folder, checker=checker)

    results = await asyncio.gather(*(_one(k, s, f) for k, s, f in stores))
    return dict(results)


def denied_message(viewer_email: str, reason: str = "") -> str:
    return _denied_message(viewer_email, reason)


def client_ip_of(request: Any) -> str:
    forwarded = _header(request, "x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    client = getattr(request, "client", None)
    return str(getattr(client, "host", "") or "")


def user_agent_of(request: Any) -> str:
    return _header(request, "user-agent")[:512]


__all__: List[str] = [
    "DRIVE_ACCESS_DENIED_MESSAGE",
    "IAP_CERTS_URL",
    "IAP_JWT_HEADER",
    "IAP_MODE_ALLOWED_PATHS",
    "IapAuthError",
    "authenticate_portal_viewer",
    "client_ip_of",
    "denied_message",
    "drive_target_for_store",
    "expected_iap_audience",
    "extract_drive_file_id",
    "filter_visible_stores",
    "is_iap_mode",
    "portal_mode",
    "portal_service_url",
    "require_store_access",
    "set_drive_access_checker",
    "set_iap_verifier",
    "store_visible_to_viewer",
    "user_agent_of",
    "verify_iap_jwt",
]
