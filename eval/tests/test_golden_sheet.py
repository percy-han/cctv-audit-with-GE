"""eval/golden_sheet.py: golden set from the customer's label Sheet + 测评配置 tab (hermetic fakes)."""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from eval import golden_sheet as gsh
from eval.golden_set import GoldenSetError, load_golden
from eval.tests.fake_workspace import FakeDrive, FakeSheets, drive_files_from_manifest

EVAL_DIR = Path(__file__).resolve().parent.parent
DATA = EVAL_DIR / "data"
SID = "1SyntheticLabelSheetId_000000000"
HEADER = ["Focus", "Outlet", "Outlet Name", "Audit Clause", "Findings", "对应的视频文件名称"]
LABELS = [
    HEADER,
    ["Handwashing Monitoring", "1", "Demo Store A", "1.5 Handwashing", "Footage 1: 080518 not dry hand", "A_clip1.mov"],
    ["", "", "", "", "Footage 2: 080610 & 080640 soaping < 20s", "A_clip2.mov"],
    ["Ice Maker Weekly Cleaning", "2", "Demo Store B", "5.8 Ice Maker", "Footage 1: 221647 dwell < 5 min", "B_clip1.mov"],
]
FOLDERS = {
    "1FolderA_aaaaaaaaaa": [{"id": "fA1", "name": "A_clip1.mov",
                             "videoMediaMetadata": {"durationMillis": "300000", "width": 1920, "height": 1080}},
                            {"id": "fA2", "name": "A_clip2.mov", "videoMediaMetadata": {}},
                            {"id": "fA3", "name": "A_clip3_unlabelled.mov", "videoMediaMetadata": {}}],
    "1FolderB_bbbbbbbbbb": [{"id": "fB1", "name": "B_clip1.mov", "videoMediaMetadata": {}}],
}


def _cfg(**over) -> gsh.GoldenSheetConfig:
    base = dict(
        name="golden_demo", label_range="Sheet1!A1:F40", expected_rows=3, note="demo",
        folders=[gsh.FolderConfig("ga", "Demo Store A", "Handwashing Monitoring", "1FolderA_aaaaaaaaaa", ["j1"], 0),
                 gsh.FolderConfig("gb", "Demo Store B", "Ice Maker Weekly Cleaning", "1FolderB_bbbbbbbbbb", [], 0)],
        dev_groups=[("Handwashing Monitoring", "Demo Store A")],
        compound={},
        items=[gsh.ItemSetting("R02", "A_Handwashing", "POINT", True, 0),
               gsh.ItemSetting("R04", "B_IceMaker", "", False, 0)],
    )
    base.update(over)
    return gsh.GoldenSheetConfig(**base)


def _load(**kw):
    sheets, drive = _fakes(**kw)
    return gsh.load_golden_from_sheet(SID, sheets=sheets, drive=drive)


def _fakes(labels=LABELS, cfg=None, folders=FOLDERS, extra_tabs=None):
    tabs = {"Sheet1": labels, gsh.CONFIG_TAB: gsh.render_config_rows(cfg or _cfg())}
    tabs.update(extra_tabs or {})
    return FakeSheets(tabs), FakeDrive(folders)


class ConfigParsingTest(unittest.TestCase):
    def test_round_trip(self):
        cfg = _cfg(compound={2: ["first issue", "second issue"]})
        parsed = gsh.parse_config(gsh.render_config_rows(cfg))
        self.assertEqual((parsed.name, parsed.label_range, parsed.expected_rows), ("golden_demo", "Sheet1!A1:F40", 3))
        self.assertEqual([f.folder_id for f in parsed.folders], ["1FolderA_aaaaaaaaaa", "1FolderB_bbbbbbbbbb"])
        self.assertEqual(parsed.folders[0].cache_job_ids, ["j1"])
        self.assertEqual(parsed.compound, {2: ["first issue", "second issue"]})
        self.assertEqual([(i.item_id, i.temporal_mode, i.stable_baseline) for i in parsed.items],
                         [("R02", "POINT", True), ("R04", "", False)])

    def test_errors_name_the_cells(self):
        rows = gsh.render_config_rows(_cfg())
        rows = [list(r) for r in rows]
        def find(first):
            return next(i for i, r in enumerate(rows) if r and r[0] == first)
        rows[find("黄金集名称")][1] = "bad name!"
        rows[find("ga")][3] = "https://drive.google.com/x"
        rows[find("R02")][2] = "SOMETIMES"
        rows.append(["gb", "X", "Y", "1FolderC_cccccccccc", ""])  # lands in [逐题设置] -> stable column bad
        rows.append(["[未知分区]"])
        with self.assertRaises(gsh.GoldenSheetError) as ctx:
            gsh.parse_config(rows)
        msg = str(ctx.exception)
        self.assertIn(f"{gsh.CONFIG_TAB}!B{find('黄金集名称') + 1}", msg)
        self.assertIn(f"{gsh.CONFIG_TAB}!D{find('ga') + 1}", msg)
        self.assertIn(f"{gsh.CONFIG_TAB}!C{find('R02') + 1}: 时间判定模式只能是", msg)
        self.assertIn("不认识的分区 [未知分区]", msg)

    def test_missing_sections_and_header(self):
        with self.assertRaises(gsh.GoldenSheetError) as ctx:
            gsh.parse_config([["[基本设置]"], ["项目", "值"], ["黄金集名称", "g1"]])
        self.assertIn("缺少必填分区 [视频文件夹]", str(ctx.exception))
        self.assertIn("缺少 标注页范围", str(ctx.exception))
        with self.assertRaises(gsh.GoldenSheetError) as ctx:
            gsh.parse_config([["[视频文件夹]"], ["wrong", "header"]])
        self.assertIn("表头应为", str(ctx.exception))

    def test_blank_and_comment_rows_are_ignored(self):
        rows = [["# note"], [], ["", ""]] + gsh.render_config_rows(_cfg()) + [[], ["# trailing"]]
        self.assertEqual(gsh.parse_config(rows).name, "golden_demo")


class SheetToGoldenTest(unittest.TestCase):
    def test_builds_golden_with_unlabelled_clips_and_origin(self):
        sheets, drive = _fakes()
        g = gsh.load_golden_from_sheet(SID, sheets=sheets, drive=drive)
        self.assertTrue(g.version.startswith("golden_demo@"))
        self.assertEqual([it["item_id"] for it in g.items], ["R02", "R03", "R04"])
        self.assertEqual([p["part_id"] for p in g.items[1]["parts"]], ["R03a", "R03b"])
        self.assertEqual(g.items[0]["split"], "dev")
        self.assertEqual(g.items[0]["sop_category"], "A_Handwashing")  # from 逐题设置 via manifest overrides
        self.assertEqual(g.items[0]["source_sheet_id"], SID)
        folders = g.manifest["folders"]
        self.assertEqual([v["file_id"] for v in folders[0]["videos"]], ["fA1", "fA2", "fA3"])  # unlabelled kept
        self.assertEqual(folders[0]["videos"][0]["duration_sec"], 300.0)
        self.assertNotIn("duration_sec", folders[0]["videos"][1])  # unknown metadata left for ingest probe
        self.assertEqual(g.stable_baseline(), (["R02"], "manifest"))
        self.assertEqual(g.origin["sheet_id"], SID)
        self.assertEqual(g.summary()["golden_origin"]["sheet_modified_time"], "2026-10-10T00:00:00Z")

    def test_label_edit_changes_version_but_formatting_does_not(self):
        g1 = _load()
        edited = [list(r) for r in LABELS]
        edited[1][4] = "Footage 1: 080519 not dry hand"
        g2 = _load(labels=edited)
        self.assertNotEqual(g1.version, g2.version)
        trailing = [list(r) + [""] for r in LABELS] + [[""] * 6]
        self.assertEqual(_load(labels=trailing).version, g1.version)

    def test_snapshot_reloads_to_the_same_version(self):
        g = _load()
        with tempfile.TemporaryDirectory() as d:
            path = g.write_snapshot(Path(d))
            self.assertEqual(load_golden(path).version, g.version)
            self.assertTrue((Path(d) / "golden_origin.json").exists())

    def test_filename_must_match_exactly(self):
        labels = [list(r) for r in LABELS]
        labels[3][5] = "B_clip1 .mov"
        with self.assertRaises(gsh.GoldenSheetError) as ctx:
            _load(labels=labels)
        self.assertIn("必须完全一致", str(ctx.exception))
        self.assertIn("B_clip1 .mov", str(ctx.exception))

    def test_duplicate_filename_across_folders_fails(self):
        folders = {k: list(v) for k, v in FOLDERS.items()}
        folders["1FolderB_bbbbbbbbbb"] = folders["1FolderB_bbbbbbbbbb"] + [{"id": "dup", "name": "A_clip1.mov"}]
        with self.assertRaises(gsh.GoldenSheetError) as ctx:
            _load(folders=folders)
        self.assertIn("出现了不止一次", str(ctx.exception))

    def test_unknown_item_in_settings_and_missing_tab_and_access(self):
        cfg = _cfg(items=[gsh.ItemSetting("R99", "X", "", False, 0)])
        with self.assertRaises(GoldenSetError) as ctx:
            _load(cfg=cfg)
        self.assertIn("R99", str(ctx.exception))
        sheets = FakeSheets({"Sheet1": LABELS})
        with self.assertRaises(gsh.GoldenSheetError) as ctx:
            gsh.load_golden_from_sheet(SID, sheets=sheets, drive=FakeDrive(FOLDERS))
        self.assertIn(f"没有「{gsh.CONFIG_TAB}」页", str(ctx.exception))
        with self.assertRaises(gsh.GoldenSheetError) as ctx:
            gsh.load_golden_from_sheet(SID, sheets=FakeSheets({}, fail_status=403), drive=FakeDrive(FOLDERS))
        self.assertIn("共享给评测使用的 Workspace 身份", str(ctx.exception))


@unittest.skipUnless((DATA / "manual_audit_result_raw.json").exists() and (DATA / "golden_v1.manifest.json").exists(),
                     "private golden_v1 data not present")
class VersionContinuityTest(unittest.TestCase):
    def test_config_tab_from_v1_files_reproduces_golden_v1_version(self):
        sheet_id = json.loads((DATA / "golden_v1.build.json").read_text(encoding="utf-8"))["source_sheet_id"]
        manifest = json.loads((DATA / "golden_v1.manifest.json").read_text(encoding="utf-8"))
        build_cfg = json.loads((DATA / "golden_v1.build.json").read_text(encoding="utf-8"))
        raw = json.loads((DATA / "manual_audit_result_raw.json").read_text(encoding="utf-8"))
        cfg = gsh.config_from_files(build_cfg, manifest, name="golden_v1", label_range="Sheet1!A1:F40",
                                    sheet_id=sheet_id)
        sheets = FakeSheets({"Sheet1": raw, gsh.CONFIG_TAB: gsh.render_config_rows(cfg)})
        g = gsh.load_golden_from_sheet(sheet_id, sheets=sheets, drive=FakeDrive(drive_files_from_manifest(manifest)))
        f = load_golden(DATA / "golden_v1.jsonl")
        self.assertEqual(g.version, "golden_v1@9426fe9b52")  # ruler-v2; legacy full-content value was d931e914a7
        self.assertEqual(g.version, f.version)
        self.assertEqual((g.golden_sha256, g.manifest_sha256), (f.golden_sha256, f.manifest_sha256))
        self.assertEqual(g.manifest_sha256_full, f.manifest_sha256_full)
        # The only full-content difference is the provenance-only source_sha256 of each line.
        strip = lambda raw: [{k: v for k, v in json.loads(ln).items() if k != "source_sha256"}  # noqa: E731
                             for ln in raw.decode().splitlines() if ln.strip()]
        self.assertEqual(strip(g.raw), strip((DATA / "golden_v1.jsonl").read_bytes()))


class InitConfigAndCheckTest(unittest.TestCase):
    def _files(self, d: Path):
        build = d / "demo.build.json"
        build.write_text(json.dumps({"source_sheet_id": SID, "expected_rows": 3,
                                     "dev_groups": [["Handwashing Monitoring", "Demo Store A"]]}), encoding="utf-8")
        man = d / "golden_demo.manifest.json"
        man.write_text(json.dumps({
            "stable_baseline_items": ["R02"], "item_overrides": {"R02": {"sop_category": "A_Handwashing"}},
            "folders": [{"group_id": "ga", "folder_id": "1FolderA_aaaaaaaaaa", "label": "Demo Store A | Handwashing Monitoring",
                         "cache_job_ids": [], "videos": []},
                        {"group_id": "gb", "folder_id": "1FolderB_bbbbbbbbbb", "label": "Demo Store B | Ice Maker Weekly Cleaning",
                         "cache_job_ids": [], "videos": []}]}), encoding="utf-8")
        return build, man

    def test_init_config_writes_only_the_config_tab_and_refuses_overwrite(self):
        sheets = FakeSheets({"Sheet1": LABELS, "Other": [["keep"]]})
        with tempfile.TemporaryDirectory() as d:
            build, man = self._files(Path(d))
            argv = ["init-config", "--sheet-id", SID, "--from-build-config", str(build), "--manifest", str(man)]
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(gsh.main(argv + ["--dry-run"], services=lambda: (sheets, None)), 0)
            plan = json.loads(out.getvalue())
            self.assertEqual(plan["add_sheet"], {"requests": [{"addSheet": {"properties": {"title": gsh.CONFIG_TAB}}}]})
            self.assertEqual(plan["update"]["range"], f"'{gsh.CONFIG_TAB}'!A1")
            self.assertEqual(sheets.writes, [])  # dry run wrote nothing
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(gsh.main(argv, services=lambda: (sheets, None)), 0)
            touched = {w[1] if w[0] != "batchUpdate" else "batch" for w in sheets.writes}
            self.assertEqual(touched, {"batch", f"'{gsh.CONFIG_TAB}'!A1"})
            self.assertEqual(sheets.tabs["Sheet1"], LABELS)
            self.assertEqual(sheets.tabs["Other"], [["keep"]])
            self.assertEqual(gsh.parse_config(sheets.tabs[gsh.CONFIG_TAB]).name, "golden_demo")
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(gsh.main(argv, services=lambda: (sheets, None)), 1)
            self.assertIn("--overwrite", err.getvalue())
            sheets.writes.clear()
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(gsh.main(argv + ["--overwrite"], services=lambda: (sheets, None)), 0)
            self.assertEqual([w[:2] for w in sheets.writes],
                             [("clear", f"'{gsh.CONFIG_TAB}'"), ("update", f"'{gsh.CONFIG_TAB}'!A1")])

    def test_check_prints_version_counts_and_folders(self):
        sheets, drive = _fakes()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(gsh.main(["check", "--sheet-id", SID], services=lambda: (sheets, drive)), 0)
        text = out.getvalue()
        self.assertIn("黄金集版本: golden_demo@", text)
        self.assertIn("题数 3 / 标注点 4", text)
        self.assertIn("3 段视频，其中 2 段有标注、1 段无标注", text)
        self.assertIn("合计 4 段视频", text)
        bad_sheets, _ = _fakes(cfg=_cfg(expected_rows=5))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(gsh.main(["check", "--sheet-id", SID], services=lambda: (bad_sheets, drive)), 1)
        self.assertIn("expected 5 rows, got 3", err.getvalue())


class RunnerSelectionTest(unittest.TestCase):
    def test_sheet_and_file_together_is_rejected(self):
        from eval import run_gcp_round as rgr

        with self.assertRaises(GoldenSetError) as ctx:
            rgr.resolve_golden_source(None, None, env={"EVAL_GOLDEN_URI": "gs://b/g.jsonl",
                                                       "EVAL_GOLDEN_SHEET_ID": SID})
        self.assertIn("只能选一个", str(ctx.exception))
        self.assertEqual(rgr.resolve_golden_source(None, SID, env={}), ("sheet", SID))
        self.assertEqual(rgr.resolve_golden_source("x.jsonl", None, env={}), ("file", "x.jsonl"))
        self.assertEqual(rgr.resolve_golden_source(None, None, env={})[0], "file")

    def test_main_fails_fast_before_any_model_call(self):
        from eval import run_gcp_round as rgr

        cases = [
            (["--round", "r01", "--golden-sheet-id", SID, "--golden", "x.jsonl"], None, "只能选一个"),
            (["--round", "r01", "--golden-sheet-id", SID], (FakeSheets({"Sheet1": LABELS}), FakeDrive(FOLDERS)),
             f"没有「{gsh.CONFIG_TAB}」页"),
        ]
        for argv, services, expect in cases:
            err = io.StringIO()
            with mock.patch.object(rgr, "run_round_async") as run, \
                    mock.patch.object(gsh, "default_services", return_value=services), \
                    mock.patch.dict("os.environ", {"EVAL_GOLDEN_URI": "", "EVAL_GOLDEN_SHEET_ID": ""}), \
                    contextlib.redirect_stderr(err):
                self.assertEqual(rgr.main(argv), 2)
            run.assert_not_called()
            self.assertIn(expect, err.getvalue())
            self.assertIn("本次未调用任何模型", err.getvalue())

    def test_main_preloads_the_sheet_golden_for_the_run(self):
        from eval import run_gcp_round as rgr

        sheets, drive = _fakes()
        seen = {}

        async def fake_run(args):
            seen["golden"] = args.preloaded_golden[1]
            return 0

        with mock.patch.object(rgr, "run_round_async", fake_run), \
                mock.patch.object(gsh, "default_services", return_value=(sheets, drive)), \
                mock.patch.dict("os.environ", {"EVAL_GOLDEN_URI": "", "EVAL_GOLDEN_SHEET_ID": SID}):
            self.assertEqual(rgr.main(["--round", "r01"]), 0)
        self.assertTrue(seen["golden"].version.startswith("golden_demo@"))
        self.assertEqual(seen["golden"].origin["sheet_id"], SID)


class SnapshotTabTest(unittest.TestCase):
    def test_one_row_per_part_with_sources(self):
        from eval import sheet_report as srp

        g = _load()
        t = srp.build_golden_snapshot_table(g.items, g.stable_baseline()[0])
        self.assertEqual(t.title, "本次黄金集快照")
        self.assertEqual([r[1] for r in t.rows[1:]], ["R02", "R03a", "R03b", "R04"])
        r02 = t.rows[1]
        self.assertEqual((r02[t.col("SOP 大类")], r02[t.col("SOP 大类来源")]), ("A_Handwashing", "explicit"))
        self.assertEqual((r02[t.col("时间判定模式")], r02[t.col("时间模式来源")]), ("POINT", "explicit"))
        self.assertEqual(r02[t.col("稳定基线题")], "是")
        r03b = t.rows[3]
        self.assertEqual(r03b[t.col("标注时间点 (OSD)")], "08:06:40")
        self.assertEqual(r03b[t.col("SOP 大类来源")], "audit_clause")


if __name__ == "__main__":
    unittest.main()
