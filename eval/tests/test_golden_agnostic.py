"""Golden-set agnosticism (Round 67b): the scoring / aggregation / Sheet builders must work for a
golden set that shares nothing with golden_v1 -- different item IDs, a 4th SOP category, a new outlet,
7 items, different videos -- and must never mix golden versions.

Imports of new modules are done inside the tests so each behaviour fails on its own on older code.
Fully synthetic: no private golden / history / score files are read."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest

from eval import score_run as sr

EVAL_DIR = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 11, 1, 0, 0, 0, tzinfo=timezone.utc)

_VIDEOS = {"fA": "S1_clip1.mp4", "fB": "S1_clip2.mp4", "fC": "S2_clip1.mp4", "fD": "S3_clip1.mp4",
           "fE": "S3_clip2.mp4"}


def _item(iid, split, outlet, focus, cat, fid, osd, **extra):
    it = {"item_id": iid, "sheet_row": int(iid[1:]) + 1, "split": split, "outlet_name": outlet, "focus": focus, "sop_category": cat,
          "audit_clause": f"{cat} clause", "finding_verbatim": f"{iid} label at {osd}",
          "video_filenames": [_VIDEOS[fid]], "video_file_ids": [fid],
          "parts": [{"part_id": iid, "description": f"{iid} label", "osd_times": [osd]}]}
    it.update(extra)
    return it


SYNTH_ITEMS = [
    _item("G01", "dev", "Orchard X", "Front Bar", "A_Handwashing", "fA", "10:00:10", stable_baseline=True),
    _item("G02", "dev", "Orchard X", "Front Bar", "B_IceMaker", "fA", "10:02:00", stable_baseline=True),
    _item("G03", "holdout", "Orchard X", "Front Bar", "C_TeaBar_Hygiene", "fB", "10:06:00"),
    _item("G04", "holdout", "Jurong Y", "Back Kitchen", "D_FoodStorage", "fC", "11:00:30",
          temporal_mode="POINT"),
    _item("G05", "holdout", "Jurong Y", "Back Kitchen", "D_FoodStorage", "fC", "11:03:00"),
    _item("G06", "holdout", "Tampines Z", "Back Kitchen", "D_FoodStorage", "fD", "12:00:20"),
    _item("G07", "dev", "Tampines Z", "Back Kitchen", "A_Handwashing", "fE", "12:06:00"),
]


def _finding(fid_clock, offset):
    return {"status": "VIOLATION", "rule_id": "X1", "on_screen_clock": fid_clock,
            "global_offset_sec": float(offset), "evidence": f"violation at {fid_clock}",
            "violation_disposition": "CONFIRMED"}


def _jobs():
    # One finding near every label except G02 (so the stable item G02 regresses) and G06.
    segs = {}
    clocks = {"fA": [("10:00:12", 12)], "fB": [("10:06:05", 365)], "fC": [("11:00:31", 31), ("11:03:10", 190)],
              "fD": [], "fE": [("12:06:02", 362)]}
    for fid, fname in _VIDEOS.items():
        segs[f"{fid}:0"] = {"file_id": fid, "filename": fname, "segment_index": 0,
                            "findings": [_finding(c, o) for c, o in clocks[fid]]}
    return [{"job_id": "synthjob", "completed_segments": segs}]


def _judge(cases):
    return [(1.0, f"MATCH={c['response'].split(']')[0].lstrip('[')}; ok") for c in cases]


def _write(tmp: Path, items, name="synth_golden.jsonl", manifest=None) -> Path:
    p = tmp / name
    p.write_text("\n".join(json.dumps(i, ensure_ascii=False) for i in items) + "\n", encoding="utf-8")
    if manifest is not None:
        (tmp / name.replace(".jsonl", ".manifest.json")).write_text(json.dumps(manifest), encoding="utf-8")
    return p


def _score(tmp: Path, golden_path: Path) -> dict:
    run_dir = tmp / "run"
    run_dir.mkdir(exist_ok=True)
    (run_dir / "job_synthjob.json").write_text(json.dumps(_jobs()), encoding="utf-8")
    # score_run expects one job per file
    (run_dir / "job_synthjob.json").write_text(json.dumps(_jobs()[0]), encoding="utf-8")
    rep = sr.score_run(run_dir=run_dir, golden_path=golden_path, project="p", run_label="synth_run1",
                       judge_fn=_judge)
    rep.pop("_sdk_result", None)
    return rep


class ScorerIsGoldenAgnosticTest(unittest.TestCase):
    def test_categories_and_outlets_come_from_the_items(self):
        items = [dict(i) for i in SYNTH_ITEMS]
        rep = sr.score(items, sr.flatten_findings(_jobs()), _judge)
        self.assertEqual(set(rep["sop_category_recall"]),
                         {"A_Handwashing", "B_IceMaker", "C_TeaBar_Hygiene", "D_FoodStorage"})
        self.assertEqual(rep["sop_category_recall"]["D_FoodStorage"]["rows"], 3)
        self.assertIn("Tampines Z | Back Kitchen", rep["outlet_focus_recall"])
        self.assertEqual(rep["recall"]["all"]["rows"], 7)

    def test_unknown_clause_is_not_forced_into_a_fixed_bucket(self):
        item = dict(SYNTH_ITEMS[3])
        item.pop("sop_category")
        item["audit_clause"] = "9.9 Brand-new Clause"
        self.assertEqual(sr.classify_sop_category(item), "9.9 Brand-new Clause")

    def test_markdown_counts_use_the_data(self):
        rep = sr.score([dict(i) for i in SYNTH_ITEMS], sr.flatten_findings(_jobs()), _judge)
        rep["judge_model"] = "injected"
        md = sr.render_markdown("synth", rep)
        self.assertIn("全部 7 行", md)
        self.assertNotIn("19", md)

    def test_temporal_mode_source_is_recorded(self):
        rep = sr.score([dict(i) for i in SYNTH_ITEMS], sr.flatten_findings(_jobs()), _judge)
        src = {p["part_id"]: p["temporal_mode_source"] for it in rep["items"] for p in it["parts"]}
        self.assertEqual(src["G04"], "explicit")
        self.assertEqual(src["G05"], "heuristic")


class GoldenLineageTest(unittest.TestCase):
    def test_version_counts_stable_set_and_folders(self):
        from eval.golden_set import derive_folder_specs, load_golden

        with tempfile.TemporaryDirectory() as d:
            g = load_golden(_write(Path(d), SYNTH_ITEMS))
            v1 = load_golden(_write(Path(d), SYNTH_ITEMS[:5], name="other_golden.jsonl"))
            self.assertTrue(g.version.startswith("synth_golden@"))
            self.assertNotEqual(g.version, v1.version)
            self.assertEqual((g.item_count, g.part_count, g.split_counts), (7, 7, {"dev": 3, "holdout": 4}))
            self.assertEqual(g.stable_baseline(), (["G01", "G02"], "golden_field"))
            specs = derive_folder_specs(g)
            self.assertEqual(sorted(s["label"] for s in specs),
                             ["Jurong Y | Back Kitchen", "Orchard X | Front Bar", "Tampines Z | Back Kitchen"])
            self.assertEqual(sum(len(s["videos"]) for s in specs), 5)
            # Formatting-only changes do not change the version; content changes do.
            p2 = Path(d) / "synth_golden.jsonl"
            p2.write_text("\r\n".join(json.dumps(i, indent=None, sort_keys=True) for i in SYNTH_ITEMS) + "\r\n\r\n",
                          encoding="utf-8")
            self.assertEqual(load_golden(p2).version, g.version)
            changed = [dict(i) for i in SYNTH_ITEMS]
            changed[0] = {**changed[0], "split": "holdout"}
            self.assertNotEqual(load_golden(_write(Path(d), changed)).version, g.version)

    def test_manifest_folders_must_cover_every_labelled_clip(self):
        from eval.golden_set import GoldenSetError, derive_folder_specs, load_golden

        manifest = {"folders": [{"group_id": "only", "videos": [{"file_id": "fA", "filename": _VIDEOS["fA"]}]}]}
        with tempfile.TemporaryDirectory() as d:
            g = load_golden(_write(Path(d), SYNTH_ITEMS, manifest=manifest))
            with self.assertRaises(GoldenSetError):
                derive_folder_specs(g)

    def test_runner_folder_specs_follow_the_golden(self):
        from eval import run_gcp_round as rgr

        with tempfile.TemporaryDirectory() as d:
            specs = rgr.load_folder_specs(_write(Path(d), SYNTH_ITEMS))
        self.assertEqual(sum(len(s["videos"]) for s in specs), 5)
        self.assertEqual({v["file_id"] for s in specs for v in s["videos"]}, set(_VIDEOS))


class GuardrailSourceTest(unittest.TestCase):
    def test_stable_set_comes_from_the_golden_and_unconfigured_is_reported(self):
        from eval.tune_loop import evaluate_run_guardrails

        with tempfile.TemporaryDirectory() as d:
            rep = _score(Path(d), _write(Path(d), SYNTH_ITEMS))
            g = evaluate_run_guardrails(rep)
            self.assertEqual(g["stable_items_checked"], ["G01", "G02"])
            self.assertEqual(g["regressed_stable_items"], ["G02"])
            self.assertEqual(g["stable_guardrail"], "fail")
            self.assertFalse(g["passed"])

            no_stable = [{k: v for k, v in i.items() if k != "stable_baseline"} for i in SYNTH_ITEMS]
            rep2 = _score(Path(d), _write(Path(d), no_stable, name="synth_nostable.jsonl"))
            g2 = evaluate_run_guardrails(rep2)
            self.assertEqual(g2["stable_guardrail"], "not_configured")
            self.assertEqual(g2["stable_items_checked"], [])


def _other_version_history(d: Path) -> list[dict]:
    """5 runs over rounds r00/r01/r02 (2/1/2 runs) scored on a DIFFERENT synthetic golden version."""
    from eval.eval_records import build_eval_monitoring_record

    rep = _score(d, _write(d, SYNTH_ITEMS[:5], name="other_golden.jsonl"))
    out = []
    for i, (rnd, run) in enumerate([("r00", "o_run1"), ("r00", "o_run2"), ("r01", "o_run3"),
                                    ("r02", "o_run4"), ("r02", "o_run5")]):
        out.append(build_eval_monitoring_record(
            project_id="p", round_id=rnd, run_id=run, model_version="gemini-3.8-flash",
            sop_version=f"Prompt_v2.6_{rnd}", media_mode="agentic", score_doc=rep,
            timestamp_iso=f"2026-10-0{i + 1}T00:00:00Z"))
    return out


class VersionSeparationTest(unittest.TestCase):
    def _records(self, d: Path):
        from eval.eval_records import build_eval_monitoring_record

        rep = _score(d, _write(d, SYNTH_ITEMS))
        synth = build_eval_monitoring_record(
            project_id="p", round_id="r02", run_id="synth_run1", model_version="gemini-3.8-flash",
            sop_version="Prompt_v2.6_r02", media_mode="agentic", score_doc=rep, timestamp_iso="2026-11-01T00:00:00Z")
        history = _other_version_history(d)
        return rep, synth, history

    def test_round_averages_never_mix_golden_versions(self):
        from eval.eval_records import compute_all_round_averages

        with tempfile.TemporaryDirectory() as d:
            _, synth, history = self._records(Path(d))
        self.assertTrue(synth["golden_version"].startswith("synth_golden@"))
        self.assertEqual(synth["golden_item_count"], 7)
        avgs = compute_all_round_averages(history + [synth])
        r02 = [a for a in avgs if a["round_id"] == "r02"]
        self.assertEqual(sorted(a["runs_count"] for a in r02), [1, 2])
        self.assertEqual(len({a["golden_version"] for a in r02}), 2)

    def test_sheet_history_tabs_only_show_the_current_golden_version(self):
        from eval import sheet_report as srp

        with tempfile.TemporaryDirectory() as d:
            rep, synth, history = self._records(Path(d))
        spec = srp.build_report_spec(history=history + [synth], score_doc=rep, run_record=synth,
                                     golden_items=SYNTH_ITEMS, now_utc=NOW)
        overview = {r[0]: r[1] for r in spec.tables[0].rows}
        self.assertIn("总体召回率 (7 题)", overview)
        self.assertEqual(overview["黄金集版本 (golden_version)"], synth["golden_version"])
        rounds, runs = spec.tables[2], spec.tables[3]
        self.assertEqual([r[0] for r in rounds.rows[1:]], ["r02 · v2.6_r02"])
        self.assertEqual([r[3] for r in runs.rows[1:]], ["synth_run1"])
        self.assertIn("D_FoodStorage", rounds.rows[0])
        self.assertIn(synth["golden_version"], rounds.note)
        self.assertIn("其他版本 3 轮（5 次运行）未纳入", rounds.note)
        notes = [v for v in srp.build_value_ranges(spec) if v["values"] == [[rounds.note]]]
        self.assertEqual(len(notes), 2)  # rounds + runs tabs


if __name__ == "__main__":
    unittest.main()
