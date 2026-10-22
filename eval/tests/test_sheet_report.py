"""Unit tests for eval/sheet_report.py (no network): pure spec builders on the real 5-run history
fixture, the thin Drive/Sheets layer against a fake client, and the never-fail-the-run policy."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
import tempfile
import unittest

from eval import sheet_report as srp

EVAL_DIR = Path(__file__).resolve().parent.parent
HISTORY = EVAL_DIR / "rounds" / "eval_history.jsonl"
SCORE = EVAL_DIR / "results" / "r02_run2" / "score.json"
GOLDEN = EVAL_DIR / "data" / "golden_v1.jsonl"
NOW = datetime(2026, 10, 10, 1, 2, 3, tzinfo=timezone.utc)
FOLDER = "1ExampleEvalResultsFolderId000000"


def _fixture():
    history = srp.load_history(HISTORY)
    score = json.loads(SCORE.read_text(encoding="utf-8"))
    record = next(r for r in history if r["run_id"] == "r02_run2")
    golden = srp.load_history(GOLDEN)
    return history, score, record, golden


def _spec(**kw):
    history, score, record, golden = _fixture()
    return srp.build_report_spec(history=history, score_doc=score, run_record=record,
                                 golden_items=golden, now_utc=NOW, time_zone=kw.get("tz", "Asia/Singapore"))


class _Call:
    def __init__(self, result):
        self.result = result

    def execute(self, num_retries=0):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeDrive:
    def __init__(self, fail: Exception | None = None):
        self.created: list[dict] = []
        self.fail = fail

    def files(self):
        return self

    def create(self, **kw):
        self.created.append(kw)
        return _Call(self.fail or {"id": "SHEET123", "webViewLink": "https://docs.google.com/spreadsheets/d/SHEET123/edit"})


class FakeSheets:
    def __init__(self):
        self.batch_updates: list[dict] = []
        self.value_updates: list[dict] = []

    def spreadsheets(self):
        return self

    def values(self):
        return self

    def get(self, **kw):
        return _Call({"sheets": [{"properties": {"sheetId": 0}}]})

    def batchUpdate(self, spreadsheetId, body):  # noqa: N802 - Google API name
        (self.value_updates if "valueInputOption" in body else self.batch_updates).append(body)
        return _Call({})


class SpecTest(unittest.TestCase):
    def test_title_pattern_uses_configured_time_zone(self):
        spec = _spec()
        self.assertEqual(spec.title, "CHAGEE_AI_Audit_Eval_r02_r02_run2_20261010-090203")
        self.assertRegex(spec.title, r"^CHAGEE_AI_Audit_Eval_r02_r02_run2_\d{8}-\d{6}$")
        self.assertEqual(_spec(tz="Etc/UTC").title, "CHAGEE_AI_Audit_Eval_r02_r02_run2_20261010-010203")
        with self.assertRaises(ValueError):
            _spec(tz="Mars/Olympus")

    def test_tabs_and_row_counts(self):
        spec = _spec()
        self.assertEqual(
            [t.title for t in spec.tables],
            [srp.TAB_OVERVIEW, srp.TAB_ITEMS, srp.TAB_ROUNDS, srp.TAB_RUNS, srp.TAB_GOLDEN],
        )
        snapshot = spec.tables[4]
        self.assertEqual(len(snapshot.rows) - 1, 21)
        r11b = next(r for r in snapshot.rows if r[1] == "R11b")
        self.assertEqual(r11b[snapshot.col("Labelled Timestamp (OSD)")], "12:11:00")
        self.assertEqual(r11b[snapshot.col("SOP Category Source")], "audit_clause")  # raw golden lines, no manifest
        items = spec.tables[1]
        self.assertEqual(len(items.rows) - 1, 21)  # 19 golden items, 2 of them compound
        rounds = spec.tables[2]
        self.assertEqual([r[0] for r in rounds.rows[1:]], ["r00 · v2.5_r00", "r01 · v2.6_r01", "r02 · v2.6_r02"])
        runs = spec.tables[3]
        self.assertEqual(len(runs.rows) - 1, 5)
        self.assertEqual([r[1] for r in runs.rows[1:]].count("★"), 1)

    def test_numbers_come_from_score_doc_and_history(self):
        spec = _spec()
        _, score, record, _ = _fixture()
        overview = {r[0]: r[1] for r in spec.tables[0].rows}
        self.assertEqual(overview["Overall Recall (19 items)"], score and record["metrics"]["overall_recall"])
        self.assertAlmostEqual(overview["Overall Recall (19 items)"], score["recall"]["all"]["recall"], places=5)
        rounds = spec.tables[2]
        r02 = rounds.rows[3]
        self.assertAlmostEqual(r02[rounds.col("Overall Recall")], (0.434211 + 0.421053) / 2, places=5)
        items = spec.tables[1]
        r08 = next(r for r in items.rows if r[0] == "R08")
        self.assertEqual(r08[items.col("Part Score")], 1.0)
        self.assertIn("F007", r08[items.col("Matched AI Findings")])
        self.assertEqual(r08[items.col("Human Label (Verbatim)")], "Footage 2: 080530 Soaping time is less than 20s")
        r11a = next(r for r in items.rows if r[0] == "R11a")
        self.assertEqual(r11a[items.col("Labelled Timestamp (OSD)")], "12:10:36")
        self.assertEqual(overview["Input Tokens (incl. Agentic Frame Sampling)"], record["metrics"]["prompt_token_count"])
        self.assertEqual(overview["Thinking Tokens"], record["metrics"]["thoughts_token_count"])
        self.assertEqual(overview["Output Tokens"], record["metrics"]["candidates_token_count"])
        runs = spec.tables[3]
        r01_row = next(r for r in runs.rows[1:] if r[runs.col("run_id")] == "r01_run1")
        self.assertAlmostEqual(r01_row[runs.col("Mean Cost / Clip (USD)")], 0.331525, places=6)
        self.assertEqual(r01_row[runs.col("Input Tokens")], 6183314)
        self.assertEqual(r01_row[runs.col("Thinking Tokens")], 114826)
        self.assertEqual(r01_row[runs.col("Output Tokens")], 63016)
        self.assertEqual(r01_row[runs.col("Total Tokens")], 6361156)

    def test_percent_values_are_numbers_with_percent_format(self):
        spec = _spec()
        rounds = spec.tables[2]
        for h in ("Overall Recall", "CONFIRMED-Only Recall", "Alert Hit Rate", "A_Handwashing",
                  "Bau Cat | Handwashing Monitoring"):
            c = rounds.col(h)
            self.assertEqual(rounds.col_formats[c], srp.PERCENT)
            for row in rounds.rows[1:]:
                self.assertIsInstance(row[c], float)
        for table in spec.tables:
            flat_text = "\n".join(str(cell) for row in table.rows for cell in row)
            self.assertNotIn("留出集", flat_text)
            self.assertNotIn("开发集", flat_text)
            self.assertNotIn("数据集划分", flat_text)
        overview = spec.tables[0]
        recall_row = next(i for i, r in enumerate(overview.rows) if r[0] == "Overall Recall (19 items)")
        self.assertIn((recall_row, 1, srp.PERCENT), overview.cell_formats)
        fmt = srp.build_format_requests(spec)
        pct = [r for r in fmt if "repeatCell" in r
               and r["repeatCell"]["cell"]["userEnteredFormat"].get("numberFormat") == srp.PERCENT]
        self.assertTrue(pct)

    def test_every_chart_domain_is_the_text_label_column(self):
        spec = _spec()
        self.assertEqual(len(spec.chart_requests), 4)
        tables = {t.sheet_id: t for t in spec.tables}
        for req in spec.chart_requests:
            chart = req["addChart"]["chart"]["spec"]["basicChart"]
            src = chart["domains"][0]["domain"]["sourceRange"]["sources"][0]
            table = tables[src["sheetId"]]
            self.assertEqual(src["startColumnIndex"], 0)
            self.assertIn(table.rows[0][0], ("Round", "Run"))
            for row in table.rows[1:]:
                self.assertIsInstance(row[0], str)
                self.assertIsNone(re.match(r"^\d{4}-\d{2}-\d{2}", row[0]))  # never a date
            self.assertEqual(chart["headerCount"], 1)
        chart4 = spec.chart_requests[3]["addChart"]["chart"]["spec"]["basicChart"]
        self.assertIn("RIGHT_AXIS", {s["targetAxis"] for s in chart4["series"]})

    def test_structure_freezes_headers_and_drops_default_sheet(self):
        spec = _spec()
        reqs = srp.build_structure_requests(spec, [0])
        adds = [r["addSheet"]["properties"] for r in reqs if "addSheet" in r]
        self.assertEqual(len(adds), 5)
        self.assertTrue(all(a["gridProperties"]["frozenRowCount"] == 1 for a in adds))
        self.assertIn({"deleteSheet": {"sheetId": 0}}, reqs)
        self.assertEqual(reqs[0]["updateSpreadsheetProperties"]["properties"]["timeZone"], "Asia/Singapore")

    def test_history_is_cut_at_the_reported_run(self):
        history, score, _, golden = _fixture()
        rec = next(r for r in history if r["run_id"] == "r01_run1")
        spec = srp.build_report_spec(history=history, score_doc=score, run_record=rec,
                                     golden_items=golden, now_utc=NOW)
        self.assertEqual(len(spec.tables[3].rows) - 1, 3)
        with self.assertRaises(ValueError):
            srp.history_up_to_run(history, "nope")


class PublishTest(unittest.TestCase):
    def _publish(self, tmp, **kw):
        _, score, record, golden = _fixture()
        return srp.publish_run_sheet_report_safely(
            history_path=HISTORY, score_doc=score, run_record=record, golden_items=golden,
            res_dir=Path(tmp), now_utc=NOW, env={}, **kw)

    def test_creates_file_in_folder_and_writes_report_json(self):
        drive, sheets = FakeDrive(), FakeSheets()
        with tempfile.TemporaryDirectory() as tmp:
            out = self._publish(tmp, folder_id=FOLDER, services=lambda: (drive, sheets))
            self.assertEqual(out.status, "created")
            self.assertEqual(out.spreadsheet_url, "https://docs.google.com/spreadsheets/d/SHEET123/edit")
            body = drive.created[0]["body"]
            self.assertEqual(body["parents"], [FOLDER])
            self.assertEqual(body["mimeType"], "application/vnd.google-apps.spreadsheet")
            self.assertRegex(body["name"], r"^CHAGEE_AI_Audit_Eval_r02_r02_run2_\d{8}-\d{6}$")
            self.assertTrue(drive.created[0]["supportsAllDrives"])
            self.assertEqual(sheets.value_updates[0]["valueInputOption"], "RAW")
            charts = [r for r in sheets.batch_updates[-1]["requests"] if "addChart" in r]
            self.assertEqual(len(charts), 4)
            saved = json.loads((Path(tmp) / "sheet_report.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["spreadsheet_url"], out.spreadsheet_url)
            self.assertEqual(saved["folder_id"], FOLDER)
            self.assertIn(out.spreadsheet_url, out.status_line)

    def test_skips_loudly_when_folder_id_empty(self):
        def boom():
            raise AssertionError("must not build API clients when skipped")

        with tempfile.TemporaryDirectory() as tmp, self.assertLogs("eval.sheet_report", "WARNING"):
            out = self._publish(tmp, services=boom)
            self.assertEqual(out.status, "skipped")
            self.assertIn("eval_results_folder_id", out.status_line)
            self.assertEqual(json.loads((Path(tmp) / "sheet_report.json").read_text())["status"], "skipped")

    def test_api_error_does_not_raise_and_is_logged_at_error(self):
        drive = FakeDrive(fail=RuntimeError("HttpError 500 backend"))
        with tempfile.TemporaryDirectory() as tmp, self.assertLogs("eval.sheet_report", "ERROR") as logs:
            out = self._publish(tmp, folder_id=FOLDER, services=lambda: (drive, FakeSheets()))
            self.assertEqual(out.status, "failed")
            self.assertIn("HttpError 500 backend", out.status_line)
            self.assertTrue(any("FAILED" in m for m in logs.output))
            self.assertEqual(json.loads((Path(tmp) / "sheet_report.json").read_text())["status"], "failed")

    def test_credentials_error_does_not_raise(self):
        def no_creds():
            raise RuntimeError("Reauthentication is needed")

        with tempfile.TemporaryDirectory() as tmp, self.assertLogs("eval.sheet_report", "ERROR"):
            out = self._publish(tmp, folder_id=FOLDER, services=no_creds)
        self.assertEqual(out.status, "failed")

    def test_update_report_spreadsheet_in_place(self):
        spec = _spec()
        sheets = FakeSheets()
        url = srp.update_report_spreadsheet(spec, "SHEET123", sheets=sheets)
        self.assertEqual(url, "https://docs.google.com/spreadsheets/d/SHEET123/edit")
        self.assertEqual(len(sheets.batch_updates), 3)
        self.assertEqual(len(sheets.value_updates), 1)

    def test_chart_row_cap_keeps_header_and_latest_rounds_or_runs(self):
        base_rows, score, _, golden = _fixture()
        template = base_rows[-1]
        many_records = []
        for idx in range(20):
            rec = dict(template)
            rec["round_id"] = f"r{idx:02d}"
            rec["run_id"] = f"r{idx:02d}_run1"
            rec["sop_version"] = f"Prompt_v2.6_r{idx:02d}"
            many_records.append(rec)
        spec = srp.build_report_spec(
            history=many_records,
            score_doc=score,
            run_record=many_records[-1],
            golden_items=golden,
            now_utc=NOW,
            time_zone="Asia/Singapore",
        )
        # Full tables preserve all 20 rounds + 1 header = 21 rows
        self.assertEqual(len(spec.tables[2].rows), 21)
        self.assertEqual(len(spec.tables[3].rows), 21)
        chart_reqs = spec.chart_requests
        # Round chart (chart 0) caps to latest MAX_CHART_ROUNDS (12) while keeping row 0 header
        round_domain_sources = chart_reqs[0]["addChart"]["chart"]["spec"]["basicChart"]["domains"][0]["domain"]["sourceRange"]["sources"]
        self.assertEqual(len(round_domain_sources), 2)
        self.assertEqual(round_domain_sources[0]["startRowIndex"], 0)
        self.assertEqual(round_domain_sources[0]["endRowIndex"], 1)
        self.assertEqual(round_domain_sources[1]["startRowIndex"], 21 - srp.MAX_CHART_ROUNDS)
        self.assertEqual(round_domain_sources[1]["endRowIndex"], 21)
        # Run chart (chart 3) caps to latest MAX_CHART_RUNS (15) while keeping row 0 header
        run_domain_sources = chart_reqs[3]["addChart"]["chart"]["spec"]["basicChart"]["domains"][0]["domain"]["sourceRange"]["sources"]
        self.assertEqual(len(run_domain_sources), 2)
        self.assertEqual(run_domain_sources[0]["startRowIndex"], 0)
        self.assertEqual(run_domain_sources[0]["endRowIndex"], 1)
        self.assertEqual(run_domain_sources[1]["startRowIndex"], 21 - srp.MAX_CHART_RUNS)
        self.assertEqual(run_domain_sources[1]["endRowIndex"], 21)


if __name__ == "__main__":
    unittest.main()
