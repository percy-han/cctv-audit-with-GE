"""Unit tests for eval/run_visibility_probe.py ("能不能看见" Oracle Visibility Probe)."""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cctv_audit.agentic_auditor import Finding, TokenLedgerRow, WindowResult
from eval import run_gcp_round as rgr
from eval import run_visibility_probe as rvp

THIS_DIR = Path(__file__).resolve().parent.parent


class ProbeSpecsTest(unittest.TestCase):
    def test_probe_specs_cover_all_target_items_and_valid_files(self) -> None:
        frozen_specs = rgr.load_folder_specs()
        known_file_ids = {
            str(v["file_id"]) for fspec in frozen_specs for v in fspec["videos"]
        }
        items_by_role: dict[str, set[str]] = {
            "dev_miss": set(),
            "holdout_miss": set(),
            "positive_control": set(),
        }
        for spec in rvp.PROBE_SPECS:
            self.assertIn(spec.video_file_id, known_file_ids, spec.probe_id)
            self.assertGreaterEqual(spec.start_sec, 0.0, spec.probe_id)
            self.assertLess(spec.start_sec, spec.end_sec, spec.probe_id)
            self.assertLessEqual(spec.end_sec, 305.0, spec.probe_id)
            dur = spec.end_sec - spec.start_sec
            self.assertGreaterEqual(dur, 50.0, spec.probe_id)
            self.assertLessEqual(dur, 75.0, spec.probe_id)
            items_by_role[spec.role].update(spec.item_ids)
            cfg = rvp.build_focused_prompt_config(
                rounds_dir=THIS_DIR / "rounds",
                rule_ids=spec.rule_ids,
                model_version="gemini-test-flash",
                probe_id=spec.probe_id,
            )
            self.assertEqual({r.rule_id for r in cfg.rules}, set(spec.rule_ids))

        self.assertEqual(
            items_by_role["dev_miss"], {"R02", "R03", "R04", "R05", "R06", "R20"}
        )
        self.assertEqual(items_by_role["holdout_miss"], {"R07", "R10", "R16", "R17"})
        self.assertEqual(items_by_role["positive_control"], {"R08", "R19"})

    def test_classify_item_visibility_taxonomy(self) -> None:
        code, _ = rvp.classify_item_visibility(
            flash_sop_score=0.0,
            flash_diary_score=0.5,
            pro_sop_score=1.0,
            pro_diary_score=1.0,
        )
        self.assertEqual(code, "A1_FLASH_CROPPED_RECOVERABLE")

        code, _ = rvp.classify_item_visibility(
            flash_sop_score=0.0,
            flash_diary_score=0.0,
            pro_sop_score=0.0,
            pro_diary_score=1.0,
        )
        self.assertEqual(code, "A2_PRO_ESCALATION_RECOVERABLE")

        code, _ = rvp.classify_item_visibility(
            flash_sop_score=0.0,
            flash_diary_score=0.0,
            pro_sop_score=0.0,
            pro_diary_score=0.0,
        )
        self.assertEqual(code, "B_VISUAL_CEILING")

    def test_pro_model_disables_agentic_media_processing_while_flash_enables_it(self) -> None:
        seg = rvp.VideoSliceSegment(
            source_file_id="f1",
            source_filename="f1.mp4",
            segment_index=0,
            start_offset_sec=0.0,
            end_offset_sec=60.0,
            local_path=None,
            gcs_uri="gs://bucket/f1.mp4",
            width=1920,
            height=1080,
        )
        flash_part = rvp.build_video_part(seg, "gemini-3.8-flash")
        pro_part = rvp.build_video_part(seg, "gemini-3.1-pro-preview")
        self.assertIsNotNone(getattr(flash_part, "media_processing", None))
        self.assertIsNone(getattr(pro_part, "media_processing", None))


class SubclipOsdScoringTest(unittest.TestCase):
    def setUp(self) -> None:
        self.golden_items = [
            json.loads(line)
            for line in (THIS_DIR / "data" / "golden_v1.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        ]

    def test_subclip_relative_mmss_maps_to_true_osd_via_zero_start_offset(self) -> None:
        # P03_R04_R05_hw_c_f4 starts at clip 110s -> sub-clip start OSD is 12:06:51.
        # At sub-clip offset 29s (00:29), true OSD is 12:07:20 (matching R05).
        spec = next(s for s in rvp.PROBE_SPECS if s.probe_id == "P03_R04_R05_hw_c_f4")
        f = (
            Finding(
                rule_id="A3",
                disposition="CONFIRMED",
                severity="RED_LINE",
                timestamp_in_clip="00:29",
                on_screen_clock="12:07:20",
                evidence="00:29 员工干手直接先按压洗手液，未先湿手",
                confidence=0.95,
            )
            .sanitise()
            .with_segment_context(segment_index=0, start_offset_sec=0.0)
        )

        scored = rvp.score_focused_sop_findings(
            golden_items=self.golden_items,
            spec=spec,
            findings_json=[f.model_dump(mode="json")],
            judge_fn=lambda cases: [
                (1.0, f"MATCH={c['response'].split(']')[0].lstrip('[')}; ok")
                for c in cases
            ],
        )
        self.assertEqual(scored["R05"]["score"], 1.0)
        self.assertEqual(scored["R04"]["score"], 1.0)

    def test_r20_footage3_target_osd_passes_prefilter(self) -> None:
        # R20 in golden_v1 only has osd_times=['17:43:40'], which is in Footage 2.
        # In Footage 3 (P06_R20_ice_b_f3), the white cover is brushed at 17:45:18 (+98s).
        spec = next(s for s in rvp.PROBE_SPECS if s.probe_id == "P06_R20_ice_b_f3")
        f = (
            Finding(
                rule_id="B3",
                disposition="CONFIRMED",
                severity="RED_LINE",
                timestamp_in_clip="00:20",
                on_screen_clock="17:45:18",
                evidence="17:45:18 使用黄色清洁剂瓶配滚筒刷清洁制冰机白盖",
                confidence=0.95,
            )
            .sanitise()
            .with_segment_context(segment_index=0, start_offset_sec=0.0)
        )

        scored = rvp.score_focused_sop_findings(
            golden_items=self.golden_items,
            spec=spec,
            findings_json=[f.model_dump(mode="json")],
            judge_fn=lambda cases: [
                (1.0, f"MATCH={c['response'].split(']')[0].lstrip('[')}; ok")
                for c in cases
            ],
        )
        self.assertEqual(scored["R20"]["score"], 1.0)


class RunProbeOfflineAndResumeTest(unittest.TestCase):
    def test_end_to_end_offline_and_checkpoint_resume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            probe_ids = [
                "P01_R02_hw_c_f2",
                "P05_R20_ice_b_f2",
                "P06_R20_ice_b_f3",
                "P11_CTRL_R08_hw_b_f2",
            ]
            args = argparse.Namespace(
                probe_run_id="vprobe_unit_test",
                rounds_dir=THIS_DIR / "rounds",
                golden=THIS_DIR / "data" / "golden_v1.jsonl",
                out_dir=tmp / "out",
                flash_model="gemini-test-flash",
                pro_model="gemini-test-pro",
                judge_model="gemini-test-pro",
                judge_passes=1,
                concurrency=2,
                probe_ids=probe_ids,
                ckpt_local_dir=tmp / "ckpt",
                skip_gcs_sync=True,
            )

            auditor_calls: list[str] = []
            diary_calls: list[str] = []

            class FakeProbeAuditor:
                def __init__(self, *_: object, **__: object) -> None:
                    pass

                async def analyze_segment(
                    self, *, segment: rvp.VideoSliceSegment, prompt_cfg: object, **_: object
                ) -> tuple[WindowResult, TokenLedgerRow]:
                    model_ver = getattr(prompt_cfg, "active_model_version", "")
                    auditor_calls.append(f"{segment.gcs_uri}:{model_ver}")
                    # Emit a finding only for P11_CTRL_R08_hw_b_f2
                    findings: list[Finding] = []
                    if "P11_CTRL_R08_hw_b_f2" in (segment.gcs_uri or ""):
                        findings.append(
                            Finding(
                                rule_id="A2",
                                disposition="CONFIRMED",
                                severity="RED_LINE",
                                timestamp_in_clip="00:30",
                                on_screen_clock="08:05:30",
                                evidence="08:05:30 搓手时间仅10秒不足20秒",
                                confidence=0.95,
                            )
                            .sanitise()
                            .with_segment_context(0, 0.0)
                        )
                    row = mock.MagicMock(model_dump=lambda mode="json": {})
                    return (
                        WindowResult(
                            calibrated_wall_clock_start="",
                            people=[],
                            findings=findings,
                            carryover_state_summary="",
                        ),
                        row,
                    )

            async def _fake_diary(
                *, segment: rvp.VideoSliceSegment, model_version: str, label: str
            ) -> str:
                diary_calls.append(label)
                if "P06_R20_ice_b_f3" in label and model_version == "gemini-test-pro":
                    return "17:45:18 员工拿黄瓶清洁剂刷洗制冰机白色外盖与挡水帘"
                return "08:05:18 员工在水槽旁站立"

            async def _fake_cutter(**kw: object) -> tuple[Path, str]:
                pid = str(kw["probe_id"])
                p = tmp / f"{pid}.mp4"
                p.write_bytes(b"fake")
                return p, f"gs://test-bucket/{pid}.mp4"

            async def _fake_full_slice(*, video_dict: dict[str, object], **_: object) -> rvp.VideoSliceSegment:
                return rvp.VideoSliceSegment(
                    source_file_id=str(video_dict["file_id"]),
                    source_filename=str(video_dict["filename"]),
                    segment_index=0,
                    start_offset_sec=0.0,
                    end_offset_sec=300.0,
                    local_path=None,
                    gcs_uri=f"gs://test-bucket/full/{video_dict['file_id']}.mp4",
                    width=1920,
                    height=1080,
                )

            def _fake_sop_judge(
                cases: list[dict[str, str]],
            ) -> list[tuple[float | None, str]]:
                out: list[tuple[float | None, str]] = []
                for c in cases:
                    fid = c["response"].split("]")[0].lstrip("[")
                    out.append((1.0, f"MATCH={fid}; hit"))
                return out

            def _fake_diary_judge(
                cases: list[dict[str, str]],
            ) -> list[tuple[float | None, str]]:
                out: list[tuple[float | None, str]] = []
                for c in cases:
                    if "17:45:18" in c["response"]:
                        out.append((1.0, "OBSERVED=CLEARLY_OBSERVED; 看到刷洗制冰机白盖"))
                    else:
                        out.append((0.0, "OBSERVED=NOT_OBSERVED; 未看到"))
                return out

            active_cfg = mock.MagicMock(active_model_version="gemini-test-flash")
            pm = mock.create_autospec(rvp.PromptManager, instance=True)
            pm.load_active_config = mock.AsyncMock(return_value=active_cfg)

            env = {"GCP_PROJECT": "test-project", "STAGING_BUCKET": "test-bucket"}
            with mock.patch.dict("os.environ", env), mock.patch.object(
                rvp,
                "config",
                rvp.AuditConfig(gcp_project="test-project", staging_bucket="test-bucket"),
            ), mock.patch.object(
                rvp, "AgenticAuditor", FakeProbeAuditor
            ), mock.patch.object(
                rvp, "run_neutral_diary_with_retry", _fake_diary
            ), mock.patch.object(
                rvp, "resolve_or_ingest_video_slice", _fake_full_slice
            ), mock.patch.object(
                rvp, "GoogleSheetsConfigClient", autospec=True
            ), mock.patch.object(
                rvp, "PromptManager", autospec=True, return_value=pm
            ):
                rep1 = asyncio.run(
                    rvp.run_probe_async(
                        args,
                        subclip_cutter=_fake_cutter,
                        sop_judge_fn=_fake_sop_judge,
                        diary_judge_fn=_fake_diary_judge,
                    )
                )
                self.assertEqual(len(auditor_calls), 8)  # 4 windows * 2 models
                self.assertEqual(len(diary_calls), 8)

                by_item = {d["item_id"]: d for d in rep1["item_diagnostics"]}
                # R02: all 0 -> B_VISUAL_CEILING
                self.assertEqual(by_item["R02"]["diagnosis_code"], "B_VISUAL_CEILING")
                # R20: P05=0, P06 pro_diary=1.0 -> max across windows = 1.0 -> A2_PRO_ESCALATION_RECOVERABLE
                self.assertEqual(
                    by_item["R20"]["diagnosis_code"], "A2_PRO_ESCALATION_RECOVERABLE"
                )
                # R08 (positive control): flash_sop=1.0 -> A1_FLASH_CROPPED_RECOVERABLE, excluded from summary_counts
                self.assertEqual(
                    by_item["R08"]["diagnosis_code"], "A1_FLASH_CROPPED_RECOVERABLE"
                )
                self.assertEqual(
                    rep1["summary_counts"],
                    {
                        "A1_FLASH_CROPPED_RECOVERABLE": 0,
                        "A2_PRO_ESCALATION_RECOVERABLE": 1,
                        "B_VISUAL_CEILING": 1,
                    },
                )

                # Re-running with the same ckpt_local_dir must make zero new model calls
                auditor_calls.clear()
                diary_calls.clear()
                rep2 = asyncio.run(
                    rvp.run_probe_async(
                        args,
                        subclip_cutter=_fake_cutter,
                        sop_judge_fn=_fake_sop_judge,
                        diary_judge_fn=_fake_diary_judge,
                    )
                )
                self.assertEqual(len(auditor_calls), 0)
                self.assertEqual(len(diary_calls), 0)
                self.assertEqual(rep2["summary_counts"], rep1["summary_counts"])

    def test_concurrent_cut_and_upload_subclip_atomic_download_no_race(self) -> None:
        """Regression test for P03 + P04 concurrent download of the same 5-min source MP4."""
        import time

        with tempfile.TemporaryDirectory() as tmp_str:
            work_dir = Path(tmp_str)
            dl_calls: list[str] = []
            seen_input_bytes: list[bytes] = []

            class FakeBlob:
                def __init__(self, name: str) -> None:
                    self.name = name

                def download_to_filename(self, filename: str) -> None:
                    dl_calls.append(self.name)
                    p = Path(filename)
                    # Simulate partial write first (no moov atom yet), then sleep, then complete file
                    p.write_bytes(b"PARTIAL_NO_MOOV")
                    time.sleep(0.05)
                    p.write_bytes(b"COMPLETE_VALID_MP4_WITH_MOOV_ATOM")

                def upload_from_filename(self, filename: str, content_type: str = "") -> None:
                    pass

            class FakeBucket:
                def blob(self, name: str) -> FakeBlob:
                    return FakeBlob(name)

            class FakeStorageClient:
                def __init__(self, project: str = "") -> None:
                    pass

                def bucket(self, name: str) -> FakeBucket:
                    return FakeBucket()

            async def _fake_exec(*cmd: str, **_: object) -> object:
                # Inspect the -i <local_src> argument when ffmpeg is invoked
                idx = list(cmd).index("-i")
                src_path = Path(cmd[idx + 1])
                seen_input_bytes.append(src_path.read_bytes())
                out_path = Path(cmd[-1])
                out_path.write_bytes(b"SUBCLIP_MP4")
                proc = mock.MagicMock()
                proc.returncode = 0
                proc.communicate = mock.AsyncMock(return_value=(b"", b""))
                return proc

            async def _run_both() -> None:
                await asyncio.gather(
                    rvp.cut_and_upload_subclip(
                        source_gcs_uri="gs://b/jobs/media/8c43ab/1wWjhOh0lm3aa4rOF0H1byAudzmh8e0NI/seg_0.mp4",
                        start_sec=110.0,
                        end_sec=175.0,
                        probe_id="P03_R04_R05_hw_c_f4",
                        probe_run_id="vprobe_01",
                        bucket_name="b",
                        project_id="p",
                        work_dir=work_dir,
                    ),
                    rvp.cut_and_upload_subclip(
                        source_gcs_uri="gs://b/jobs/media/8c43ab/1wWjhOh0lm3aa4rOF0H1byAudzmh8e0NI/seg_0.mp4",
                        start_sec=235.0,
                        end_sec=301.0,
                        probe_id="P04_R06_hw_c_f4",
                        probe_run_id="vprobe_01",
                        bucket_name="b",
                        project_id="p",
                        work_dir=work_dir,
                    ),
                )

            with mock.patch("google.cloud.storage.Client", FakeStorageClient), mock.patch.object(
                rvp.shutil, "which", return_value="/usr/bin/ffmpeg"
            ), mock.patch.object(rvp.asyncio, "create_subprocess_exec", side_effect=_fake_exec):
                asyncio.run(_run_both())

            # Source MP4 downloaded exactly once and both FFmpeg invocations saw the complete file
            self.assertEqual(len(dl_calls), 1)
            self.assertEqual(
                seen_input_bytes,
                [b"COMPLETE_VALID_MP4_WITH_MOOV_ATOM", b"COMPLETE_VALID_MP4_WITH_MOOV_ATOM"],
            )


if __name__ == "__main__":
    unittest.main()

