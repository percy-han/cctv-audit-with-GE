"""Offline end-to-end test of eval/run_gcp_round.py (all cloud calls faked).

Why: the first live Cloud Build runs of r01 failed on bugs no unit test touched
(import of a non-existent ``score_run``, reading ``preflight`` instead of
``preflight_report``, writing a job shape the scorer cannot read). This test
drives ``run_round_async`` from folder specs to ledger update, and checks that
replaying the frozen v6 baseline findings through the runner yields exactly the
findings the r00 baseline was scored on (shape parity with production jobs).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import tempfile
import unittest
from functools import partial
from pathlib import Path
from unittest import mock

from cctv_audit.agentic_auditor import Finding, TokenLedgerRow, WindowResult
from eval import run_gcp_round as rgr
from eval import score_run as sr

THIS_DIR = Path(__file__).resolve().parent.parent
BASELINE = THIS_DIR / "data" / "runs" / "v6_0928_0811"


def _expected_clip_count() -> int:
    manifest = json.loads((THIS_DIR / "data" / "golden_v1.manifest.json").read_text(encoding="utf-8"))
    return sum(len(f["videos"]) for f in manifest["folders"])


def _baseline_jobs() -> list[dict]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(BASELINE.glob("job_*.json"))]


def _baseline_segments_by_file() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for job in _baseline_jobs():
        for seg in job["completed_segments"].values():
            out[seg["file_id"]] = seg
    return out


class FakeAuditor:
    """Replays the frozen v6 per-segment findings for each clip."""

    def __init__(self, *_, **__):
        self.segs = _baseline_segments_by_file()
        self.calls: list[str] = []

    async def analyze_segment(self, *, segment, **_):
        self.calls.append(segment.source_file_id)
        seg = self.segs.get(segment.source_file_id) or {"findings": [], "ledger_row": None}
        findings = [Finding.model_validate(f) for f in seg["findings"]]
        ledger = seg.get("ledger_row")
        row = TokenLedgerRow.model_validate(ledger) if ledger else mock.MagicMock(
            model_dump=lambda mode="json": {})
        return WindowResult(calibrated_wall_clock_start="", people=[], findings=findings,
                            carryover_state_summary="c"), row


async def _fake_slice(*, video_dict, **_):
    return rgr.VideoSliceSegment(
        source_file_id=str(video_dict["file_id"]), source_filename=str(video_dict["filename"]),
        segment_index=0, start_offset_sec=0.0, end_offset_sec=300.0, local_path=None,
        gcs_uri=f"gs://fake/{video_dict['file_id']}.mp4", width=1920, height=1080)


def _fake_judge(cases):
    # Hit every case with its first cited candidate id.
    out = []
    for c in cases:
        fid = c["response"].split("]")[0].lstrip("[")
        out.append((1.0, f"MATCH={fid}; fake"))
    return out


class RunRoundOfflineTest(unittest.TestCase):
    def test_frozen_specs_cover_all_16_clips(self):
        specs = rgr.load_folder_specs()
        manifest = json.loads((THIS_DIR / "data" / "golden_v1.manifest.json").read_text(encoding="utf-8"))
        self.assertEqual([s["baseline_job_id"] for s in specs], [f["group_id"] for f in manifest["folders"]])
        self.assertEqual(sum(len(s["videos"]) for s in specs), _expected_clip_count())

    def test_end_to_end_replay_matches_baseline_findings(self):
        tmp = Path(tempfile.mkdtemp(prefix="rgr_test_"))
        run_id = "test_offline_replay"
        run_data_dir = rgr.THIS_DIR / "data" / "runs" / run_id
        try:
            rounds = tmp / "rounds"
            shutil.copytree(THIS_DIR / "rounds", rounds)
            args = argparse.Namespace(
                round="r01", run_id=run_id, rounds_dir=rounds, results_dir=tmp / "results",
                golden=THIS_DIR / "data" / "golden_v1.jsonl", judge_model=None,
                folder_concurrency=2, sync_sop_tab=False, skip_gcs_sync=True, ckpt_local_dir=tmp / "ckpt")
            active = mock.MagicMock(active_model_version="m-active", fallback_model_version="m-fb",
                                    model_fallback_warning=None)
            pm = mock.MagicMock()
            pm.load_active_config = mock.AsyncMock(return_value=active)
            fake_auditor = FakeAuditor()
            env = {"GCP_PROJECT": "test-project", "STAGING_BUCKET": "test-bucket"}
            with mock.patch.dict("os.environ", env), \
                    mock.patch.object(rgr, "config", rgr.AuditConfig(gcp_project="test-project",
                                                                     staging_bucket="test-bucket")), \
                    mock.patch.object(rgr, "AgenticAuditor", return_value=fake_auditor), \
                    mock.patch.object(rgr, "resolve_or_ingest_video_slice", _fake_slice), \
                    mock.patch.object(rgr, "GoogleSheetsConfigClient"), \
                    mock.patch.object(rgr, "PromptManager", return_value=pm), \
                    mock.patch.object(rgr, "GoogleWorkspaceGateway"), \
                    mock.patch.object(rgr, "score_run", partial(sr.score_run, judge_fn=_fake_judge)):
                rc = asyncio.run(rgr.run_round_async(args))
            self.assertEqual(rc, 0)
            self.assertEqual(len(fake_auditor.calls), _expected_clip_count())

            # Shape parity: the runner's job docs flatten to the same findings as the baseline.
            emitted = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(run_data_dir.glob("job_*.json"))]
            got = sr.flatten_findings(emitted)
            want = sr.flatten_findings(_baseline_jobs())
            key = lambda f: (f.filename, f.rule_id, f.osd, f.evidence)
            self.assertEqual(len(got), len(want))
            self.assertEqual(sorted(map(key, got)), sorted(map(key, want)))

            # Scored and recorded into the round ledger.
            score = json.loads((tmp / "results" / run_id / "score.json").read_text(encoding="utf-8"))
            self.assertNotIn("_sdk_result", score)
            # The exact golden used is snapshotted next to the results and reloads to the same version.
            from eval.golden_set import load_golden
            snap = tmp / "results" / run_id / "golden_snapshot" / "golden_v1.jsonl"
            self.assertEqual(load_golden(snap).version, score["golden"]["golden_version"])
            self.assertTrue((snap.parent / "golden_v1.manifest.json").exists())
            self.assertEqual(score["recall"]["all"]["rows"], 19)
            manifest = json.loads((rounds / "r01" / "manifest.json").read_text(encoding="utf-8"))
            self.assertIn(run_id, [r["run_id"] for r in manifest["runs"]])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
            shutil.rmtree(run_data_dir, ignore_errors=True)


class ConfigGuardTest(unittest.TestCase):
    def _args(self, tmp):
        return argparse.Namespace(round="r01", run_id="x", rounds_dir=THIS_DIR / "rounds",
                                  results_dir=Path(tmp), golden=THIS_DIR / "data" / "golden_v1.jsonl",
                                  judge_model=None, folder_concurrency=1, sync_sop_tab=False,
                                  skip_gcs_sync=True, ckpt_local_dir=None)

    def test_refuses_code_default_project(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict("os.environ", {}, clear=False) as env:
            env.pop("GCP_PROJECT", None); env.pop("GOOGLE_CLOUD_PROJECT", None)
            with mock.patch.object(rgr, "config", rgr.AuditConfig()):
                with self.assertRaisesRegex(RuntimeError, "GCP_PROJECT is not set"):
                    asyncio.run(rgr.run_round_async(self._args(tmp)))

    def test_refuses_import_time_config_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict("os.environ", {"GCP_PROJECT": "run-project", "STAGING_BUCKET": "b"}):
            with mock.patch.object(rgr, "config", rgr.AuditConfig(gcp_project="other", staging_bucket="b")):
                with self.assertRaisesRegex(RuntimeError, "cctv_audit.config was built"):
                    asyncio.run(rgr.run_round_async(self._args(tmp)))


class _FlakyAuditor(FakeAuditor):
    """Fails the first call for each clip in ``fail_once`` with a bare TimeoutError (the live
    ac14a9e8 failure: empty message, so cctv_audit.gcp's string-matching retry skipped it)."""

    def __init__(self, fail_once=(), forbid=()):
        super().__init__()
        self.fail_once, self.forbid = set(fail_once), set(forbid)

    async def analyze_segment(self, *, segment, **kw):
        fid = segment.source_file_id
        if fid in self.forbid:
            raise AssertionError(f"checkpointed clip {fid} was re-analysed")
        if fid in self.fail_once:
            self.fail_once.discard(fid)
            self.calls.append(fid)
            raise TimeoutError()
        return await super().analyze_segment(segment=segment, **kw)


def _one_folder():
    return next(s for s in rgr.load_folder_specs() if len(s["videos"]) >= 3)


def _run_folder(auditor, ckpt):
    active = mock.MagicMock(active_model_version="m", fallback_model_version="m",
                            model_fallback_warning=None)
    prompt_cfg = mock.MagicMock(model_dump=lambda mode="json": {})
    with mock.patch.object(rgr, "resolve_or_ingest_video_slice", _fake_slice):
        return asyncio.run(rgr.evaluate_single_folder(
            folder_spec=_one_folder(), run_id="t", cfg=rgr.AuditConfig(gcp_project="p", staging_bucket="b"),
            auditor=auditor, prompt_cfg=prompt_cfg, ingestor=None, ckpt=ckpt))


class ClipRetryAndResumeTest(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(rgr, "CLIP_RETRY_BASE_SEC", 0.0)
        p.start()
        self.addCleanup(p.stop)

    def test_bare_timeout_is_retried(self):
        vids = [v["file_id"] for v in _one_folder()["videos"]]
        aud = _FlakyAuditor(fail_once={vids[1]})
        doc = _run_folder(aud, ckpt=None)
        self.assertEqual(aud.calls.count(vids[1]), 2)
        self.assertEqual(len(doc["completed_segments"]), len(vids))

    def test_gives_up_after_max_attempts(self):
        vids = [v["file_id"] for v in _one_folder()["videos"]]

        class AlwaysFail(FakeAuditor):
            async def analyze_segment(self, *, segment, **_):
                self.calls.append(segment.source_file_id)
                raise TimeoutError()

        aud = AlwaysFail()
        with self.assertRaises(TimeoutError):
            _run_folder(aud, ckpt=None)
        self.assertEqual(aud.calls, [vids[0]] * rgr.CLIP_MAX_ATTEMPTS)

    def test_resume_skips_checkpointed_clips_and_matches(self):
        vids = [v["file_id"] for v in _one_folder()["videos"]]
        with tempfile.TemporaryDirectory() as d:
            ckpt = rgr.ClipCheckpointStore(run_id="t", bucket_name=None, project=None, local_dir=Path(d))
            full = _run_folder(FakeAuditor(), ckpt=None)

            # First attempt dies on the last clip after exhausting retries; earlier clips are saved.
            class DieOnLast(FakeAuditor):
                async def analyze_segment(self, *, segment, **kw):
                    if segment.source_file_id == vids[-1]:
                        raise TimeoutError()
                    return await super().analyze_segment(segment=segment, **kw)

            with self.assertRaises(TimeoutError):
                _run_folder(DieOnLast(), ckpt=ckpt)
            saved = sorted(p.stem for p in Path(d).rglob("*.json"))
            self.assertEqual(saved, sorted(vids[:-1]))

            # Resume: done clips must not be re-called; result equals an uninterrupted run.
            aud = _FlakyAuditor(forbid=set(vids[:-1]))
            resumed = _run_folder(aud, ckpt=ckpt)
            self.assertEqual(aud.calls, [vids[-1]])
            self.assertEqual(resumed["completed_segments"], full["completed_segments"])
            self.assertEqual(resumed["window_results"], full["window_results"])


class ScoreRunApiTest(unittest.TestCase):
    def test_rejects_even_passes_and_empty_dir(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):
                sr.score_run(run_dir=d, golden_path="x", project="p", run_label="l", judge_passes=2)
            with self.assertRaises(FileNotFoundError):
                sr.score_run(run_dir=d, golden_path="x", project="p", run_label="l", judge_fn=_fake_judge)


if __name__ == "__main__":
    unittest.main()
