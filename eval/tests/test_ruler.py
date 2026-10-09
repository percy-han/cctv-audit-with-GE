"""Unit tests for the eval ruler's deterministic parts (no network)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import build_dataset as bd  # noqa: E402
import score_run as sr  # noqa: E402

HEADER = ["Focus", "Outlet", "Outlet Name", "Audit Clause", "Findings", "对应的视频文件名称"]


class BuildDatasetTest(unittest.TestCase):
    def test_parse_osd_times(self):
        self.assertEqual(bd.parse_osd_times("Footage 5: 121036 & 121100 x"), ["12:10:36", "12:11:00"])
        self.assertEqual(bd.parse_osd_times("Footage 5: apron"), [])
        self.assertEqual(bd.parse_osd_times("20260910165815394 991234"), [])

    def test_forward_fill_split_and_compound(self):
        raw = [
            HEADER,
            ["Handwashing Monitoring", "1", "Cantavil D2", "1.5 HW", "Footage 2: 080518 a", "a.mov"],
            ["", "2", "Bau Cat", "", "Footage 5: 121036 & 121100 b", "b.mov"],
            ["Ice Maker Weekly Cleaning", "2", "Bau Cat", "Red", "Footage 2&3: 174340 c", "c.mov,d.mov"],
        ]
        idx = {"a.mov": "A", "b.mov": "B", "c.mov": "C", "d.mov": "D"}
        items = bd.build_items(raw, idx)
        self.assertEqual([i.item_id for i in items], ["R02", "R03", "R04"])
        self.assertEqual(items[1].focus, "Handwashing Monitoring")
        self.assertEqual(items[1].audit_clause, "1.5 HW")
        self.assertEqual([i.split for i in items], ["dev", "holdout", "dev"])
        self.assertEqual([p.part_id for p in items[1].parts], ["R03a", "R03b"])
        self.assertEqual(items[2].video_file_ids, ["C", "D"])

    def test_missing_video_fails_loudly(self):
        raw = [HEADER, ["Handwashing Monitoring", "1", "Cantavil D2", "x", "Footage 2: 080518 a", "zz.mov"]]
        with self.assertRaises(ValueError):
            bd.build_items(raw, {"a.mov": "A"})


class ScoreRunTest(unittest.TestCase):
    def _finding(self, **kw):
        base = {"status": "VIOLATION", "rule_id": "A3", "on_screen_clock": "12:07:10",
                "global_offset_sec": 430.0, "evidence": "x"}
        base.update(kw)
        return base

    def test_osd_times_from_clip_relative_evidence(self):
        f = self._finding(evidence="员工 07:10~07:40 洗手，OSD 12:08:00 离开")
        times = sr.finding_osd_times(f)
        start = 12 * 3600 + 7 * 60 + 10 - 430  # clip start OSD
        self.assertIn(start + 7 * 60 + 40, times)
        self.assertIn(12 * 3600 + 8 * 60, times)

    def test_time_window_and_span(self):
        t = 12 * 3600
        self.assertTrue(sr.time_matches(t, [t + 60]))
        self.assertFalse(sr.time_matches(t, [t + 61]))
        self.assertTrue(sr.time_matches(t, [t - 120, t + 120]))
        self.assertFalse(sr.time_matches(t, [t - 400, t + 400]))

    def test_prefilter_same_video_only(self):
        job = {"job_id": "j", "completed_segments": {
            "a": {"filename": "a.mov", "segment_index": 0, "findings": [self._finding()]},
            "b": {"filename": "b.mov", "segment_index": 0, "findings": [self._finding()]},
        }}
        fs = sr.flatten_findings([job])
        part = {"osd_times": ["12:07:23"]}
        self.assertEqual([f.filename for f in sr.prefilter(part, ["a.mov"], fs)], ["a.mov"])
        self.assertEqual(len(sr.prefilter({"osd_times": []}, ["a.mov"], fs)), 1)

    def test_parse_match_and_judge_pick(self):
        self.assertEqual(sr.parse_match("MATCH=F003, F010; 理由"), ["F003", "F010"])
        self.assertEqual(sr.parse_match("MATCH=NONE; 没有"), [])
        self.assertEqual(sr.parse_match("MATCH=[F019], [F020]; x"), ["F019", "F020"])
        self.assertEqual(sr.parse_match("MATCH=[F005]; x"), ["F005"])
        self.assertEqual(sr.pick_judge_model(
            ["gemini-2.5-pro", "gemini-3.1-pro-preview", "gemini-3.8-flash", "gemini-3-pro-image"]),
            "gemini-3.1-pro-preview")

    def test_compound_row_scores_half(self):
        items = [{"item_id": "R11", "sheet_row": 11, "split": "holdout", "focus": "Handwashing Monitoring",
                  "outlet_name": "Bau Cat", "audit_clause": "x", "finding_verbatim": "v",
                  "video_filenames": ["a.mov"],
                  "parts": [{"part_id": "R11a", "description": "d", "osd_times": ["12:07:10"]},
                            {"part_id": "R11b", "description": "d", "osd_times": ["12:07:30"]}]}]
        job = {"job_id": "j", "completed_segments": {
            "a": {"filename": "a.mov", "segment_index": 0, "findings": [self._finding()]}}}
        fs = sr.flatten_findings([job])
        rep = sr.score(items, fs, lambda cases: [(1.0, "MATCH=F001; ok")] * len(cases))
        self.assertEqual(rep["items"][0]["score"], 0.5)
        self.assertEqual(rep["recall"]["holdout"]["points"], 0.5)

    def test_one_finding_cannot_score_two_time_parts(self):
        items = [{"item_id": "R11", "sheet_row": 11, "split": "holdout", "focus": "Handwashing Monitoring",
                  "outlet_name": "Bau Cat", "audit_clause": "x", "finding_verbatim": "v",
                  "video_filenames": ["a.mov"],
                  "parts": [{"part_id": "R11a", "description": "d", "osd_times": ["12:06:40"]},
                            {"part_id": "R11b", "description": "d", "osd_times": ["12:07:05"]}]}]
        # F001 @12:07:10 is nearer R11b; F002 @12:06:30 is nearer R11a.
        job = {"job_id": "j", "completed_segments": {
            "a": {"filename": "a.mov", "segment_index": 0, "findings": [
                self._finding(evidence="x"),
                self._finding(on_screen_clock="12:06:30", global_offset_sec=390.0, evidence="y")]}}}
        seen = {}

        def judge(cases):
            for c in cases:
                seen[c["case_id"]] = c["response"]
            return [(1.0, "MATCH=F002; a") if c["case_id"] == "R11a" else (1.0, "MATCH=F001; b")
                    for c in cases]

        rep = sr.score(items, sr.flatten_findings([job]), judge)
        self.assertNotIn("[F001]", seen["R11a"])
        self.assertNotIn("[F002]", seen["R11b"])
        self.assertEqual(rep["items"][0]["score"], 1.0)

    def test_reassigned_finding_leaves_part_empty(self):
        items = [{"item_id": "R11", "sheet_row": 11, "split": "holdout", "focus": "f",
                  "outlet_name": "o", "audit_clause": "x", "finding_verbatim": "v",
                  "video_filenames": ["a.mov"],
                  "parts": [{"part_id": "R11a", "description": "d", "osd_times": ["12:06:40"]},
                            {"part_id": "R11b", "description": "d", "osd_times": ["12:07:05"]}]}]
        job = {"job_id": "j", "completed_segments": {
            "a": {"filename": "a.mov", "segment_index": 0, "findings": [self._finding()]}}}
        rep = sr.score(items, sr.flatten_findings([job]), lambda cases: [(1.0, "MATCH=F001; b")])
        parts = {p["part_id"]: p for p in rep["items"][0]["parts"]}
        self.assertEqual(parts["R11a"]["candidate_ids"], [])
        self.assertEqual(parts["R11a"]["score"], 0.0)
        self.assertEqual(rep["items"][0]["score"], 0.5)

    def test_midnight_wrap(self):
        self.assertEqual(sr.ring_gap(23 * 3600 + 59 * 60 + 58, 15), 17)
        self.assertTrue(sr.time_matches(15, [23 * 3600 + 59 * 60 + 58]))

    def test_numbered_model_builds(self):
        self.assertEqual(sr.pick_judge_model(["gemini-1.5-pro-001", "gemini-1.5-pro-002"]),
                         "gemini-1.5-pro-002")
        self.assertEqual(sr.pick_judge_model(["gemini-3.8-flash", "gemini-3.7-flash"]),
                         "gemini-3.8-flash")

    def test_vote_median(self):
        out = sr.vote([[(0.0, "MATCH=NONE; a")], [(0.5, "MATCH=F1; b")], [(0.0, "MATCH=NONE; c")]])
        self.assertEqual(out[0][0], 0.0)
        self.assertIn("取中位", out[0][1])
        self.assertEqual(sr.vote([[(1.0, "x")]]), [(1.0, "x")])

    def test_off_scale_score_rejected(self):
        items = [{"item_id": "R02", "sheet_row": 2, "split": "dev", "focus": "f", "outlet_name": "o",
                  "audit_clause": "x", "finding_verbatim": "v", "video_filenames": ["a.mov"],
                  "parts": [{"part_id": "R02", "description": "d", "osd_times": ["12:07:10"]}]}]
        job = {"job_id": "j", "completed_segments": {
            "a": {"filename": "a.mov", "segment_index": 0, "findings": [self._finding()]}}}
        with self.assertRaises(RuntimeError):
            sr.score(items, sr.flatten_findings([job]), lambda c: [(0.7, "MATCH=F001; x")])

    def test_point_vs_window_temporal_mode_and_drift(self):
        # 1.5 Handwashing -> POINT (±20s, drift enabled)
        hw_item = {
            "item_id": "R08",
            "sheet_row": 8,
            "split": "holdout",
            "focus": "Handwashing Monitoring",
            "outlet_name": "Cantavil D2",
            "audit_clause": "1.5 Handwashing and Sanitation Standard",
            "finding_verbatim": "Footage 4: 120720 Partner apply soap before wet hand",
            "video_filenames": ["a.mov"],
            "parts": [{"part_id": "R08", "description": "Partner apply soap before wet hand", "osd_times": ["12:07:20"]}],
        }
        # 5.8 Ice Maker -> WINDOW (±60s, drift exempt)
        ice_item = {
            "item_id": "R14",
            "sheet_row": 14,
            "split": "dev",
            "focus": "Ice Maker Weekly Cleaning",
            "outlet_name": "Cantavil D2",
            "audit_clause": "5.8 Ice Maker Routine Cleaning and Maintenance",
            "finding_verbatim": "Footage 1: 221647 Leave chemical for 5 minutes",
            "video_filenames": ["b.mov"],
            "parts": [{"part_id": "R14", "description": "Leave chemical for 5 minutes", "osd_times": ["22:16:47"]}],
        }
        self.assertEqual(sr.classify_temporal_mode(hw_item, hw_item["parts"][0]), "POINT")
        self.assertEqual(sr.part_window_sec("POINT"), 20)
        self.assertEqual(sr.classify_temporal_mode(ice_item, ice_item["parts"][0]), "WINDOW")
        self.assertEqual(sr.part_window_sec("WINDOW"), 60)

        # Finding at +25s from label: must be REJECTED for POINT (±20s) but ACCEPTED for WINDOW (±60s).
        job_25s = {"job_id": "j", "completed_segments": {
            "a": {"filename": "a.mov", "segment_index": 0, "findings": [
                self._finding(rule_id="A3", on_screen_clock="12:07:45", global_offset_sec=465.0, evidence="soap")
            ]},
            "b": {"filename": "b.mov", "segment_index": 0, "findings": [
                self._finding(rule_id="B2", on_screen_clock="22:17:12", global_offset_sec=132.0, evidence="dwell")
            ]},
        }}
        fs_25s = sr.flatten_findings([job_25s])
        self.assertEqual(sr.prefilter(hw_item["parts"][0], ["a.mov"], fs_25s, window_sec=20), [])
        self.assertEqual(len(sr.prefilter(ice_item["parts"][0], ["b.mov"], fs_25s, window_sec=60)), 1)

        # Finding at +6s for POINT (12:07:26 vs 12:07:20) and +25s for WINDOW (22:17:12 vs 22:16:47):
        job_match = {"job_id": "j2", "completed_segments": {
            "a": {"filename": "a.mov", "segment_index": 0, "findings": [
                self._finding(
                    rule_id="A3",
                    violation_disposition="CONFIRMED",
                    on_screen_clock="12:07:26",
                    global_offset_sec=446.0,
                    evidence="soap before wet",
                )
            ]},
            "b": {"filename": "b.mov", "segment_index": 0, "findings": [
                self._finding(
                    rule_id="B2",
                    violation_disposition="SUSPECTED",
                    on_screen_clock="22:17:12",
                    global_offset_sec=132.0,
                    evidence="dwell < 5 min",
                )
            ]},
        }}
        fs_match = sr.flatten_findings([job_match])
        rep = sr.score(
            [hw_item, ice_item],
            fs_match,
            lambda cases: [
                (1.0, "MATCH=F001; ok") if c["case_id"] == "R08" else (1.0, "MATCH=F002; ok")
                for c in cases
            ],
        )
        parts_by_id = {p["part_id"]: p for it in rep["items"] for p in it["parts"]}
        self.assertEqual(parts_by_id["R08"]["temporal_mode"], "POINT")
        self.assertEqual(parts_by_id["R08"]["window_sec"], 20)
        self.assertEqual(parts_by_id["R08"]["timestamp_drift_sec"], 6.0)
        self.assertEqual(parts_by_id["R14"]["temporal_mode"], "WINDOW")
        self.assertEqual(parts_by_id["R14"]["window_sec"], 60)
        self.assertIsNone(parts_by_id["R14"]["timestamp_drift_sec"])

        qm = rep["quality_metrics"]
        self.assertEqual(qm["mean_point_timestamp_drift_sec"], 6.0)
        self.assertEqual(qm["confirmed_only_recall"], 0.5)
        self.assertEqual(qm["hit_rate"], 1.0)
        self.assertEqual(rep["sop_category_recall"]["A_Handwashing"]["recall"], 1.0)
        self.assertEqual(rep["sop_category_recall"]["B_IceMaker"]["recall"], 1.0)
        self.assertEqual(rep["video_breakdown"]["a.mov"]["recall"], 1.0)


class MonitoringPublisherTest(unittest.TestCase):
    def test_build_record_and_timeseries_and_history_dedup(self):
        import json
        import tempfile
        from pathlib import Path
        from unittest import mock
        import monitoring_publisher as mp

        score_doc = {
            "recall": {
                "all": {"points": 15.0, "rows": 19, "recall": 0.7895},
                "holdout": {"points": 7.5, "rows": 9, "recall": 0.8333},
                "dev": {"points": 7.5, "rows": 10, "recall": 0.75},
            },
            "quality_metrics": {
                "confirmed_only_points": 12.0,
                "confirmed_only_recall": 0.6316,
                "matched_findings_count": 16,
                "total_findings_count": 32,
                "hit_rate": 0.5,
                "point_window_sec": 20,
                "window_sec": 60,
                "point_parts_total": 8,
                "point_parts_matched": 6,
                "window_parts_total": 13,
                "mean_point_timestamp_drift_sec": 3.5,
                "max_point_timestamp_drift_sec": 6.0,
            },
            "sop_category_recall": {
                "A_Handwashing": {"points": 9.0, "rows": 11, "recall": 0.8182},
                "B_IceMaker": {"points": 5.0, "rows": 6, "recall": 0.8333},
                "C_TeaBar_Hygiene": {"points": 1.0, "rows": 2, "recall": 0.5},
            },
            "outlet_focus_recall": {
                "Cantavil D2 | Handwashing Monitoring": {"points": 4.0, "rows": 5, "recall": 0.8},
            },
            "video_breakdown": {
                "a.mov": {"points": 2.0, "rows": 2, "recall": 1.0, "findings_count": 3},
            },
            "items": [
                {"item_id": "R02", "score": 1.0},
                {"item_id": "R07", "score": 1.0},
                {"item_id": "R08", "score": 1.0},
            ],
        }
        job_docs = [
            {
                "completed_segments": {"a:0": {}, "b:0": {}},
                "token_ledger": [
                    {"estimated_cost_usd": 0.2, "processing_latency_sec": 350.0, "total_token_count": 200000},
                    {"estimated_cost_usd": 0.3, "processing_latency_sec": 370.0, "total_token_count": 250000},
                ],
            }
        ]
        manifest = {
            "runs": [
                {"run_id": "r01_a", "row_scores": {"R02": 1.0, "R07": 1.0, "R08": 0.0}},
                {"run_id": "r01_b", "row_scores": {"R02": 1.0, "R07": 1.0, "R08": 1.0}},
            ]
        }
        rec = mp.build_eval_monitoring_record(
            project_id="example-project",
            round_id="r01",
            run_id="r01_b",
            model_version="gemini-3.8-flash",
            sop_version="v2.6_r01",
            media_mode="agentic",
            score_doc=score_doc,
            job_docs=job_docs,
            round_manifest=manifest,
            timestamp_iso="2026-10-08T12:00:00Z",
        )
        self.assertEqual(rec["metrics"]["cost_per_clip_usd"], 0.25)
        self.assertEqual(rec["metrics"]["mean_clip_latency_sec"], 360.0)
        self.assertAlmostEqual(rec["metrics"]["flip_rate"], 1 / 3, places=4)

        ts = mp.build_cloud_monitoring_timeseries(rec)
        metric_types = {t["metric"]["type"] for t in ts}
        self.assertIn("custom.googleapis.com/cctv_audit/eval/overall_recall", metric_types)
        self.assertIn("custom.googleapis.com/cctv_audit/eval/confirmed_only_recall", metric_types)
        self.assertIn("custom.googleapis.com/cctv_audit/eval/mean_point_timestamp_drift_sec", metric_types)
        self.assertIn("custom.googleapis.com/cctv_audit/eval/sop_category_recall", metric_types)
        self.assertIn("custom.googleapis.com/cctv_audit/eval/outlet_focus_recall", metric_types)
        self.assertIn("custom.googleapis.com/cctv_audit/eval/video_recall", metric_types)
        self.assertIn("custom.googleapis.com/cctv_audit/eval/video_findings_count", metric_types)

        with tempfile.TemporaryDirectory() as tmp:
            hist = Path(tmp) / "eval_history.jsonl"
            rec_a = {
                **rec,
                "run_id": "r01_a",
                "metrics": {**rec["metrics"], "overall_recall": 0.60, "findings_per_clip": 5.0},
                "video_breakdown": {
                    "a.mov": {"points": 1.0, "rows": 2, "recall": 0.5, "findings_count": 2},
                },
            }
            mp.append_eval_history_jsonl(rec_a, hist)
            mp.append_eval_history_jsonl(rec, hist)
            mp.append_eval_history_jsonl(rec, hist)
            lines = [json.loads(line) for line in hist.read_text(encoding="utf-8").splitlines() if line.strip()]
            self.assertEqual(len(lines), 2)
            self.assertEqual([r["run_id"] for r in lines], ["r01_a", "r01_b"])

            round_avg_path = Path(tmp) / "eval_round_averages.jsonl"
            round_avgs = mp.write_round_averages_jsonl(hist, round_avg_path)
            self.assertEqual(len(round_avgs), 1)
            r_avg = round_avgs[0]
            self.assertEqual(r_avg["round_id"], "r01")
            self.assertEqual(r_avg["runs_count"], 2)
            self.assertEqual(r_avg["run_ids"], ["r01_a", "r01_b"])
            self.assertAlmostEqual(r_avg["metrics"]["overall_recall"], (0.60 + 0.7895) / 2, places=5)
            self.assertAlmostEqual(r_avg["metrics"]["findings_per_clip"], 2.5, places=5)
            self.assertAlmostEqual(r_avg["video_breakdown"]["a.mov"]["findings_count"], 2.5, places=2)

            round_ts = mp.build_round_monitoring_timeseries(r_avg, emit_timestamp="2026-10-09T00:00:00Z")
            round_metric_types = {t["metric"]["type"] for t in round_ts}
            self.assertIn("custom.googleapis.com/cctv_audit/eval_round/overall_recall", round_metric_types)
            self.assertIn("custom.googleapis.com/cctv_audit/eval_round/sop_category_recall", round_metric_types)
            self.assertIn("custom.googleapis.com/cctv_audit/eval_round/outlet_focus_recall", round_metric_types)
            self.assertIn("custom.googleapis.com/cctv_audit/eval_round/video_recall", round_metric_types)
            self.assertIn("custom.googleapis.com/cctv_audit/eval_round/video_findings_count", round_metric_types)
            for t in round_ts:
                self.assertEqual(t["metric"]["labels"]["runs_count"], "2")
                self.assertNotIn("run_id", t["metric"]["labels"])

            exp_round_payload = mp.build_vertex_experiment_run_payload(r_avg, is_round_average=True)
            self.assertEqual(exp_round_payload["run_name"], "r01-v2-6-r01-gemini-3-8-flash")
            self.assertEqual(exp_round_payload["params"]["sop_version"], "v2.6_r01")
            self.assertEqual(exp_round_payload["params"]["model_version"], "gemini-3.8-flash")
            self.assertEqual(exp_round_payload["params"]["runs_count"], 2)
            self.assertAlmostEqual(
                exp_round_payload["metrics"]["overall_recall"], (0.60 + 0.7895) / 2, places=5
            )
            self.assertIn("recall_sop_A_Handwashing", exp_round_payload["metrics"])

            exp_run_payload = mp.build_vertex_experiment_run_payload(rec, is_round_average=False)
            self.assertEqual(exp_run_payload["run_name"], "r01-r01-b-v2-6-r01")
            self.assertEqual(exp_run_payload["params"]["runs_count"], 1)

            with (
                mock.patch("google.cloud.aiplatform.init") as mock_init,
                mock.patch(
                    "google.cloud.aiplatform.start_run",
                    side_effect=[
                        RuntimeError("404 Context not found"),
                        RuntimeError("403 PermissionDenied on first run"),
                        RuntimeError("404 Context not found"),
                        mock.MagicMock(),
                    ],
                ) as mock_start,
                mock.patch("google.cloud.aiplatform.log_params") as mock_params,
                mock.patch("google.cloud.aiplatform.log_metrics") as mock_metrics,
                mock.patch("google.cloud.aiplatform.end_run") as mock_end,
            ):
                bad_avg = {**r_avg, "round_id": "r00", "sop_version": "v2.5_r00"}
                logged = mp.publish_vertex_experiment_records(
                    "example-project",
                    [bad_avg, r_avg],
                    location="asia-southeast1",
                    experiment_name="chagee-cctv-audit-eval",
                    is_round_average=True,
                )
            self.assertEqual(logged, ["r01-v2-6-r01-gemini-3-8-flash"])
            mock_init.assert_called_once()
            self.assertEqual(
                mock_start.call_args_list,
                [
                    mock.call(run="r00-v2-5-r00-gemini-3-8-flash", resume=True),
                    mock.call(run="r00-v2-5-r00-gemini-3-8-flash", resume=False),
                    mock.call(run="r01-v2-6-r01-gemini-3-8-flash", resume=True),
                    mock.call(run="r01-v2-6-r01-gemini-3-8-flash", resume=False),
                ],
            )
            mock_params.assert_called_once_with(exp_round_payload["params"])
            mock_metrics.assert_called_once_with(exp_round_payload["metrics"])
            mock_end.assert_called_once()

        fake_sess = mock.MagicMock()
        fake_resp = mock.MagicMock(status_code=200, text="{}")
        fake_sess.post.return_value = fake_resp
        with mock.patch("google.auth.transport.requests.AuthorizedSession", return_value=fake_sess):
            res = mp.publish_eval_timeseries("example-project", ts, credentials=mock.MagicMock())
        self.assertEqual(res, len(ts))
        self.assertEqual(fake_sess.post.call_count, 1)

        sample_golden = [
            {
                "item_id": "R02",
                "sheet_row": 2,
                "split": "dev",
                "outlet_name": "1-Bau Cat",
                "focus": "Handwashing",
                "audit_clause": "4.2.1 Hand Washing Procedure",
                "video_filenames": ["Footage 1.mov"],
                "parts": [
                    {
                        "part_id": "R02_p0",
                        "osd_times": ["12:17:45"],
                        "description": "Not wash hand for at least 20 seconds",
                    }
                ],
            }
        ]
        score_with_parts = {
            **score_doc,
            "items": [
                {
                    "item_id": "R02",
                    "score": 1.0,
                    "parts": [{"part_id": "R02_p0", "score": 1.0, "explanation": "MATCH=0; 命中"}],
                }
            ],
        }
        items_spec = mp.build_agent_platform_evaluation_items(
            round_id="r01",
            run_id="r01_run1",
            sop_version="v2.6_r01",
            model_version="gemini-3.8-flash",
            score_doc=score_with_parts,
            golden_items=sample_golden,
            job_docs=job_docs,
            agent_engine_id="9876543210987654321",
        )
        self.assertEqual(len(items_spec), 1)
        self.assertEqual(items_spec[0]["part_id"], "R02_p0")
        self.assertIn("✓ 命中 (1.0)", items_spec[0]["formatted_response"])
        self.assertEqual(items_spec[0]["agent_engine_id"], "9876543210987654321")

        from types import SimpleNamespace

        fake_eval_client = mock.MagicMock()
        fake_eval_client.evals.create_evaluation_item.return_value = SimpleNamespace(
            name="projects/123456789012/locations/us-central1/evaluationItems/111"
        )
        fake_eval_client.evals.create_evaluation_set.return_value = SimpleNamespace(
            name="projects/123456789012/locations/us-central1/evaluationSets/222"
        )
        fake_eval_client.evals.create_evaluation_experiment.return_value = SimpleNamespace(
            name="projects/123456789012/locations/us-central1/evaluationExperiments/333"
        )
        fake_eval_client.evals.create_evaluation_run.return_value = SimpleNamespace(
            name="projects/123456789012/locations/us-central1/evaluationRuns/444"
        )
        fake_storage_client = mock.MagicMock()
        with (
            mock.patch("vertexai.Client", return_value=fake_eval_client),
            mock.patch("google.cloud.storage.Client", return_value=fake_storage_client),
        ):
            pub_res = mp.publish_agent_platform_evaluation(
                project_id="example-project",
                gcs_bucket="gs://example-project-tfstate",
                round_id="r01",
                run_id="r01_run1",
                sop_version="v2.6_r01",
                model_version="gemini-3.8-flash",
                score_doc=score_with_parts,
                golden_items=sample_golden,
                job_docs=job_docs,
                agent_engine_id="9876543210987654321",
            )
        self.assertEqual(
            pub_res,
            {
                "experiment_name": "projects/123456789012/locations/us-central1/evaluationExperiments/333",
                "evaluation_set_name": "projects/123456789012/locations/us-central1/evaluationSets/222",
                "evaluation_run_name": "projects/123456789012/locations/us-central1/evaluationRuns/444",
            },
        )
        exp_call_kwargs = fake_eval_client.evals.create_evaluation_experiment.call_args.kwargs
        self.assertEqual(
            exp_call_kwargs["labels"]["vertex-ai-evaluation-agent-engine-id"],
            "9876543210987654321",
        )
        self.assertEqual(
            exp_call_kwargs["labels"]["vertex-ai-evaluation-set-name"],
            "projects/123456789012/locations/us-central1/evaluationSets/222",
        )


if __name__ == "__main__":
    unittest.main()


