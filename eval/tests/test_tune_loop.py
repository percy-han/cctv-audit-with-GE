#!/usr/bin/env python3
"""Unit tests for Step 2 Automated Prompt-Tuning Loop (`eval/tune_loop.py`)."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

THIS_DIR = Path(__file__).resolve().parent
CODE_ROOT = THIS_DIR.parent.parent
if str(CODE_ROOT) not in sys.path:
    sys.path.insert(0, str(CODE_ROOT))

from cctv_audit.prompt_manager import (  # noqa: E402
    DEFAULT_CHAGEE_V25_RULES,
    DEFAULT_LAYER1_TEMPLATE,
    parse_rules_from_sheet_rows,
    render_v25_system_instruction,
    rules_to_sheet_rows,
)
from eval.tune_loop import (  # noqa: E402
    DEFAULT_RESULTS_DIR,
    MAX_RULE_CHARS,
    PROPOSED_R01_LAYER1_TEMPLATE,
    bootstrap_r00,
    create_round_snapshot,
    evaluate_loop_state,
    evaluate_run_guardrails,
    propose_r01,
    record_round_run,
    round_selection_key,
    validate_layer1_template,
    validate_layer2_rules,
    validate_single_layer_mutation,
)


# Test fixture only: the stable set the synthetic docs declare in their golden block.
STABLE_BASELINE_ITEMS: tuple[str, ...] = ("R08", "R09", "R12", "R13", "R14", "R19")


def _make_synthetic_score_doc(
    *,
    run_id: str,
    dev_hits: float,
    holdout_hits: float,
    mean_per_clip: float = 2.5,
    regressed_item: str | None = None,
) -> dict:
    total_hits = dev_hits + holdout_hits
    items = []
    for rid in ("R02", "R03", "R04", "R05", "R06", "R20"):
        score = 1.0 if rid in STABLE_BASELINE_ITEMS else 0.5
        if rid == regressed_item:
            score = 0.0
        items.append({"item_id": rid, "split": "dev", "score": score})
    for rid in (
        "R07",
        "R08",
        "R09",
        "R10",
        "R11",
        "R12",
        "R13",
        "R14",
        "R15",
        "R16",
        "R17",
        "R18",
        "R19",
    ):
        score = 1.0 if rid in STABLE_BASELINE_ITEMS else 0.5
        if rid == regressed_item:
            score = 0.0
        items.append({"item_id": rid, "split": "holdout", "score": score})

    return {
        "run_id": run_id,
        "judge_model": "gemini-2.5-pro",
        "golden": {"golden_version": "golden_test@0000000000",
                   "stable_baseline_items": list(STABLE_BASELINE_ITEMS)},
        "recall": {
            "all": {"rows": 19, "points": total_hits, "recall": round(total_hits / 19.0, 4)},
            "dev": {"rows": 6, "points": dev_hits, "recall": round(dev_hits / 6.0, 4)},
            "holdout": {"rows": 13, "points": holdout_hits, "recall": round(holdout_hits / 13.0, 4)},
        },
        "alert_density": {
            "findings": int(mean_per_clip * 16),
            "clips": 16,
            "mean_per_clip": mean_per_clip,
        },
        "items": items,
    }


class TestPromptTuningLoop(unittest.TestCase):
    def test_default_and_r01_layer1_templates_are_valid_and_generic(self) -> None:
        self.assertEqual(validate_layer1_template(DEFAULT_LAYER1_TEMPLATE), [])
        self.assertEqual(validate_layer1_template(PROPOSED_R01_LAYER1_TEMPLATE), [])
        # Rendering baseline template matches legacy render_v25_system_instruction output
        rendered_default = render_v25_system_instruction("v2.5", DEFAULT_CHAGEE_V25_RULES)
        rendered_explicit = render_v25_system_instruction(
            "v2.5", DEFAULT_CHAGEE_V25_RULES, layer1_template=DEFAULT_LAYER1_TEMPLATE
        )
        self.assertEqual(rendered_default, rendered_explicit)

    def test_layer1_rejects_appliance_and_golden_label_terms(self) -> None:
        for forbidden in ("制冰机", "Langtuo", "挡水帘", "yellow bottle", "apply soap before wet hand", "Cantavil"):
            bad_tpl = DEFAULT_LAYER1_TEMPLATE + f"\n注意检查 {forbidden}\n"
            errs = validate_layer1_template(bad_tpl)
            self.assertTrue(
                any(forbidden.lower() in e.lower() for e in errs),
                f"Expected forbidden term '{forbidden}' to be rejected, got {errs}",
            )

        bad_chagee = DEFAULT_LAYER1_TEMPLATE + "\nCHAGEE 额外品牌的规则\n"
        self.assertTrue(any("CHAGEE" in e for e in validate_layer1_template(bad_chagee)))

    def test_layer2_rules_budget_and_sheet_roundtrip(self) -> None:
        self.assertEqual(validate_layer2_rules(DEFAULT_CHAGEE_V25_RULES), [])
        rows = rules_to_sheet_rows(DEFAULT_CHAGEE_V25_RULES)
        self.assertEqual(len(rows), 25)  # 1 header + 24 rules
        self.assertEqual(len(rows[0]), 9)
        parsed = parse_rules_from_sheet_rows(rows)
        self.assertEqual(parsed, list(DEFAULT_CHAGEE_V25_RULES))

        # Exceeding single-rule budget is rejected
        bloated_rule = DEFAULT_CHAGEE_V25_RULES[0].model_copy(
            update={"check_instruction": "X" * (MAX_RULE_CHARS + 10)}
        )
        bloated_list = [bloated_rule, *DEFAULT_CHAGEE_V25_RULES[1:]]
        errs = validate_layer2_rules(bloated_list)
        self.assertTrue(any("MAX_RULE_CHARS" in e for e in errs))

    def test_single_layer_mutation_guardrail(self) -> None:
        modified_rule0 = DEFAULT_CHAGEE_V25_RULES[0].model_copy(
            update={"check_instruction": DEFAULT_CHAGEE_V25_RULES[0].check_instruction + "\n补充说明。"}
        )
        mod_rules = [modified_rule0, *DEFAULT_CHAGEE_V25_RULES[1:]]

        layer, errs = validate_single_layer_mutation(
            DEFAULT_LAYER1_TEMPLATE,
            DEFAULT_CHAGEE_V25_RULES,
            PROPOSED_R01_LAYER1_TEMPLATE,
            DEFAULT_CHAGEE_V25_RULES,
        )
        self.assertEqual(layer, "layer1")
        self.assertEqual(errs, [])

        layer, errs = validate_single_layer_mutation(
            DEFAULT_LAYER1_TEMPLATE,
            DEFAULT_CHAGEE_V25_RULES,
            DEFAULT_LAYER1_TEMPLATE,
            mod_rules,
        )
        self.assertEqual(layer, "layer2")
        self.assertEqual(errs, [])

        layer, errs = validate_single_layer_mutation(
            DEFAULT_LAYER1_TEMPLATE,
            DEFAULT_CHAGEE_V25_RULES,
            PROPOSED_R01_LAYER1_TEMPLATE,
            mod_rules,
        )
        self.assertEqual(layer, "both")
        self.assertEqual(len(errs), 1)

        layer, errs = validate_single_layer_mutation(
            DEFAULT_LAYER1_TEMPLATE,
            DEFAULT_CHAGEE_V25_RULES,
            DEFAULT_LAYER1_TEMPLATE,
            DEFAULT_CHAGEE_V25_RULES,
        )
        self.assertEqual(layer, "none")
        self.assertEqual(len(errs), 1)

    def test_guardrails_on_real_baseline_runs_and_synthetic_regressions(self) -> None:
        for run_id in ("v6_0928_0811", "r_0928_1009"):
            score_path = DEFAULT_RESULTS_DIR / run_id / "score.json"
            if score_path.exists():
                doc = json.loads(score_path.read_text(encoding="utf-8"))
                g = evaluate_run_guardrails(doc)
                self.assertTrue(g["passed"], f"Baseline run {run_id} should pass guardrails: {g}")

        reg_doc = _make_synthetic_score_doc(
            run_id="bad_reg", dev_hits=5.0, holdout_hits=10.0, regressed_item="R19"
        )
        g_reg = evaluate_run_guardrails(reg_doc)
        self.assertFalse(g_reg["passed"])
        self.assertEqual(g_reg["regressed_stable_items"], ["R19"])

        dense_doc = _make_synthetic_score_doc(
            run_id="bad_dense", dev_hits=5.0, holdout_hits=10.0, mean_per_clip=8.5
        )
        g_dense = evaluate_run_guardrails(dense_doc)
        self.assertFalse(g_dense["passed"])
        self.assertFalse(g_dense["density_ok"])

    def test_tie_breaker_order(self) -> None:
        base_m = {
            "char_len_system_instruction": 20000,
            "aggregates": {
                "all_runs_pass_guardrails": True,
                "mean_all_recall": 0.7895,
                "mean_holdout_recall": 0.80,
                "mean_per_clip": 3.0,
            },
        }
        higher_holdout = {
            "char_len_system_instruction": 20000,
            "aggregates": {
                "all_runs_pass_guardrails": True,
                "mean_all_recall": 0.7895,
                "mean_holdout_recall": 0.85,
                "mean_per_clip": 3.5,
            },
        }
        lower_density = {
            "char_len_system_instruction": 20000,
            "aggregates": {
                "all_runs_pass_guardrails": True,
                "mean_all_recall": 0.7895,
                "mean_holdout_recall": 0.80,
                "mean_per_clip": 2.1,
            },
        }
        shorter_prompt = {
            "char_len_system_instruction": 18500,
            "aggregates": {
                "all_runs_pass_guardrails": True,
                "mean_all_recall": 0.7895,
                "mean_holdout_recall": 0.80,
                "mean_per_clip": 3.0,
            },
        }
        failed_guardrails_high_recall = {
            "char_len_system_instruction": 18000,
            "aggregates": {
                "all_runs_pass_guardrails": False,
                "mean_all_recall": 0.95,
                "mean_holdout_recall": 0.95,
                "mean_per_clip": 9.2,
            },
        }

        self.assertGreater(round_selection_key(base_m), round_selection_key(failed_guardrails_high_recall))
        self.assertGreater(round_selection_key(higher_holdout), round_selection_key(base_m))
        self.assertGreater(round_selection_key(lower_density), round_selection_key(base_m))
        self.assertGreater(round_selection_key(shorter_prompt), round_selection_key(base_m))

    def test_loop_early_stop_and_variance_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rounds_dir = Path(tmp) / "rounds"
            bootstrap_r00(rounds_dir=rounds_dir, overwrite=True)
            propose_r01(rounds_dir=rounds_dir, overwrite=True)

            # Immutable snapshot check when overwrite=False
            with self.assertRaises(FileExistsError):
                propose_r01(rounds_dir=rounds_dir, overwrite=False)

            # Record r01 with no gain over r00 (8.0 / 19 = 42.1%)
            doc_r01 = _make_synthetic_score_doc(run_id="r01_run1", dev_hits=0.5, holdout_hits=7.5)
            record_round_run(
                rounds_dir=rounds_dir, round_id="r01", run_id="r01_run1", score_doc=doc_r01, score_md="# r01"
            )
            state1 = evaluate_loop_state(rounds_dir)
            self.assertEqual(state1["consecutive_no_gain"], 1)
            self.assertEqual(state1["status"], "IN_PROGRESS")

            # Create r02 (Layer 2 mutation) also with no gain -> triggers 2-round patience early stop
            mod_rule0 = DEFAULT_CHAGEE_V25_RULES[0].model_copy(
                update={"check_instruction": DEFAULT_CHAGEE_V25_RULES[0].check_instruction + "\n补充检查。"}
            )
            create_round_snapshot(
                rounds_dir=rounds_dir,
                round_id="r02",
                parent_round_id="r00",
                layer1_template=DEFAULT_LAYER1_TEMPLATE,
                layer2_rules=[mod_rule0, *DEFAULT_CHAGEE_V25_RULES[1:]],
                hypothesis="Layer 2 test",
                target_dev_misses=["R02"],
            )
            doc_r02 = _make_synthetic_score_doc(run_id="r02_run1", dev_hits=0.5, holdout_hits=7.5)
            record_round_run(
                rounds_dir=rounds_dir, round_id="r02", run_id="r02_run1", score_doc=doc_r02, score_md="# r02"
            )
            state2 = evaluate_loop_state(rounds_dir)
            self.assertEqual(state2["consecutive_no_gain"], 2)
            self.assertEqual(state2["best_round_id"], "r00")
            self.assertTrue(state2["status"].startswith("STOPPED_NO_GAIN"))

            # Now simulate r01 achieving >= 85% (16.5 / 19 = 86.8%) across 3 runs
            for i in range(1, 4):
                hit_doc = _make_synthetic_score_doc(
                    run_id=f"r01_run{i}", dev_hits=5.5, holdout_hits=11.0, mean_per_clip=3.1
                )
                record_round_run(
                    rounds_dir=rounds_dir,
                    round_id="r01",
                    run_id=f"r01_run{i}",
                    score_doc=hit_doc,
                    score_md=f"# r01 run {i}",
                )
            state_conv = evaluate_loop_state(rounds_dir)
            self.assertEqual(state_conv["best_round_id"], "r01")
            self.assertTrue(state_conv["target_met"])
            self.assertTrue(state_conv["variance_confirmed"])
            self.assertEqual(state_conv["status"], "CONVERGED_TARGET_MET")

    def test_failed_guardrails_baseline_does_not_leak_inflated_recall(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rounds_dir = Path(tmp) / "rounds"
            create_round_snapshot(
                rounds_dir=rounds_dir,
                round_id="r00",
                parent_round_id=None,
                layer1_template=DEFAULT_LAYER1_TEMPLATE,
                layer2_rules=DEFAULT_CHAGEE_V25_RULES,
                hypothesis="Noisy baseline",
                target_dev_misses=["R02"],
            )
            # r00 fails guardrails (mean_per_clip=9.5 > 8.0) with inflated 90% recall (17.1/19)
            noisy_r00 = _make_synthetic_score_doc(
                run_id="r00_noisy", dev_hits=5.6, holdout_hits=11.5, mean_per_clip=9.5
            )
            record_round_run(
                rounds_dir=rounds_dir,
                round_id="r00",
                run_id="r00_noisy",
                score_doc=noisy_r00,
                score_md="# r00 noisy",
            )

            # r01 passes guardrails with 73.7% recall (14.0/19) -> overtakes r00 and syncs best_all_recall
            propose_r01(rounds_dir=rounds_dir, overwrite=True)
            valid_r01 = _make_synthetic_score_doc(
                run_id="r01_valid", dev_hits=4.0, holdout_hits=10.0, mean_per_clip=4.2
            )
            record_round_run(
                rounds_dir=rounds_dir,
                round_id="r01",
                run_id="r01_valid",
                score_doc=valid_r01,
                score_md="# r01 valid",
            )
            st1 = evaluate_loop_state(rounds_dir)
            self.assertEqual(st1["best_round_id"], "r01")
            self.assertEqual(st1["consecutive_no_gain"], 0)

            # r02 passes guardrails with 78.9% recall (15.0/19 > 14.0/19) -> also counts as gain
            mod_rule0 = DEFAULT_CHAGEE_V25_RULES[0].model_copy(
                update={"check_instruction": DEFAULT_CHAGEE_V25_RULES[0].check_instruction + "\n补充检查。"}
            )
            create_round_snapshot(
                rounds_dir=rounds_dir,
                round_id="r02",
                parent_round_id="r01",
                layer1_template=PROPOSED_R01_LAYER1_TEMPLATE,
                layer2_rules=[mod_rule0, *DEFAULT_CHAGEE_V25_RULES[1:]],
                hypothesis="Layer 2 gain",
                target_dev_misses=["R02"],
            )
            valid_r02 = _make_synthetic_score_doc(
                run_id="r02_valid", dev_hits=4.5, holdout_hits=10.5, mean_per_clip=4.0
            )
            record_round_run(
                rounds_dir=rounds_dir,
                round_id="r02",
                run_id="r02_valid",
                score_doc=valid_r02,
                score_md="# r02 valid",
            )
            st2 = evaluate_loop_state(rounds_dir)
            self.assertEqual(st2["best_round_id"], "r02")
            self.assertEqual(st2["consecutive_no_gain"], 0)

    def test_gcs_backed_video_slice_segment_allows_none_local_path(self) -> None:
        from cctv_audit.agentic_auditor import build_video_part
        from cctv_audit.video_ingestor import VideoSliceSegment

        seg_gcs = VideoSliceSegment(
            source_file_id="fid123",
            source_filename="Footage1.mov",
            segment_index=0,
            start_offset_sec=0.0,
            end_offset_sec=300.0,
            local_path=None,
            gcs_uri="gs://example-project-cctv-staging/eval/media/fid123/seg_0.mp4",
        )
        part = build_video_part(seg_gcs, "gemini-3.8-flash")
        self.assertEqual(
            part.file_data.file_uri,
            "gs://example-project-cctv-staging/eval/media/fid123/seg_0.mp4",
        )

        seg_missing = VideoSliceSegment(
            source_file_id="fid123",
            source_filename="Footage1.mov",
            segment_index=0,
            start_offset_sec=0.0,
            end_offset_sec=300.0,
            local_path=None,
            gcs_uri="",
        )
        with self.assertRaises(FileNotFoundError):
            build_video_part(seg_missing, "gemini-3.8-flash")


if __name__ == "__main__":
    unittest.main()
