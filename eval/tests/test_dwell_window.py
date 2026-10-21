"""Dwell-aware asymmetric scoring windows (Round 80). Hermetic: no network, no judge."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import score_run as sr  # noqa: E402

ICE = "5.8 Ice Maker Routine Cleaning and Maintenance"


def _item(desc, osd, *, clause=ICE, video="v.mov", **extra):
    part = {"part_id": "P1", "description": desc, "osd_times": [osd] if osd else []}
    part.update(extra.pop("part_extra", {}))
    it = {"item_id": "P1", "audit_clause": clause, "finding_verbatim": desc, "video_filenames": [video],
          "parts": [part], "split": "dev", "focus": "Ice Maker", "outlet_name": "X"}
    it.update(extra)
    return it


def _f(fid, osd_sec, filename="v.mov", times=None, evidence="e"):
    return sr.Finding(finding_id=fid, job_id="j", filename=filename, rule_id="B2", disposition="SUSPECTED",
                      severity="", confidence=0.5, osd=sr.sec_to_hms(osd_sec), clip_ts="", evidence=evidence,
                      osd_times=sorted(times if times is not None else [osd_sec]))


def _hms(s):
    return sr.hms_to_sec(s)


class ParseDwellTest(unittest.TestCase):
    def test_english(self):
        self.assertEqual(sr.parse_dwell_sec("After spray sanitiser, not wait 5 min before wiping"), 300)
        self.assertEqual(sr.parse_dwell_sec("do not wait for 5 minutes contact time before wiping"), 300)
        self.assertEqual(sr.parse_dwell_sec("Langtuo: not wait 10 minutes"), 600)
        self.assertEqual(sr.parse_dwell_sec("not wait 2.5 mins"), 150)
        self.assertEqual(sr.parse_dwell_sec("not wait 45 seconds"), 45)
        self.assertEqual(sr.parse_dwell_sec("Not stir tea base within 30s"), 30)
        self.assertEqual(sr.parse_dwell_sec("stir within 1 min"), 60)

    def test_chinese(self):
        self.assertEqual(sr.parse_dwell_sec("喷消毒液后静置未满 5 分钟就擦拭"), 300)
        self.assertEqual(sr.parse_dwell_sec("浸泡不足10分钟"), 600)
        self.assertEqual(sr.parse_dwell_sec("静置 30 秒"), 30)
        self.assertEqual(sr.parse_dwell_sec("未等5分钟即装回"), 300)

    def test_no_match(self):
        self.assertIsNone(sr.parse_dwell_sec("Soaping time is less than 20s"))
        self.assertIsNone(sr.parse_dwell_sec("Not used a clean red edge towel"))
        self.assertIsNone(sr.parse_dwell_sec(""))
        self.assertIsNone(sr.parse_dwell_sec(None))


class PartWindowTest(unittest.TestCase):
    def test_point_untouched_even_with_dwell_text(self):
        it = _item("Footage 1: 080039 Soaping time is less than 20s, not wait 5 min", "08:00:39",
                   clause="1.5 Handwashing and Sanitation Standard")
        mode = sr.classify_temporal_mode(it, it["parts"][0])
        self.assertEqual(mode, "POINT")
        pw = sr.part_time_window(it, it["parts"][0], mode)
        self.assertEqual((pw.before, pw.after, pw.dwell_sec), (20, 20, None))
        self.assertEqual(pw.label(), "±20s")

    def test_non_dwell_window_is_exactly_60(self):
        it = _item("Not used a clean red edge towel from sanitising bucket", "12:55:11")
        pw = sr.part_time_window(it, it["parts"][0], "WINDOW")
        self.assertEqual((pw.before, pw.after, pw.dwell_sec), (60, 60, None))
        label = _hms("12:55:11")
        fs = [_f("F001", label + 60), _f("F002", label - 60), _f("F003", label + 61), _f("F004", label - 61)]
        got = sr.prefilter(it["parts"][0], ["v.mov"], fs, window_sec=60,
                           window_before_sec=pw.before, window_after_sec=pw.after)
        self.assertEqual([f.finding_id for f in got], ["F001", "F002"])

    def test_dwell_5min(self):
        it = _item("After spray sanitiser, do not wait for 5 minutes contact time before wiping", "12:56:00")
        pw = sr.part_time_window(it, it["parts"][0], "WINDOW")
        self.assertEqual((pw.before, pw.after, pw.dwell_sec, pw.dwell_source), (60, 330, 300, "label_text"))
        self.assertEqual(pw.label(), "−60s / +330s（静置 5 分钟）")

    def test_dwell_capped_at_span_max(self):
        it = _item("Langtuo: not wait 10 minutes before installing back", "12:00:00")
        pw = sr.part_time_window(it, it["parts"][0], "WINDOW")
        self.assertEqual((pw.before, pw.after, pw.dwell_sec), (60, sr.SPAN_MAX_SEC, 600))

    def test_short_dwell_never_shrinks_default(self):
        it = _item("Not stir tea base within 30s", "12:09:46")
        pw = sr.part_time_window(it, it["parts"][0], "WINDOW")
        self.assertEqual((pw.before, pw.after, pw.dwell_sec), (60, 60, 30))

    def test_end_anchor_cue_extends_before(self):
        it = _item("Installed back at 13:00:00, not wait 5 min after spray", "13:00:00")
        pw = sr.part_time_window(it, it["parts"][0], "WINDOW")
        self.assertEqual((pw.before, pw.after), (330, 330))

    def test_explicit_overrides_win(self):
        it = _item("Not used a clean towel", "12:00:00", part_extra={"dwell_sec": 120})
        pw = sr.part_time_window(it, it["parts"][0], "WINDOW")
        self.assertEqual((pw.before, pw.after, pw.dwell_sec, pw.dwell_source), (60, 150, 120, "explicit"))
        it2 = _item("not wait 5 min", "12:00:00", dwell_sec=0)  # item-level opt-out beats text
        pw2 = sr.part_time_window(it2, it2["parts"][0], "WINDOW")
        self.assertEqual((pw2.before, pw2.after, pw2.dwell_sec), (60, 60, None))
        it3 = _item("not wait 5 min", "12:00:00", part_extra={"window_before_sec": 10, "window_after_sec": 200})
        pw3 = sr.part_time_window(it3, it3["parts"][0], "WINDOW")
        self.assertEqual((pw3.before, pw3.after, pw3.dwell_sec), (10, 200, 300))


class WindowMathTest(unittest.TestCase):
    def test_signed_ring_gap(self):
        self.assertEqual(sr.signed_ring_gap(100, 40), 60)
        self.assertEqual(sr.signed_ring_gap(40, 100), -60)
        self.assertEqual(sr.signed_ring_gap(10, 86390), 20)  # 00:00:10 is 20s after 23:59:50
        self.assertEqual(sr.signed_ring_gap(86390, 10), -20)

    def test_asymmetric_ring_wrap(self):
        label = 86400 - 100  # 23:58:20
        self.assertTrue(sr.time_matches_window(label, [200], 60, 330))  # +300s across midnight
        self.assertFalse(sr.time_matches_window(label, [260], 60, 330))  # +360s
        self.assertTrue(sr.time_matches_window(label, [label - 60], 60, 330))
        self.assertFalse(sr.time_matches_window(label, [label - 61], 60, 330))
        self.assertTrue(sr.time_matches_window(30, [86400 - 30], 60, 330))  # -60s across midnight

    def test_symmetric_wrapper_unchanged(self):
        t = 12 * 3600
        self.assertTrue(sr.time_matches(t, [t + 60]))
        self.assertFalse(sr.time_matches(t, [t + 61]))
        self.assertTrue(sr.time_matches(t, [t - 120, t + 120]))
        self.assertFalse(sr.time_matches(t, [t - 400, t + 400]))
        self.assertTrue(sr.time_matches(t, [t + 20], window_sec=20))
        self.assertFalse(sr.time_matches(t, [t + 21], window_sec=20))
        self.assertTrue(sr.time_matches(t, [t - 20, t + 20], window_sec=20))
        self.assertFalse(sr.time_matches(t, [t - 25, t + 25], window_sec=20))

    def test_span_rule_unchanged_for_dwell(self):
        t = 12 * 3600
        self.assertTrue(sr.time_matches_window(t, [t - 300, t + 10], 60, 330))  # 310s span contains label
        self.assertFalse(sr.time_matches_window(t, [t - 400, t + 400], 60, 330))  # span > SPAN_MAX
        self.assertFalse(sr.time_matches_window(t, [t - 400, t - 100], 60, 330))  # span not containing


class Ic5aCaseTest(unittest.TestCase):
    def test_ic5a_style_match(self):
        it = _item("After spray sanitiser at the white cover, do not wait for 5 minutes contact time "
                   "before cleaning and installing back.", "13:31:25")
        fs = [_f("F001", _hms("13:33:06"), evidence="installed back without waiting"),
              _f("F002", _hms("13:40:00"), evidence="unrelated later"),
              _f("F003", _hms("13:33:06"), filename="other.mov")]
        old = sr.prefilter(it["parts"][0], ["v.mov"], fs, window_sec=60)
        self.assertEqual(old, [])
        rep = sr.score([it], fs, lambda cases: [(1.0, "MATCH=F001; ok") for _ in cases])
        part = rep["items"][0]["parts"][0]
        self.assertEqual(part["candidate_ids"], ["F001"])
        self.assertEqual((part["window_before_sec"], part["window_after_sec"], part["dwell_sec"]), (60, 330, 300))
        self.assertEqual(part["window_sec"], 60)
        self.assertEqual(part["window_label"], "−60s / +330s（静置 5 分钟）")
        self.assertEqual(rep["quality_metrics"]["dwell_parts_total"], 1)
        self.assertEqual(rep["quality_metrics"]["window_sec"], 60)
        self.assertEqual(rep["quality_metrics"]["point_window_sec"], 20)
        self.assertEqual(rep["items"][0]["score"], 1.0)
        # F002 is +515s: outside the new window, and inside the near band (330 + 120 = 450)? no -> not listed
        self.assertEqual(part["near_misses"], [])
        md = sr.render_markdown("t", rep)
        self.assertIn("WINDOW(−60s / +330s（静置 5 分钟）)", md)

    def test_near_miss_relative_to_new_window(self):
        it = _item("do not wait for 5 minutes contact time before wiping", "13:31:25")
        label = _hms("13:31:25")
        fs = [_f("F001", label + 400), _f("F002", label + 451), _f("F003", label - 150), _f("F004", label - 181)]
        rep = sr.score([it], fs, lambda cases: [(0.0, "MATCH=NONE; x") for _ in cases])
        part = rep["items"][0]["parts"][0]
        self.assertEqual(part["candidate_ids"], [])
        self.assertEqual([n.split()[0] for n in part["near_misses"]], ["F001", "F003"])
        self.assertIn("−60s / +330s", part["explanation"])

    def test_non_dwell_window_score_row_regression(self):
        it = _item("Not used a clean red edge towel", "12:55:11")
        label = _hms("12:55:11")
        fs = [_f("F001", label + 61), _f("F002", label + 179), _f("F003", label + 181)]
        rep = sr.score([it], fs, lambda cases: [(0.0, "MATCH=NONE; x") for _ in cases])
        part = rep["items"][0]["parts"][0]
        self.assertEqual((part["window_before_sec"], part["window_after_sec"], part["dwell_sec"]), (60, 60, None))
        self.assertEqual(part["candidate_ids"], [])
        self.assertEqual([n.split()[0] for n in part["near_misses"]], ["F001", "F002"])
        self.assertEqual(part["explanation"], "代码预筛：同一视频 ±60s 内没有任何 AI 条目")
        self.assertEqual(rep["quality_metrics"]["dwell_parts_total"], 0)


if __name__ == "__main__":
    unittest.main()
