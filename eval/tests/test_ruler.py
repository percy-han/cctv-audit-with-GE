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


if __name__ == "__main__":
    unittest.main()
