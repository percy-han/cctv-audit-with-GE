"""Pure GCS URI helpers (`gcs_uri.py`) with no intra-package imports.

Kept dependency-free so `config.py` (imported by everything) can normalise a `gs://` Master SOP
workbook path in `MASTER_PROMPT_SHEET_ID` without an import cycle.
"""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import parse_qs, unquote, urlparse

GCS_SCHEME = "gs://"

_GCS_BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,220}[a-z0-9]$")
_GCS_URL_HOSTS = ("storage.cloud.google.com", "storage.googleapis.com")
_GENERATION_RE = re.compile(r"^[0-9]+$")


def normalize_gcs_target(url_or_uri: str) -> Optional[str]:
    """Canonical `gs://bucket[/prefix]` for a GCS URI / Console Storage URL / storage URL, else None.

    Accepted forms (Zero-GWS fallback mode):
      * `gs://bucket/prefix/` (trailing slashes dropped)
      * `https://console.cloud.google.com/storage/browser/bucket/prefix?project=...`
      * `https://console.cloud.google.com/storage/browser/_details/bucket/path/obj.mp4`
      * `https://storage.cloud.google.com/bucket/prefix` / `https://storage.googleapis.com/bucket/prefix`
    Object generation pinning (GCS Object Versioning) is preserved as a `#<digits>` suffix:
    `gs://bucket/obj#1728547200123456` (gcloud syntax) or `?generation=<digits>` on a URL. A
    non-numeric fragment (browser anchors such as `#tab=live_object`) is still dropped.
    Anything else (Drive links, bare Drive IDs) returns None so the Drive parser stays untouched.
    A recognised GCS form with an invalid bucket name raises ValueError.
    """
    raw = (url_or_uri or "").strip()
    if not raw:
        return None
    generation = ""
    if raw[:5].lower() == "gs://":
        rest, _, fragment = raw[5:].partition("#")
        path = rest.split("?", 1)[0]
        if _GENERATION_RE.match(fragment):
            generation = fragment
    else:
        parsed = urlparse(raw)
        gen_values = parse_qs(parsed.query).get("generation", [])
        if gen_values and _GENERATION_RE.match(gen_values[0]):
            generation = gen_values[0]
        host = (parsed.hostname or "").lower()
        segs = [unquote(p) for p in parsed.path.split("/") if p]
        if host == "console.cloud.google.com":
            if len(segs) < 3 or segs[0] != "storage" or segs[1] != "browser":
                return None
            segs = segs[2:]
            if segs and segs[0] == "_details":
                segs = segs[1:]
        elif host not in _GCS_URL_HOSTS:
            return None
        path = "/".join(segs)
    parts = [p for p in path.split("/") if p]
    if not parts:
        raise ValueError(f"无法从链接中解析出有效的 GCS 存储桶: {raw}")
    bucket = parts[0].lower()
    if not _GCS_BUCKET_RE.match(bucket):
        raise ValueError(f"GCS 存储桶名称不合法: `{parts[0]}`（来自 {raw}）")
    prefix = "/".join(parts[1:])
    if not prefix:
        return f"gs://{bucket}"
    return f"gs://{bucket}/{prefix}#{generation}" if generation else f"gs://{bucket}/{prefix}"


def split_generation(uri: str) -> tuple[str, str]:
    """(`gs://bucket/obj`, generation or '') for a canonical URI that may end in `#<digits>`."""
    base, sep, frag = (uri or "").partition("#")
    if sep and _GENERATION_RE.match(frag):
        return base, frag
    return (uri or ""), ""


def is_gcs_target(target_id: str) -> bool:
    return (target_id or "").strip()[:5].lower() == GCS_SCHEME


def is_gcs_reference(value: str) -> bool:
    """True for `gs://...` and Cloud Console / storage URLs (anything `normalize_gcs_target` claims)."""
    try:
        return normalize_gcs_target(value) is not None
    except ValueError:
        return True
