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

AUDIT_LOG_SCHEMA_VERSION = 1
AUDIT_LOG_PREFIX = "audit_logs/reviews"
PHASE_REQUESTED = "requested"
PHASE_SHEET_WRITE_FAILED = "sheet_write_failed"

_SAFE_SEGMENT_RE = re.compile(r"[^A-Za-z0-9._-]+")


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
        "old_status": old_status,
        "new_status": new_status,
        "old_note": old_note,
        "new_note": new_note,
        "report_sheet_url": report_sheet_url or "",
        "client_ip": client_ip or "",
        "user_agent": user_agent or "",
        "phase": phase,
    }


def _default_client() -> Any:
    from google.cloud import storage

    return storage.Client(project=os.environ.get("GCP_PROJECT", "").strip() or None)


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
            name = audit_object_name(record)
            blob = bkt.blob(name)
            blob.upload_from_string(
                json.dumps(record, ensure_ascii=False, sort_keys=True),
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
