"""Skip the tests that replay the customer's private golden dataset when it is not present.

`eval/data/` (golden_v1.jsonl + recorded baseline runs) holds customer labels and video IDs, so it is
not shipped in this repository. Put your own dataset there (see eval/data/README.md) to run them.
"""
from __future__ import annotations

from pathlib import Path

import pytest

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
GOLDEN = DATA_DIR / "golden_v1.jsonl"
BASELINE_RUN = DATA_DIR / "runs" / "v6_0928_0811"

# Tests that read GOLDEN and/or the recorded baseline run under DATA_DIR/runs.
NEEDS_PRIVATE_DATA = {
    "test_end_to_end_replay_matches_baseline_findings",
    "test_frozen_specs_cover_all_16_clips",
    "test_bare_timeout_is_retried",
    "test_gives_up_after_max_attempts",
    "test_resume_skips_checkpointed_clips_and_matches",
    "test_loop_early_stop_and_variance_confirmation",
}


# Whole modules that replay recorded private runs (eval/rounds/eval_history.jsonl + eval/results/).
NEEDS_PRIVATE_DATA_MODULES = {"test_sheet_report.py"}
HISTORY = DATA_DIR.parent / "rounds" / "eval_history.jsonl"


def pytest_collection_modifyitems(config, items):
    have_golden = GOLDEN.is_file() and BASELINE_RUN.is_dir()
    have_history = HISTORY.is_file()
    skip = pytest.mark.skip(reason=f"private golden dataset / run history not present under {DATA_DIR.parent}")
    for item in items:
        if not have_golden and item.name in NEEDS_PRIVATE_DATA:
            item.add_marker(skip)
        elif not (have_golden and have_history) and Path(str(item.fspath)).name in NEEDS_PRIVATE_DATA_MODULES:
            item.add_marker(skip)
