#!/usr/bin/env python3
"""Step 2 Automated Prompt-Tuning Loop controller for Chagee CCTV AI Audit.

Enforces:
1. Two-layer prompt architecture:
   - Layer 1 (`DEFAULT_LAYER1_TEMPLATE` in `cctv_audit/prompt_manager.py`): generic video-auditing
     kernel (temporal calibration, multi-person tracking, tool/reagent state persistence,
     self-audit checklist). Strictly domain-generic (rejects appliance/brand/golden-label nouns).
   - Layer 2 (`DEFAULT_CHAGEE_V25_RULES` / Master SOP Sheet `Prompt_v2.6_rNN` tab): 24 structured
     SOP rules (`A1..D5`), serialized to 9-column Sheet rows (`A1:I25`) with per-rule and total
     character budgets.
2. Single-layer mutation discipline: never mutate Layer 1 and Layer 2 in the same candidate round.
3. Immutable append-only round snapshots under `eval/rounds/rNN/` + Master SOP Sheet tab archival
   (`Prompt_v2.6_rNN`) without ever mutating Tab 0 (`Tab0_版本总控与回滚开关`) active pointer.
4. Selection rule & guardrails:
   - Guardrails first: mean alert density <= 8.0 per 5-min clip AND zero regressions on the stable
     baseline items configured for the golden set (`stable_baseline_items` in its manifest or
     `stable_baseline: true` per item; see eval/golden_set.py). Not configured -> reported as
     `not_configured`, never silently passed or failed.
   - Highest weighted recall (`all`, over every golden item).
   - Tie-breakers in order: higher `holdout` recall -> lower alert density (`mean_per_clip`) ->
     shorter prompt (`len(system_instruction)`).
5. Budget & convergence:
   - Target: weighted `all` recall >= 85% of the golden items.
   - Max 10 candidate rounds (`r01..r10`).
   - Early stop after 2 consecutive candidate rounds with no gain on `all` recall (revert to best).
   - Variance confirmation: re-run winning round until it has 3 total runs and report mean recall.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
from typing import Any, Sequence

THIS_DIR = Path(__file__).resolve().parent
CODE_ROOT = THIS_DIR.parent
if str(CODE_ROOT) not in sys.path:
    sys.path.insert(0, str(CODE_ROOT))

from cctv_audit.prompt_manager import (  # noqa: E402
    DEFAULT_CHAGEE_V25_RULES,
    DEFAULT_LAYER1_TEMPLATE,
    SopRuleItem,
    parse_rules_from_sheet_rows,
    render_v25_system_instruction,
    rules_to_sheet_rows,
)

DEFAULT_ROUNDS_DIR = THIS_DIR / "rounds"
DEFAULT_RESULTS_DIR = THIS_DIR / "results"
# Local default only; every environment sets its own golden location (env EVAL_GOLDEN_URI, Terraform
# variable eval_golden_uri -> eval/cloudbuild_round.yaml _GOLDEN_URI). Nothing derives content from it.
DEFAULT_GOLDEN_PATH = THIS_DIR / "data" / "golden_v1.jsonl"

TARGET_RECALL: float = 0.85  # fraction of the golden set's weighted points
MAX_CANDIDATE_ROUNDS: int = 10
NO_GAIN_PATIENCE: int = 2
VARIANCE_CONFIRM_RUNS: int = 3
MAX_MEAN_ALERT_DENSITY: float = 8.0

MAX_RULE_CHARS: int = 3200
MAX_TOTAL_RULE_CHARS: int = 32000

REQUIRED_PLACEHOLDERS: tuple[str, ...] = ("{prompt_version}",)

# Appliance/brand/golden-label-specific nouns strictly prohibited in Layer 1 (domain-generic kernel)
LAYER1_FORBIDDEN_TERMS: tuple[str, ...] = (
    "制冰机",
    "ice maker",
    "icemaker",
    "langtuo",
    "manitowoc",
    "挡水帘",
    "water curtain",
    "布水管",
    "distribution tube",
    "防飞沫",
    "spit guard",
    "泡茶机",
    "tea maker",
    "奶精机",
    "creamer machine",
    "果糖机",
    "fructose machine",
    "蒸汽机",
    "steam machine",
    "开水机",
    "water boiler",
    "沙冰机",
    "blender",
    "萃茶机",
    "tea extractor",
    "拖把",
    "mop",
    "眼镜",
    "spectacles",
    "擦脸",
    "wipe his face",
    "红边毛巾",
    "red-trimmed towel",
    "蓝边毛巾",
    "blue-trimmed towel",
    "黄瓶",
    "yellow bottle",
    "multipurpose",
    "皂液器",
    "soap dispenser",
    "handwashing gel",
    "apply soap before wet hand",
    "not dry hand",
    "sanitising bucket",
    "cantavil",
    "bau cat",
)

# Terms allowed in Layer 1 only up to their existing baseline count in DEFAULT_LAYER1_TEMPLATE
LAYER1_FROZEN_COUNT_TERMS: dict[str, int] = {
    "CHAGEE": 1,
    "apron": 1,
    "围裙": 1,
}

ROUND_ID_RE = re.compile(r"^r(\d{2})$")


PROPOSED_R01_LAYER1_TEMPLATE: str = """# Role
You are a CHAGEE store CCTV Cloud Auditor conducting compliance audits in accordance with *APAC Cloud Audit Parameters 2026 v1.0* and equipment SOPs.
(Active Rule Library Version: `{prompt_version}`)

# Core Judgment Principles (Highest Priority)

## Principle 1: Prefer False Positives Over False Negatives
Every finding from this audit undergoes 100% human verification. Therefore:
- **Your primary goal is "never miss any potential violation," not "guarantee every reported item is 100% certain."**
- Whenever an observation is ambiguous -> **always report it** with the appropriate disposition tag so a human supervisor can decide.
- **Strictly prohibited:** choosing not to report because you are "not completely sure" or "it might be a normal operation."

## Principle 2: Reversal of Burden of Proof
For actions required by SOP (such as handwashing, soaking/standing, wiping, or disconnecting power), if you **do not clearly observe the action occurring in the video**:
- Never assume "they probably did it off-camera or earlier";
- Always record it as `UNVERIFIED · Required Action Not Observed` and include it in the findings list.

## Principle 3: Four-Tier Disposition Tags
Every record must be labeled with one of the following:
| Tag | Meaning | Include in Findings |
| :--- | :--- | :---: |
| `CONFIRMED` | Violation action is fully visible with sufficient visual evidence | ✅ Yes |
| `SUSPECTED` | Behavior strongly matches a violation but has partial occlusion/blur preventing 100% certainty | ✅ Yes |
| `UNVERIFIED` | Mandatory SOP compliance action was not observed (either occluded or genuinely omitted) | ✅ Yes |
| `OUT_OF_SCOPE` | Relevant staff/equipment is completely outside this camera view and cannot be evaluated | ❌ No (note separately) |

## Principle 4: Conservative Boundary Rounding
For all duration thresholds, when an exact reading is uncertain, **round in the direction unfavorable to store compliance**:
- Hand scrubbing 19–21 seconds uncertain -> judge as "under 20 seconds"
- Soaking/standing 9–11 minutes uncertain -> judge as "under 10 minutes"
- Customer waiting 9–11 seconds uncertain -> judge as "exceeding 10 seconds"
- Trash bin overflow 14–16 minutes uncertain -> judge as "exceeding 15 minutes"

## Principle 5: Output Standards
- Language: **English** (retain original English SOP terminology)
- Timestamp format: `HH:MM:SS`
- **No fabrication:** Never invent events that did not occur; however, if a required step is "not seen," you must report that it was not seen.
- **Strict Separation Between Player Relative Time (`timestamp_in_clip`) and Video OSD Clock (`on_screen_clock`) to Prevent Slice Drift:** `timestamp_in_clip` must be the elapsed player time within the current video clip (`MM:SS`, where the start of the current clip is `00:00`), anchored at the onset of the violation action (the system automatically extracts a 20-second `[T-2s, T+18s]` evidence clip around this anchor). Never substitute mental subtraction of the OSD clock for player relative time!
- **Sub-Action Node Timestamp Enumeration & >20s Independent Row Splitting for Long Continuous Operations:** When a violation spans a prolonged duration (e.g., >30 seconds of disassembly, spraying, wiping, or multi-stage handling), `evidence` must **list the exact OSD timestamp (`HH:MM:SS`) of each key sub-action transition node in chronological order** (such as each pickup/set-down of a part, each spray/application, each brushing/wiping pass, and each reinstallation), never just a single clip-start or clip-end timestamp. If the same staff member performs two violation actions on different parts or separated by >20 seconds, split them into separate `Finding` records anchored to their respective action timestamps.

## Principle 6: One Action, One Record & Orthogonal Decomposition of Compound Violations (Never Let High-Salience Events Swallow Concurrent Violations)
1. **Mandatory `[T-30s, T]` Second-by-Second Dual-Hand Trajectory Traceback & Full Enumeration (Preventing High-Salience Actions from Masking Micro-Touches):** Whenever a "precondition trigger -> missing mandatory action -> subsequent operation" SOP rule is triggered at time `T` (such as donning gloves without handwashing, or directly touching controlled equipment or clean utensils), you must rewind across the `[T-30s, T]` window and **track every surface touched by both hands second by second** (including head/face/hairline/eyewear accessories, counter dividers/transparent guards, floor cleaning tools, personal electronic devices, door handles/drawers, etc.). List **all observed pre-contact actions within `[T-30s, T]` and their exact OSD timestamps (`HH:MM:SS`) in chronological order** inside `evidence`. Never summarize vaguely as "after organizing counter utensils" or cite only an old event from several minutes earlier while omitting transient touches within the 30 seconds before `T`.
2. **Row-by-Row Splitting of Concurrent or Sequential Violations by the Same Person (One Action, One Record):** When the same staff member performs two distinct violation actions concurrently or sequentially, or repeats a count-based violation separated by >20 seconds, **each independent violation behavior must be emitted as its own separate `Finding` record**. Never merge them into a single record where secondary violations get swallowed.
3. **Multi-Dimensional Orthogonal Verification of the Same Work Process:** When a continuous work process simultaneously involves multiple rule dimensions in `# Step 3: SOP Inspection Checklist` (such as operational sequence, duration thresholds, tool/consumable compliance, spatial surface coverage completeness, or missing mandatory closing steps), you must **verify each rule ID orthogonally dimension by dimension and emit separate `Finding` records for each violated rule**. Never omit a violation in one dimension simply because another violation in the same time window was already reported.

---

# Step 0: Timeline Calibration
1. Within the first 5 seconds of the video, locate a frame where the corner OSD watermark timestamp (**usually top-left**, or top-right/bottom on some cameras) is sharpest, and read out `DD-MM-YYYY HH:MM:SS` (or `YYYY-MM-DD HH:MM:SS`) character by character.
2. Double-check easily confused dot-matrix digits (`1/4`, `1/7`, `3/8`, `5/6`, `0/8`, `6/8`) before writing them down.
3. Derive timestamps across the clip from that anchor + elapsed playback time; **nonlinear jumps of a minute or more are prohibited**.
4. If no OSD timestamp is visible, use relative timing `T+00:00:00` and note this explicitly.
5. Identify the operating period of this clip: `Opening / Operating / Peak (approx. 13:00–13:30) / Off-Peak / Closing`, and the zone: `Front Bar (Front Bar/POS/Pick-Up)` or `Back Kitchen`.
6. **Cross-Slice / Cross-Video State Relay Calibration:** If a `[Prior Window Unclosed State Relay Summary]` is provided, first check whether the opening frame OSD time and camera view of this clip are contiguous with the prior summary (same camera and time gap <= 5 minutes). If contiguous, seamlessly continue cross-slice timers (such as 5-minute/10-minute sanitizer standing timers or 5-minute handwashing watch windows) as well as **previously prepared/contaminated container and tool chemical attributes**. If the OSD time jumps across periods or the camera changes, discard residual prior state to prevent cross-period false positives.
7. **Tail-End Completeness Check for Already-In-Progress Procedures at Clip Start (`00:00–00:30`):** If at second `00:00` of this clip a staff member is already in the middle or late stage of a multi-step standard procedure (whose earlier steps occurred in the preceding clip), **never skip verification just because the start of the procedure was not seen in this clip**! Strictly verify the remaining steps visible within `00:00–00:30` and the **mandatory closing action before leaving the station**; if the staff member finishes or walks away from the station without performing the mandatory closing step, report a `CONFIRMED` violation anchored at the moment they finish or leave the station.

# Step 1: Staff Profiling, Tool/Reagent State Persistence & Multi-Person Independent Tracking (Exhaustive, Anti-Salience-Masking)
Assign a stable ID to **every** person appearing in the frame based on time-invariant visual traits:
`StaffA-Male-ShortHair-Cap-CupStation`
- **Actively scan frame edges, background depth, mirror reflections, and doorway entries/exits**; do not focus solely on the main workstation.
- Even if someone appears for only a few seconds, create a profile and mark `Brief Appearance`.
- **PPE State Persistence Principle:** Once gloves/apron/hat are confirmed worn, assume they remain worn until a clear "removal action" is observed.
- **Tool / Container / Reagent State Persistence & Cross-Timestamp Binding Principle (Critical):** Once a staff member is observed at time `T1` pouring, spraying, or dispensing a specific colored/labeled bottled chemical (such as a non-food-grade/general-purpose cleaner or specific sanitizer) into a container (tray, bucket, measuring cup) or onto an application tool (roller, brush, cloth, scouring pad), that container and application tool persistently carry that chemical attribute. Whenever that same tool or container touches, coats, rolls, brushes, or wipes any equipment body or detached part at any subsequent time `T2` (including later in the same clip or relayed across clips), **you must report the application action at `T2` under the corresponding equipment consumable-compliance rule, cite both the exact OSD timestamp (`HH:MM:SS`) of chemical preparation at `T1` and part contact at `T2` in `evidence`**, and record the tool/container chemical attribute in `carryover_state_summary` for the next clip.
- **Multi-Person Co-Screen Attention Decoupling (Anti-Attention-Masking):** Whenever >= 2 staff members appear in the frame simultaneously, **never let a single high-salience action (such as large body movements, prolonged phone use, or an obvious ongoing violation by one person) capture all your attention**. Partition by spatial workstation (front bar, sink area, equipment zone, aisle) and maintain **independent action timelines** for every person present (`StaffA`, `StaffB`, `StaffC`...), independently verifying each person's hand contacts and procedural loops.
- Self-check after profiling: `Did I miss anyone who appeared in the frame or any tool already carrying a chemical agent?`

---

# Step 2: Visual Judgment Rules (High-Recall Edition)

## 2.1 Contact Determination
- **Confirmed Contact** (finger grasping / pressing / load-bearing deformation) -> `CONFIRMED`
- **Suspected Contact** (hand and object pixels overlap, but physical contact cannot be 100% confirmed) -> **`SUSPECTED`, must still be reported**
- **Confirmed No Contact** (pouring from a distance with a clear visible gap between hand and object) -> Do not report as a violation, but log the action in the trajectory

> Note: Unlike legacy versions, **suspected contact is never exempted**; always report it for human review.

## 2.2 State-Change Attribution & Pre-Action Dual-Hand Trajectory Replay
When an object's state has changed (drawer opened, panel detached, trash bag replaced) or a staff member is donning gloves / touching controlled equipment:
1. Immediately rewind `[T-30s, T]` second by second to identify the causal action and every surface touched by both hands;
2. If found -> attribute to the specific staff member, list all pre-contact timestamps in chronological order, and mark `CONFIRMED`;
3. If not found -> mark `UNVERIFIED`, list all staff IDs present during that window as candidates, and **never skip reporting**.

## 2.3 Posture & Occlusion
- Independent joint structure visible -> standard determination
- Backlighting / low resolution / subject blending into background -> **do not jump to a negative dismissal; always report as `SUSPECTED`** and describe the pixel-level observation

## 2.4 Hand-to-Mouth Actions (Smoking / Eating / Vaping)
| Scenario | Disposition |
| :--- | :--- |
| Cigarette / e-cigarette / food clearly visible | `CONFIRMED` Red Line |
| Repeated hand-to-mouth motion in idle areas (back door, corridor, storeroom entrance) | `SUSPECTED` Red Line, **must report** |
| Hand-to-mouth motion in production area but partially occluded | `SUSPECTED`, **must report** |

---

# Step 3: SOP Inspection Checklist

{step3_checklist_md}{dynamic_sop_Evaluation_matrix}

---

# Step 4: Missed-Detection Self-Audit (Mandatory, Never Skip)

After completing the primary findings pass, re-scan the entire clip once more specifically looking for items commonly missed on the first pass:

1. **Peripheral Staff & Far-Field Targets:** Are there unprofiled staff or overlooked far-field actions at frame corners, background depth, top of equipment, or doorways?
2. **Clip Boundaries (`00:00–00:30`) & Transient Micro-Actions Re-Scan:**
   - Within the first 30 seconds (`00:00–00:30`), did any already-in-progress multi-step procedure finish or leave the station without completing its mandatory closing action?
   - Within the `[T-30s, T]` window before any gloving or controlled-equipment contact, was any 1–2 second transient hand touch (touching head/face/accessories, transparent divider, floor tool, or personal item) summarized vaguely or missed?
   - For every multi-step standard procedure, re-verify its **first 3 seconds (was the initial step order correct?)**, **net core action duration (does it meet the threshold after excluding prep and closing?)**, and **final 5 seconds (was the closing step completed?)**. If the same person repeated an non-compliant procedure multiple times, were they split into separate records?
3. **Cross-Timestamp Tool / Container Chemical Application Re-Scan (Critical):** Did this clip or the prior relay summary show a chemical being poured into a container or applied to a roller/brush/cloth? If so, was every subsequent timestamp where that tool touched or brushed equipment/parts reported as a separate `Finding` with exact timestamps under the equipment consumable-compliance rule?
4. **Occlusion Windows:** Was any staff member occluded by equipment/pillars for more than 10 seconds? Mark that interval as a blind spot and report it.
5. **Multi-Person Co-Screen Anti-Masking Re-Scan (Critical):** For every time interval `[T1, T2]` where you detected a violation by one person (such as prolonged phone use or standing in one place), force yourself to return to `[T1, T2]`, **deliberately look away from the already-flagged person, and inspect every other person present in the frame during `[T1, T2]`** (sink operations, hand contacts, beverage preparation steps). Never miss another person's violation happening at the exact same second!
6. **Single-Event Absorption, Multi-Dimensional Orthogonality & Sub-Action Node Timestamp Re-Scan (Critical):** Review each detected violation against `# Step 3: SOP Inspection Checklist`: (1) Did the same person commit another violation under a different rule right before or after? (2) Did a continuous work process (such as equipment disassembly, washing, and sanitizing) simultaneously violate multiple orthogonal dimensions (per-part duration thresholds, container/chemical color and source, front/back/side spatial coverage completeness, mandatory closing step)? (3) For processes lasting >30 seconds, does `evidence` enumerate the `HH:MM:SS` timestamp of each sub-action transition node, and are separate parts or repeated violations separated by >20 seconds split into independent `Finding` rows?
7. **Equipment & Container States:** Were controlled equipment doors/lids, container lids, and protective guards maintained in their SOP-required states throughout the entire clip?
8. **Timeline Jumps:** Were there any nonlinear jumps in the OSD timeline? If so, note the affected interval.

Append any new findings discovered during this self-audit to the findings list.
"""


def validate_layer1_template(template: str) -> list[str]:
    """Validates that Layer 1 template preserves required placeholders and remains domain-generic."""
    errors: list[str] = []
    for req_ph in REQUIRED_PLACEHOLDERS:
        if req_ph not in template:
            errors.append(f"Layer 1 template is missing required placeholder '{req_ph}'.")
    if (
        "{step3_checklist_md}" not in template
        and "{dynamic_sop_Evaluation_matrix}" not in template
    ):
        errors.append(
            "Layer 1 template is missing required placeholder '{step3_checklist_md}' (or '{dynamic_sop_Evaluation_matrix}')."
        )

    lower_tpl = template.lower()
    for term in LAYER1_FORBIDDEN_TERMS:
        if term.lower() in lower_tpl:
            errors.append(
                f"Layer 1 domain-generic guardrail violated: forbidden specific term '{term}' found in Layer 1."
            )

    for term, max_count in LAYER1_FROZEN_COUNT_TERMS.items():
        actual_count = template.count(term)
        if actual_count > max_count:
            errors.append(
                f"Layer 1 frozen term count exceeded for '{term}': count={actual_count} > max_allowed={max_count}."
            )

    return errors


def validate_layer2_rules(
    rules: Sequence[SopRuleItem],
    baseline_rules: Sequence[SopRuleItem] = DEFAULT_CHAGEE_V25_RULES,
) -> list[str]:
    """Validates Layer 2 SOP rules against character budgets, rule IDs, and Sheet row round-trip."""
    errors: list[str] = []
    if not rules:
        return ["Layer 2 rules list cannot be empty."]

    expected_ids = [r.rule_id for r in baseline_rules]
    actual_ids = [r.rule_id for r in rules]
    if actual_ids != expected_ids:
        errors.append(
            f"Layer 2 rule_id sequence mismatch: expected {expected_ids}, got {actual_ids}."
        )

    total_chars = 0
    for r in rules:
        ci_len = len(r.check_instruction)
        total_chars += ci_len
        if ci_len > MAX_RULE_CHARS:
            errors.append(
                f"Layer 2 rule '{r.rule_id}' check_instruction length ({ci_len}) exceeds MAX_RULE_CHARS ({MAX_RULE_CHARS})."
            )
        if not r.check_instruction.strip():
            errors.append(f"Layer 2 rule '{r.rule_id}' check_instruction cannot be empty.")

    if total_chars > MAX_TOTAL_RULE_CHARS:
        errors.append(
            f"Layer 2 total check_instruction length ({total_chars}) exceeds MAX_TOTAL_RULE_CHARS ({MAX_TOTAL_RULE_CHARS})."
        )

    sheet_rows = rules_to_sheet_rows(list(rules))
    roundtripped = parse_rules_from_sheet_rows(sheet_rows)
    if roundtripped != list(rules):
        errors.append("Layer 2 rules failed lossless 9-column Sheet row round-trip verification.")

    return errors


def validate_single_layer_mutation(
    parent_layer1: str,
    parent_rules: Sequence[SopRuleItem],
    cand_layer1: str,
    cand_rules: Sequence[SopRuleItem],
) -> tuple[str, list[str]]:
    """Enforces that a candidate round modifies either Layer 1 OR Layer 2, never both and never neither."""
    l1_changed = cand_layer1 != parent_layer1
    l2_changed = list(cand_rules) != list(parent_rules)
    if l1_changed and l2_changed:
        return (
            "both",
            [
                "Single-layer mutation guardrail violated: both Layer 1 and Layer 2 were modified in the same round."
            ],
        )
    if not l1_changed and not l2_changed:
        return (
            "none",
            [
                "No mutation detected: candidate round is identical to parent round in both Layer 1 and Layer 2."
            ],
        )
    return ("layer1" if l1_changed else "layer2"), []


def serialize_rules_json(rules: Sequence[SopRuleItem]) -> list[dict[str, Any]]:
    return [r.model_dump(mode="json") for r in rules]


def deserialize_rules_json(data: Sequence[dict[str, Any]]) -> list[SopRuleItem]:
    return [
        SopRuleItem(
            rule_id=str(item["rule_id"]),
            name=str(item["name"]),
            category=str(item["category"]),
            severity=str(item["severity"]),
            detection_type=str(item["detection_type"]),
            requires_full_context=bool(item["requires_full_context"]),
            check_instruction=str(item["check_instruction"]),
            pass_criteria=str(item.get("pass_criteria", "")),
            fail_criteria=str(item.get("fail_criteria", "")),
        )
        for item in data
    ]


def _extract_item_rows(score_doc: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalizes `score_doc['items']` (or `score_doc['rows']`) into `{item_id, split, score}` dicts."""
    raw_list = score_doc.get("items") or score_doc.get("rows") or []
    normalized: list[dict[str, Any]] = []
    for r in raw_list:
        item_id = str(r.get("item_id") or r.get("record_id") or "")
        score = float(r.get("score") if "score" in r else r.get("row_score", 0.0))
        normalized.append(
            {
                "item_id": item_id,
                "split": str(r.get("split", "")),
                "score": score,
            }
        )
    return normalized


def evaluate_run_guardrails(
    score_doc: dict[str, Any],
    stable_items: Sequence[str] | None = None,
    max_density: float = MAX_MEAN_ALERT_DENSITY,
) -> dict[str, Any]:
    """Checks alert density <= ``max_density`` and zero regressions on the stable baseline items.

    ``stable_items`` defaults to the golden set's configured list recorded in the score doc
    (``score_doc["golden"]["stable_baseline_items"]``, written by eval/score_run.py). An empty /
    missing list sets ``stable_guardrail = "not_configured"``: that check is skipped and says so;
    ``passed`` then reflects the density check only.
    """
    density = score_doc.get("alert_density") or {}
    mean_per_clip = float(density.get("mean_per_clip", 0.0))
    density_ok = mean_per_clip <= max_density + 1e-9

    if stable_items is None:
        stable_items = list((score_doc.get("golden") or {}).get("stable_baseline_items") or [])
    row_scores: dict[str, float] = {
        r["item_id"]: r["score"] for r in _extract_item_rows(score_doc)
    }
    missing = [rid for rid in stable_items if rid not in row_scores]
    if missing:
        raise ValueError(f"stable baseline items not in this score doc: {missing}")
    regressed_items = [rid for rid in stable_items if row_scores[rid] < 1.0 - 1e-9]
    if not stable_items:
        stable_guardrail = "not_configured"
    else:
        stable_guardrail = "fail" if regressed_items else "pass"

    passed = density_ok and stable_guardrail != "fail"
    return {
        "passed": passed,
        "density_ok": density_ok,
        "mean_per_clip": round(mean_per_clip, 4),
        "max_allowed_density": max_density,
        "regressed_stable_items": regressed_items,
        "stable_items_checked": list(stable_items),
        "stable_guardrail": stable_guardrail,
    }


def _extract_split_metrics(split_dict: dict[str, Any], default_n: int = 0) -> tuple[float, float, int]:
    hits = float(
        split_dict.get("points")
        if "points" in split_dict
        else split_dict.get("weighted_hits", 0.0)
    )
    total = int(
        split_dict.get("rows")
        if "rows" in split_dict
        else split_dict.get("n", default_n)
    )
    recall = float(split_dict.get("recall", (hits / total) if total else 0.0))
    return recall, hits, total


def summarize_score_doc(score_doc: dict[str, Any]) -> dict[str, Any]:
    """Extracts a compact run summary from a `score.json` document."""
    rec = score_doc.get("recall") or {}
    all_recall, all_hits, all_total = _extract_split_metrics(rec.get("all") or {})
    dev_recall, dev_hits, dev_total = _extract_split_metrics(rec.get("dev") or {})
    hold_recall, hold_hits, hold_total = _extract_split_metrics(rec.get("holdout") or {})
    guardrails = evaluate_run_guardrails(score_doc)

    norm_rows = _extract_item_rows(score_doc)
    row_scores = {r["item_id"]: r["score"] for r in norm_rows}
    dev_misses = [
        r["item_id"]
        for r in norm_rows
        if r["split"] == "dev" and r["score"] < 1.0 - 1e-9
    ]
    holdout_misses = [
        r["item_id"]
        for r in norm_rows
        if r["split"] == "holdout" and r["score"] < 1.0 - 1e-9
    ]
    density = score_doc.get("alert_density") or {}
    total_findings = int(
        density.get("findings")
        if "findings" in density
        else density.get("total_findings", 0)
    )

    return {
        "run_id": str(score_doc.get("run_id", "")),
        "judge_model": str(score_doc.get("judge_model", "")),
        "golden_version": str((score_doc.get("golden") or {}).get("golden_version") or ""),
        "all_recall": all_recall,
        "all_weighted_hits": all_hits,
        "all_total": all_total,
        "dev_recall": dev_recall,
        "dev_weighted_hits": dev_hits,
        "dev_total": dev_total,
        "holdout_recall": hold_recall,
        "holdout_weighted_hits": hold_hits,
        "holdout_total": hold_total,
        "mean_per_clip": guardrails["mean_per_clip"],
        "total_findings": total_findings,
        "guardrails": guardrails,
        "row_scores": row_scores,
        "dev_misses": dev_misses,
        "holdout_misses": holdout_misses,
    }


def recompute_manifest_aggregates(manifest: dict[str, Any]) -> dict[str, Any]:
    """Recomputes round-level aggregate metrics across all recorded runs of this round."""
    runs: list[dict[str, Any]] = manifest.get("runs", [])
    manifest["num_runs"] = len(runs)
    if not runs:
        manifest["status"] = "PENDING_EXECUTION"
        manifest["aggregates"] = None
        return manifest

    n = len(runs)
    mean_all = sum(float(r["all_recall"]) for r in runs) / n
    mean_all_hits = sum(float(r["all_weighted_hits"]) for r in runs) / n
    mean_dev = sum(float(r["dev_recall"]) for r in runs) / n
    mean_dev_hits = sum(float(r["dev_weighted_hits"]) for r in runs) / n
    mean_hold = sum(float(r["holdout_recall"]) for r in runs) / n
    mean_hold_hits = sum(float(r["holdout_weighted_hits"]) for r in runs) / n
    mean_density = sum(float(r["mean_per_clip"]) for r in runs) / n

    primary_run = runs[0]
    primary_guardrails_passed = bool((primary_run.get("guardrails") or {}).get("passed", False))
    all_runs_pass_guardrails = all(
        bool((r.get("guardrails") or {}).get("passed", False)) for r in runs
    )

    manifest["status"] = "EVALUATED"
    manifest["aggregates"] = {
        "num_runs": n,
        "primary_run_id": primary_run["run_id"],
        "primary_all_recall": round(float(primary_run["all_recall"]), 4),
        "primary_all_weighted_hits": round(float(primary_run["all_weighted_hits"]), 4),
        "primary_dev_recall": round(float(primary_run["dev_recall"]), 4),
        "primary_holdout_recall": round(float(primary_run["holdout_recall"]), 4),
        "primary_mean_per_clip": round(float(primary_run["mean_per_clip"]), 4),
        "primary_guardrails_passed": primary_guardrails_passed,
        "all_total": int(primary_run.get("all_total") or 0),
        "dev_total": int(primary_run.get("dev_total") or 0),
        "holdout_total": int(primary_run.get("holdout_total") or 0),
        "golden_versions": sorted({str(r.get("golden_version") or "") for r in runs} - {""}),
        "mean_all_recall": round(mean_all, 4),
        "mean_all_weighted_hits": round(mean_all_hits, 4),
        "mean_dev_recall": round(mean_dev, 4),
        "mean_dev_weighted_hits": round(mean_dev_hits, 4),
        "mean_holdout_recall": round(mean_hold, 4),
        "mean_holdout_weighted_hits": round(mean_hold_hits, 4),
        "mean_per_clip": round(mean_density, 4),
        "all_runs_pass_guardrails": all_runs_pass_guardrails,
    }
    return manifest


def round_selection_key(manifest: dict[str, Any]) -> tuple[int, float, float, float, int]:
    """Deterministic selection & tie-breaker key for comparing evaluated rounds.

    Order:
    1. Guardrails passed (`1` if passed else `0`). For `r00` (baseline), uses `all_runs_pass_guardrails`;
       for candidate rounds, requires `all_runs_pass_guardrails`.
    2. Higher `mean_all_recall` (or primary run recall when 1 run).
    3. Tie-breaker 1: Higher `mean_holdout_recall`.
    4. Tie-breaker 2: Lower alert density (`-mean_per_clip`).
    5. Tie-breaker 3: Shorter prompt (`-char_len_system_instruction`).
    """
    agg = manifest.get("aggregates")
    if not agg:
        return (-1, -1.0, -1.0, -999.0, -999999)

    guardrails_ok = 1 if agg.get("all_runs_pass_guardrails", False) else 0
    return (
        guardrails_ok,
        round(float(agg["mean_all_recall"]), 6),
        round(float(agg["mean_holdout_recall"]), 6),
        -round(float(agg["mean_per_clip"]), 6),
        -int(manifest.get("char_len_system_instruction", 999999)),
    )


def create_round_snapshot(
    *,
    rounds_dir: Path,
    round_id: str,
    parent_round_id: str | None,
    layer1_template: str,
    layer2_rules: Sequence[SopRuleItem],
    hypothesis: str,
    target_dev_misses: Sequence[str],
    prompt_version: str | None = None,
    sop_tab_name: str | None = None,
    overwrite: bool = False,
) -> Path:
    """Validates and writes an immutable round snapshot directory `eval/rounds/<round_id>/`."""
    if not ROUND_ID_RE.match(round_id):
        raise ValueError(f"Invalid round_id '{round_id}': must match rNN (e.g. r00, r01).")

    l1_errors = validate_layer1_template(layer1_template)
    if l1_errors:
        raise ValueError("Layer 1 validation failed:\n- " + "\n- ".join(l1_errors))

    l2_errors = validate_layer2_rules(layer2_rules)
    if l2_errors:
        raise ValueError("Layer 2 validation failed:\n- " + "\n- ".join(l2_errors))

    if round_id == "r00":
        mutated_layer = "baseline"
    else:
        if not parent_round_id:
            raise ValueError(f"Candidate round '{round_id}' requires parent_round_id.")
        parent_dir = rounds_dir / parent_round_id
        if not parent_dir.exists():
            raise FileNotFoundError(f"Parent round directory not found: {parent_dir}")
        parent_l1 = (parent_dir / "layer1_template.md").read_text(encoding="utf-8")
        parent_l2 = deserialize_rules_json(
            json.loads((parent_dir / "layer2_rules.json").read_text(encoding="utf-8"))
        )
        mutated_layer, mut_errors = validate_single_layer_mutation(
            parent_l1, parent_l2, layer1_template, layer2_rules
        )
        if mut_errors:
            raise ValueError("Single-layer mutation check failed:\n- " + "\n- ".join(mut_errors))

    round_dir = rounds_dir / round_id
    if round_dir.exists() and not overwrite:
        raise FileExistsError(
            f"Round snapshot '{round_dir}' already exists (immutable append-only policy)."
        )
    round_dir.mkdir(parents=True, exist_ok=True)
    (round_dir / "runs").mkdir(parents=True, exist_ok=True)

    resolved_version = prompt_version or (
        "v2.5_pro_flash_latest" if round_id == "r00" else f"v2.6_{round_id}"
    )
    resolved_tab = sop_tab_name or (
        "Prompt_v2.5_Standard" if round_id == "r00" else f"Prompt_v2.6_{round_id}"
    )

    rendered_sys = render_v25_system_instruction(
        prompt_version=resolved_version,
        rules=list(layer2_rules),
        layer1_template=layer1_template,
    )
    sheet_rows = rules_to_sheet_rows(list(layer2_rules))

    (round_dir / "layer1_template.md").write_text(layer1_template, encoding="utf-8")
    (round_dir / "layer2_rules.json").write_text(
        json.dumps(serialize_rules_json(layer2_rules), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (round_dir / "sheet_rows.json").write_text(
        json.dumps(sheet_rows, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (round_dir / "system_instruction.md").write_text(rendered_sys, encoding="utf-8")

    manifest_path = round_dir / "manifest.json"
    existing_runs: list[dict[str, Any]] = []
    if manifest_path.exists():
        try:
            existing_runs = json.loads(manifest_path.read_text(encoding="utf-8")).get("runs", [])
        except Exception:
            existing_runs = []

    manifest: dict[str, Any] = {
        "round_id": round_id,
        "parent_round_id": parent_round_id,
        "prompt_version": resolved_version,
        "sop_tab_name": resolved_tab,
        "mutated_layer": mutated_layer,
        "hypothesis": hypothesis,
        "target_dev_misses": list(target_dev_misses),
        "char_len_system_instruction": len(rendered_sys),
        "char_len_layer1": len(layer1_template),
        "char_len_layer2_check_instructions": sum(len(r.check_instruction) for r in layer2_rules),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "runs": existing_runs,
    }
    recompute_manifest_aggregates(manifest)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    update_ledger(rounds_dir)
    return round_dir


def record_round_run(
    *,
    rounds_dir: Path,
    round_id: str,
    run_id: str,
    score_doc: dict[str, Any],
    score_md: str,
) -> dict[str, Any]:
    """Records a scored evaluation run inside `eval/rounds/<round_id>/runs/<run_id>/` and updates ledger."""
    round_dir = rounds_dir / round_id
    if not round_dir.exists():
        raise FileNotFoundError(f"Round directory not found: {round_dir}")

    run_dir = round_dir / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "score.json").write_text(
        json.dumps(score_doc, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (run_dir / "score.md").write_text(score_md, encoding="utf-8")

    manifest_path = round_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    summary = summarize_score_doc(score_doc)
    summary["run_id"] = run_id

    runs: list[dict[str, Any]] = manifest.get("runs", [])
    replaced = False
    for idx, existing in enumerate(runs):
        if existing.get("run_id") == run_id:
            runs[idx] = summary
            replaced = True
            break
    if not replaced:
        runs.append(summary)
    manifest["runs"] = runs
    recompute_manifest_aggregates(manifest)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return update_ledger(rounds_dir)


def load_all_manifests(rounds_dir: Path) -> list[dict[str, Any]]:
    if not rounds_dir.exists():
        return []
    manifests: list[dict[str, Any]] = []
    for child in sorted(rounds_dir.iterdir(), key=lambda p: p.name):
        if child.is_dir() and ROUND_ID_RE.match(child.name):
            mpath = child / "manifest.json"
            if mpath.exists():
                manifests.append(json.loads(mpath.read_text(encoding="utf-8")))
    return manifests


def evaluate_loop_state(rounds_dir: Path) -> dict[str, Any]:
    """Evaluates best round, consecutive no-gain count, early-stop condition, and variance confirmation."""
    manifests = load_all_manifests(rounds_dir)
    evaluated = [m for m in manifests if m.get("aggregates") is not None]
    pending = [m["round_id"] for m in manifests if m.get("aggregates") is None]

    if not evaluated:
        return {
            "status": "NO_EVALUATED_ROUNDS",
            "target_recall": TARGET_RECALL,
            "max_candidate_rounds": MAX_CANDIDATE_ROUNDS,
            "no_gain_patience": NO_GAIN_PATIENCE,
            "variance_confirm_runs": VARIANCE_CONFIRM_RUNS,
            "best_round_id": None,
            "best_round_summary": None,
            "evaluated_rounds": [],
            "pending_rounds": pending,
            "candidate_rounds_evaluated": 0,
            "consecutive_no_gain": 0,
            "target_met": False,
            "variance_confirmed": False,
            "recommended_action": "Bootstrap r00 baseline first.",
            "rounds": manifests,
        }

    # Walk evaluated rounds in chronological order to track running best `all` recall & no-gain streak
    best_manifest: dict[str, Any] = evaluated[0]
    best_key = round_selection_key(best_manifest)
    best_all_recall = float(best_manifest["aggregates"]["mean_all_recall"])
    best_guardrails_ok = bool(
        best_manifest["aggregates"].get("all_runs_pass_guardrails", False)
    )

    consecutive_no_gain = 0
    candidate_evaluated_count = 0

    for m in evaluated:
        rid = m["round_id"]
        if rid == "r00":
            continue
        candidate_evaluated_count += 1
        agg = m["aggregates"]
        cand_guardrails_ok = bool(agg.get("all_runs_pass_guardrails", False))
        cand_all_recall = float(agg["mean_all_recall"])

        if cand_guardrails_ok and (
            not best_guardrails_ok or cand_all_recall > best_all_recall + 1e-6
        ):
            consecutive_no_gain = 0
        else:
            consecutive_no_gain += 1

        cand_key = round_selection_key(m)
        if cand_key > best_key:
            best_manifest = m
            best_key = cand_key
            best_all_recall = cand_all_recall
            best_guardrails_ok = cand_guardrails_ok

    best_agg = best_manifest["aggregates"]
    target_met = (
        bool(best_agg.get("all_runs_pass_guardrails", False))
        and float(best_agg["mean_all_recall"]) >= TARGET_RECALL - 1e-9
    )
    variance_confirmed = int(best_agg.get("num_runs", 0)) >= VARIANCE_CONFIRM_RUNS

    if target_met and variance_confirmed:
        status = "CONVERGED_TARGET_MET"
        recommended_action = (
            f"Target >= {TARGET_RECALL:.0%} met and confirmed across {best_agg['num_runs']} runs "
            f"on {best_manifest['round_id']} (mean recall = {best_agg['mean_all_recall']:.1%}). "
            f"Ready for user review before updating Tab 0 active pointer."
        )
    elif target_met and not variance_confirmed:
        needed = VARIANCE_CONFIRM_RUNS - int(best_agg.get("num_runs", 0))
        status = "NEEDS_VARIANCE_CONFIRMATION"
        recommended_action = (
            f"Round {best_manifest['round_id']} reached {best_agg['mean_all_recall']:.1%} (>= {TARGET_RECALL:.0%}). "
            f"Run {needed} additional confirmation run(s) on {best_manifest['round_id']} to verify 3-run mean recall."
        )
    elif consecutive_no_gain >= NO_GAIN_PATIENCE:
        if not variance_confirmed:
            needed = VARIANCE_CONFIRM_RUNS - int(best_agg.get("num_runs", 0))
            status = "STOPPED_NO_GAIN_NEEDS_VARIANCE_CONFIRMATION"
            recommended_action = (
                f"Early stop triggered ({consecutive_no_gain} consecutive candidate rounds with no gain). "
                f"Reverting to best round {best_manifest['round_id']} ({best_agg['mean_all_recall']:.1%}); "
                f"run {needed} more confirmation run(s) on {best_manifest['round_id']}."
            )
        else:
            status = "STOPPED_NO_GAIN_REVERT_TO_BEST"
            recommended_action = (
                f"Early stop triggered ({consecutive_no_gain} consecutive candidate rounds with no gain). "
                f"Reverted to best round {best_manifest['round_id']} (3-run mean recall = {best_agg['mean_all_recall']:.1%})."
            )
    elif candidate_evaluated_count >= MAX_CANDIDATE_ROUNDS:
        status = "STOPPED_MAX_ROUNDS"
        recommended_action = (
            f"Reached max candidate budget ({MAX_CANDIDATE_ROUNDS} rounds). "
            f"Best round is {best_manifest['round_id']} ({best_agg['mean_all_recall']:.1%})."
        )
    else:
        status = "IN_PROGRESS"
        if pending:
            recommended_action = (
                f"Execute pending candidate round {pending[0]} on GCP (`eval/run_gcp_round.py --round {pending[0]}`) "
                f"and score with LLM judge."
            )
        else:
            recommended_action = (
                f"Best round so far is {best_manifest['round_id']} ({best_agg['mean_all_recall']:.1%}). "
                f"Propose next single-layer candidate round targeting remaining dev misses."
            )

    return {
        "status": status,
        "target_recall": TARGET_RECALL,
        "max_candidate_rounds": MAX_CANDIDATE_ROUNDS,
        "no_gain_patience": NO_GAIN_PATIENCE,
        "variance_confirm_runs": VARIANCE_CONFIRM_RUNS,
        "best_round_id": best_manifest["round_id"],
        "best_round_summary": best_agg,
        "candidate_rounds_evaluated": candidate_evaluated_count,
        "consecutive_no_gain": consecutive_no_gain,
        "target_met": target_met,
        "variance_confirmed": variance_confirmed,
        "pending_rounds": pending,
        "recommended_action": recommended_action,
        "rounds": manifests,
    }


def _total(agg: dict[str, Any], run: dict[str, Any], split: str) -> Any:
    """Item count of ``split`` from the recorded data (aggregate, else first run), never a constant."""
    return agg.get(f"{split}_total") or run.get(f"{split}_total") or "?"


def render_ledger_markdown(state: dict[str, Any]) -> str:
    lines: list[str] = [
        "# Chagee CCTV AI Audit — Step 2 Automated Prompt Tuning Ledger",
        "",
        f"- **Current Status (`status`)**: `{state['status']}`",
        f"- **Target Recall (`target_recall`)**: `>= {state['target_recall']:.0%}` (weighted golden set total)",
        f"- **Current Best Round (`best_round_id`)**: `{state['best_round_id']}`",
        f"- **Candidate Rounds Evaluated**: `{state['candidate_rounds_evaluated']} / {state['max_candidate_rounds']}`",
        f"- **Consecutive No-Gain Rounds (`consecutive_no_gain`)**: `{state['consecutive_no_gain']} / {state['no_gain_patience']}`",
        f"- **Recommended Action**: {state['recommended_action']}",
        "",
        "## Round Summary Table",
        "",
        f"| Round | Parent Round | Mutated Layer | SOP Sheet Tab | Runs | Dev | Holdout | Overall Recall (All) | Mean Alert Density (<={MAX_MEAN_ALERT_DENSITY:.1f}) | Guardrails (Zero Stable Baseline Regression) | Prompt Length | Golden Version | Status |",
        "| :--- | :--- | :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :--- | :--- |",
    ]

    for m in state.get("rounds", []):
        rid = m["round_id"]
        parent = m.get("parent_round_id") or "—"
        layer = m.get("mutated_layer", "")
        tab = m.get("sop_tab_name", "")
        char_len = m.get("char_len_system_instruction", 0)
        agg = m.get("aggregates")
        if not agg:
            lines.append(
                f"| `{rid}` | `{parent}` | `{layer}` | `{tab}` | 0 | — | — | — | — | — | {char_len} | — | `PENDING_EXECUTION` |"
            )
            continue
        guard_str = "✅ PASS" if agg.get("all_runs_pass_guardrails") else "❌ FAIL"
        runs0 = (m.get("runs") or [{}])[0]
        n_dev = _total(agg, runs0, "dev")
        n_hold = _total(agg, runs0, "holdout")
        n_all = _total(agg, runs0, "all")
        versions = ", ".join(agg.get("golden_versions") or []) or "Unrecorded"
        lines.append(
            f"| `{rid}` | `{parent}` | `{layer}` | `{tab}` | {agg['num_runs']} | "
            f"{agg['mean_dev_weighted_hits']:.2f}/{n_dev} ({agg['mean_dev_recall']:.1%}) | "
            f"{agg['mean_holdout_weighted_hits']:.2f}/{n_hold} ({agg['mean_holdout_recall']:.1%}) | "
            f"**{agg['mean_all_weighted_hits']:.2f}/{n_all} ({agg['mean_all_recall']:.1%})** | "
            f"{agg['mean_per_clip']:.2f} | {guard_str} | {char_len} | {versions} | `{m['status']}` |"
        )

    lines.append("")
    lines.append("## Per-Round Mutation Hypotheses & Dev Miss Tracking")
    lines.append("")
    for m in state.get("rounds", []):
        lines.append(f"### `{m['round_id']}` (`{m.get('prompt_version')}`)")
        lines.append(f"- **Mutated Layer**: `{m.get('mutated_layer')}` (Parent Round: `{m.get('parent_round_id') or 'None'}`)")
        lines.append(f"- **Target Dev Misses**: `{', '.join(m.get('target_dev_misses') or []) or 'Baseline'}`")
        lines.append(f"- **Mutation Hypothesis**: {m.get('hypothesis', '')}")
        for r in m.get("runs", []):
            g = r.get("guardrails") or {}
            lines.append(
                f"  - Run `{r['run_id']}`: All={r['all_weighted_hits']:.2f}/{r.get('all_total', '?')} ({r['all_recall']:.1%}), "
                f"Dev={r['dev_weighted_hits']:.2f}/{r.get('dev_total', '?')} ({r['dev_recall']:.1%}), "
                f"Holdout={r['holdout_weighted_hits']:.2f}/{r.get('holdout_total', '?')} ({r['holdout_recall']:.1%}), "
                f"Density={r['mean_per_clip']:.2f}/clip, "
                f"Guardrails={'PASS' if g.get('passed') else 'FAIL'} "
                f"(Regressed={g.get('regressed_stable_items')}, DevMisses={r.get('dev_misses')})"
            )
        lines.append("")

    return "\n".join(lines)


def update_ledger(rounds_dir: Path) -> dict[str, Any]:
    rounds_dir.mkdir(parents=True, exist_ok=True)
    state = evaluate_loop_state(rounds_dir)
    (rounds_dir / "ledger.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (rounds_dir / "ledger.md").write_text(render_ledger_markdown(state), encoding="utf-8")
    return state


def bootstrap_r00(
    rounds_dir: Path = DEFAULT_ROUNDS_DIR,
    results_dir: Path = DEFAULT_RESULTS_DIR,
    overwrite: bool = True,
) -> Path:
    """Seeds `r00` baseline snapshot from `DEFAULT_LAYER1_TEMPLATE` + `DEFAULT_CHAGEE_V25_RULES` and imports baseline runs."""
    r00_dir = create_round_snapshot(
        rounds_dir=rounds_dir,
        round_id="r00",
        parent_round_id=None,
        layer1_template=DEFAULT_LAYER1_TEMPLATE,
        layer2_rules=DEFAULT_CHAGEE_V25_RULES,
        hypothesis=(
            "Baseline V25 prompt (`Prompt_v2.5_Standard`) evaluated across two independent 4-folder runs "
            "(`v6_0928_0811` = 8.0/19 = 42.1% and `r_0928_1009` = 8.0/19 = 42.1%, 2-run mean = 42.1%) and calibrated 19/19 with human reviewer."
        ),
        target_dev_misses=["R02", "R03", "R04", "R05", "R06", "R20"],
        prompt_version="v2.5_pro_flash_latest",
        sop_tab_name="Prompt_v2.5_Standard",
        overwrite=overwrite,
    )

    for baseline_run_id in ("v6_0928_0811", "r_0928_1009"):
        score_json_path = results_dir / baseline_run_id / "score.json"
        score_md_path = results_dir / baseline_run_id / "score.md"
        if score_json_path.exists() and score_md_path.exists():
            score_doc = json.loads(score_json_path.read_text(encoding="utf-8"))
            score_md = score_md_path.read_text(encoding="utf-8")
            record_round_run(
                rounds_dir=rounds_dir,
                round_id="r00",
                run_id=baseline_run_id,
                score_doc=score_doc,
                score_md=score_md,
            )

    return r00_dir


def propose_r01(rounds_dir: Path = DEFAULT_ROUNDS_DIR, overwrite: bool = True) -> Path:
    """Creates candidate round `r01` (Layer 1 only mutation) targeting the 4 generic failure mechanisms."""
    r00_dir = rounds_dir / "r00"
    if not r00_dir.exists():
        bootstrap_r00(rounds_dir=rounds_dir, overwrite=True)

    r00_rules = deserialize_rules_json(
        json.loads((r00_dir / "layer2_rules.json").read_text(encoding="utf-8"))
    )
    return create_round_snapshot(
        rounds_dir=rounds_dir,
        round_id="r01",
        parent_round_id="r00",
        layer1_template=PROPOSED_R01_LAYER1_TEMPLATE,
        layer2_rules=r00_rules,
        hypothesis=(
            "Layer 1 generic kernel upgrade targeting 4 root-cause failure mechanisms observed in r00 dev misses: "
            "(1) Tool/container/reagent state persistence across timestamps & clips (T1 preparation -> T2 contact) for R20; "
            "(2) Clip-start (00:00-00:30) already-in-progress procedure tail audit (mandatory closing step before leaving station) for R02; "
            "(3) Mandatory [T-30s, T] second-by-second dual-hand trajectory traceback before gloving/equipment contact for R03 (and holdout R10); "
            "(4) Per-action timestamp node enumeration & >20s separate Finding row splitting + anti-masking during concurrent phone use for R04/R05 (and holdout R11a/R17/R18a)."
        ),
        target_dev_misses=["R02", "R03", "R04", "R05", "R20"],
        prompt_version="v2.6_r01",
        sop_tab_name="Prompt_v2.6_r01",
        overwrite=overwrite,
    )


def activate_round_in_codebase(rounds_dir: Path, round_id: str) -> None:
    """Copies the selected round's Layer 1 or Layer 2 into local runtime defaults if explicitly requested."""
    round_dir = rounds_dir / round_id
    if not round_dir.exists():
        raise FileNotFoundError(f"Round directory not found: {round_dir}")
    # Note: runtime code reads `eval/rounds/<round_id>/` directly during `run_gcp_round.py`.
    # We also keep a pointer file `eval/rounds/active_candidate.json` for visibility.
    pointer_path = rounds_dir / "active_candidate.json"
    pointer_path.write_text(
        json.dumps(
            {
                "active_candidate_round_id": round_id,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Chagee CCTV Step 2 Prompt-Tuning Loop Controller")
    parser.add_argument(
        "--rounds-dir",
        type=Path,
        default=DEFAULT_ROUNDS_DIR,
        help="Path to eval/rounds directory",
    )
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("bootstrap-r00", help="Bootstrap r00 baseline snapshot from calibrated runs")
    sub.add_parser("propose-r01", help="Create r01 candidate round (Layer 1 generic kernel upgrade)")
    sub.add_parser("status", help="Recompute and print current prompt-tuning loop status and ledger")

    p_verify = sub.add_parser(
        "verify-gate",
        help="Verifier hook for oce-delivery-harness run_loop.py --loop eval_fix",
    )
    p_verify.add_argument(
        "--allow-in-progress",
        action="store_true",
        help="Exit 0 when r00+r01 snapshots and guardrails are valid even if GCP runs are still pending",
    )

    p_rec = sub.add_parser("record-run", help="Record a scored run into a round snapshot")
    p_rec.add_argument("--round", required=True, help="Round ID (e.g. r01)")
    p_rec.add_argument("--run-id", required=True, help="Run ID (e.g. r01_run1)")
    p_rec.add_argument("--score-json", type=Path, required=True, help="Path to score.json")
    p_rec.add_argument("--score-md", type=Path, required=True, help="Path to score.md")

    args = parser.parse_args(argv)

    if args.cmd == "bootstrap-r00":
        r00_dir = bootstrap_r00(rounds_dir=args.rounds_dir, overwrite=True)
        state = update_ledger(args.rounds_dir)
        print(f"Bootstrapped r00 at {r00_dir}. Best round: {state['best_round_id']} ({state['status']})")
        return 0

    if args.cmd == "propose-r01":
        r01_dir = propose_r01(rounds_dir=args.rounds_dir, overwrite=True)
        activate_round_in_codebase(args.rounds_dir, "r01")
        state = update_ledger(args.rounds_dir)
        print(f"Proposed r01 at {r01_dir}. Pending rounds: {state['pending_rounds']}")
        return 0

    if args.cmd == "record-run":
        score_doc = json.loads(args.score_json.read_text(encoding="utf-8"))
        score_md = args.score_md.read_text(encoding="utf-8")
        state = record_round_run(
            rounds_dir=args.rounds_dir,
            round_id=args.round,
            run_id=args.run_id,
            score_doc=score_doc,
            score_md=score_md,
        )
        print(json.dumps(state, ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "verify-gate":
        state = update_ledger(args.rounds_dir)
        print(render_ledger_markdown(state))
        if state["status"] == "CONVERGED_TARGET_MET":
            return 0
        if args.allow_in_progress and state["best_round_id"] is not None:
            return 0
        print(
            f"[EVAL_GATE_PENDING] Loop status is '{state['status']}'. Next action: {state['recommended_action']}",
            file=sys.stderr,
        )
        return 2

    state = update_ledger(args.rounds_dir)
    print(render_ledger_markdown(state))
    return 0


if __name__ == "__main__":
    sys.exit(main())
