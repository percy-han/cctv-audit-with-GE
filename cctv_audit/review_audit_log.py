"""Append-only GCS audit log of supervisor portal review actions (Round 86A).

Every status / note change submitted from the portal is written as one JSON object, create-only
(`if_generation_match=0`, so an existing object can never be overwritten), at

    gs://<AUDIT_LOG_BUCKET>/audit_logs/reviews/<YYYY>/<MM>/<DD>/<job_or_batch_id>/
        <UTC %Y%m%dT%H%M%S%fZ>_<8 hex>.json

`apply_portal_reviews` writes these records BEFORE touching the Google Sheet and refuses to change
the Sheet if the write fails ("no audit log, no change"). Edits made directly in the Sheet are not
captured (there is no Sheets change callback).
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import re
import uuid
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger("cctv_audit.review_audit_log")

AUDIT_LOG_SCHEMA_VERSION = 2  # v2: + evidence_clip_url / evidence_clip_file_id / sheet_row_snapshot
AUDIT_LOG_PREFIX = "audit_logs/reviews"
MAX_AUDIT_LOOKBACK_DAYS = 120
PHASE_REQUESTED = "requested"
PHASE_SHEET_WRITE_FAILED = "sheet_write_failed"
SGT_TZ = _dt.timezone(_dt.timedelta(hours=8), name="SGT")

_SAFE_SEGMENT_RE = re.compile(r"[^A-Za-z0-9._-]+")
_EVIDENCE_LINK_RE = re.compile(
    r"(drive\.google\.com/(file/d/|open\?id=)|^gs://.+\.mp4|storage\.cloud\.google\.com/.+\.mp4)", re.IGNORECASE
)
_DRIVE_FILE_ID_RE = re.compile(r"(?:/file/d/|[?&]id=)([A-Za-z0-9_-]{10,})")


class ReviewAuditLogError(RuntimeError):
    """The audit record could not be written; the review must not be applied (HTTP 500)."""


def audit_log_bucket() -> str:
    """Bucket from env `AUDIT_LOG_BUCKET` (Terraform: the versioned workspace bucket)."""
    value = os.environ.get("AUDIT_LOG_BUCKET", "").strip()
    if value.lower().startswith("gs://"):
        value = value[len("gs://") :]
    return value.strip("/")


def utc_now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _safe_segment(value: str) -> str:
    cleaned = _SAFE_SEGMENT_RE.sub("_", str(value or "").strip()).strip("._")
    return cleaned[:120] or "unknown"


def _record_time(record: Dict[str, Any]) -> _dt.datetime:
    raw = str(record.get("ts_utc") or "").strip()
    if raw:
        try:
            parsed = _dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=_dt.timezone.utc)
            return parsed.astimezone(_dt.timezone.utc)
        except ValueError:
            pass
    return utc_now()


def audit_object_name(record: Dict[str, Any], *, suffix: Optional[str] = None) -> str:
    """Object name for one record (see module docstring)."""
    ts = _record_time(record)
    scope = record.get("batch_id") or record.get("job_id") or "unknown"
    rand = suffix or uuid.uuid4().hex[:8]
    return (
        f"{AUDIT_LOG_PREFIX}/{ts:%Y}/{ts:%m}/{ts:%d}/{_safe_segment(scope)}/"
        f"{ts:%Y%m%dT%H%M%S%f}Z_{rand}.json"
    )


def format_ts_sgt(raw_ts: str) -> str:
    """Formats an ISO-8601 UTC timestamp string as `YYYY-MM-DD HH:MM:SS SGT` (`Asia/Singapore`, UTC+8)."""
    text = str(raw_ts or "").strip()
    if not text:
        return ""
    try:
        parsed = _dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=_dt.timezone.utc)
        return parsed.astimezone(SGT_TZ).strftime("%Y-%m-%d %H:%M:%S SGT")
    except ValueError:
        return text


def build_review_audit_record(
    *,
    reviewer_email: str,
    reviewer_source: str,
    job_id: str,
    batch_id: str,
    store_job_id: str,
    composite_key: str,
    row: Sequence[Any],
    old_status: str,
    new_status: str,
    old_note: str,
    new_note: str,
    report_sheet_url: str,
    client_ip: str,
    user_agent: str,
    phase: str = PHASE_REQUESTED,
    now: Optional[_dt.datetime] = None,
) -> Dict[str, Any]:
    """One audit record. `row` is the Tab-1 row (A..K) the review targets."""

    def _cell(idx: int) -> str:
        return str(row[idx]) if len(row) > idx and row[idx] is not None else ""

    ts = (now or utc_now()).astimezone(_dt.timezone.utc)
    cells = [("" if c is None else str(c)) for c in (row or [])]
    evidence_url = next((c for c in cells if _EVIDENCE_LINK_RE.search(c)), "")
    m = _DRIVE_FILE_ID_RE.search(evidence_url)
    return {
        "schema_version": AUDIT_LOG_SCHEMA_VERSION,
        "ts_utc": ts.isoformat().replace("+00:00", "Z"),
        "reviewer_email": reviewer_email or "",
        "reviewer_source": reviewer_source,
        "job_id": job_id or "",
        "batch_id": batch_id or "",
        "store_job_id": store_job_id or "",
        "composite_key": composite_key,
        "audit_id": _cell(0),
        "video_filename": _cell(1),
        "timestamp_in_clip": _cell(2),
        "rule_id": _cell(3),
        "disposition": _cell(4),
        "severity": _cell(5),
        # The evidence clip the supervisor actually judged (source video + timestamp alone is ambiguous).
        "evidence_clip_url": evidence_url,
        "evidence_clip_file_id": m.group(1) if m else "",
        # Full Tab-1 row as it was when the decision was made (description, confidence, links, ...).
        "sheet_row_snapshot": cells,
        "old_status": old_status,
        "new_status": new_status,
        "old_note": old_note,
        "new_note": new_note,
        "report_sheet_url": report_sheet_url or "",
        "client_ip": client_ip or "",
        "user_agent": user_agent or "",
        "phase": phase,
    }


class _RestBlob:
    def __init__(self, bucket: str, name: str) -> None:
        self._bucket, self._name = bucket, name

    def upload_from_string(self, data: str, *, content_type: str, if_generation_match: int) -> None:
        import urllib.parse
        import urllib.request

        import google.auth
        import google.auth.transport.requests

        creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/devstorage.read_write"])
        creds.refresh(google.auth.transport.requests.Request())
        url = (
            "https://storage.googleapis.com/upload/storage/v1/b/"
            f"{urllib.parse.quote(self._bucket, safe='')}/o"
            f"?uploadType=media&name={urllib.parse.quote(self._name, safe='')}"
            f"&ifGenerationMatch={int(if_generation_match)}"
        )
        req = urllib.request.Request(url, data=data.encode("utf-8"), method="POST")
        req.add_header("Authorization", f"Bearer {creds.token}")
        req.add_header("Content-Type", content_type)
        with urllib.request.urlopen(req, timeout=15) as resp:  # HTTPError (e.g. 412) propagates
            resp.read()


class _RestBucket:
    def __init__(self, name: str) -> None:
        self._name = name

    def blob(self, name: str) -> _RestBlob:
        return _RestBlob(self._name, name)


class _RestGcsClient:
    """Minimal GCS JSON-API client (google-auth + urllib, like cctv_audit/jobs.py). The worker image
    does not ship google-cloud-storage, so the audit log must not depend on it."""

    def bucket(self, name: str) -> _RestBucket:
        return _RestBucket(name)


def _default_client() -> Any:
    return _RestGcsClient()


def write_review_audit_records(
    records: Sequence[Dict[str, Any]],
    *,
    bucket: str,
    client: Any = None,
) -> List[str]:
    """Writes each record as its own create-only JSON object; returns the `gs://` URIs.

    Raises `ReviewAuditLogError` on any failure (missing bucket included).
    """
    if not records:
        return []
    bucket_name = (bucket or "").strip()
    if not bucket_name:
        raise ReviewAuditLogError("AUDIT_LOG_BUCKET is not configured.")
    written: List[str] = []
    try:
        gcs = client if client is not None else _default_client()
        bkt = gcs.bucket(bucket_name)
        for record in records:
            suffix = uuid.uuid4().hex[:8]
            name = audit_object_name(record, suffix=suffix)
            payload_json = json.dumps(record, ensure_ascii=False, sort_keys=True)
            blob = bkt.blob(name)
            blob.upload_from_string(
                payload_json,
                content_type="application/json",
                if_generation_match=0,
            )
            written.append(f"gs://{bucket_name}/{name}")
    except ReviewAuditLogError:
        raise
    except Exception as exc:  # noqa: BLE001 - any storage failure fails the review closed
        logger.error(
            "Review audit log write failed after %d/%d records to gs://%s: %s",
            len(written),
            len(records),
            bucket_name,
            exc,
        )
        raise ReviewAuditLogError(f"Review audit log write failed: {exc}") from exc
    return written


def format_audit_record_for_ui(record: Dict[str, Any]) -> Dict[str, Any]:
    """Formats one raw review audit record into a compact UI history entry for a violation card."""
    ts_utc = str(record.get("ts_utc") or "").strip()
    reviewer = str(record.get("reviewer_email") or "").strip()
    source = str(record.get("reviewer_source") or "").strip()
    return {
        "ts_utc": ts_utc,
        "ts_sgt": format_ts_sgt(ts_utc),
        "reviewer_email": reviewer or ("Signed Link User" if source == "hmac_link" else "Supervisor"),
        "reviewer_source": source,
        "composite_key": str(record.get("composite_key") or "").strip(),
        "audit_id": str(record.get("audit_id") or record.get("store_job_id") or record.get("job_id") or "").strip(),
        "video_filename": str(record.get("video_filename") or "").strip(),
        "timestamp_in_clip": str(record.get("timestamp_in_clip") or "").strip(),
        "rule_id": str(record.get("rule_id") or "").strip(),
        "old_status": str(record.get("old_status") or "PENDING").strip(),
        "new_status": str(record.get("new_status") or "PENDING").strip(),
        "old_note": str(record.get("old_note") or "").strip(),
        "new_note": str(record.get("new_note") or "").strip(),
    }


def list_review_audit_records_for_scope(
    *,
    bucket: str,
    job_id: str = "",
    batch_id: str = "",
    store_job_id: str = "",
    lookback_days: int = 14,
    client: Any = None,
) -> List[Dict[str, Any]]:
    """Lists append-only review audit records (`phase == 'requested'`) for the given store `job_id` / `batch_id`.

    Lists the exact dated per-scope folders (`audit_logs/reviews/<YYYY>/<MM>/<DD>/<scope>/`) for the
    last `lookback_days` days (callers pass the job age; capped at `MAX_AUDIT_LOOKBACK_DAYS`). De-duplicates by `(ts_utc, composite_key, reviewer_email, new_status, new_note)`
    and returns records sorted chronologically (oldest -> newest).
    """
    bucket_name = (bucket or "").strip()
    if bucket_name.lower().startswith("gs://"):
        bucket_name = bucket_name[len("gs://") :]
    bucket_name = bucket_name.strip("/")
    if not bucket_name:
        return []

    target_job = (store_job_id or job_id or "").strip()
    scopes: List[str] = []
    for cand in (target_job, job_id.strip(), batch_id.strip()):
        if cand and cand not in scopes:
            scopes.append(cand)
    if not scopes:
        return []

    raw_docs: List[Dict[str, Any]] = []

    # Support fake/custom clients in unit tests that expose `.sink` (list of uploaded dicts)
    if client is not None and hasattr(client, "sink"):
        for item in getattr(client, "sink", []) or []:
            try:
                doc = json.loads(item.get("data") or "{}")
                if isinstance(doc, dict):
                    raw_docs.append(doc)
            except Exception:
                continue
    elif not os.environ.get("PYTEST_CURRENT_TEST") or client is not None:
        try:
            import urllib.parse
            import urllib.request
            from concurrent.futures import ThreadPoolExecutor

            import google.auth
            import google.auth.transport.requests

            creds, _ = google.auth.default(
                scopes=["https://www.googleapis.com/auth/devstorage.read_only"]
            )
            if not getattr(creds, "valid", False):
                creds.refresh(google.auth.transport.requests.Request())
            token_str = str(creds.token)
            bkt_q = urllib.parse.quote(bucket_name, safe="")

            # Records live at `audit_logs/reviews/<YYYY>/<MM>/<DD>/<scope>/...` (one write per record),
            # so list only the exact per-day/per-scope prefixes for the lookback window: each list call
            # is tiny and the scan never truncates no matter how large the global audit log grows.
            prefixes: List[str] = []
            now_dt = utc_now()
            for day_offset in range(max(1, min(int(lookback_days), MAX_AUDIT_LOOKBACK_DAYS))):
                d = now_dt - _dt.timedelta(days=day_offset)
                for sc in scopes:
                    prefixes.append(f"{AUDIT_LOG_PREFIX}/{d:%Y}/{d:%m}/{d:%d}/{_safe_segment(sc)}/")

            def _list_prefix(pfx: str) -> List[str]:
                names: List[str] = []
                page_token = ""
                while True:
                    url = (
                        f"https://storage.googleapis.com/storage/v1/b/{bkt_q}/o"
                        f"?prefix={urllib.parse.quote(pfx, safe='')}"
                        f"&fields=items(name),nextPageToken&maxResults=1000"
                    )
                    if page_token:
                        url += f"&pageToken={urllib.parse.quote(page_token, safe='')}"
                    req = urllib.request.Request(url, method="GET")
                    req.add_header("Authorization", f"Bearer {token_str}")
                    try:
                        with urllib.request.urlopen(req, timeout=8) as resp:
                            body = json.loads(resp.read().decode("utf-8") or "{}")
                    except Exception:
                        return names
                    names.extend(
                        str(it.get("name") or "")
                        for it in (body.get("items") or [])
                        if str(it.get("name") or "").endswith(".json")
                    )
                    page_token = str(body.get("nextPageToken") or "")
                    if not page_token:
                        return names

            object_names: List[str] = []
            seen_names: set[str] = set()
            with ThreadPoolExecutor(max_workers=min(8, max(1, len(prefixes)))) as pool:
                for names in pool.map(_list_prefix, prefixes):
                    for n in names:
                        if n not in seen_names:
                            seen_names.add(n)
                            object_names.append(n)

            def _download_one(obj_name: str) -> Optional[Dict[str, Any]]:
                url = (
                    f"https://storage.googleapis.com/storage/v1/b/{bkt_q}/o/"
                    f"{urllib.parse.quote(obj_name, safe='')}?alt=media"
                )
                req = urllib.request.Request(url, method="GET")
                req.add_header("Authorization", f"Bearer {token_str}")
                try:
                    with urllib.request.urlopen(req, timeout=8) as resp:
                        doc = json.loads(resp.read().decode("utf-8") or "{}")
                    return doc if isinstance(doc, dict) else None
                except Exception:
                    return None

            if object_names:
                with ThreadPoolExecutor(max_workers=min(8, len(object_names))) as pool:
                    for doc in pool.map(_download_one, object_names):
                        if doc is not None:
                            raw_docs.append(doc)
        except Exception as exc:
            logger.debug("Non-fatal error listing review audit records for %s: %s", scopes, exc)

    deduped: Dict[Tuple[str, str, str, str, str], Dict[str, Any]] = {}
    for doc in raw_docs:
        if str(doc.get("phase") or PHASE_REQUESTED) != PHASE_REQUESTED:
            continue
        rec_job = str(doc.get("store_job_id") or doc.get("job_id") or "").strip()
        rec_batch = str(doc.get("batch_id") or "").strip()
        if target_job and rec_job and rec_job != target_job:
            continue
        if not target_job and batch_id and rec_batch != batch_id.strip():
            continue
        ui_entry = format_audit_record_for_ui(doc)
        dedup_key = (
            ui_entry["ts_utc"],
            ui_entry["composite_key"],
            ui_entry["reviewer_email"],
            ui_entry["new_status"],
            ui_entry["new_note"],
        )
        deduped[dedup_key] = ui_entry

    sorted_entries = sorted(deduped.values(), key=lambda x: x["ts_utc"])
    return sorted_entries
