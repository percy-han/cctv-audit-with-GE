# Base image pinned by digest (python:3.12-slim multi-arch index, 2026-09-26): rebuilding the same
# source tree starts from the same bytes. To move to a newer base, change the digest on purpose;
# that changes the image tag (deploy/ci_pipeline.py hashes this file) and rolls out like code.
FROM python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

# Install system-level ffmpeg/ffprobe for audio stripping (-an), 30m slicing, and 5-10s evidence clipping.
# Chromium/Playwright intentionally omitted: pure Google Drive + Workspace Zero-DB Serverless architecture.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# requirements.txt is a lock generated from requirements.in: every package, transitive ones included,
# at one version with its sha256 hashes. --require-hashes makes pip refuse anything else.
COPY requirements.txt .
RUN pip install --no-cache-dir --require-hashes -r requirements.txt

COPY cctv_audit/ ./cctv_audit/

EXPOSE 8080

CMD ["uvicorn", "cctv_audit.server:app", "--host", "0.0.0.0", "--port", "8080"]
