#!/usr/bin/env python3
"""The golden set ("ruler") as data: loading, validation, version lineage and everything derived from it.

Nothing in the eval pipeline may hard-code golden CONTENT (item IDs, counts, SOP categories, outlets,
videos, folders, stable baseline items). Everything comes from two customer-private files that live
side by side (local path or ``gs://``):

* ``<stem>.jsonl``  the golden set, one labelled finding per line (schema: ``eval/data/README.md``).
* ``<stem>.manifest.json``  OPTIONAL explicit config for that golden set:

  - ``folders``: the evaluation folders and ALL clips to run, including unlabelled clips (those
    still count for alert density / hit rate). Each folder: ``group_id`` (stable job key),
    ``folder_id`` (Drive folder), ``label``, ``cache_job_ids`` (optional, where pre-sliced clips of
    older production jobs may be reused) and ``videos`` (``file_id``, ``filename``, optional
    ``duration_sec``/``width``/``height``/``size_bytes``). Missing -> folders are derived from the
    golden items (grouped by optional item ``drive_folder_id``, else ``outlet_name | focus``; only
    labelled clips; video metadata probed at ingest).
  - ``stable_baseline_items``: item IDs that must stay at 1.0 (the ``regressed_stable_items``
    guardrail). Also settable per item with ``"stable_baseline": true`` in the golden. Neither ->
    the guardrail reports ``not_configured`` (never a silent pass/fail).
  - ``item_overrides``: ``{item_id: {"sop_category": ..., "temporal_mode": ...}}`` for frozen golden
    files that cannot be edited without changing their hash.

``golden_version`` = ``<stem>@<sha256[:10]>`` over the *scoring ruler* only (scheme ``ruler-v2``): the
golden lines minus provenance fields, plus the ruler part of the manifest (clip universe per folder,
item overrides, stable-baseline set). Notes, clip metadata, Drive folder / cache job IDs and source
timestamps do not change it; full-content hashes are kept separately for audit. Recall scored on
different golden versions is never averaged or charted together.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import logging
from pathlib import Path
import re
import tempfile
from typing import Any, Mapping, Sequence

logger = logging.getLogger("eval.golden_set")

MANIFEST_SUFFIX = ".manifest.json"
VERSION_HASH_LEN = 10
REQUIRED_ITEM_FIELDS = ("item_id", "split", "outlet_name", "focus", "finding_verbatim",
                        "video_filenames", "video_file_ids", "parts")
REQUIRED_PART_FIELDS = ("part_id", "osd_times")
TEMPORAL_MODES = ("POINT", "WINDOW")


class GoldenSetError(ValueError):
    """The golden file or its manifest is malformed; the message names the offending item/field."""


def _canonical_line(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def canonical_golden_sha256(raw: bytes) -> str:
    """sha256 of the golden JSONL independent of formatting: BOM/CRLF/blank lines/key order/spacing."""
    text = raw.decode("utf-8-sig").replace("\r\n", "\n")
    lines = [_canonical_line(json.loads(ln)) for ln in text.split("\n") if ln.strip()]
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def canonical_manifest_sha256(manifest: Mapping[str, Any] | None) -> str:
    if not manifest:
        return ""
    return hashlib.sha256(_canonical_line(manifest).encode("utf-8")).hexdigest()


VERSION_SCHEME = "ruler-v2"
# Golden-line fields nothing in scoring reads (provenance only); excluded from the ruler hash.
PROVENANCE_ITEM_FIELDS = frozenset({"source_sheet_id", "source_sha256", "outlet_no"})
RULER_OVERRIDE_KEYS = ("sop_category", "temporal_mode", "stable_baseline")


def ruler_golden_sha256(raw: bytes) -> str:
    """sha256 over the golden lines minus provenance fields (``PROVENANCE_ITEM_FIELDS``): every field
    eval/score_run.py or this module reads -- ids, split, outlet/focus, clause, label text, videos,
    parts (OSD, descriptions), optional sop_category / temporal_mode / stable_baseline / drive_folder_id."""
    text = raw.decode("utf-8-sig").replace("\r\n", "\n")
    lines = []
    for ln in text.split("\n"):
        if ln.strip():
            obj = {k: v for k, v in json.loads(ln).items() if k not in PROVENANCE_ITEM_FIELDS}
            lines.append(_canonical_line(obj))
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def ruler_manifest(manifest: Mapping[str, Any] | None) -> dict[str, Any]:
    """The manifest fields that change scoring: clip universe per folder (group, label, file_id +
    filename; order-free), item overrides and the stable-baseline set. Excludes the free-text note,
    Drive folder IDs, cache job IDs and clip metadata (duration / size / resolution)."""
    m = manifest or {}
    out: dict[str, Any] = {}
    if m.get("stable_baseline_items"):
        out["stable_baseline_items"] = sorted(str(x) for x in m["stable_baseline_items"])
    overrides = {str(k): {kk: vv for kk, vv in (v or {}).items() if kk in RULER_OVERRIDE_KEYS}
                 for k, v in (m.get("item_overrides") or {}).items()}
    overrides = {k: v for k, v in overrides.items() if v}
    if overrides:
        out["item_overrides"] = overrides
    if m.get("folders"):
        out["folders"] = sorted(
            ({"group_id": str(f.get("group_id") or ""), "label": str(f.get("label") or ""),
              "videos": sorted(({"file_id": str(v["file_id"]), "filename": str(v["filename"])}
                                for v in f.get("videos") or []), key=lambda v: (v["filename"], v["file_id"]))}
             for f in m["folders"]), key=lambda f: f["group_id"])
    return out


def ruler_manifest_sha256(manifest: Mapping[str, Any] | None) -> str:
    return canonical_manifest_sha256(ruler_manifest(manifest))


def compute_golden_version(stem: str, golden_sha: str, manifest_sha: str) -> str:
    combined = hashlib.sha256(f"{golden_sha}\n{manifest_sha}".encode("utf-8")).hexdigest() if manifest_sha \
        else golden_sha
    return f"{stem}@{combined[:VERSION_HASH_LEN]}"


def validate_items(items: Sequence[Mapping[str, Any]]) -> None:
    if not items:
        raise GoldenSetError("黄金集为空：至少需要 1 条标注")
    seen_items: set[str] = set()
    seen_parts: set[str] = set()
    for n, it in enumerate(items, 1):
        missing = [f for f in REQUIRED_ITEM_FIELDS if f not in it or it[f] in (None, "", [])]
        if missing:
            raise GoldenSetError(f"第 {n} 行 (item_id={it.get('item_id')!r}) 缺少必填字段 {missing}")
        iid = str(it["item_id"])
        if iid in seen_items:
            raise GoldenSetError(f"item_id {iid!r} 重复")
        seen_items.add(iid)
        if len(it["video_filenames"]) != len(it["video_file_ids"]):
            raise GoldenSetError(f"{iid}: video_filenames 与 video_file_ids 数量不一致")
        for p in it["parts"]:
            pmiss = [f for f in REQUIRED_PART_FIELDS if f not in p]
            if pmiss:
                raise GoldenSetError(f"{iid}: part 缺少必填字段 {pmiss}")
            pid = str(p["part_id"])
            if pid in seen_parts:
                raise GoldenSetError(f"part_id {pid!r} 重复")
            seen_parts.add(pid)
            mode = str(p.get("temporal_mode") or it.get("temporal_mode") or "").upper()
            if mode and mode not in TEMPORAL_MODES:
                raise GoldenSetError(f"{pid}: temporal_mode 只能是 POINT / WINDOW，收到 {mode!r}")


@dataclass
class GoldenSet:
    source: str
    stem: str
    items: list[dict[str, Any]]
    golden_sha256: str  # ruler hash (feeds golden_version)
    manifest: dict[str, Any] = field(default_factory=dict)
    manifest_sha256: str = ""  # ruler hash (feeds golden_version)
    manifest_source: str = ""
    golden_sha256_full: str = ""  # full-content hashes, audit only
    manifest_sha256_full: str = ""
    raw: bytes = b""  # the golden JSONL bytes this set was built from (snapshotted with every run)
    origin: dict[str, Any] = field(default_factory=dict)  # e.g. the source label Sheet (eval/golden_sheet.py)

    @property
    def version(self) -> str:
        return compute_golden_version(self.stem, self.golden_sha256, self.manifest_sha256)

    @property
    def item_count(self) -> int:
        return len(self.items)

    @property
    def part_count(self) -> int:
        return sum(len(it["parts"]) for it in self.items)

    @property
    def split_counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for it in self.items:
            out[str(it["split"])] = out.get(str(it["split"]), 0) + 1
        return dict(sorted(out.items()))

    @property
    def video_count(self) -> int:
        return len({fid for it in self.items for fid in it["video_file_ids"]})

    def stable_baseline(self) -> tuple[list[str], str]:
        """(item ids, source) with source in golden_field | manifest | golden_field+manifest | none."""
        from_golden = [str(it["item_id"]) for it in self.items if it.get("stable_baseline") is True]
        from_manifest = [str(x) for x in (self.manifest.get("stable_baseline_items") or [])]
        known = {str(it["item_id"]) for it in self.items}
        unknown = sorted(set(from_manifest) - known)
        if unknown:
            raise GoldenSetError(f"manifest stable_baseline_items 含黄金集中不存在的 item_id: {unknown}")
        ids = list(dict.fromkeys(from_golden + from_manifest))
        source = "+".join(s for s, v in (("golden_field", from_golden), ("manifest", from_manifest)) if v)
        return ids, (source or "none")

    def summary(self) -> dict[str, Any]:
        stable, stable_src = self.stable_baseline()
        return {
            "golden_version": self.version,
            "golden_source": self.source,
            "golden_version_scheme": VERSION_SCHEME,
            "golden_sha256": self.golden_sha256,
            "golden_sha256_full": self.golden_sha256_full,
            "manifest_source": self.manifest_source,
            "manifest_sha256": self.manifest_sha256,
            "manifest_sha256_full": self.manifest_sha256_full,
            "item_count": self.item_count,
            "part_count": self.part_count,
            "split_counts": self.split_counts,
            "video_count": self.video_count,
            "stable_baseline_items": stable,
            "stable_baseline_source": stable_src,
            **({"golden_origin": dict(self.origin)} if self.origin else {}),
        }

    def write_snapshot(self, dest_dir: Path) -> Path:
        """Writes ``<stem>.jsonl`` (+ ``<stem>.manifest.json``) to ``dest_dir``; reloading it yields the
        same ``golden_version``. Returns the JSONL path."""
        dest_dir.mkdir(parents=True, exist_ok=True)
        path = dest_dir / f"{self.stem}.jsonl"
        path.write_bytes(self.raw)
        if self.manifest:
            (dest_dir / f"{self.stem}{MANIFEST_SUFFIX}").write_text(
                json.dumps(self.manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if self.origin:
            (dest_dir / "golden_origin.json").write_text(
                json.dumps(self.origin, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return path


def _apply_overrides(items: list[dict[str, Any]], manifest: Mapping[str, Any]) -> None:
    overrides = manifest.get("item_overrides") or {}
    known = {str(it["item_id"]) for it in items}
    unknown = sorted(set(overrides) - known)
    if unknown:
        raise GoldenSetError(f"manifest item_overrides 含黄金集中不存在的 item_id: {unknown}")
    allowed = {"sop_category", "temporal_mode", "stable_baseline"}
    for it in items:
        ov = overrides.get(str(it["item_id"])) or {}
        bad = sorted(set(ov) - allowed)
        if bad:
            raise GoldenSetError(f"item_overrides[{it['item_id']}] 只允许 {sorted(allowed)}，收到 {bad}")
        it.update(ov)


def manifest_path_for(golden_path: str) -> str:
    if golden_path.endswith(".jsonl"):
        return golden_path[: -len(".jsonl")] + MANIFEST_SUFFIX
    return golden_path + MANIFEST_SUFFIX


def _read_bytes(uri: str) -> bytes | None:
    if uri.startswith("gs://"):
        from google.cloud import storage

        bucket, _, name = uri[len("gs://"):].partition("/")
        blob = storage.Client().bucket(bucket).blob(name)
        return blob.download_as_bytes() if blob.exists() else None
    p = Path(uri)
    return p.read_bytes() if p.exists() else None


def load_golden(golden_uri: str | Path, manifest_uri: str | None = None) -> GoldenSet:
    """Loads + validates a golden set (local path or gs://) and its optional sidecar manifest."""
    src = str(golden_uri)
    raw = _read_bytes(src)
    if raw is None:
        raise FileNotFoundError(f"黄金集文件不存在: {src}")
    m_src = manifest_uri or manifest_path_for(src)
    m_raw = _read_bytes(m_src)
    manifest = json.loads(m_raw.decode("utf-8")) if m_raw else {}
    stem = re.sub(r"\.jsonl$", "", src.rstrip("/").split("/")[-1])
    return build_golden(raw, manifest, stem=stem, source=src, manifest_source=m_src if m_raw else "")


def build_golden(raw: bytes, manifest: Mapping[str, Any] | None, *, stem: str, source: str,
                 manifest_source: str = "", origin: Mapping[str, Any] | None = None) -> GoldenSet:
    """A validated GoldenSet from golden JSONL bytes + manifest dict (file loader and Sheet loader share it)."""
    items = [json.loads(ln) for ln in raw.decode("utf-8-sig").splitlines() if ln.strip()]
    manifest = dict(manifest or {})
    _apply_overrides(items, manifest)
    validate_items(items)
    gs = GoldenSet(
        source=source, stem=stem, items=items, golden_sha256=ruler_golden_sha256(raw),
        manifest=manifest, manifest_sha256=ruler_manifest_sha256(manifest),
        golden_sha256_full=canonical_golden_sha256(raw), manifest_sha256_full=canonical_manifest_sha256(manifest),
        manifest_source=manifest_source, raw=raw, origin=dict(origin or {}),
    )
    gs.stable_baseline()  # validates manifest ids
    return gs


def materialize_golden(golden_uri: str, dest_dir: Path | None = None) -> Path:
    """Downloads a gs:// golden (+ sidecar manifest if present) to a local dir; local paths pass through."""
    if not str(golden_uri).startswith("gs://"):
        return Path(golden_uri)
    dest = Path(dest_dir or tempfile.mkdtemp(prefix="golden_"))
    dest.mkdir(parents=True, exist_ok=True)
    name = golden_uri.rstrip("/").split("/")[-1]
    raw = _read_bytes(golden_uri)
    if raw is None:
        raise FileNotFoundError(f"黄金集文件不存在: {golden_uri}")
    (dest / name).write_bytes(raw)
    m_raw = _read_bytes(manifest_path_for(golden_uri))
    if m_raw is not None:
        (dest / manifest_path_for(name)).write_bytes(m_raw)
    logger.info("Golden set %s downloaded to %s (manifest: %s)", golden_uri, dest, m_raw is not None)
    return dest / name


def _natural_key(name: str) -> list[Any]:
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


def derive_folder_specs(golden: GoldenSet) -> list[dict[str, Any]]:
    """Evaluation folders + clips: manifest ``folders`` if configured, else derived from golden items.

    Raises when a labelled clip would not be evaluated (it could never be scored)."""
    specs: list[dict[str, Any]] = []
    if golden.manifest.get("folders"):
        for f in golden.manifest["folders"]:
            videos = [dict(v) for v in f.get("videos") or []]
            if not f.get("group_id") or not videos:
                raise GoldenSetError(f"manifest folder {f.get('label')!r} 需要 group_id 和至少 1 个 video")
            specs.append({
                "group_id": str(f["group_id"]),
                "folder_id": str(f.get("folder_id") or ""),
                "label": str(f.get("label") or f["group_id"]),
                "cache_job_ids": [str(x) for x in f.get("cache_job_ids") or []],
                "videos": videos,
            })
    else:
        groups: dict[str, dict[str, Any]] = {}
        for it in golden.items:
            key = str(it.get("drive_folder_id") or f"{it['outlet_name']} | {it['focus']}")
            g = groups.setdefault(key, {
                "group_id": "g" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:6],
                "folder_id": str(it.get("drive_folder_id") or ""),
                "label": key, "cache_job_ids": [], "videos": {},
            })
            for fid, fn in zip(it["video_file_ids"], it["video_filenames"]):
                g["videos"].setdefault(str(fid), {"file_id": str(fid), "filename": str(fn)})
        for g in groups.values():
            g["videos"] = sorted(g["videos"].values(), key=lambda v: _natural_key(v["filename"]))
            specs.append(g)
    ids = [s["group_id"] for s in specs]
    if len(set(ids)) != len(ids):
        raise GoldenSetError(f"folder group_id 重复: {ids}")
    evaluated = {str(v["file_id"]) for s in specs for v in s["videos"]}
    labelled = {str(fid) for it in golden.items for fid in it["video_file_ids"]}
    missing = sorted(labelled - evaluated)
    if missing:
        raise GoldenSetError(f"黄金集标注的视频不在评测文件夹清单中（无法计分）: {missing}")
    return specs
