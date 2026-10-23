"""Round 86B: de-hardcoded stable-baseline detection and judge location (hermetic)."""

from __future__ import annotations

import json
import os
from pathlib import Path
import unittest
from unittest import mock

from eval import golden_sheet as gsh
from eval import score_run
from eval.golden_set import load_golden

DATA = Path(__file__).resolve().parent.parent / "data"


def _recs(*findings: str) -> list[dict[str, str]]:
    return [{"item_id": f"R{i:02d}", "finding_verbatim": f} for i, f in enumerate(findings, 1)]


class StableBaselineSignatureTest(unittest.TestCase):
    SIGS = ("080530", "apron not kept")

    def test_every_signature_matches_exactly_one_item(self):
        recs = _recs("Footage 2: 080530 soaping", "x", "Apron not kept at storage")
        self.assertEqual(gsh.stable_baseline_from_signatures(recs, self.SIGS), {"R01", "R03"})

    def test_unmatched_signature_yields_no_baseline_and_warns(self):
        recs = _recs("Footage 2: 080530 soaping", "x")
        with self.assertLogs(gsh._LOGGER, level="WARNING") as cm:
            self.assertEqual(gsh.stable_baseline_from_signatures(recs, self.SIGS), set())
        self.assertIn("apron not kept", cm.output[0])

    def test_ambiguous_signature_yields_no_baseline_and_warns(self):
        recs = _recs("080530 a", "080530 b", "apron not kept")
        with self.assertLogs(gsh._LOGGER, level="WARNING") as cm:
            self.assertEqual(gsh.stable_baseline_from_signatures(recs, self.SIGS), set())
        self.assertIn("080530", cm.output[0])
        self.assertIn("R02", cm.output[0])

    def test_unrelated_golden_never_falls_back_to_row_ids(self):
        recs = _recs(*["no signature here"] * 20)
        with self.assertLogs(gsh._LOGGER, level="WARNING"):
            self.assertEqual(gsh.stable_baseline_from_signatures(recs), set())

    def test_default_signatures_reproduce_golden_v1_stable_set(self):
        recs = [json.loads(line) for line in (DATA / "golden_v1.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual(gsh.stable_baseline_from_signatures(recs), {"R08", "R09", "R12", "R13", "R14", "R19"})

    def test_golden_v1_version_unchanged(self):
        self.assertEqual(load_golden(DATA / "golden_v1.jsonl").version, "golden_v1@9426fe9b52")


class JudgeLocationTest(unittest.TestCase):
    def test_default_is_global(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("EVAL_JUDGE_LOCATION", None)
            self.assertEqual(score_run.judge_location(), "global")

    def test_blank_is_global(self):
        with mock.patch.dict(os.environ, {"EVAL_JUDGE_LOCATION": "  "}):
            self.assertEqual(score_run.judge_location(), "global")

    def test_env_override(self):
        with mock.patch.dict(os.environ, {"EVAL_JUDGE_LOCATION": "us-central1"}):
            self.assertEqual(score_run.judge_location(), "us-central1")


if __name__ == "__main__":
    unittest.main()
