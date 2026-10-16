"""Cloud Run Jobs (24h execution engine) entrypoint for Chagee CCTV Scheduled Store Audits.

Invoked by `google_cloud_run_v2_job.cctv_audit_job_worker` (`python3 -m cctv_audit.job_runner`)
with per-execution container environment overrides:
  - `CCTV_AUDIT_JOB_ID`: Target store `AuditJob.job_id`
  - `CCTV_AUDIT_USER_ID`: Supervisor email owning the `AuditJob`
  - `CCTV_AUDIT_BATCH_ID`: Optional parent `ScheduledBatchRecord.batch_id`
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from typing import Optional, Sequence

from .audit_service import AuditService
from .gcs_gateway import RoutingStorageGateway
from .jobs import JobState
from .scheduled_auditor import ScheduledAuditEngine
from .video_ingestor import VideoIngestor
from .workspace_reporter import WorkspaceReporter

logger = logging.getLogger(__name__)


def _build_default_audit_service() -> AuditService:
    """Constructs an `AuditService` wired with `RoutingStorageGateway` so both Google Drive
    and `gs://` store folders can download videos, upload evidence clips, and write reports."""
    gw = RoutingStorageGateway()
    return AuditService(
        ingestor=VideoIngestor(drive_reader=gw),
        reporter=WorkspaceReporter(gateway=gw),
    )


async def run_job_async(
    *,
    job_id: str,
    user_id: str,
    batch_id: str = "",
    service: Optional[AuditService] = None,
) -> int:
    """Executes one store's scheduled `AuditJob` and returns the process exit code (0 or 1)."""
    if not job_id or not user_id:
        logger.error("Missing required CCTV_AUDIT_JOB_ID (%r) or CCTV_AUDIT_USER_ID (%r)", job_id, user_id)
        return 2

    svc = service if service is not None else _build_default_audit_service()
    engine = ScheduledAuditEngine(audit_service=svc)
    finished = await engine.execute_scheduled_store_job(
        job_id=job_id,
        user_id=user_id,
        batch_id=batch_id,
    )
    if finished is None:
        logger.error("Scheduled store job %s (user=%s) could not be loaded", job_id, user_id)
        return 1
    if finished.state == JobState.DONE:
        logger.info(
            "Scheduled store job %s completed: violations=%d, tokens=%d, sheet=%s",
            finished.job_id,
            finished.violations_found,
            finished.total_tokens_used,
            finished.report_sheet_url,
        )
        return 0
    if finished.needs_operator_fix:
        # Operator setup problem (e.g. unshared folder): do not trigger Cloud Run Job infra retry
        logger.warning(
            "Scheduled store job %s failed with operator setup problem (skipping infra retry): %s",
            finished.job_id,
            finished.error_message,
        )
        return 0
    logger.error("Scheduled store job %s failed: %s", finished.job_id, finished.error_message)
    return 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    parser = argparse.ArgumentParser(description="Chagee CCTV Scheduled Store Audit Job Runner")
    parser.add_argument(
        "--job-id",
        default=os.environ.get("CCTV_AUDIT_JOB_ID", "").strip(),
        help="Target AuditJob ID (default: env CCTV_AUDIT_JOB_ID)",
    )
    parser.add_argument(
        "--user-id",
        default=os.environ.get("CCTV_AUDIT_USER_ID", "").strip(),
        help="Target supervisor user email (default: env CCTV_AUDIT_USER_ID)",
    )
    parser.add_argument(
        "--batch-id",
        default=os.environ.get("CCTV_AUDIT_BATCH_ID", "").strip(),
        help="Parent scheduled batch ID (default: env CCTV_AUDIT_BATCH_ID)",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    return asyncio.run(
        run_job_async(
            job_id=args.job_id,
            user_id=args.user_id,
            batch_id=args.batch_id,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
