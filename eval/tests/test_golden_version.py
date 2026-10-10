"""golden_version (scheme ruler-v2) changes exactly when the scoring ruler changes (Round 68b)."""

from __future__ import annotations

import copy
import json
import unittest

from eval import golden_sheet as gsh
from eval.golden_set import build_golden
from eval.tests.fake_workspace import FakeDrive, FakeSheets
from eval.tests.test_golden_sheet import FOLDERS, LABELS, SID, _cfg

BASE_META = {"name": "Manual Audit Result", "modifiedTime": "2026-10-10T00:00:00Z", "version": "42"}


def _version(labels=LABELS, cfg=None, folders=FOLDERS, meta=BASE_META) -> str:
    sheets = FakeSheets({"Sheet1": labels, gsh.CONFIG_TAB: gsh.render_config_rows(cfg or _cfg())})
    return gsh.load_golden_from_sheet(SID, sheets=sheets, drive=FakeDrive(folders, sheet_meta=meta)).version


class NonRulerChangesKeepTheVersionTest(unittest.TestCase):
    def setUp(self):
        self.base = _version()

    def test_note_change(self):
        self.assertEqual(_version(cfg=_cfg(note="a completely different note")), self.base)

    def test_clip_metadata_change(self):
        folders = copy.deepcopy(FOLDERS)
        folders["1FolderA_aaaaaaaaaa"][0]["videoMediaMetadata"] = {"durationMillis": "301500", "width": 1280,
                                                                   "height": 720}
        folders["1FolderA_aaaaaaaaaa"][1]["videoMediaMetadata"] = {"durationMillis": "299000"}
        self.assertEqual(_version(folders=folders), self.base)

    def test_cache_job_ids_change(self):
        cfg = _cfg()
        cfg.folders[0].cache_job_ids = ["other1", "other2"]
        cfg.folders[1].cache_job_ids = ["x"]
        self.assertEqual(_version(cfg=cfg), self.base)

    def test_sheet_modified_time_change(self):
        self.assertEqual(_version(meta={**BASE_META, "modifiedTime": "2027-01-01T00:00:00Z", "version": "99"}),
                         self.base)

    def test_drive_folder_moved_with_same_clips(self):
        cfg = _cfg()
        cfg.folders[1].folder_id = "1FolderB_moved_zzzz"
        folders = dict(FOLDERS)
        folders["1FolderB_moved_zzzz"] = folders.pop("1FolderB_bbbbbbbbbb")
        self.assertEqual(_version(cfg=cfg, folders=folders), self.base)

    def test_provenance_fields_in_golden_lines(self):
        lines = [json.loads(ln) for ln in _golden_raw().decode().splitlines()]
        changed = "".join(json.dumps({**r, "source_sha256": "x", "source_sheet_id": "y", "outlet_no": "9"}) + "\n"
                          for r in lines)
        self.assertEqual(build_golden(changed.encode(), {}, stem="g", source="t").version,
                         build_golden(_golden_raw(), {}, stem="g", source="t").version)


def _golden_raw() -> bytes:
    sheets = FakeSheets({"Sheet1": LABELS, gsh.CONFIG_TAB: gsh.render_config_rows(_cfg())})
    return gsh.load_golden_from_sheet(SID, sheets=sheets, drive=FakeDrive(FOLDERS)).raw


class RulerChangesChangeTheVersionTest(unittest.TestCase):
    def setUp(self):
        self.base = _version()

    def test_label_text(self):
        labels = copy.deepcopy(LABELS)
        labels[3][4] = "Footage 1: 221647 dwell < 3 min"
        self.assertNotEqual(_version(labels=labels), self.base)

    def test_osd_time(self):
        labels = copy.deepcopy(LABELS)
        labels[1][4] = "Footage 1: 080519 not dry hand"
        self.assertNotEqual(_version(labels=labels), self.base)

    def test_split_group(self):
        self.assertNotEqual(_version(cfg=_cfg(dev_groups=[])), self.base)

    def test_clip_added_or_removed(self):
        added = copy.deepcopy(FOLDERS)
        added["1FolderB_bbbbbbbbbb"].append({"id": "fB2", "name": "B_clip2_negative.mov"})
        self.assertNotEqual(_version(folders=added), self.base)
        removed = copy.deepcopy(FOLDERS)
        removed["1FolderA_aaaaaaaaaa"].pop()  # the unlabelled clip
        self.assertNotEqual(_version(folders=removed), self.base)

    def test_stable_set(self):
        cfg = _cfg()
        cfg.items[1].stable_baseline = True
        self.assertNotEqual(_version(cfg=cfg), self.base)

    def test_overrides(self):
        cfg = _cfg()
        cfg.items[1].sop_category = "C_Other"
        self.assertNotEqual(_version(cfg=cfg), self.base)
        cfg2 = _cfg()
        cfg2.items[1].temporal_mode = "POINT"
        self.assertNotEqual(_version(cfg=cfg2), self.base)


class CompareReportTest(unittest.TestCase):
    def test_compare_messages(self):
        sheets = FakeSheets({"Sheet1": LABELS, gsh.CONFIG_TAB: gsh.render_config_rows(_cfg())})
        g = gsh.load_golden_from_sheet(SID, sheets=sheets, drive=FakeDrive(FOLDERS))
        self.assertIn("评分标尺未变", gsh.compare_report(g, g.version))
        self.assertIn("评分标尺已变化", gsh.compare_report(g, "golden_demo@0000000000"))
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as d:
            same = Path(d) / "same.json"
            same.write_text(json.dumps({"golden": g.summary()}), encoding="utf-8")
            self.assertIn("完全相同", gsh.compare_report(g, str(same)))
            note_only = Path(d) / "note.json"
            note_only.write_text(json.dumps({"golden": {**g.summary(), "manifest_sha256_full": "different"}}),
                                 encoding="utf-8")
            self.assertIn("只有非评分字段变化", gsh.compare_report(g, str(note_only)))
            legacy = Path(d) / "legacy.json"
            legacy.write_text(json.dumps({"golden": {"golden_version": "golden_demo@d931e914a7"}}), encoding="utf-8")
            self.assertIn("旧的版本方案", gsh.compare_report(g, str(legacy)))


if __name__ == "__main__":
    unittest.main()
