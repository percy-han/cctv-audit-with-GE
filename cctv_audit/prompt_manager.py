"""Module 3: Google Sheets Master Prompt & Model Version Control Engine (`prompt_manager.py`).

Implements `REQ-013` (Dual-Layer Sheet Version Control & 0.1s 1-Click Rollback) and embeds
the canonical `CHAGEE_CCTV_Cloud_Auditor_Prompt_v1.0` in English:
1. **Layer 1: Code-Fixed Engine Kernel**:
   - `# Role`
   - `# Core Adjudication Principles (Highest Priority)`: Principle 1 (Prefer False Positives Over False Negatives),
     Principle 2 (Burden-of-Proof Reversal), Principle 3 (Four-Tier Disposition Labels),
     Principle 4 (Strict Boundary Rounding), Principle 5 (Output Specification & Timestamp Separation),
     Principle 6 (One-Action-One-Record & Orthogonal Decomposition of Compound Violations).
   - `# Step 0: Timeline Calibration` (1–6)
   - `# Step 1: Personnel Cataloging & Independent Multi-Person Tracking`
   - `# Step 2: Visual Adjudication Rules (High-Recall Edition)` (`2.1 Contact Determination`,
     `2.2 State-Change Traceback`, `2.3 Posture & Occlusion`, `2.4 Hand-to-Mouth Actions`)
   - `# Step 4: Missed-Detection Self-Audit (Mandatory, Cannot Skip)` (1–7)
2. **Layer 2: Sheet-Managed Dynamic SOP Checklist**:
   - `# Step 3: SOP Checklist` with all **24 complete submodules** (`A1`–`A5`, `B0`, `B1-1` Langtuo 15-step table,
     `B1-2` Manitowoc 13-step table, `B2`–`B5`, `C1`–`C8`, `D1`–`D4` Operations & IPLH) stored in
     `CHAGEE_SOP_and_Model_Control_Tower` (specified by `MASTER_PROMPT_SHEET_ID`),
     fetched in real time and assembled seamlessly with the Layer 1 kernel.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import Path
from typing import List, Optional, Protocol, Sequence

import yaml
from pydantic import BaseModel, Field

from .config import config, extract_spreadsheet_id
from .gcp import get_genai_client

logger = logging.getLogger("cctv_audit.prompt_manager")

BUNDLED_SOP_YAML_PATH = Path(__file__).resolve().parent / "sop_rules.chagee-store-v2.yaml"

# Global TTL Cache to prevent Google Sheets 429 Quota Exhaustion (user limit: 60 RPM)
_SHEET_RANGE_CACHE: dict[str, tuple[float, list[list[str]]]] = {}
_SHEET_RANGE_CACHE_TTL_SEC = 300.0  # 5 minutes
# GCS-hosted Master SOP workbook (`gs://.../master_sheet.xlsx|.json`): one download serves both the
# Tab 0 pointer read and the rules-tab read of a turn; a re-uploaded file is picked up within 60s.
_GCS_WORKBOOK_CACHE: dict[str, tuple[float, dict[str, list[list[str]]]]] = {}
_GCS_WORKBOOK_CACHE_TTL_SEC = 60.0


class VisualScanTarget(BaseModel):
    """Pre-verdict visual observation anchor (`visual_scan`)."""

    subject: str = Field(description="Observation subject, e.g., Personnel, Hands & Gloves")
    question: str = Field(description="Mandatory visual grounding question before verdict")


class SopRuleItem(BaseModel):
    """One structured SOP rule row read from a versioned Prompt Tab or bundled baseline."""

    rule_id: str = Field(description="Rule ID, e.g., A1, A2, B1-1, C1, D1")
    name: str = Field(description="Rule display name")
    category: str = Field(default="SOP", description="Category name, e.g., [A] Handwashing & Hand Hygiene")
    severity: str = Field(default="RED_LINE", description="RED_LINE or NORMAL")
    detection_type: str = Field(default="presence", description="presence or absence")
    requires_full_context: bool = Field(default=False, description="True if chain/duration dependent")
    check_instruction: str = Field(description="Verbatim markdown SOP check instruction and tables")
    pass_criteria: str = Field(default="", description="Criteria for COMPLIANT")
    fail_criteria: str = Field(default="", description="Criteria for VIOLATION")


class PromptModelConfig(BaseModel):
    """Resolved runtime configuration for a single audit execution."""

    active_prompt_version: str = Field(default_factory=lambda: config.default_prompt_version)
    active_model_version: str = Field(default_factory=lambda: config.fallback_model_version)
    fallback_model_version: str = Field(default_factory=lambda: config.fallback_model_version)
    model_fallback_warning: Optional[str] = Field(default=None)
    visual_scan_targets: List[VisualScanTarget] = Field(default_factory=list)
    rules: List[SopRuleItem] = Field(default_factory=list)
    system_instruction: str = Field(default="")


TAB0_TITLE = "Tab0_Version_Control"
TAB2_SCHEDULED_TITLE = "Tab2_Scheduled_Jobs"
TAB3_EVAL_TITLE = "Tab3_Golden_Eval"
TAB4_SPOT_CHECK_TITLE = "Tab4_Spot_Check"
DEFAULT_PROMPT_TAB_TITLE = "Prompt_v1.0_Standard"

TAB0_ALIASES: tuple[str, ...] = (
    "Tab0_Version_Control",
    "Tab0_版本总控与回滚开关",
)
TAB2_ALIASES: tuple[str, ...] = (
    "Tab2_Scheduled_Jobs",
    "Tab2_定时巡检配置表",
    "Scheduled_Jobs",
    "定时巡检配置表",
)
TAB3_ALIASES: tuple[str, ...] = (
    "Tab3_Golden_Eval",
    "Tab3_黄金集评测工作台",
    "Golden_Eval",
    "黄金集评测工作台",
)
TAB4_ALIASES: tuple[str, ...] = (
    "Tab4_Spot_Check",
    "Tab4_专项测试表",
    "Spot_Check",
    "专项测试表",
)
PROMPT_STANDARD_ALIASES: tuple[str, ...] = (
    "Prompt_v1.0_Standard",
    "Prompt_v2.5_Standard",
    "Prompt_v2.5_V10全量17条标准版",
)
PROMPT_TAB_ALIASES: dict[str, str] = {
    "Prompt_v1.0_Standard": "Prompt_v2.5_V10全量17条标准版",
    "Prompt_v2.5_Standard": "Prompt_v2.5_V10全量17条标准版",
    "Prompt_v2.5_V10全量17条标准版": "Prompt_v1.0_Standard",
    "Prompt_v2.5_Archive_Baseline": "Prompt_v2.5_归档基准",
    "Prompt_v2.5_归档基准": "Prompt_v2.5_Archive_Baseline",
}


def _alias_candidates_for_tab(tab_name: str) -> list[str]:
    """Returns `[tab_name, ...fallback_aliases]` for transparent English/Chinese tab resolution."""
    cleaned = (tab_name or "").strip()
    if not cleaned:
        return []
    out: list[str] = [cleaned]
    for group in (TAB0_ALIASES, TAB2_ALIASES, TAB3_ALIASES, TAB4_ALIASES, PROMPT_STANDARD_ALIASES):
        if cleaned in group:
            for item in group:
                if item not in out:
                    out.append(item)
    mapped = PROMPT_TAB_ALIASES.get(cleaned)
    if mapped and mapped not in out:
        out.append(mapped)
    return out


def _populate_workbook_tab_aliases(tabs: dict[str, list[list[str]]]) -> dict[str, list[list[str]]]:
    """Populates both canonical English and legacy Chinese tab keys on an in-memory workbook dict."""
    for group in (TAB0_ALIASES, TAB2_ALIASES, TAB3_ALIASES, TAB4_ALIASES, PROMPT_STANDARD_ALIASES):
        present = next((t for t in group if t in tabs), None)
        if present is not None:
            for alias in group:
                tabs.setdefault(alias, tabs[present])
    for src, dst in list(PROMPT_TAB_ALIASES.items()):
        if src in tabs and dst not in tabs:
            tabs[dst] = tabs[src]
    return tabs


SCHEDULED_RULE_HEADERS: tuple[str, ...] = (
    "Rule_ID",
    "Rule_Name",
    "Root_Folder_URL",
    "Trigger_Hour_UTC",
    "Trigger_Minute_UTC",
    "Lookback_Days",
    "Supervisor_Email",
    "Enabled",
    "Last_Run_Status",
)
LEGACY_SCHEDULED_RULE_HEADERS: tuple[str, ...] = (
    "Rule_ID",
    "Rule_Name",
    "Root_Folder_URL",
    "Trigger_Time",
    "Timezone",
    "Lookback_Hours",
    "Supervisor_Email",
    "Enabled",
    "Last_Run_Status",
)
VALID_TRIGGER_HOURS_UTC: tuple[str, ...] = tuple(f"{h:02d}" for h in range(24))
VALID_TRIGGER_MINUTES_UTC: tuple[str, ...] = ("00", "15", "30", "45")
VALID_LOOKBACK_DAYS_OPTIONS: tuple[str, ...] = ("0.5", "1", "2", "3", "4", "5", "6", "7")
VALID_LOOKBACK_DAYS_VALUES: frozenset[float] = frozenset({0.5, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0})
VALID_ENABLED_DROPDOWN_OPTIONS: tuple[str, ...] = ("ENABLED", "PAUSED")
_VALID_SHEET_CACHE: dict[str, tuple[str, ...]] = {}

# Tab0 rows whose column B (`Active_Value`) may name a model. Column D (`Candidates`) is read on every row.
TAB0_MODEL_VALUE_KEYS: frozenset[str] = frozenset(
    {"Active_Model_Version", "Fallback_Model_Version", "Candidate_Models_Discovered"}
)
TAB0_MODEL_CANDIDATES_RANGE = "A1:D30"
_MODEL_CANDIDATE_RE = re.compile(r"^gemini-\d[\w.\-]*$", re.ASCII)
_TRAILING_PAREN_NOTE_RE = re.compile(r"(?:\s*[（(][^()（）]*[)）])+\s*$")


def collect_model_candidates_from_tab0(rows: Sequence[Sequence[str]]) -> list[str]:
    """Model names an operator may pick, read from `Tab0_Version_Control` (or legacy `Tab0_版本总控与回滚开关`) rows (`A1:D30`).

    Row layout: `Config_Key` | `Active_Value` | `Description` | `Candidates` (also supports legacy Chinese headers).
    Collects column B of the `Active_Model_Version` / `Fallback_Model_Version` /
    `Candidate_Models_Discovered` rows and column D of every row; each cell is split on `|`,
    every token loses surrounding whitespace and any trailing parenthetical note (`(...)` /
    `（...）`), and only tokens that look like a Gemini model id (`^gemini-\\d[\\w.\\-]*$`) are
    kept. Returns the ordered, de-duplicated list (first occurrence wins). Header rows, empty
    cells and non-model text (`AUTO_LATEST_FLASH`, notes, URLs) are ignored.
    """
    found: dict[str, None] = {}

    def _take(cell: object) -> None:
        for raw_token in str(cell or "").split("|"):
            token = _TRAILING_PAREN_NOTE_RE.sub("", raw_token.strip()).strip()
            if token and _MODEL_CANDIDATE_RE.match(token):
                found.setdefault(token, None)

    for row in rows or []:
        cells = [str(c) if c is not None else "" for c in row]
        key = cells[0].strip() if cells else ""
        if key in TAB0_MODEL_VALUE_KEYS and len(cells) > 1:
            _take(cells[1])
        if len(cells) > 3:
            _take(cells[3])
    return list(found)


def default_model_dropdown_options() -> list[str]:
    """The baseline model options every dropdown carries even when Tab0 names none."""
    return [config.fallback_model_version, "gemini-3-flash-preview", "gemini-2.5-flash"]


def model_dropdown_options(tab0_rows: Sequence[Sequence[str]]) -> list[str]:
    """Baseline options followed by the Tab0 candidates, de-duplicated in order."""
    return list(dict.fromkeys(default_model_dropdown_options() + collect_model_candidates_from_tab0(tab0_rows)))


class ScheduledAuditRule(BaseModel):
    """One row from `[Tab 2: Scheduled Jobs]` (`Tab2_Scheduled_Jobs`) in the Master SOP Sheet."""

    row_index: int = Field(ge=2, description="1-based row number in Tab2_Scheduled_Jobs")
    rule_id: str = Field(description="Unique schedule rule ID, e.g. SCHED-SG-01")
    rule_name: str = Field(default="", description="Human-readable rule name")
    root_folder_url: str = Field(description="Google Drive root folder URL/ID or gs:// prefix")
    root_folder_id: str = Field(description="Normalized Google Drive folder ID or gs:// prefix")
    trigger_time: str = Field(default="02:00", description="Daily trigger time in HH:MM (24h)")
    timezone: str = Field(default="UTC", description="IANA timezone name (defaults to UTC)")
    lookback_days: float = Field(
        default=1.0, ge=0.01, le=30.0, description="Lookback window in days (0.5 to 7)"
    )
    lookback_hours: float = Field(default=24.0, ge=1.0, le=720.0)
    supervisor_email: str = Field(description="Supervisor email for DWD and Chat @-mention")
    enabled: bool = Field(default=True)
    last_run_status: str = Field(default="")


class ScheduledRuleRowError(BaseModel):
    """Diagnostic record for an invalid row in `Tab2_Scheduled_Jobs` so the engine can write a loud error back to Column I (`Last_Run_Status`)."""

    row_index: int = Field(ge=2)
    rule_id: str = Field(default="")
    error_message: str
    previous_status: str = Field(default="")


EVAL_WORKBENCH_HEADERS: tuple[str, ...] = (
    "Eval_ID",
    "Eval_Name",
    "Prompt_Tab_Name",
    "Model_Version",
    "Golden_Videos_Folder_URL",
    "Human_Labels_Sheet_URL",
    "Results_Folder_URL",
    "Supervisor_Email",
    "Trigger_Action",
    "Last_Eval_Report",
)
LEGACY_EVAL_WORKBENCH_HEADERS: tuple[str, ...] = (
    "Eval_ID",
    "Eval_Name",
    "Prompt_Tab_Name",
    "Model_Version",
    "Golden_Sheet_URL",
    "Results_Folder_URL",
    "Supervisor_Email",
    "Trigger_Action",
    "Last_Eval_Report",
)
VALID_EVAL_TRIGGER_ACTIONS: tuple[str, ...] = (
    "IDLE",
    "START_EVAL",
    "RUNNING",
    "DONE",
    "FAILED",
)
DEFAULT_TAB3_SAMPLE_ROW: tuple[str, ...] = (
    "EVAL-EXAMPLE-01",
    "[Example] Golden Dataset Baseline Evaluation (Provide Golden Videos Folder, Human Labels Sheet, and Results Folder; set Trigger_Action to START_EVAL to run in background)",
    "Prompt_v1.0_Standard",
    "gemini-3.8-flash",
    "https://drive.google.com/drive/folders/1ExampleGoldenVideosFolderIdReplaceMe",
    "https://docs.google.com/spreadsheets/d/1ExampleGoldenSheetIdReplaceMe0000/edit",
    "https://drive.google.com/drive/folders/1ExampleEvalResultsFolderIdReplaceMe",
    "auditor@example.com",
    "IDLE",
    "Default example (Not started; select START_EVAL to execute automatically within 15 minutes)",
)

DEFAULT_TAB4_SAMPLE_ROW: tuple[str, ...] = (
    "B4",
    "B4 · Red-Edged Towel & 3-Color Towel Segregation (Param 3.1)",
    "[B] Ice Maker Weekly Deep Clean & Ice Handling (Week 2 Core Module)",
    "RED_LINE",
    "presence",
    "FALSE",
    (
        "When wiping the ice maker interior cavity, water distribution tube, water curtain, and white inner door liner, staff MUST use a **clean red-edged towel taken fresh from the dedicated labeled sanitizing bucket**:\n"
        "- 🔴 Towel not taken from the dedicated labeled sanitizing bucket (e.g., left on the sink rim, counter, machine top, or faucet and reused) → RED_LINE violation;\n"
        "- 🔴 Cross-using red, blue, and yellow color-coded towels (e.g., wiping the ice maker interior liner with a blue-edged bar towel or yellow-edged utensil towel) → RED_LINE violation."
    ),
    "Clean red-edged towel taken directly from the dedicated labeled sanitizing bucket used to wipe the ice maker interior; 3-color towels strictly segregated by zone",
    "Towel not taken from the dedicated sanitizing bucket (reused from counter/sink rim) or red/blue/yellow towels cross-used across zones",
)


class ScheduledEvalRule(BaseModel):
    """One row from `[Tab 3: Golden Set Evaluation Workbench]` (`Tab3_Golden_Eval`) in the Master SOP Sheet."""

    row_index: int = Field(ge=2, description="1-based row number in Tab3_Golden_Eval")
    eval_id: str = Field(description="Unique evaluation ID, e.g. EVAL-01")
    eval_name: str = Field(default="", description="Human-readable evaluation name")
    prompt_tab_name: str = Field(description="Target Prompt_* tab in Master Sheet to evaluate")
    model_version: str = Field(description="Target Gemini model version to evaluate")
    golden_videos_folder_url: str = Field(
        default="", description="Dedicated Google Drive folder URL/ID containing golden dataset videos"
    )
    golden_videos_folder_id: str = Field(
        default="", description="Normalized Google Drive folder ID containing golden dataset videos"
    )
    human_labels_sheet_url: str = Field(
        default="", description="Human annotation Google Sheet URL/ID or gs://.../*.jsonl"
    )
    golden_sheet_url: str = Field(description="Golden Set Google Sheet URL/ID or gs://.../*.jsonl")
    golden_source_id: str = Field(description="Normalized Golden Sheet ID or gs://.../*.jsonl URI")
    results_folder_url: str = Field(default="", description="Optional Drive folder URL/ID for report Sheets")
    results_folder_id: str = Field(default="", description="Normalized Drive folder ID for report Sheets")
    supervisor_email: str = Field(description="Supervisor email for DWD and Chat @-mention")
    trigger_action: str = Field(default="IDLE", description="IDLE, START_EVAL, RUNNING, DONE, or FAILED")
    last_eval_report: str = Field(default="")


class ScheduledEvalRowError(BaseModel):
    """Diagnostic record for an invalid row in `Tab3_Golden_Eval`."""

    row_index: int = Field(ge=2)
    eval_id: str = Field(default="")
    trigger_action: str = Field(default="IDLE")
    error_message: str
    previous_status: str = Field(default="")


DEFAULT_VISUAL_SCAN_TARGETS: List[VisualScanTarget] = [
    VisualScanTarget(subject="Personnel", question="How many individuals appear in the frame? Describe each person's gender, hairstyle, attire/PPE traits, and assigned station."),
    VisualScanTarget(subject="Hands & Gloves", question="Are each person's hands bare or gloved? What is the glove color, are there any tears or stains, and what are they holding right now?"),
    VisualScanTarget(subject="Head, Face & Accessories", question="Are hairnets and masks worn completely and properly? Is any jewelry or accessory worn on the hands or wrists?"),
    VisualScanTarget(subject="Uniform & PPE Storage", question="Does on-duty attire comply with standards? Are any removed uniforms or aprons left unstored or misplaced?"),
    VisualScanTarget(subject="Hand Contact Sequence", question="List in chronological order every non-food-contact surface, personal item, cleaning tool, equipment component, or food utensil touched by each person's hands."),
    VisualScanTarget(subject="Sink & Cleaning Procedures", question="Is there a physical sink or cleaning task in frame? Record the initial step sequence, net active duration, and closing drying action for each occurrence."),
    VisualScanTarget(subject="Equipment, Containers & Utensils", question="What is the open/closed or storage state of controlled equipment (including top/internal removable parts), containers, measuring tools, and dispensing utensils?"),
    VisualScanTarget(subject="Trash & Floor Environment", question="Are trash bins covered and free of overflow? Are the floor and operating zones clean and orderly?"),
    VisualScanTarget(subject="Consumables, Container Color Codes & Personal Items", question="What are the towel color codes, source containers, and spray/chemical bottle colors and labels used during tasks? Are any personal items present in the operating area?"),
    VisualScanTarget(subject="Customer & Beverage Handover Flow", question="Is there any customer interaction or beverage handover? Record the response and transition timestamps at each stage."),
]


# 24 SOP sections: the 23 verbatim ones from Percy's canonical CHAGEE_CCTV_Cloud_Auditor_Prompt_v1.0,
# plus C8 (customer SOP 6.2 Automated Tea Maker Machine, "Filter Tea into an Ice-Prepared Container").
DEFAULT_CHAGEE_V25_RULES: List[SopRuleItem] = [
    # ───────────────────────── [A] Handwashing & Hand Hygiene (Week 1 Core Module) ─────────────────────────
    SopRuleItem(
        rule_id="A1",
        name="A1 · Mandatory Handwashing Trigger Scenarios (Param 1.5.5 + Reference Case 1)",
        category="[A] Handwashing & Hand Hygiene (Week 1 Core Module)",
        severity="RED_LINE",
        detection_type="absence",
        requires_full_context=True,
        check_instruction=(
            "When any of the following scenarios occurs, **immediately open a 5-minute monitoring window** and track the staff member second by second:\n\n"
            "**High-Risk Contamination Triggers (RED_LINE)**\n"
            "- **Touching head, face, or personal accessories**: touching hair, adjusting spectacles/glasses, touching face/mouth/nose, touching or pulling mask, coughing or sneezing into hands\n"
            "- **Using the same paper towel to wipe face/sweat first and then continuing to wipe hands**\n"
            "- **Touching non-food-contact barriers and handles**: touching the front bar transparent spit guard, door handles/curtains, or outer equipment frames\n"
            "- Handling trash bags, touching trash bins or trash drawers (**including handles**), or lining trash bins\n"
            "- Picking up items from the floor / touching the floor or floor utility baskets\n"
            "- **Using or holding cleaning tools**: holding a mop, floor squeegee, broom, or cleaning the grease trap\n"
            "- Stepping onto operating countertops\n\n"
            "**Routine Triggers**\n"
            "- **Starting shift / entering work zone (Partner enter work, on the switch: staff enters work area and turns on equipment power switches; handwashing must occur before donning gloves or touching any food-contact tools)**\n"
            "- **Returning from break / after using a mobile phone**\n"
            "- **After using the restroom**\n"
            "- **After handling cash / POS register**\n"
            "- After completing any cleaning task\n"
            "- After gloves are torn or contaminated\n\n"
            "- ⚠️ **[Exhaustive Enumeration of Co-Occurring Contamination Sources (Never Swallow Micro-Actions)]**: If the same staff member performs multiple contamination-contact actions within an unwashed window (e.g., using a mobile phone AND touching hair/spectacles, touching the bar spit guard, touching a door handle, or holding a mop within seconds of each other), when outputting `A1`/`A2` evidence you **must list every observed contamination trigger action and its exact timestamp**, never reporting only the salient phone use while omitting touching hair, spectacles, spit guard, or mop!"
        ),
        pass_criteria="Completes standard 5-step handwashing within the 5-minute monitoring window after turning on equipment switches at shift start (before gloving) and after any contamination trigger above",
        fail_criteria="Dons gloves or touches food-contact tools after entering work zone and turning on switches without handwashing (before starting shift), or fails to wash hands after touching hair/spectacles/spit guard/mop/phone/trash bin (list all co-occurring triggers; leaving frame -> UNVERIFIED; window truncated -> UNVERIFIED)",
    ),
    SopRuleItem(
        rule_id="A2",
        name="A2 · Violation Determination (Three-Element Chain)",
        category="[A] Handwashing & Hand Hygiene (Week 1 Core Module)",
        severity="RED_LINE",
        detection_type="absence",
        requires_full_context=True,
        check_instruction=(
            "Within the 5-minute monitoring window, if **all three conditions** hold simultaneously:\n"
            "1. An `A1` trigger event occurs (record **all** preceding contamination actions and timestamps, such as touching hair/spectacles, touching the bar spit guard, holding a mop, reusing a face-wiped paper towel on hands, or using a mobile phone)\n"
            "2. **No complete 5-step handwashing procedure is observed**\n"
            "3. The staff member **directly dons gloves** OR **resumes touching food-contact tools/equipment/beverage preparation** (record timestamp)\n"
            "   > Food-contact tools & equipment list: ice maker machine door/ice bin, ice bucket, upright chiller/refrigerator door & internal food ingredients, "
            "tea brewer pot, tea filter net, tea base container, shaker cup, ice scoop, CHAGEE paper cups, straws, measuring cups, bar spoons, strainers\n\n"
            "→ **RED_LINE Violation**\n\n"
            "> [!HIGH-RECALL RULES]\n"
            "> - **[Reusing Face-Wiped Paper Towel on Hands = Mandatory Cross-Contamination Report]**: If a staff member washes hands, takes a paper towel to **wipe their face/mouth/nose first, and then uses the same paper towel to dry hands before donning gloves, opening the ice maker, or resuming beverage prep**, the hands are re-contaminated by the face — must report `A2` (`CONFIRMED`) RED_LINE violation!\n"
            "> - **[List All Co-Occurring Triggers]**: If before directly donning gloves or touching the ice maker/chiller/bar utensils the staff member both used a phone AND touched hair/spectacles/spit guard/mop, list all contamination sources in the `A2` evidence (or split into separate rows); never let phone use mask other triggers.\n"
            "> - If the staff member **leaves the frame** after the trigger, mark `UNVERIFIED · Destination after contamination unknown` and **still report**.\n"
            "> - If the video ends within the 5-minute window, mark `UNVERIFIED · Monitoring window truncated` and **still report**.\n"
            "> - **'Directly donning gloves without handwashing' is itself an independent violation** (Param 1.5.3), even if no food is touched afterward."
        ),
        pass_criteria="Completes 5-step handwashing and dries hands one-way with a clean paper towel after any trigger event before donning fresh gloves or touching the ice maker/chiller/food-contact tools",
        fail_criteria="Directly dons gloves (1.5.3) or touches ice maker/chiller ingredients/bar utensils (1.5.5 RED_LINE) without handwashing after touching hair/spectacles/spit guard/mop/phone, or wipes face then hands with the same paper towel before donning gloves and resuming work",
    ),
    SopRuleItem(
        rule_id="A3",
        name="A3 · 5-Step Handwashing Procedure Verification",
        category="[A] Handwashing & Hand Hygiene (Week 1 Core Module)",
        severity="NORMAL",
        detection_type="absence",
        requires_full_context=True,
        check_instruction=(
            "Verify and output every handwashing instance item by item:\n"
            "| # | Step | Verification Point | Common Violation |\n"
            "|---|---|---|---|\n"
            "| 1 | **Wet hands first (must precede soap)** | **In the first 3s at the sink, both hands must be placed under running faucet water to wet/rinse first**, before pressing the soap dispenser | **Applying soap with dry hands without wetting first (apply soap before wet hand / not rinse hand before applying handwashing gel) = mandatory sequence violation** |\n"
            "| 2 | Apply handwashing soap | Presses soap dispenser / applies handwashing gel after wetting hands | Rinses with water only without soap (e.g., 2s water rinse then leaves) |\n"
            "| 3 | **Scrub with foam >= 20 seconds** | **Exclude wetting, soap pressing & final rinsing time; measure only net foam-rubbing duration** (`soap rubbing start -> hands enter running water to rinse`) | **Net foam scrubbing < 20s (including 15–22s borderline range, strictly reported per Principle 4)** |\n"
            "| 4 | Rinse with clean running water | Rinses off foam completely | Wipes dry immediately after applying soap |\n"
            "| 5 | **Dry with paper towel (mandatory closing check)** | After rinsing off foam, **must pull a clean paper towel from dispenser to thoroughly dry both hands (dry hands with hand towel)** | **Failing to dry hands with a paper towel after washing (shaking / air-drying / wiping on cloth or apron / directly donning gloves) = missing step; or wiping face then hands with same towel** |\n\n"
            "- Handwashing must occur at a **physical sink with visible running water**; wiping with a towel != handwashing; washing gloved hands != handwashing.\n"
            "- **[Step Sequence & First 3s / Last 5s Slow-Motion Check]**: When any staff member approaches the sink:\n"
            "  1. **First 3s (Wet vs. Soap)**: Do hands enter running water first, or **reach directly for the soap dispenser with dry hands (apply soap before wet hand)**? If dry hands press soap first, report an independent `A3` (`CONFIRMED`) sequence violation!\n"
            "  2. **Middle phase (Net foam scrubbing)**: Never count total standing time at the sink as scrubbing time; if net foam rubbing is < 20s (including 15–22s borderline), report `A3` (`CONFIRMED`). **If the same staff member washes hands twice at different times and both are < 20s, emit 2 separate `A3` Finding rows — never merge them!**\n"
            "  3. **Last 5s (Paper towel drying)**: After rinsing, does the staff member pull a clean paper towel to dry hands? If they leave or grab items/gloves **without drying hands with a paper towel**, report an independent `A3` (`CONFIRMED`, `not dry hand with hand towel after handwashing`)!\n"
            "- Missing any step or **wrong step order** (soap before wetting, no paper towel drying) → 🟡 Common Deviation (non-RED_LINE, mandatory report; state both if both occur)."
        ),
        pass_criteria="At a physical sink: wet hands under running water first -> apply soap -> net foam scrubbing >= 20s (excluding wetting and rinsing) -> rinse with clean water -> pull clean paper towel to thoroughly dry hands (each handwash evaluated independently)",
        fail_criteria="Presses soap dispenser with dry hands before wetting (apply soap before wet hand), net foam scrubbing < 20s (report each occurrence separately), fails to dry hands with paper towel after washing (not dry hand with hand towel), wipes face then hands with same towel, or wipes hands on rag/apron",
    ),
    SopRuleItem(
        rule_id="A4",
        name="A4 · Glove Usage Compliance (Param 1.6)",
        category="[A] Handwashing & Hand Hygiene (Week 1 Core Module)",
        severity="RED_LINE",
        detection_type="absence",
        requires_full_context=True,
        check_instruction=(
            "- Must wear gloves when handling food or food-contact ice/surfaces\n"
            "- **Must immediately replace with a fresh pair of gloves after handwashing** (reusing old gloves after washing hands = violation)\n"
            "- Must immediately replace gloves whenever torn or contaminated"
        ),
        pass_criteria="Wears gloves throughout food handling and immediately puts on fresh gloves after handwashing or glove contamination",
        fail_criteria="Handles food or cleans interior food-contact surfaces bare-handed, reuses old gloves after handwashing, or fails to replace contaminated/torn gloves",
    ),
    SopRuleItem(
        rule_id="A5",
        name="A5 · 2-Hour Periodic Handwashing (Active Only When Input >= 2 Hours)",
        category="[A] Handwashing & Hand Hygiene (Week 1 Core Module)",
        severity="NORMAL",
        detection_type="absence",
        requires_full_context=True,
        check_instruction=(
            "- Track handwashing timestamps for each staff member and compute intervals between consecutive washes\n"
            "- Interval > 2 hours → violation\n"
            "- **If total video duration < 2 hours, explicitly state: `Segment duration under 2 hours; 2-hour periodic rule not applicable`** and do not force a violation verdict"
        ),
        pass_criteria="Completes at least one compliant handwash every 2 hours on duty (or noted as not applicable when video < 2 hours)",
        fail_criteria="Continuously on duty for more than 2 hours without handwashing",
    ),
    # ───────────────────────── [B] Ice Maker Weekly Deep Clean (Week 2 Core Module) ─────────────────────────
    SopRuleItem(
        rule_id="B0",
        name="B0 · Ice Maker Model Identification & Visual Priors (Step 1, Mandatory Output)",
        category="[B] Ice Maker Weekly Deep Clean (Week 2 Core Module)",
        severity="NORMAL",
        detection_type="presence",
        requires_full_context=False,
        check_instruction=(
            "Observe the machine brand badge, panel shape, water curtain structure, evaporator grid layout, and door opening mechanism:\n"
            "- Output: `Model = Langtuo` / `Manitowoc (HT1000)` / `Manitowoc (Indigo NXT)` / `Unconfirmed`\n"
            "- State your visual identification basis\n"
            "- **When unconfirmed, enforce the strictest Langtuo standard (sanitizer soak >= 10 minutes) and label `Model Unconfirmed`**\n"
            "- ⚠️ **[Official SOP Visual Priors for Upright Modular Ice Makers (CHAGEE APAC SOP)]**:\n"
            "  1. **`Langtuo` Dual-Door Upright Modular Ice Maker (SOP 21)**:\n"
            "     - **Structure**: ~1.8–2.0m tall upright unit; upper ice-making head has **left/right stainless front door panels (lined with white food-contact `white cover`) + middle pillar + top-right blue LCD**, lower angled-lid ice bin (blue liner). Inside head: upper horizontal white **water distribution tube**, metal **evaporator cube grid**, lower **water trough**, covered by two white grooved **water curtains**.\n"
            "     - **Top-Surface Disassembly & Soaking Prior (SOP 21 Steps 5–10)**: Staff climb a step stool, unscrew left/right front door panels (`door panels / white cover`), middle pillar, and two white **water curtains**, **lay them flat on top of the ice maker (white inner surface facing up), spray sanitizer, and soak >= 10 minutes**, then scrub/wipe with a white long-handled bent brush and red-edged towel before reinstalling.\n"
            "  2. **`Manitowoc` Upright Ice Maker (Choice HT1000 / Indigo NXT, SOP 8)**:\n"
            "     - **Structure**: After removing the front door panel (`white cover`, silver exterior, white plastic inner liner) and louvered **water curtain**, top interior has a screw-mounted horizontal white **water distribution tube**, middle metal **ice making plates**, right-side three white **float switches / water pump**, bottom clip-out **water trough**, and top-right `ICE / OFF / CLEAN` toggle switch (HT1000) or Indigo NXT touchscreen.\n"
            "  - **Never misclassify staff on a step stool spraying/scrubbing/wiping the `white cover` and `water curtain` on top of the ice maker, wiping the upper `water distribution tube`, or pouring sanitizer into the `water trough` as 'cleaning an air vent/AC' or 'wiping a chiller top', which would miss `B1`–`B4`!**"
        ),
        pass_criteria="Accurately identifies Langtuo or Manitowoc ice maker and its 5 core components (door panel/white cover, water curtain, upper horizontal water distribution tube, evaporator grid, water trough) and applies the corresponding soak threshold",
        fail_criteria="Fails to verify against the corresponding model standard, or misidentifies the white door cover/water curtain on top of the ice maker or internal distribution tube as another appliance, missing B1–B4",
    ),
    SopRuleItem(
        rule_id="B1-1",
        name="B1-1 · Langtuo Complete 15-Step Weekly Clean Verification (SOP 21)",
        category="[B] Ice Maker Weekly Deep Clean (Week 2 Core Module)",
        severity="RED_LINE",
        detection_type="absence",
        requires_full_context=True,
        check_instruction=(
            "| # | Step | Key Verification Point |\n"
            "|---|---|---|\n"
            "| 1 | **Turn off main power switch & empty ice bin** | Press `[Power]->[Off]` to drain, **wear dry gloves to turn off rear/top red main power switch**, scoop out all ice (SOP 21 1–4) |\n"
            "| 2 | **Prep: Fill white/clear spray bottle with colorless sanitizer** | Only Ecolab KAY-5 sanitizer; **prohibit multipurpose solution in yellow bottle** (SOP 21 4 & B3) |\n"
            "| 3 | Prep: **8L sanitizer solution** (clear bucket) | For pouring into right side of water trough during self-cleaning (SOP 21 4 & 26) |\n"
            "| 4 | Prep: Soak white long-handled bent brush in sanitizer | (SOP 21 4) |\n"
            "| 5 | **Wipe top of ice maker with blue-edged towel, unscrew door panels & pillar** | Keep screws in a stainless-steel tray (SOP 21 5–6) |\n"
            "| 6 | **Spray sanitizer on both sides of door panels / white covers & sit >= 10 min** | Placed on top with **white inner surface facing up**; **must sit full 10 min** — no early wipe/scrub/reinstall (SOP 21 7–8) |\n"
            "| 7 | **Remove both white water curtains, spray sanitizer & sit >= 10 min** | **Must sit full 10 min** before scrubbing, wiping, or reinstalling (SOP 21 9–10) |\n"
            "| 8 | **Spray internal evaporator grid, side grid & upper water distribution tube** | Heavily spray: (1) **evaporator cube grid**; (2) **side grid**; (3) **upper horizontal water distribution tube & nozzles** (SOP 21 11–14) |\n"
            "| 9 | **Spray all ice bin walls, top, corners & interior of door** | After spraying all interior, exterior & removable parts, <= 1/4 of bottle remains (SOP 21 15–18) |\n"
            "| 10 | **Close ice bin lid first**, then spray **machine exterior** | Do not reverse order (SOP 21 18) |\n"
            "| 11 | **Independent >= 10-min contact time per component (Allow it to sit for 10 minutes)** | Verify `spray end -> first wipe/scrub/reinstall` for **distribution tube, white cover/door panel, water curtain, interior of door & cavity**; < 10 min triggers `B2` (SOP 21 19) |\n"
            "| 12 | **After 10 min, clean interior, distribution tube & removable parts (all 4 sides/edges) with brush and clean red-edged towel from sanitizing bucket** | (1) Clean evaporator grid, **upper water distribution tube**, crevices & trough with wet red-edged towel + brush; (2) Scrub/wipe **front, back & all 4 perimeter sides, edges & corners (`especially at the corners, sides and edges`)** of water curtains & white covers; rinse curtains & dry with red-edged towel; (3) Wipe **white inner surface** of door panels/pillar with dry red-edged towel, and **stainless outer surface & bottom border** with dry blue-edged towel (SOP 21 19–35) |\n"
            "| 13 | **Pour 8L sanitizer into right side of water trough (`Pour sanitiser solution into water trough`)** | **Mandatory after interior wiping**: pour 8L sanitizer into water trough (omitting = violation) (SOP 21 26) |\n"
            "| 14 | **Reinstall curtains, pillar & door panels (`Close back ice maker machine`)**, turn on power & self-clean, wipe exterior with **dry blue-edged towel** | All parts reinstalled and doors closed (SOP 21 29–38) |\n"
            "| 15 | **After ~2h / 5 batches: wipe bin lid holes with dry red-edged towel, discard sanitizer-ice + rinse bin & dry crevices with red-edged towel** | Mark `OUT_OF_SCOPE` if outside video window (SOP 21 39–42) |"
        ),
        pass_criteria="Completes full Langtuo weekly clean: turns off main switch, uses only colorless sanitizer in white/clear bottle, removes door panels (white cover) & water curtains to spray and soak >= 10 min, sprays interior grid/side grid/distribution tube/bin liner and soaks >= 10 min, wipes distribution tube/interior/removable parts (including all 4 sides & edges) with clean red-edged towel from sanitizing bucket, pours 8L sanitizer into water trough, reinstalls doors & closes machine, wipes exterior with blue-edged towel",
        fail_criteria="Fails to turn off main power switch, uses yellow-bottle multipurpose cleaner (B3), wipes/scrubs/reinstalls any component (distribution tube / white cover / water curtain / interior of door / interior cavity) before 10-min soak completes (B2), fails to use clean red-edged towel from sanitizing bucket (B4), fails to wipe all 4 sides/edges of white cover or water curtain, omits pouring sanitizer into water trough, or fails to close back ice maker machine",
    ),
    SopRuleItem(
        rule_id="B1-2",
        name="B1-2 · Manitowoc Complete 13-Step Weekly Clean Verification (SOP 8)",
        category="[B] Ice Maker Weekly Deep Clean (Week 2 Core Module)",
        severity="RED_LINE",
        detection_type="absence",
        requires_full_context=True,
        check_instruction=(
            "| # | Step | Key Verification Point |\n"
            "|---|---|---|\n"
            "| 1 | **Remove front door panel, toggle CLEAN for 5-min flush, then toggle OFF and empty ice** | Toggle must be `OFF` before removing internal parts or scooping ice; **prohibit multipurpose solution in yellow bottle** (SOP 8 1–3) |\n"
            "| 2 | **Remove 3 internal parts: water distribution tube, water trough, and water curtain** | Unscrew upper `distribution tube`, disconnect pump slip joint, release clips to pull out `water trough` and `water curtain` (SOP 8 4–6) |\n"
            "| 3 | **Spray sanitizer on door panel / white cover and let sit >= 5 min** | Must **sit full 5 min** after spraying; never wipe, scrub, or reinstall early (SOP 8 7) |\n"
            "| 4 | **Spray sanitizer on water curtain, water trough & distribution tube and let sit >= 5 min** | All removable parts must **sit full 5 min** after spraying before wiping, scrubbing, or reinstalling (SOP 8 7 & 12) |\n"
            "| 5 | **Spray internal ice making plates / evaporator grid, float switches, and side walls** | Heavily spray sanitizer and **let sit >= 5 min** (SOP 8 8) |\n"
            "| 6 | **Spray interior ice bin, interior of door, and exterior surfaces** | Heavily spray sanitizer and **let sit >= 5 min** (SOP 8 9–10) |\n"
            "| 7 | Clean **air filter** | Wash with water, dry with blue-edged towel, reinstall; exempt if rear-mounted & inaccessible (must note) (SOP 8 p.7) |\n"
            "| 8 | **Independent >= 5-min contact time per component (`After 5 minutes, proceed to wipe all sanitized components`)** | Verify `spray end -> first wipe/scrub/reinstall` for **distribution tube, door panel/white cover, water curtain, interior of door, and interior cavity**; any part < 5 min triggers `B2` |\n"
            "| 9 | **Wipe float switches, water pump, water curtain, water trough, distribution tube & bin corners (including all 4 sides & edges) with clean red-edged towel from sanitizing bucket** | Must take clean red-edged towel from dedicated `sanitizing bucket`; for `white cover`, `water curtain` & `distribution tube`, **thoroughly wipe front, back, and all 4 perimeter sides, thickness edges (`sides/edges`) & corners** (SOP 8 11–15) |\n"
            "| 10 | **Reinstall water distribution tube, water trough, and water curtain first** | Reinstall internal components in order after wiping clean (SOP 8 13) |\n"
            "| 11 | **Toggle CLEAN and after first drain tilt water curtain 45° to pour 3L sanitizer solution into water trough (`Pour 3L of sanitizer solution into water trough`)** | ⚠️ **Mandatory Action**: starts ~35-min self-cleaning cycle, then toggle back to `ICE` (SOP 8 16–18) |\n"
            "| 12 | **Wipe white inner surface of door panel with red-edged towel and reinstall door panel (`Close back ice maker machine`)** | ⚠️ **Mandatory Action**: wipe white inner door liner (including all 4 side edges) with **red-edged towel** and reinstall (SOP 8 19) |\n"
            "| 13 | **Wipe silver exterior of door panel and machine body with blue-edged towel** | Remove exterior sanitizer residue and water marks (SOP 8 20–21) |"
        ),
        pass_criteria="Toggles OFF power, removes door panel/distribution tube/water trough/water curtain, sprays colorless sanitizer on all parts & interior and soaks >= 5 min independently, wipes distribution tube/water curtain/trough/white cover (front, back + all 4 sides/edges) & interior with clean red-edged towel from sanitizing bucket, reinstalls 3 internal parts & pours 3L sanitizer into water trough for Self-Cleaning, wipes white inner door liner with red-edged towel & closes machine, wipes silver exterior with blue-edged towel",
        fail_criteria="Fails to toggle OFF switch, uses yellow-bottle multipurpose cleaner (B3), wipes/scrubs/reinstalls any part (distribution tube / white cover / water curtain / interior of door / interior cavity) before 5-min soak completes (B2), fails to use clean red-edged towel from sanitizing bucket (B4), fails to thoroughly wipe all 4 sides/edges of white cover or water curtain, omits pouring sanitizer into water trough, or fails to close back ice maker machine",
    ),
    SopRuleItem(
        rule_id="B2",
        name="B2 · Per-Component Sanitizer Contact Time Lock & Full-Surface (Sides/Edges) Coverage (Mandatory RED_LINE)",
        category="[B] Ice Maker Weekly Deep Clean (Week 2 Core Module)",
        severity="RED_LINE",
        detection_type="absence",
        requires_full_context=True,
        check_instruction=(
            "- Must **independently track and compute sanitizer contact/soak duration for each of the 5 core components** (`[Component] Spray HH:MM:SS -> First Wipe/Scrub/Reinstall HH:MM:SS = Actual X min Y sec`):\n"
            "  1. **Upper horizontal white water distribution tube (`water distribution tube`)**\n"
            "  2. **Front door panel / white inner cover (`door panel / white cover`, including panels on top of the ice maker or counter)**\n"
            "  3. **White grooved water curtain (`water curtain`)**\n"
            "  4. **Interior of ice maker door / ice bin door liner (`interior of ice maker door`)**\n"
            "  5. **Machine interior cavity & evaporator grid (`all interior / evaporator grid / water trough`)**\n"
            "  - **Manitowoc threshold >= 5 minutes**; **Langtuo / Unconfirmed threshold >= 10 minutes**.\n"
            "- 🔴 **[Component Soak Lock Blocking Rule (Early Wipe/Scrub/Reinstall of Any Component = Immediate Independent B2 CONFIRMED)]**:\n"
            "  - Once any of the 5 components is sprayed with sanitizer (or removed for cleaning), it must sit undisturbed for the full required duration (5 or 10 min) before being wiped, scrubbed, rinsed, or reinstalled!\n"
            "  - Whenever staff before soak completion: (1) **wipe the water distribution tube early (`wipe water distribution tube without waiting 5/10 min`)**; (2) **scrub/wipe/reinstall the white cover / door panel early**; (3) **scrub/wipe/reinstall the water curtain early**; or (4) **wipe and close the interior of the ice maker door early**, **immediately output an independent `B2` (`CONFIRMED`) RED_LINE finding for that component**!\n"
            "  - **[Never Swallowed by Other Rules]**: Even if `A4` (bare-handed), `B4` (towel source), or `B3` (wrong chemical) is reported in the same timeframe, **always output an independent `B2` finding whenever any component is wiped/scrubbed/reinstalled early!**\n"
            "- 🔴 **[Removable Component 'All Four Sides & Thickness Edges' Full Coverage (`Wipe the sides and edges thoroughly`)]**:\n"
            "  - Per SOP (Langtuo Step 27 `especially at the corners and edges`), when scrubbing/wiping the **white cover / door panel** and **water curtain**, staff must thoroughly wipe front, back, and **all four perimeter frames, lateral thickness edges (`sides of the white cover`), and corner grooves**.\n"
            "  - If staff only wipe the flat central face **without thoroughly wiping its four lateral edges (`do not wipe the sides of white cover thoroughly`)**, report a separate `B2` (or `B4`) finding!\n"
            "- **Video ends while soak is in progress (and no early wipe occurred) → `UNVERIFIED · Soak period truncated by end of video`, still report** and note follow-up footage required.\n"
            "- When exact timing is ambiguous → apply Principle 4 strict rounding and judge as 'insufficient duration'."
        ),
        pass_criteria="Sanitizer contact duration for each of water distribution tube, white cover/door panel, water curtain, interior of door, and interior cavity meets the model threshold (Manitowoc >= 5 min / Langtuo >= 10 min), and wiping of white cover & water curtain thoroughly covers front, back, and all 4 lateral sides/edges",
        fail_criteria="Any component (water distribution tube, white cover, water curtain, interior of ice maker door, interior cavity) is wiped/scrubbed/reinstalled before completing the 5/10-min sanitizer soak (RED_LINE, report B2 per component), or fails to thoroughly wipe the four lateral sides/edges of the white cover or water curtain (do not wipe the sides of white cover thoroughly)",
    ),
    SopRuleItem(
        rule_id="B3",
        name="B3 · Chemical Container Visual Verification ('0' Tolerance + Official SOP Spray Bottle Standard)",
        category="[B] Ice Maker Weekly Deep Clean (Week 2 Core Module)",
        severity="RED_LINE",
        detection_type="presence",
        requires_full_context=False,
        check_instruction=(
            "- ✅ **Only Permitted Weekly Cleaning Chemical (Sanitiser) Visual Signature**: Per Manitowoc and Langtuo official SOP diagrams, only a **colorless, clear solution** prepared from **Ecolab KAY-5 Sanitizer** is permitted, held in a **white-bodied clear/translucent spray bottle (with pink/red Sanitizer label)** or **clear graduated measuring bucket (3L/8L)** (monthly descaling permits Delimer in a clear bucket).\n"
            "- 🔴 **Strictly Prohibit Multipurpose Cleaner in Yellow Bottle (`Multipurpose chemical in yellow bottle` = Zero-Tolerance RED_LINE)**:\n"
            "  - A **bright yellow spray bottle (`yellow bottle / yellow spray bottle`)** or **yellow liquid** in the store is **multipurpose detergent / cleaner (`Multipurpose solution`)** and is strictly prohibited on any ice maker component!\n"
            "  - **[Mandatory Hard Rule]**: Whenever staff are observed holding a **yellow spray bottle / yellow bottle** or non-standard chemical container to spray/apply and wipe/scrub the ice maker **white cover / door panel, upper water distribution tube, water curtain, or interior cavity**, **immediately emit an independent `B3` (`CONFIRMED` RED_LINE) finding** (explicitly stating: used multipurpose chemical in yellow bottle to clean ice maker white cover / distribution tube / components)!\n"
            "  - **[Never Omit]**: Even if `B2` (insufficient soak time) or `A1` (touching floor items without handwashing) is already reported in the same timeframe, **you must also independently output this `B3` chemical violation — never let `B2`/`A1` swallow it!**\n"
            "- 🔴 **Prohibited: Mixed use of multiple chemicals** (`Mixed use of chemicals` = RED_LINE)\n"
            "- Track **every chemical bottle / spray bottle** appearing in frame, describing its bottle color (white/clear vs. **yellow bottle**), liquid color, and label; if suspicious or unreadable, report `SUSPECTED · Unidentified chemical container`."
        ),
        pass_criteria="Uses only colorless Ecolab KAY-5 Sanitiser in a white/translucent spray bottle (pink label) or clear graduated bucket throughout (plus Delimer for monthly descaling); never uses a yellow-bottle multipurpose cleaner",
        fail_criteria="Uses multipurpose chemical in a yellow bottle to clean the ice maker white cover, water distribution tube, or interior (RED_LINE, must report B3 independently), mixes multiple chemicals, or uses an unidentified chemical container (SUSPECTED)",
    ),
    SopRuleItem(
        rule_id="B4",
        name="B4 · 3-Color Towel Segregation, Sanitizing Bucket Source Traceback & Perimeter Edge Wiping (Param 3.1 + Official SOP)",
        category="[B] Ice Maker Weekly Deep Clean (Week 2 Core Module)",
        severity="RED_LINE",
        detection_type="presence",
        requires_full_context=False,
        check_instruction=(
            "| Towel Color | Official SOP Designated Use | Storage & Retrieval Requirement |\n"
            "| :---: | :--- | :--- |\n"
            "| 🔴 **Red-Edged Towel** | **Direct food-contact surfaces**: ice maker interior cavity, evaporator grid, **upper water distribution tube**, water trough, float switches, **water curtain**, **white interior surface of door panel**, middle pillar inner face | **Must be stored inside a dedicated labeled sanitizing bucket filled with sanitizer solution and taken fresh from the sanitizing bucket when needed**; **strictly prohibit picking up an unsoaked towel from the countertop, machine top, step stool, or apron** |\n"
            "| 🔵 **Blue-Edged Towel** | **Indirect / non-food-contact exterior surfaces**: stainless-steel exterior body, ice maker top surface, **silver exterior surface of door panel**, middle pillar outer face & bottom border, air filter | Taken from blue sanitizing bucket; may rest on operating counter |\n"
            "| 🟢 **Green-Edged Towel** | Outdoor & customer seating area | **Strictly prohibited on operating counters**; must be hung or kept in a separate container |\n\n"
            "- 🔴 **[5-Second Reverse Traceback of Towel Retrieval Source]**: Whenever staff wipe the ice maker interior cavity, distribution tube, door liner, white cover, or water curtain with a towel, **trace back 5 seconds to verify where the towel was picked up** — compliant operation requires pulling a clean red-edged towel from the **dedicated sanitizing bucket**; if staff pick up a towel lying loose on the counter, sink rim, ice maker top, step stool, or on their person without taking it from a sanitizing bucket (or use the wrong towel color), immediately report an independent `B4` (`CONFIRMED`) RED_LINE violation!\n"
            "- 🔴 **[White Cover & Water Curtain 'Four Lateral Sides & Edges' Wiping Check]**: When cleaning the `white cover / door panel` and `water curtain` with a red-edged towel or brush, staff must wipe both faces and **all four perimeter frames, lateral thickness edges (`sides of white cover`), and corners (`corners and edges`)**; if only the flat center is wiped while omitting the sides/edges, explicitly report this violation!\n"
            "- Sanitizing buckets must carry dedicated labels (positioning tape on an outer lid is invalid)\n"
            "- Wrong towel color / towel not taken from sanitizing bucket / omitting removable part sides & edges / improper storage → violation"
        ),
        pass_criteria="Uses a clean red-edged towel taken directly from a dedicated labeled sanitizing bucket to wipe the ice maker interior, distribution tube, water curtain, and white inner door liner (including all 4 lateral sides/edges), uses blue-edged towel for silver exterior surfaces, and never brings green-edged towels into the operating zone",
        fail_criteria="Does not use a clean red-edged towel taken from a dedicated sanitizing bucket (not used a clean red edge towel from sanitising bucket), omits wiping the four lateral sides/edges of the white cover or water curtain (sides of white cover), cross-uses 3-color towels, leaves red-edged towel loose on the counter, or brings green-edged towel into the operating area",
    ),
    SopRuleItem(
        rule_id="B5",
        name="B5 · Ice Scoop & Ice Handling (Param 4.1 / 4.5)",
        category="[B] Ice Maker Weekly Deep Clean (Week 2 Core Module)",
        severity="RED_LINE",
        detection_type="presence",
        requires_full_context=False,
        check_instruction=(
            "- 🔴 **Ice scoop stored inside ice bin (`stored in ice bins`) = RED_LINE**\n"
            "- Before and after use, the ice scoop must be returned to its **dedicated storage caddy** with the lid closed immediately\n"
            "- Ice may only be dispensed using an ice scoop; **scooping ice bare-handed or with a cup is strictly prohibited**\n"
            "- Ice buckets and ice maker lids must be covered when transporting ice\n"
            "- 🔴 **Pouring excess ice back into the ice maker or ice bin = violation**\n"
            "- Ice bin and ice maker lids must be closed promptly after use"
        ),
        pass_criteria="Dispenses ice only with an ice scoop, returns scoop immediately to its covered dedicated caddy after use, and closes ice bin lid promptly",
        fail_criteria="Stores ice scoop inside the ice bin (RED_LINE), grabs ice by hand, pours excess ice back into the ice maker/ice bin, or leaves ice bin lid open",
    ),
    # ───────────────────────── [C] Continuous Red-Line Scan (Active Every Week) ─────────────────────────
    SopRuleItem(
        rule_id="C1",
        name="C1 · Cross-Contamination ('0' Tolerance)",
        category="[C] Continuous Red-Line Scan (Active Every Week)",
        severity="RED_LINE",
        detection_type="presence",
        requires_full_context=False,
        check_instruction=(
            "- Reusing dropped tools or utensils from the floor without thorough washing and sanitizing\n"
            "- Staff **stepping onto operating countertops** and resuming work without sanitizing the surface\n"
            "- 🔴 **Washing a mop, floor squeegee, or grease trap in a food-prep sink**\n"
            "- Using food packaging materials to hold chemicals\n"
            "- Failing to wash hands after high-risk contamination actions (→ see [A])\n"
            "- Pouring excess ingredients back into original packaging\n"
            "- 🔴 **Fingers touching the interior wall of a customer beverage cup**\n"
            "- 🔴 **Grabbing ready-to-eat ingredients bare-handed** (ice cubes, sliced fruit)\n"
            "- Continuing to use ready-to-eat food dropped onto the operating counter instead of discarding\n"
            "- Reusing ingredient packaging dropped on the floor or dirty sink area without sanitizing\n"
            "- Placing automated dispenser hoses directly inside ingredient containers"
        ),
        pass_criteria="Zero cross-contamination actions; food sinks used strictly for food prep, fingers never touch inner walls of customer cups, dropped utensils thoroughly sanitized",
        fail_criteria="Washes mop/squeegee/grease trap in food sink, touches inner wall of customer cup with fingers, grabs ice/fruit bare-handed, or reuses dropped utensils without sanitizing",
    ),
    SopRuleItem(
        rule_id="C2",
        name="C2 · Cup Usage Compliance ('0' Tolerance)",
        category="[C] Continuous Red-Line Scan (Active Every Week)",
        severity="RED_LINE",
        detection_type="presence",
        requires_full_context=False,
        check_instruction=(
            "- 🔴 Reusing disposable beverage cups\n"
            "- 🔴 Failing to discard uncollected/cancelled customer drinks or reusing their cups\n"
            "- 🔴 Applying multiple stacked order labels on a cup body (reusing a cup without removing old label)\n"
            "- 🔴 Placing POS receipts, pens, or stationery inside beverage cups or food containers"
        ),
        pass_criteria="Single-use beverage cups only, single current order label per cup, no receipts or pens placed inside cups or food containers",
        fail_criteria="Reuses beverage cups, applies multiple labels on a cup, or places receipts/pens/stationery inside beverage cups or food containers",
    ),
    SopRuleItem(
        rule_id="C3",
        name="C3 · Workplace Misconduct ('0' Tolerance)",
        category="[C] Continuous Red-Line Scan (Active Every Week)",
        severity="RED_LINE",
        detection_type="presence",
        requires_full_context=False,
        check_instruction=(
            "- 🔴 Smoking / vaping inside the store\n"
            "- 🔴 **Playing cards**\n"
            "- 🔴 Sleeping during operating hours\n"
            "- 🔴 Eating or drinking during food preparation or in front of customers\n"
            "- 🔴 **Recording videos** (filming in front of customers or filming non-work content)\n"
            "- 🔴 Displaying hostility, sarcasm, or disrespect toward customers\n"
            "- 🔴 **Ignoring customer presence or requests**"
        ),
        pass_criteria="No smoking/vaping, card playing, sleeping, eating/drinking in prep zones, filming non-work videos, or ignoring customers while on duty",
        fail_criteria="Smokes/vapes in store, plays cards, sleeps, eats/drinks during prep or in front of customers, films non-work videos, or ignores customers",
    ),
    SopRuleItem(
        rule_id="C4",
        name="C4 · Grooming, Apron/Personal Item Storage & On-Duty Conduct (1.1–1.4)",
        category="[C] Continuous Red-Line Scan (Active Every Week)",
        severity="RED_LINE",
        detection_type="presence",
        requires_full_context=False,
        check_instruction=(
            "- Accessories: earrings, watches, rings, bracelets, necklaces, false fingernails, ear studs, **decorative hair clips** — strictly prohibited\n"
            "- Attire: uniform + hairnet + hat + mask + gloves; uniform and apron must be clean and stain-free; no loose threads, hair, or expiry stickers on uniform\n"
            "- No loose hair exposed outside the hairnet\n"
            "- Brightly colored pants, shorts, and open-toed shoes are prohibited\n"
            "- 🔴 **Using a personal mobile phone for non-work matters at the customer-facing bar or back kitchen**\n"
            "- 🔴 **Leaning on the counter while waiting for customers (`Leaning on the counter`)**\n"
            "- 🟡 **[Designated Storage Area for Aprons & Personal Items (`Partner apron not kept at designated storage area`)]**:\n"
            "  - After **removing a work apron in the back kitchen or bar, staff must hang/store it in the designated apron/staff storage area (`designated storage area`)**; **never leave a removed apron loose on operating counters, equipment tops, shelves, low stools, or corners**!\n"
            "  - **[Hard Rule Against Swallowing by 'Phone Use']**: If the same staff member both '**removes an apron and leaves it outside the designated storage area (on a counter/stool/corner)**' AND '**sits or stands using a mobile phone in the kitchen/bar**', **you must split them into two separate `C4` Finding rows** — Row 1 reporting `Partner apron not kept at designated storage area` (`NORMAL`) and Row 2 reporting personal mobile phone use in the work zone (`RED_LINE`). Never merge apron misplacement into a single phone-use record!\n"
            "- Personal belongings not kept in designated storage; personal food or drinks placed in the production zone"
        ),
        pass_criteria="Zero prohibited accessories, complete uniform/hairnet/hat/mask/gloves, removed aprons and personal items stored in designated storage area (never left on counters or stools), no personal phone use at bar or kitchen, no leaning on counters",
        fail_criteria="Wears watch/ring/studs/decorative hair clip, missing hat or pulled-down mask/exposed hair, removed apron not kept at designated storage area (Partner apron not kept at designated storage area — must report as separate row), uses personal mobile phone at bar or kitchen (RED_LINE), leans on counter (RED_LINE), or keeps personal drinks in production zone",
    ),
    SopRuleItem(
        rule_id="C5",
        name="C5 · Trash & Floor Hygiene (4.3 / 4.4)",
        category="[C] Continuous Red-Line Scan (Active Every Week)",
        severity="RED_LINE",
        detection_type="presence",
        requires_full_context=True,
        check_instruction=(
            "- Trash bin lid left open after use / lid missing or damaged\n"
            "- 🔴 **Trash overflowing for more than 15 minutes**\n"
            "- Non-foot-pedal trash bin used / broken pedal / bin not lined with a trash bag\n"
            "- Using sinks, cardboard boxes, or unlabeled food containers as trash receptacles\n"
            "- **Temporary trash bags left untied**\n"
            "- Standing water or trash on prep / bar floor left uncleaned for **more than 15 minutes**"
        ),
        pass_criteria="Foot-pedal trash bins kept covered and properly lined, temporary trash bags tied closed, no trash overflow or floor standing water exceeding 15 minutes",
        fail_criteria="Trash bin left uncovered, trash overflowing > 15 minutes (RED_LINE), floor trash or standing water uncleaned > 15 minutes, or temporary trash bag untied",
    ),
    SopRuleItem(
        rule_id="C6",
        name="C6 · Equipment & Utensils (6.x / 7.x / 8.x)",
        category="[C] Continuous Red-Line Scan (Active Every Week)",
        severity="NORMAL",
        detection_type="presence",
        requires_full_context=False,
        check_instruction=(
            "- Automated tea maker: visible residual water at bottom of tea barrel / tea net not drained / **hot water nozzle and latch not wiped before and after brewing** / brewing sequence violates SOP "
            "(filtering tea into ice container and stirring within 30s are checked separately under C8; do not double-report under C6)\n"
            "- Chiller doors: improper installation (off-track, not closed promptly), air gaps leaking cold air\n"
            "- Blenders must not be stored sealed\n"
            "- Steam machine: steam wand **must be purged before and after use** and wiped clean without blockage\n"
            "- Mixer: mixing spindle **must be cleaned before and after use**\n"
            "- Tea utensils (tea barrel, tea filter net, bar spoon, strainer) must be clean before brewing; tea barrel and filter bag must be **disassembled and washed separately**\n"
            "- Opened tea leaf bags must have air squeezed out and be sealed tightly\n"
            "- Chilled ingredients left at ambient temperature for **more than 15 minutes**\n"
            "- Failing to weigh ingredients with a digital scale\n"
            "- Omitting morning thermometer calibration / omitting manual brew temperature check / failing to verify or falsifying tea maker temperature and water volume"
        ),
        pass_criteria="Tea maker/steam wand/mixer wiped, purged, and cleaned before and after use per SOP, no residual water in tea barrel, chiller doors closed tightly, ingredients weighed on digital scale",
        fail_criteria="Hot water nozzle/latch not wiped, residual water in tea barrel, barrel and filter bag not washed separately, chiller door left open, chilled ingredients at room temp > 15 min, or digital scale not used",
    ),
    SopRuleItem(
        rule_id="C7",
        name="C7 · Customer Service Standards (9.x)",
        category="[C] Continuous Red-Line Scan (Active Every Week)",
        severity="RED_LINE",
        detection_type="absence",
        requires_full_context=True,
        check_instruction=(
            "- 🔴 Customer waiting in the counter area for **more than 10 seconds** without proactive greeting\n"
            "- Failing to hand over receipt/change with **both hands** after payment; failing to guide customer to waiting or seating area\n"
            "- Failing to verify receipt or mobile order at beverage pickup\n"
            "- Beverage handed over unsealed/uncapped, improperly oriented, or missing straws/napkins/spoons"
        ),
        pass_criteria="Proactively greets arriving customers within 10s, hands over receipts/change with both hands, verifies order at pickup, and serves sealed beverages with complete straws/napkins",
        fail_criteria="Customer waits > 10s without greeting (RED_LINE), one-handed handover, order not verified at pickup, or drink unsealed/missing straws or napkins",
    ),
    SopRuleItem(
        rule_id="C8",
        name="C8 · Automated Tea Maker Filtering into Ice & 30s Stirring (6.2 Automated Tea Maker Machine)",
        category="[C] Continuous Red-Line Scan (Active Every Week)",
        severity="NORMAL",
        detection_type="absence",
        requires_full_context=True,
        check_instruction=(
            "Customer SOP verbatim text (`6.2 Automated Tea Maker Machine · Filter Tea into an Ice-Prepared Container`):\n"
            "- Pour the filtered tea into a container with ice, prepared 2-3 minutes beforehand "
            "(but not more than 3 minutes in advance).\n"
            "- Stir the tea within 30 seconds of pouring to ensure even cooling. "
            "(30s from the machine prompt: “Tea is ready” / dripping stage)\n"
            "\n"
            "Verify and record every tea dispensing cycle independently (if multiple cycles occur in one clip, check each one):\n"
            "1. **Ice First, Tea Second**: Filtered tea must be poured (or dripped) into a container **already pre-loaded with ice**; pouring hot tea first and adding ice afterward → violation.\n"
            "2. **Ice Prepared 2–3 Minutes Beforehand**: The interval from adding ice into the container until filtered tea starts pouring/dripping into it should be 2–3 minutes. "
            "**More than 3 minutes** (ice sitting too long) → violation; less than 2 minutes also deviates from the 2–3 minute SOP window and must be reported with the actual interval. "
            "If ice was added before this clip started: continue timing if the carryover summary provides the ice-prep timestamp; otherwise mark `OUT_OF_SCOPE` (do not guess).\n"
            "3. **Stir Within 30 Seconds**:\n"
            "   - Timer start = the moment the tea maker prompts 'Tea is ready' / enters the dripping stage. Since video has no audio, use the **earliest visible cue**: machine screen/indicator showing completion, tea stream at the spout transitioning from continuous flow to dripping, or staff beginning to pour filtered tea into the ice container.\n"
            "   - Timer end = the moment staff first insert a long bar spoon / stirring rod / paddle into the container to stir the tea.\n"
            "   - If the container remains visible and stirring has not started > 30 seconds after the start cue → `CONFIRMED` (strict boundary rounding); "
            "if the container is carried out of frame or occluded within 30 seconds so stirring cannot be verified → `UNVERIFIED · Required action not observed`.\n"
            "   - If the start cue occurred before this clip: continue timing from the carryover summary if present and unstirred; otherwise mark `OUT_OF_SCOPE`.\n"
            "4. In `evidence`, clearly state: start cue timestamp (OSD) and visual signal observed, stirring start timestamp (or 'not observed'), actual elapsed seconds, and staff ID.\n"
            "5. Record any unclosed timers at the end of the clip (ice added awaiting tea, or tea ready awaiting stir) in `carryover_state_summary` with OSD timestamp and container description."
        ),
        pass_criteria=(
            "Filtered tea is poured into a container pre-loaded with ice 2–3 minutes beforehand (<= 3 minutes), "
            "and stirred within 30 seconds from the tea maker 'Tea is ready' / dripping stage"
        ),
        fail_criteria=(
            "Pours tea before adding ice; ice prepared > 3 minutes or < 2 minutes before pouring tea; "
            "or stirs > 30 seconds after 'Tea is ready' / dripping stage (or stirring not observed)"
        ),
    ),
    # ───────────────────────── [D] Operations & Labor Productivity (Week 3 Core Module) ─────────────────────────
    SopRuleItem(
        rule_id="D1",
        name="D1 · Valid Cup Output Counting",
        category="[D] Operations & Labor Productivity (Week 3 Core Module)",
        severity="NORMAL",
        detection_type="presence",
        requires_full_context=True,
        check_instruction=(
            "A valid completed cup output must traverse all four stations in frame:\n"
            "`Station 1 Label application + AI scanner` → `Station 2 Ice dosing + shaker blending` → "
            "`Station 3 Pouring into customer cup (including heating step for hot drinks)` → `Station 4 Cup sealing + handover at pick-up`\n"
            "- Traversing only partial stations → record separately as 'Unclosed N cups' and exclude from total valid count\n"
            "- Single water dispense or single cup sealing **does not count as a completed cup**"
        ),
        pass_criteria="Beverage traverses all four stations (Station 1->2->3->4) to count as a valid completed cup",
        fail_criteria="Fails to traverse all four stations (recorded as unclosed cup count and excluded from total valid cups)",
    ),
    SopRuleItem(
        rule_id="D2",
        name="D2 · IPLH Calculation (Week 3 Deliverable)",
        category="[D] Operations & Labor Productivity (Week 3 Core Module)",
        severity="NORMAL",
        detection_type="presence",
        requires_full_context=True,
        check_instruction=(
            "```\n"
            "IPLH (Items Per Labour Hour) = Total Valid Completed Cups ÷ Actual On-Duty Labour Hours\n"
            "Actual On-Duty Labour Hours = Σ(Each staff member's visible on-duty duration in frame, converted to hours)\n"
            "```\n"
            "Output: **Total Store IPLH** + **Individual Per-Staff Hourly Cup Rate**. For samples < 1 hour, extrapolate linearly and state the note."
        ),
        pass_criteria="Accurately outputs total store IPLH and individual per-staff hourly cup rates",
        fail_criteria="Fails to compute actual on-duty labor hours and valid cup output according to the IPLH formula",
    ),
    SopRuleItem(
        rule_id="D3",
        name="D3 · Labor Deployment & Staffing Alignment",
        category="[D] Operations & Labor Productivity (Week 3 Core Module)",
        severity="NORMAL",
        detection_type="presence",
        requires_full_context=True,
        check_instruction=(
            "1. Identify operating period (Opening / Peak 13:00–13:30 / Off-Peak / Closing)\n"
            "2. Output active headcount changes over time\n"
            "3. Identify anomalies: understaffing during peak hours causing order backlog / overstaffed idle labor during off-peak or closing periods"
        ),
        pass_criteria="Staffing deployment matches customer flow across operating periods",
        fail_criteria="Understaffing during peak hours causes order backlog, or excess idle staffing during off-peak/closing hours",
    ),
    SopRuleItem(
        rule_id="D4",
        name="D4 · Beverage Preparation Quality Spot-Check (Param 8.4, Minimum 3 Cups)",
        category="[D] Operations & Labor Productivity (Week 3 Core Module)",
        severity="NORMAL",
        detection_type="presence",
        requires_full_context=True,
        check_instruction=(
            "- Whether beverages are prepared strictly following recipe SOP\n"
            "- Proper use of measuring cups (jiggers) and shaker cups\n"
            "- **Whether ice-adding and ingredient-dosing sequence is correct**\n"
            "- Whether contents are poured out completely (after skimming excess foam, all liquid and fruit pulp in the shaker cup must be poured into the serving cup)"
        ),
        pass_criteria="Spot-checks >= 3 cups: all follow recipe SOP, proper measuring/shaker cup use, correct ice and ingredient sequence, excess foam skimmed and contents completely poured out",
        fail_criteria="Incorrect ice and ingredient sequence, measuring cup omitted, excess foam not skimmed, or liquid/pulp not completely poured out from shaker cup",
    ),
]


def load_rules_from_yaml(yaml_path: Optional[Path] = None) -> tuple[List[VisualScanTarget], List[SopRuleItem]]:
    """Returns the 10 visual scan targets and the 24 bundled SOP rule sections (23 verbatim V10/V25 + C8)."""
    target_path = yaml_path or BUNDLED_SOP_YAML_PATH
    scan_targets = list(DEFAULT_VISUAL_SCAN_TARGETS)
    if target_path.exists():
        try:
            raw = yaml.safe_load(target_path.read_text(encoding="utf-8")) or {}
            loaded_scans = [
                VisualScanTarget(subject=entry["subject"], question=entry["question"])
                for entry in raw.get("visual_scan", [])
                if "subject" in entry and "question" in entry
            ]
            if loaded_scans:
                scan_targets = loaded_scans
        except Exception as exc:
            logger.warning("Failed to read visual_scan from %s (%s); using defaults.", target_path, exc)

    return scan_targets, list(DEFAULT_CHAGEE_V25_RULES)


DEFAULT_LAYER1_TEMPLATE: str = """# Role
You are the CHAGEE Store CCTV Cloud Auditor, executing SOP compliance audits in accordance with the *APAC Cloud Audit Parameters 2026 v1.0* and equipment SOPs.
(Active SOP Rule Library Version: `{prompt_version}`)

# Core Adjudication Principles (Highest Priority)

## Principle 1: Prefer False Positives Over False Negatives
Every finding from this audit undergoes 100% human supervisor review. Therefore:
- **Your primary objective is 'zero missed potential violations', not 'guaranteeing every reported item is 100% certain'.**
- Whenever an observation is ambiguous or borderline → **always report it** with the appropriate disposition label for human verification.
- **Never** omit a report simply because you are 'not completely sure' or 'it might be normal operation'.

## Principle 2: Burden-of-Proof Reversal
For any mandatory SOP action (such as handwashing, sanitizer soaking, surface wiping, or power-off disconnection), if you **do not clearly observe it happening in the video**:
- Never assume 'they probably did it off-camera';
- Always record it as `UNVERIFIED · Required action not observed` and include it in the `findings` array.

## Principle 3: Four-Tier Disposition Labels
Every evaluated rule record must carry one of the following labels:
| Label | Meaning | Include in Findings? |
| :--- | :--- | :---: |
| `CONFIRMED` | Violation action is clearly visible with complete visual evidence | ✅ Yes |
| `SUSPECTED` | Behavior closely matches a violation but has partial occlusion/blur preventing 100% certainty | ✅ Yes |
| `UNVERIFIED` | Mandatory SOP action was not observed (may have occurred out of frame or was omitted) | ✅ Yes |
| `OUT_OF_SCOPE` | Relevant staff/equipment is completely outside this camera view or time window | ❌ No (note separately) |

## Principle 4: Strict Boundary Rounding
For all duration thresholds, whenever exact reading is uncertain, **round in the direction stricter toward store compliance**:
- Hand scrubbing 19–21 seconds uncertain → judge as 'under 20 seconds'
- Sanitizer soaking 9–11 minutes uncertain → judge as 'under 10 minutes'
- Customer waiting 9–11 seconds uncertain → judge as 'over 10 seconds'
- Trash overflow 14–16 minutes uncertain → judge as 'over 15 minutes'

## Principle 5: Output Specification
- Language: **English**
- Timestamps: `HH:MM:SS`
- **No fabrication**: Never invent actions that did not occur; however, 'not observed' must be explicitly stated.
- **Strict Separation Between Player Relative Timestamp (`timestamp_in_clip`) and CCTV On-Screen Clock (`on_screen_clock`) to Prevent Slice Drift**: `timestamp_in_clip` must be the elapsed player time within the current video slice (`MM:SS`, where the start of this slice is `00:00`), anchored at the onset of the violation action (the system automatically cuts a 20-second `[T-2s, T+18s]` evidence clip around it). Never substitute mental subtraction of the OSD wall clock for `timestamp_in_clip`!

## Principle 6: One-Action-One-Record & Orthogonal Decomposition of Compound Violations (Never Let High-Salience Events Swallow Companion Violations)
1. **Exhaustive Enumeration of Preceding Trigger Actions in the Same Causal Chain (Preventing High-Salience Actions from Masking Micro-Actions)**: When a 'Preceding Trigger → Missing Mandatory Action → Subsequent Operation' SOP rule fires within a monitoring window where multiple preceding triggers occurred, list **every observed preceding trigger action and its timestamp in chronological order** in `evidence`; never report only the longest or most visually salient action while omitting 1–2 second micro-actions before or after it.
2. **Row-by-Row Splitting of Concurrent or Sequential Independent Violations by the Same Person (One-Action-One-Record)**: When the same staff member commits two distinct types of violations concurrently or sequentially, or repeatedly violates a per-occurrence rule multiple times, **each independent violation must be emitted as its own separate `Finding` row** — never merge them into a single record.
3. **Multi-Dimensional Orthogonal Verification of a Continuous Task**: When a continuous operation spans multiple dimensions in `# Step 3: SOP Checklist` (such as step sequence, duration threshold, tool/consumable compliance, full spatial surface/edge coverage, and mandatory closing steps), **verify each dimension orthogonally by its `rule_id` and emit separate `Finding` rows**. Never omit one dimension because another was already reported in the same timeframe.

---

# Step 0: Timeline Calibration
1. Within the first 5 seconds of the video, locate a frame where the corner OSD watermark timestamp (**usually top-left**, or top-right/bottom on some cameras) is clearest, and read out `DD-MM-YYYY HH:MM:SS` (or `YYYY-MM-DD HH:MM:SS`) character by character.
2. Double-check easily confused dot-matrix digits (`1/4`, `1/7`, `3/8`, `5/6`, `0/8`, `6/8`) before writing.
3. Derive timestamps across the clip from this anchor + elapsed player time; **nonlinear jumps of a minute or more are prohibited**.
4. When no OSD watermark exists, use relative timing `T+00:00:00` and state this explicitly.
5. Identify the operating period: `Opening / Operating / Peak (~13:00–13:30) / Off-Peak / Closing`, and zone: `Front Bar (POS/Pick-Up)` or `Back Kitchen`.
6. **Cross-Slice / Cross-Video State Carryover Calibration**: When a `[Prior Segment Unclosed State Carryover Summary]` is provided, first verify whether the first-frame OSD clock and camera scene are continuous with the prior summary (same camera angle and time gap <= 5 minutes); if continuous, seamlessly resume cross-slice timers (such as 5-min / 10-min soaking timers or 5-min handwashing monitoring windows); if the OSD clock jumps across periods or the camera angle differs, ignore prior carryover state to prevent false positives.

# Step 1: Personnel Cataloging & Independent Multi-Person Tracking (Exhaustive, Anti-Attention-Masking)
Assign a stable ID to **every** person appearing in the frame based on time-invariant traits:
`Staff_A-Male-ShortHair-Capped-CupStation`
- **Actively scan frame edges, background depth, mirror reflections, and doorway entries** — do not fixate only on the main prep zone.
- Even if someone appears for only a few seconds, catalog them and tag `Brief appearance`.
- **PPE State Persistence Principle**: Once gloves/apron/hat are confirmed worn, assume they remain worn until a clear removal action is observed.
- **Multi-Person Attention Decoupling (Anti-Attention-Masking)**: Whenever >= 2 people are on screen simultaneously, **never let a single high-salience action (such as large body movements, prolonged stationary posture, or an obvious violation) capture all your attention**. Partition by spatial station (front bar, sink zone, equipment zone, aisle) and maintain **independent action timelines** for each person present (`Staff_A`, `Staff_B`, `Staff_C`…), verifying each person's hand contacts and procedural closures independently.
- After cataloging, ask yourself: `Did I miss anyone who appeared in the frame?`

## Pre-Verdict Visual Grounding Scan Targets
{visual_scan_targets}

---

# Step 2: Visual Adjudication Rules (High-Recall Edition)

## 2.1 Contact Determination
- **Definite Contact** (finger grasping / pressing / load-bearing deformation) → `CONFIRMED`
- **Suspected Contact** (hand and object pixels overlap, but physical touch cannot be 100% confirmed) → **`SUSPECTED`, must still be reported**
- **Definite Non-Contact** (air-gap pouring with visible clearance between hand and object) → Do not report as a violation, but log the action in the trajectory

> Note: Unlike legacy rules, **suspected contact is never exempted** — always report for human review.

## 2.2 State-Change Traceback
When an object's state has changed (drawer opened, panel removed, trash bag replaced) without initially noticing the transition:
1. Trace backward to find the causal action;
2. If found → attribute to the specific staff member, `CONFIRMED`;
3. If not found → record `UNVERIFIED`, listing all staff IDs present during that window as candidates; **never skip reporting**.

## 2.3 Posture & Occlusion
- Independent joint structure visible → standard adjudication
- Backlighting / low resolution / subject blending into background → **do not make a hard claim, but must report as `SUSPECTED`**, describing the pixel-level visual observation

## 2.4 Hand-to-Mouth Actions (Smoking / Eating / Vaping)
| Scenario | Disposition |
| :--- | :--- |
| Cigarette / vape / food clearly visible | `CONFIRMED` RED_LINE |
| Repeated hand-to-mouth motion in idle zones (back door, corridor, storeroom entrance) | `SUSPECTED` RED_LINE, **must report** |
| Hand-to-mouth motion in production zone with partial occlusion | `SUSPECTED`, **must report** |

---

# Step 3: SOP Checklist

{step3_checklist_md}{dynamic_sop_Evaluation_matrix}

---

# Step 4: Missed-Detection Self-Audit (Mandatory, Cannot Skip)

After completing the primary scan, re-scan the entire clip once to specifically catch items easily missed on the first pass, and output your self-audit findings:

1. **Edge Personnel & Far-Field Targets**: Are there uncataloged personnel or overlooked far-field actions at frame corners, background depth, elevated equipment tops, or doorways?
2. **Transient Micro-Actions & Procedure Start/End Node Rescan**:
   - Were any 1–2 second transient hand contacts or casual placement of items/PPE skipped or swallowed by a long-duration main event?
   - For every multi-step standard procedure, re-verify its **first 3 seconds (was the initial step sequence correct?)**, **net active duration of the core middle step (excluding prep and closing steps, did it meet the threshold?)**, and **last 5 seconds (was the mandatory closing action fully executed?)**. If the same staff member performed the procedure multiple times and failed each time, were they split into separate Finding rows?
3. **Occlusion Intervals**: Was any staff member occluded by equipment or pillars for more than 10 seconds? Flag that interval as a blind spot and report it.
4. **Concurrent Multi-Person & Same-Second Masking Rescan (Critical)**: Whenever a violation is detected for one staff member during `[T1, T2]`, you must return to `[T1, T2]`, **deliberately look away from that already-flagged person, and inspect every other person present in the frame during `[T1, T2]` for their hand contacts and procedural steps**, ensuring a second person's violation at the exact same second is never missed!
5. **Single-Event Absorption & Multi-Dimensional Orthogonal Rescan (Critical)**: Review each detected violation against `# Step 3: SOP Checklist`: (1) Did the same person commit another independent rule violation shortly before or after? (2) Did the same continuous task (such as equipment disassembly, cleaning, and sanitizing) simultaneously violate multiple orthogonal dimensions in the checklist (per-component duration thresholds, container/consumable color and source, full spatial coverage of front/back and all four lateral sides/edges, mandatory closing steps)? Every violated dimension must be emitted as its own independent `Finding` by `rule_id`!
6. **Equipment & Container States**: Were controlled equipment doors/lids, container covers, and protective barriers kept in their SOP-mandated states throughout the clip?
7. **Timeline Jumps**: Were there any nonlinear jumps in the OSD timeline? If so, note the affected intervals.

Append any new violations uncovered during the self-audit to the `findings` array.

---

# Output Structure & Generation Order (Strictly Follow JSON Field Order: Catalog People -> Log Trajectories -> Self-Audit -> Final Findings)

You must generate content strictly in the following JSON field order, **never skipping trajectory logging or self-audit to jump straight to `findings`**:

1. **`calibrated_wall_clock_start` (Step 0 Calibration Info)**: Write the first-frame anchor OSD wall-clock timestamp (`DD-MM-YYYY HH:MM:SS`), ambiguous digit verification notes, operating period, and zone classification.
2. **`people` (Step 1 Personnel Roster)**: Exhaustively list every staff ID, appearance/PPE traits, and first-to-last seen timestamps (including brief passersby).
3. **`person_trajectories` (Per-Person Independent Event Trajectories: Event-Driven + Merged Heartbeats)**:
   - **Must correspond 1-to-1 with the `people` list (one independent trajectory string per person)**; never mix everyone's actions into a single timeline!
   - Format each person's trajectory chronologically: `Staff_ID [Station]: HH:MM:SS (Player MM:SS) specific action, hand-contacted surface, and tool state -> ...`.
   - Whenever a high-risk contamination contact, multi-step procedure (including first 3s, net duration, and last 5s), or container/tool chemical preparation & application occurs, record the exact start/end timestamps and duration in that person's trajectory.
   - For routine anomaly-free spans lasting several minutes, merge into a heartbeat entry (e.g., `10:00:00-10:05:00 [HEARTBEAT] routine compliant work at station`).
4. **`self_check_notes` (Step 4 Missed-Detection Self-Audit Notes)**:
   - Before emitting the final `findings` array, answer each check in `# Step 4: Missed-Detection Self-Audit` against `person_trajectories`.
   - Specifically verify whether other on-screen staff during `[T1, T2]` had unwashed hand contacts, insufficient step durations, missing closing steps, or improper tool usage while a salient event occurred.
5. **`findings` (Combined Primary Scan + Self-Audit Violation Array)**:
   - Output every `CONFIRMED`, `SUSPECTED`, and `UNVERIFIED` finding from both the primary scan and `self_check_notes` as an independent `Finding` object.
6. **`carryover_state_summary` (Cross-Slice State Carryover Summary)**: Record the end-of-slice OSD clock timestamp and all unclosed cross-slice monitoring windows, soak timer start points, and tool/container chemical states.
"""

SHEET_RULE_HEADER: List[str] = [
    "rule_id",
    "name",
    "category",
    "severity",
    "detection_type",
    "requires_full_context",
    "check_instruction",
    "pass_criteria",
    "fail_criteria",
]


def rules_to_sheet_rows(rules: List[SopRuleItem]) -> List[List[str]]:
    """Serializes `SopRuleItem` list to 9-column Google Sheet rows (`A1:I25`, including header row)."""
    rows: List[List[str]] = [list(SHEET_RULE_HEADER)]
    for r in rules:
        rows.append(
            [
                r.rule_id,
                r.name,
                r.category,
                r.severity,
                r.detection_type,
                "TRUE" if r.requires_full_context else "FALSE",
                r.check_instruction,
                r.pass_criteria,
                r.fail_criteria,
            ]
        )
    return rows


def parse_rules_from_sheet_rows(rows: List[List[str]]) -> List[SopRuleItem]:
    """Parses 9-column Google Sheet rows (`A1:I100`, skipping header row) into `SopRuleItem` list.
    Accepts both English headers (`rule_id`, `Rule_ID`, ...) and legacy Chinese headers (`规则编号`, ...).
    """
    if not rows:
        return []
    first_cell = str(rows[0][0]).strip().lower() if rows[0] else ""
    start_idx = (
        1
        if first_cell in {"rule_id", "规则编号", "规则id", "id"}
        or len(rows) > 1
        and first_cell.startswith("rule")
        else 1
    )
    if len(rows) <= start_idx:
        return []
    rules: List[SopRuleItem] = []
    for row in rows[start_idx:]:
        if not row or not str(row[0]).strip():
            continue
        padded = [str(c) if c is not None else "" for c in row] + [""] * max(0, 9 - len(row))
        rules.append(
            SopRuleItem(
                rule_id=padded[0].strip(),
                name=padded[1].strip() or padded[0].strip(),
                category=padded[2].strip() or "SOP",
                severity=padded[3].strip() or "RED_LINE",
                detection_type=padded[4].strip() or "presence",
                requires_full_context=padded[5].strip().upper() in {"TRUE", "1", "YES", "是"},
                check_instruction=padded[6].strip(),
                pass_criteria=padded[7].strip(),
                fail_criteria=padded[8].strip(),
            )
        )
    return rules


parse_sop_rules_from_rows = parse_rules_from_sheet_rows


def _render_visual_scan_targets_md(
    visual_scan_targets: Optional[List[VisualScanTarget]] = None,
) -> str:
    targets = visual_scan_targets if visual_scan_targets is not None else DEFAULT_VISUAL_SCAN_TARGETS
    return "\n".join(f"- **{t.subject}**: {t.question}" for t in targets)


def render_v25_system_instruction(
    prompt_version: str,
    rules: List[SopRuleItem],
    visual_scan_targets: Optional[List[VisualScanTarget]] = None,
    layer1_template: Optional[str] = None,
    is_spot_check: bool = False,
) -> str:
    """Renders the canonical `CHAGEE_CCTV_Cloud_Auditor_Prompt_v1.0` with dynamic Sheet rules."""
    grouped_blocks: List[str] = []
    current_category = None
    for r in rules:
        if r.category != current_category:
            current_category = r.category
            grouped_blocks.append(f"\n## {current_category}\n")
            if "[C]" in current_category or "【C】" in current_category:
                grouped_blocks.append(
                    "> Every item below must be explicitly evaluated as compliant or reported as a specific violation; **never omit any item**.\n"
                )
        grouped_blocks.append(f"### {r.name}\n{r.check_instruction}\n")

    spot_check_scope_lock_md = ""
    if is_spot_check or (prompt_version or "").strip() in TAB4_ALIASES:
        allowed_ids = ", ".join(f"`{r.rule_id}`" for r in rules if r.rule_id) or "`NONE`"
        spot_check_scope_lock_md = (
            "> ⚠️ **[Targeted Spot-Check / New SOP Test Scope Lock]**\n"
            f"> This run is executing in `{TAB4_SPOT_CHECK_TITLE}` targeted spot-check mode and **evaluates ONLY the {len(rules)} specified rule(s) listed below ({allowed_ids})**.\n"
            "> - Strictly execute 'Step 0: Timeline Calibration', 'Step 1: Personnel Cataloging & Independent Multi-Person Tracking (Anti-Attention-Masking)', 'Step 2: Visual Adjudication Rules', and 'Step 4: Missed-Detection Self-Audit' to prevent missed detections or timestamp drift;\n"
            f"> - **Strictly DO NOT report any other SOP rules outside this targeted list ({allowed_ids})**; every record in the output JSON `findings` array must have a `rule_id` belonging strictly to `{allowed_ids}`.\n"
        )
        grouped_blocks.insert(0, spot_check_scope_lock_md)

    step3_checklist_md = "\n".join(grouped_blocks).strip()
    visual_scan_md = _render_visual_scan_targets_md(visual_scan_targets)
    tpl = layer1_template if layer1_template is not None else DEFAULT_LAYER1_TEMPLATE
    has_step3_ph = "{step3_checklist_md}" in tpl
    has_matrix_ph = "{dynamic_sop_Evaluation_matrix}" in tpl
    if "{prompt_version}" not in tpl and not has_matrix_ph:
        raise ValueError(
            "layer1_template must contain {prompt_version} placeholder"
        )
    if not has_step3_ph and not has_matrix_ph:
        raise ValueError(
            "layer1_template must contain {step3_checklist_md} or {dynamic_sop_Evaluation_matrix} placeholder"
        )

    rendered = tpl.replace("{prompt_version}", prompt_version).replace(
        "{visual_scan_targets}", visual_scan_md
    ).replace("{spot_check_scope_lock_md}", spot_check_scope_lock_md)
    if has_step3_ph and has_matrix_ph:
        rendered = rendered.replace("{step3_checklist_md}", step3_checklist_md).replace(
            "{dynamic_sop_Evaluation_matrix}", ""
        )
    elif has_step3_ph:
        rendered = rendered.replace("{step3_checklist_md}", step3_checklist_md)
    else:
        rendered = rendered.replace("{dynamic_sop_Evaluation_matrix}", step3_checklist_md)
    return rendered


_TRIGGER_TIME_RE = re.compile(r"^\s*([01]?\d|2[0-3]):([0-5]\d)\s*$")
_DRIVE_FOLDER_ID_RE = re.compile(r"/folders/([a-zA-Z0-9_-]{10,})")
_RAW_DRIVE_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{10,}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_TRUTHY_ENABLED_TOKENS = frozenset({"TRUE", "ENABLED", "YES", "1", "ON", "启用", "Y"})
_FALSY_ENABLED_TOKENS = frozenset(
    {"FALSE", "PAUSED", "DISABLED", "NO", "0", "OFF", "暂停", "禁用", "N", ""}
)


def _extract_scheduled_folder_id(raw_url: str) -> str:
    text = (raw_url or "").strip()
    if not text or "/file/d/" in text:
        return ""
    from .gcs_gateway import normalize_gcs_target

    try:
        gcs_uri = normalize_gcs_target(text)
    except ValueError:
        gcs_uri = None
    if gcs_uri:
        return gcs_uri
    m = _DRIVE_FOLDER_ID_RE.search(text)
    if m:
        return m.group(1)
    if _RAW_DRIVE_ID_RE.match(text):
        return text
    return ""


def _is_legacy_tab2_header(header_row: List[str]) -> bool:
    if not header_row:
        return False
    col3 = str(header_row[3]).strip().lower() if len(header_row) > 3 else ""
    col5 = str(header_row[5]).strip().lower() if len(header_row) > 5 else ""
    return col3 in ("trigger_time", "触发时间") or col5 in ("lookback_hours", "回溯小时数")


def parse_scheduled_rules_with_diagnostics(
    rows: List[List[str]],
) -> tuple[List[ScheduledAuditRule], List[ScheduledRuleRowError]]:
    """Parses `[Tab 2: Scheduled Jobs]` (`Tab2_Scheduled_Jobs` or legacy `Tab2_定时巡检配置表`) rows into `(valid_rules, row_errors)`.

    Supports both:
    - The simplified UTC schema (`Trigger_Hour_UTC`, `Trigger_Minute_UTC`, `Lookback_Days`,
      with default `UTC` timezone, hours `00`–`23`, minutes `00`/`15`/`30`/`45`, and
      lookback days `0.5` or `1`–`7` converted to `lookback_hours = days * 24.0`).
    - The legacy schema (`Trigger_Time`, `Timezone`, `Lookback_Hours`) for backward compatibility.
    """
    from zoneinfo import ZoneInfo

    if not rows or len(rows) < 2:
        return [], []
    is_legacy = _is_legacy_tab2_header(rows[0])
    parsed: List[ScheduledAuditRule] = []
    errors: List[ScheduledRuleRowError] = []
    seen_rule_ids: dict[str, int] = {}

    for idx, raw_row in enumerate(rows[1:], start=2):
        padded = [str(c).strip() for c in raw_row] + [""] * max(0, 9 - len(raw_row))
        (
            rule_id,
            rule_name,
            root_url,
            col_d,
            col_e,
            col_f,
            sup_email,
            enabled_raw,
            last_status,
        ) = padded[:9]

        if not any(padded[:8]):
            continue

        def _fail(msg: str) -> None:
            logger.warning("Skipping Tab2 row %d (%s): %s", idx, rule_id or "<empty>", msg)
            errors.append(
                ScheduledRuleRowError(
                    row_index=idx,
                    rule_id=rule_id,
                    error_message=msg,
                    previous_status=last_status,
                )
            )

        if not rule_id:
            _fail("❌ Config Error: Rule_ID (Column A) cannot be empty")
            continue

        if rule_id in seen_rule_ids:
            _fail(
                f"❌ Config Error: Rule_ID '{rule_id}' duplicates row {seen_rule_ids[rule_id]}; every Rule_ID must be unique"
            )
            continue
        seen_rule_ids[rule_id] = idx

        if "/file/d/" in root_url:
            _fail(
                "❌ Config Error: Root_Folder_URL (Column C) cannot be a single video file link (/file/d/); "
                "please provide a Google Drive folder link (/folders/...) or gs:// prefix"
            )
            continue

        folder_id = _extract_scheduled_folder_id(root_url)
        if not folder_id:
            _fail(
                f"❌ Config Error: Root_Folder_URL (Column C) '{root_url}' is not a valid Google Drive folder link or gs:// prefix"
            )
            continue

        if is_legacy:
            t_match = _TRIGGER_TIME_RE.match(col_d or "02:00")
            if not t_match:
                _fail(f"❌ Config Error: Trigger_Time '{col_d}' is invalid; expected HH:MM (00:00–23:59)")
                continue
            normalized_time = f"{int(t_match.group(1)):02d}:{int(t_match.group(2)):02d}"
            tz_clean = col_e or "Asia/Singapore"
            try:
                ZoneInfo(tz_clean)
            except Exception:
                _fail(f"❌ Config Error: Timezone '{tz_clean}' is not a valid IANA timezone")
                continue
            try:
                lookback_hours = float(col_f or 24.0)
                if lookback_hours <= 0.0:
                    raise ValueError("lookback_hours must be > 0")
                lookback_hours = max(1.0, min(720.0, lookback_hours))
            except (TypeError, ValueError):
                _fail(f"❌ Config Error: Lookback_Hours '{col_f}' is not a valid number of hours")
                continue
            lookback_days = round(lookback_hours / 24.0, 4)
        else:
            raw_hour = col_d if col_d != "" else "02"
            raw_min = col_e if col_e != "" else "00"
            try:
                hh = int(raw_hour)
                if hh < 0 or hh > 23:
                    raise ValueError("hour out of range")
            except (TypeError, ValueError):
                _fail(
                    f"❌ Config Error: Trigger_Hour_UTC (Column D) '{col_d}' is invalid; select 00–23 (UTC hour) from dropdown"
                )
                continue
            try:
                mm = int(raw_min)
                if f"{mm:02d}" not in VALID_TRIGGER_MINUTES_UTC:
                    raise ValueError("minute not in {00, 15, 30, 45}")
            except (TypeError, ValueError):
                _fail(
                    f"❌ Config Error: Trigger_Minute_UTC (Column E) '{col_e}' is invalid; select 00, 15, 30, or 45 from dropdown"
                )
                continue
            normalized_time = f"{hh:02d}:{mm:02d}"
            tz_clean = "UTC"

            raw_days = col_f if col_f != "" else "1"
            try:
                lb_days = float(raw_days)
                if lb_days not in VALID_LOOKBACK_DAYS_VALUES:
                    raise ValueError("lookback_days not in allowed options")
            except (TypeError, ValueError):
                _fail(
                    f"❌ Config Error: Lookback_Days (Column F) '{col_f}' is invalid; select 0.5 (12h) or 1–7 days from dropdown"
                )
                continue
            lookback_days = lb_days
            lookback_hours = round(lb_days * 24.0, 2)

        if sup_email and not _EMAIL_RE.match(sup_email):
            _fail(f"❌ Config Error: Supervisor_Email (Column G) '{sup_email}' is not a valid email address")
            continue

        enabled_upper = enabled_raw.upper()
        if enabled_upper not in _TRUTHY_ENABLED_TOKENS and enabled_upper not in _FALSY_ENABLED_TOKENS:
            _fail(
                f"❌ Config Error: Enabled (Column H) '{enabled_raw}' is invalid; select ENABLED or PAUSED from dropdown"
            )
            continue
        enabled_flag = enabled_upper in _TRUTHY_ENABLED_TOKENS

        parsed.append(
            ScheduledAuditRule(
                row_index=idx,
                rule_id=rule_id,
                rule_name=rule_name or rule_id,
                root_folder_url=root_url,
                root_folder_id=folder_id,
                trigger_time=normalized_time,
                timezone=tz_clean,
                lookback_days=lookback_days,
                lookback_hours=lookback_hours,
                supervisor_email=sup_email or "scheduled-auditor@chagee.com",
                enabled=enabled_flag,
                last_run_status=last_status,
            )
        )
    return parsed, errors


def parse_scheduled_rules_from_rows(rows: List[List[str]]) -> List[ScheduledAuditRule]:
    """Parses `[Tab 2: Scheduled Jobs]` rows into validated `ScheduledAuditRule` items."""
    valid_rules, _ = parse_scheduled_rules_with_diagnostics(rows)
    return valid_rules


def _extract_eval_golden_source_id(raw_url: str) -> str:
    text = (raw_url or "").strip()
    if not text or "/file/d/" in text or "/folders/" in text:
        return ""
    from .gcs_gateway import normalize_gcs_target

    try:
        gcs_uri = normalize_gcs_target(text)
    except ValueError:
        gcs_uri = None
    if gcs_uri:
        return gcs_uri
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]{10,})", text)
    if m:
        return m.group(1)
    if _RAW_DRIVE_ID_RE.match(text):
        return text
    return ""


def _is_legacy_tab3_header(header_row: Sequence[str]) -> bool:
    """Returns True if `Tab3_Golden_Eval` row 1 is the legacy 9-column header (`Golden_Sheet_URL` in E1)."""
    if not header_row:
        return False
    col_e = str(header_row[4]).strip() if len(header_row) > 4 else ""
    return col_e in ("Golden_Sheet_URL", "黄金集表格链接")


def migrate_tab3_rows_to_3path_schema(rows: List[List[str]]) -> tuple[bool, List[List[str]]]:
    """Converts legacy 9-column `Tab3_Golden_Eval` rows (`Golden_Sheet_URL`, `Results_Folder_URL`, ...)
    into the 10-column 3-path schema (`Golden_Videos_Folder_URL`, `Human_Labels_Sheet_URL`, `Results_Folder_URL`, ...).

    Returns `(changed, migrated_rows)`.
    """
    if not rows:
        return True, [list(EVAL_WORKBENCH_HEADERS)]

    is_legacy = _is_legacy_tab3_header(rows[0])
    current_hdr = [str(c).strip() for c in rows[0][:10]]
    changed = current_hdr != list(EVAL_WORKBENCH_HEADERS)
    out_rows: List[List[str]] = [list(EVAL_WORKBENCH_HEADERS)]

    for raw_row in rows[1:]:
        if is_legacy:
            padded = [str(c).strip() for c in raw_row] + [""] * max(0, 9 - len(raw_row))
            (
                eval_id,
                eval_name,
                prompt_tab,
                model_ver,
                golden_url,
                results_folder_url,
                sup_email,
                trigger_raw,
                last_report,
            ) = padded[:9]
            if not any(padded[:8]):
                continue
            changed = True
            videos_folder_url = (
                "https://drive.google.com/drive/folders/1ExampleGoldenVideosFolderIdReplaceMe"
                if eval_id == "EVAL-EXAMPLE-01"
                else ""
            )
            if eval_id == "EVAL-EXAMPLE-01" and ("填入黄金集 Sheet 链接" in eval_name or "Golden Dataset" in eval_name):
                eval_name = DEFAULT_TAB3_SAMPLE_ROW[1]
            out_rows.append(
                [
                    eval_id,
                    eval_name,
                    prompt_tab,
                    model_ver,
                    videos_folder_url,
                    golden_url,
                    results_folder_url,
                    sup_email,
                    trigger_raw or "IDLE",
                    last_report,
                ]
            )
        else:
            padded = [str(c).strip() for c in raw_row] + [""] * max(0, 10 - len(raw_row))
            if not any(padded[:9]):
                continue
            out_rows.append(padded[:10])

    return changed, out_rows


def parse_scheduled_eval_rules_with_diagnostics(
    rows: List[List[str]],
    *,
    available_prompt_tabs: Optional[Sequence[str]] = None,
) -> tuple[List[ScheduledEvalRule], List[ScheduledEvalRowError]]:
    """Parses `[Tab 3: Golden Set Evaluation Workbench]` (`Tab3_Golden_Eval` or legacy `Tab3_黄金集评测工作台`) rows into `(valid_rules, row_errors)`.

    Supports both the canonical 10-column 3-path layout (`Golden_Videos_Folder_URL`, `Human_Labels_Sheet_URL`,
    `Results_Folder_URL`) and the legacy 9-column layout (`Golden_Sheet_URL`, `Results_Folder_URL`).
    """
    if not rows or len(rows) < 2:
        return [], []
    parsed: List[ScheduledEvalRule] = []
    errors: List[ScheduledEvalRowError] = []
    seen_eval_ids: dict[str, int] = {}
    prompt_tab_set = (
        {str(t).strip() for t in available_prompt_tabs if str(t).strip()}
        if available_prompt_tabs
        else set()
    )
    is_legacy = _is_legacy_tab3_header(rows[0])

    for idx, raw_row in enumerate(rows[1:], start=2):
        if is_legacy:
            padded = [str(c).strip() for c in raw_row] + [""] * max(0, 9 - len(raw_row))
            (
                eval_id,
                eval_name,
                prompt_tab,
                model_ver,
                human_labels_url,
                results_folder_url,
                sup_email,
                trigger_raw,
                last_report,
            ) = padded[:9]
            golden_videos_url = ""
            if not any(padded[:8]):
                continue
        else:
            padded = [str(c).strip() for c in raw_row] + [""] * max(0, 10 - len(raw_row))
            (
                eval_id,
                eval_name,
                prompt_tab,
                model_ver,
                golden_videos_url,
                human_labels_url,
                results_folder_url,
                sup_email,
                trigger_raw,
                last_report,
            ) = padded[:10]
            if not any(padded[:9]):
                continue

        norm_trigger = (trigger_raw or "IDLE").upper()

        def _fail(msg: str) -> None:
            logger.warning("Skipping Tab3 row %d (%s): %s", idx, eval_id or "<empty>", msg)
            errors.append(
                ScheduledEvalRowError(
                    row_index=idx,
                    eval_id=eval_id,
                    trigger_action=norm_trigger,
                    error_message=msg,
                    previous_status=last_report,
                )
            )

        if not eval_id:
            _fail("❌ Config Error: Eval_ID (Column A) cannot be empty")
            continue

        if eval_id in seen_eval_ids:
            _fail(
                f"❌ Config Error: Eval_ID '{eval_id}' duplicates row {seen_eval_ids[eval_id]}; every Eval_ID must be unique"
            )
            continue
        seen_eval_ids[eval_id] = idx

        trigger_col_label = "Column H" if is_legacy else "Column I"
        if norm_trigger not in VALID_EVAL_TRIGGER_ACTIONS:
            _fail(
                f"❌ Config Error: Trigger_Action ({trigger_col_label}) '{trigger_raw}' is invalid; select IDLE or START_EVAL from dropdown"
            )
            continue

        if not prompt_tab or not prompt_tab.startswith("Prompt_"):
            _fail(
                f"❌ Config Error: Prompt_Tab_Name (Column C) '{prompt_tab}' must select a SOP rules tab starting with Prompt_"
            )
            continue

        alias_tab = PROMPT_TAB_ALIASES.get(prompt_tab, "")
        if prompt_tab_set and prompt_tab not in prompt_tab_set and alias_tab not in prompt_tab_set:
            _fail(
                f"❌ Config Error: Prompt_Tab_Name (Column C) '{prompt_tab}' does not exist in Master Sheet; "
                f"available tabs: {', '.join(sorted(prompt_tab_set))}"
            )
            continue

        if not model_ver or model_ver.upper() == "AUTO_LATEST_FLASH":
            _fail(
                f"❌ Config Error: Model_Version (Column D) '{model_ver}' is invalid; select an explicit Gemini model version (AUTO_LATEST_FLASH is prohibited)"
            )
            continue

        labels_col_name = "Golden_Sheet_URL (Column E)" if is_legacy else "Human_Labels_Sheet_URL (Column F)"
        if "/folders/" in human_labels_url or "/file/d/" in human_labels_url:
            _fail(
                f"❌ Config Error: {labels_col_name} must be a human-labeled Google Sheet link (/spreadsheets/d/...) or gs://.../*.jsonl path, not a folder or video file link"
            )
            continue

        golden_source_id = _extract_eval_golden_source_id(human_labels_url)
        if not golden_source_id:
            _fail(
                f"❌ Config Error: {labels_col_name} '{human_labels_url}' is not a valid Google Sheet link or gs:// path"
            )
            continue

        golden_videos_folder_id = ""
        if not is_legacy:
            if golden_videos_url:
                if "/file/d/" in golden_videos_url or "/spreadsheets/d/" in golden_videos_url:
                    _fail(
                        "❌ Config Error: Golden_Videos_Folder_URL (Column E) must be a Google Drive folder link (/folders/...) containing golden CCTV videos, not a spreadsheet or single video link"
                    )
                    continue
                golden_videos_folder_id = _extract_scheduled_folder_id(golden_videos_url)
                if not golden_videos_folder_id or golden_videos_folder_id.startswith("gs://"):
                    _fail(
                        f"❌ Config Error: Golden_Videos_Folder_URL (Column E) '{golden_videos_url}' is not a valid Google Drive folder link"
                    )
                    continue
            elif not golden_source_id.startswith("gs://"):
                _fail(
                    "❌ Config Error: Golden_Videos_Folder_URL (Column E) cannot be empty; please provide the dedicated Google Drive folder link (/folders/...) containing golden CCTV videos"
                )
                continue

        results_col_name = "Results_Folder_URL (Column F)" if is_legacy else "Results_Folder_URL (Column G)"
        results_folder_id = ""
        if results_folder_url:
            if "/file/d/" in results_folder_url or "/spreadsheets/d/" in results_folder_url:
                _fail(
                    f"❌ Config Error: {results_col_name} must be a Google Drive folder link (/folders/...) for evaluation reports, or left empty to use the default directory"
                )
                continue
            results_folder_id = _extract_scheduled_folder_id(results_folder_url)
            if not results_folder_id or results_folder_id.startswith("gs://"):
                _fail(
                    f"❌ Config Error: {results_col_name} '{results_folder_url}' is not a valid Google Drive folder link"
                )
                continue

        sup_col_name = "Supervisor_Email (Column G)" if is_legacy else "Supervisor_Email (Column H)"
        if sup_email and not _EMAIL_RE.match(sup_email):
            _fail(f"❌ Config Error: {sup_col_name} '{sup_email}' is not a valid email address")
            continue

        parsed.append(
            ScheduledEvalRule(
                row_index=idx,
                eval_id=eval_id,
                eval_name=eval_name or eval_id,
                prompt_tab_name=prompt_tab,
                model_version=model_ver,
                golden_videos_folder_url=golden_videos_url,
                golden_videos_folder_id=golden_videos_folder_id,
                human_labels_sheet_url=human_labels_url,
                golden_sheet_url=human_labels_url,
                golden_source_id=golden_source_id,
                results_folder_url=results_folder_url,
                results_folder_id=results_folder_id,
                supervisor_email=sup_email or "scheduled-auditor@chagee.com",
                trigger_action=norm_trigger,
                last_eval_report=last_report,
            )
        )
    return parsed, errors


def parse_scheduled_eval_rules_from_rows(
    rows: List[List[str]],
    *,
    available_prompt_tabs: Optional[Sequence[str]] = None,
) -> List[ScheduledEvalRule]:
    """Parses `[Tab 3: Golden Set Evaluation Workbench]` rows into validated `ScheduledEvalRule` items."""
    valid_rules, _ = parse_scheduled_eval_rules_with_diagnostics(
        rows, available_prompt_tabs=available_prompt_tabs
    )
    return valid_rules


def _nearest_quarter_minute(mm: int) -> str:
    quarters = (0, 15, 30, 45)
    best = min(quarters, key=lambda q: abs(q - mm))
    return f"{best:02d}"


def _nearest_lookback_days_str(hours_raw: str) -> str:
    try:
        h = float(hours_raw or 24.0)
    except (TypeError, ValueError):
        return "1"
    days = h / 24.0
    options = (0.5, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0)
    best = min(options, key=lambda d: abs(d - days))
    return "0.5" if best == 0.5 else str(int(best))


def migrate_tab2_rows_to_utc_schema(rows: List[List[str]]) -> tuple[bool, List[List[str]]]:
    """Converts legacy `Tab2_定时巡检配置表` rows (`Trigger_Time`, `Timezone`, `Lookback_Hours`)
    into the simplified UTC schema (`Trigger_Hour_UTC`, `Trigger_Minute_UTC`, `Lookback_Days`).

    Returns `(changed, migrated_rows)`.
    """
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo

    if not rows:
        return True, [list(SCHEDULED_RULE_HEADERS)]

    is_legacy = _is_legacy_tab2_header(rows[0])
    current_hdr = [str(c).strip() for c in rows[0][:9]]
    changed = current_hdr != list(SCHEDULED_RULE_HEADERS)
    out_rows: List[List[str]] = [list(SCHEDULED_RULE_HEADERS)]

    for raw_row in rows[1:]:
        padded = [str(c).strip() for c in raw_row] + [""] * max(0, 9 - len(raw_row))
        rule_id, rule_name, root_url, col_d, col_e, col_f, sup_email, enabled_raw, last_status = padded[:9]
        if not any(padded[:8]):
            continue

        if is_legacy or ":" in col_d:
            changed = True
            t_match = _TRIGGER_TIME_RE.match(col_d or "02:00")
            local_hh = int(t_match.group(1)) if t_match else 2
            local_mm = int(t_match.group(2)) if t_match else 0
            tz_name = col_e or "Asia/Singapore"
            try:
                tz = ZoneInfo(tz_name)
                ref_local = datetime(2026, 10, 16, local_hh, local_mm, tzinfo=tz)
                ref_utc = ref_local.astimezone(timezone.utc)
                utc_hh_str = f"{ref_utc.hour:02d}"
                utc_mm_str = _nearest_quarter_minute(ref_utc.minute)
            except Exception:
                utc_hh_str = f"{local_hh:02d}"
                utc_mm_str = _nearest_quarter_minute(local_mm)
            lb_days_str = _nearest_lookback_days_str(col_f)
        else:
            try:
                hh_val = max(0, min(23, int(col_d or "2")))
                utc_hh_str = f"{hh_val:02d}"
            except ValueError:
                utc_hh_str = col_d
            try:
                mm_val = int(col_e or "0")
                utc_mm_str = f"{mm_val:02d}" if f"{mm_val:02d}" in VALID_TRIGGER_MINUTES_UTC else col_e
            except ValueError:
                utc_mm_str = col_e
            lb_days_str = col_f or "1"
            if (utc_hh_str, utc_mm_str, lb_days_str) != (col_d, col_e, col_f):
                changed = True

        norm_enabled = (
            "ENABLED"
            if enabled_raw.upper() in _TRUTHY_ENABLED_TOKENS
            else ("PAUSED" if enabled_raw.upper() in _FALSY_ENABLED_TOKENS else enabled_raw)
        )
        if norm_enabled != enabled_raw:
            changed = True

        out_rows.append(
            [
                rule_id,
                rule_name,
                root_url,
                utc_hh_str,
                utc_mm_str,
                lb_days_str,
                sup_email,
                norm_enabled,
                last_status,
            ]
        )

    return changed, out_rows


def build_master_sheet_validation_requests(
    sheet_meta: dict,
    *,
    prompt_versions: Optional[Sequence[str]] = None,
    model_versions: Optional[Sequence[str]] = None,
) -> list[dict]:
    """Builds Google Sheets API v4 `batchUpdate` requests to enforce strict dropdowns (`ONE_OF_LIST`),
    email validation (`TEXT_IS_EMAIL`), folder URL formula validation (`CUSTOM_FORMULA`), and `@` plain
    text formatting on `Trigger_Hour_UTC` / `Trigger_Minute_UTC` across the Master Config Sheet.
    """
    sheets_info = sheet_meta.get("sheets", []) or []
    title_to_id: dict[str, int] = {}
    discovered_prompt_tabs: list[str] = []
    for s in sheets_info:
        props = s.get("properties") or {}
        title = str(props.get("title") or "")
        sid = props.get("sheetId")
        if title and sid is not None:
            title_to_id[title] = int(sid)
            if title.startswith("Prompt_"):
                discovered_prompt_tabs.append(title)

    effective_prompt_versions = list(prompt_versions or discovered_prompt_tabs or [config.default_prompt_version])
    # `model_versions` is normally `model_dropdown_options(tab0_rows)` (baseline + Tab0 candidate list);
    # the baseline alone is the fallback when the caller has no Tab0 rows.
    effective_models = list(dict.fromkeys(list(model_versions or default_model_dropdown_options())))

    def _one_of_list_rule(options: Sequence[str], input_msg: str) -> dict:
        return {
            "condition": {
                "type": "ONE_OF_LIST",
                "values": [{"userEnteredValue": str(opt)} for opt in options],
            },
            "inputMessage": input_msg,
            "strict": True,
            "showCustomUi": True,
        }

    requests: list[dict] = []

    # 1. Tab 0: Active_Prompt_Version (B2), Active_Model_Version (B3), Fallback_Model_Version (B4)
    tab0_id = next((title_to_id[t] for t in TAB0_ALIASES if t in title_to_id), None)
    if tab0_id is not None:
        if effective_prompt_versions:
            requests.append(
                {
                    "setDataValidation": {
                        "range": {
                            "sheetId": tab0_id,
                            "startRowIndex": 1,
                            "endRowIndex": 2,
                            "startColumnIndex": 1,
                            "endColumnIndex": 2,
                        },
                        "rule": _one_of_list_rule(
                            effective_prompt_versions,
                            "Select an existing Prompt_* rule tab name",
                        ),
                    }
                }
            )
        if effective_models:
            requests.append(
                {
                    "setDataValidation": {
                        "range": {
                            "sheetId": tab0_id,
                            "startRowIndex": 2,
                            "endRowIndex": 4,
                            "startColumnIndex": 1,
                            "endColumnIndex": 2,
                        },
                        "rule": _one_of_list_rule(
                            effective_models,
                            "Select a governed Gemini Flash model version (AUTO_LATEST_FLASH is prohibited)",
                        ),
                    }
                }
            )

    # 2. Prompt_* tabs + Tab4_Spot_Check: severity (D), detection_type (E), requires_full_context (F)
    rule_table_tabs = list(discovered_prompt_tabs)
    for t4 in TAB4_ALIASES:
        if t4 in title_to_id and t4 not in rule_table_tabs:
            rule_table_tabs.append(t4)
    for p_title in rule_table_tabs:
        p_sid = title_to_id[p_title]
        requests.extend(
            [
                {
                    "setDataValidation": {
                        "range": {
                            "sheetId": p_sid,
                            "startRowIndex": 1,
                            "endRowIndex": 100,
                            "startColumnIndex": 3,
                            "endColumnIndex": 4,
                        },
                        "rule": _one_of_list_rule(
                            ("RED_LINE", "NORMAL"),
                            "Select RED_LINE (critical red-line violation) or NORMAL (standard deviation)",
                        ),
                    }
                },
                {
                    "setDataValidation": {
                        "range": {
                            "sheetId": p_sid,
                            "startRowIndex": 1,
                            "endRowIndex": 100,
                            "startColumnIndex": 4,
                            "endColumnIndex": 5,
                        },
                        "rule": _one_of_list_rule(
                            ("presence", "absence"),
                            "Select presence (triggered when present) or absence (triggered when absent)",
                        ),
                    }
                },
                {
                    "setDataValidation": {
                        "range": {
                            "sheetId": p_sid,
                            "startRowIndex": 1,
                            "endRowIndex": 100,
                            "startColumnIndex": 5,
                            "endColumnIndex": 6,
                        },
                        "rule": _one_of_list_rule(
                            ("TRUE", "FALSE"),
                            "Select TRUE or FALSE",
                        ),
                    }
                },
            ]
        )

    # 3. Tab 2 (`Tab2_Scheduled_Jobs`):
    #    - Col C (index 2): Root_Folder_URL custom formula validation (rejects /file/d/)
    #    - Col D..E (index 3..5): Plain text `@` format so "00"-"09" keep leading zeros
    #    - Col D (index 3): Trigger_Hour_UTC dropdown (00..23)
    #    - Col E (index 4): Trigger_Minute_UTC dropdown (00, 15, 30, 45)
    #    - Col F (index 5): Lookback_Days dropdown (0.5, 1..7)
    #    - Col G (index 6): Supervisor_Email TEXT_IS_EMAIL
    #    - Col H (index 7): Enabled dropdown (ENABLED, PAUSED)
    tab2_id = next((title_to_id[t] for t in TAB2_ALIASES if t in title_to_id), None)
    if tab2_id is not None:
        requests.extend(
            [
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": tab2_id,
                            "startRowIndex": 1,
                            "endRowIndex": 200,
                            "startColumnIndex": 3,
                            "endColumnIndex": 5,
                        },
                        "cell": {
                            "userEnteredFormat": {
                                "numberFormat": {"type": "TEXT", "pattern": "@"}
                            }
                        },
                        "fields": "userEnteredFormat.numberFormat",
                    }
                },
                {
                    "setDataValidation": {
                        "range": {
                            "sheetId": tab2_id,
                            "startRowIndex": 1,
                            "endRowIndex": 200,
                            "startColumnIndex": 2,
                            "endColumnIndex": 3,
                        },
                        "rule": {
                            "condition": {
                                "type": "CUSTOM_FORMULA",
                                "values": [
                                    {
                                        "userEnteredValue": (
                                            '=AND(NOT(REGEXMATCH(TO_TEXT(C2), "/file/d/")), '
                                            'REGEXMATCH(TO_TEXT(C2), "^(https://drive\\.google\\.com/.*folders/[a-zA-Z0-9_-]{10,}|gs://[a-z0-9._-]+.*|[a-zA-Z0-9_-]{10,})$"))'
                                        )
                                    }
                                ],
                            },
                            "inputMessage": "Enter a Google Drive root folder link (containing /folders/...) or gs:// bucket path; single video file links (/file/d/) are not allowed",
                            "strict": True,
                            "showCustomUi": False,
                        },
                    }
                },
                {
                    "setDataValidation": {
                        "range": {
                            "sheetId": tab2_id,
                            "startRowIndex": 1,
                            "endRowIndex": 200,
                            "startColumnIndex": 3,
                            "endColumnIndex": 4,
                        },
                        "rule": _one_of_list_rule(
                            VALID_TRIGGER_HOURS_UTC,
                            "Select daily trigger hour (UTC, 00-23)",
                        ),
                    }
                },
                {
                    "setDataValidation": {
                        "range": {
                            "sheetId": tab2_id,
                            "startRowIndex": 1,
                            "endRowIndex": 200,
                            "startColumnIndex": 4,
                            "endColumnIndex": 5,
                        },
                        "rule": _one_of_list_rule(
                            VALID_TRIGGER_MINUTES_UTC,
                            "Select trigger minute (UTC, only 00, 15, 30, 45 supported)",
                        ),
                    }
                },
                {
                    "setDataValidation": {
                        "range": {
                            "sheetId": tab2_id,
                            "startRowIndex": 1,
                            "endRowIndex": 200,
                            "startColumnIndex": 5,
                            "endColumnIndex": 6,
                        },
                        "rule": _one_of_list_rule(
                            VALID_LOOKBACK_DAYS_OPTIONS,
                            "Select lookback days (0.5 days = 12 hours, or 1-7 days)",
                        ),
                    }
                },
                {
                    "setDataValidation": {
                        "range": {
                            "sheetId": tab2_id,
                            "startRowIndex": 1,
                            "endRowIndex": 200,
                            "startColumnIndex": 6,
                            "endColumnIndex": 7,
                        },
                        "rule": {
                            "condition": {"type": "TEXT_IS_EMAIL"},
                            "inputMessage": "Enter a valid supervisor email address (e.g. user@domain.com)",
                            "strict": True,
                            "showCustomUi": False,
                        },
                    }
                },
                {
                    "setDataValidation": {
                        "range": {
                            "sheetId": tab2_id,
                            "startRowIndex": 1,
                            "endRowIndex": 200,
                            "startColumnIndex": 7,
                            "endColumnIndex": 8,
                        },
                        "rule": _one_of_list_rule(
                            VALID_ENABLED_DROPDOWN_OPTIONS,
                            "Select ENABLED or PAUSED",
                        ),
                    }
                },
            ]
        )

    # 4. Tab 3 (`Tab3_Golden_Eval`, 10 columns A:J):
    #    - Col C (index 2): Prompt_Tab_Name dropdown (all Prompt_* tabs)
    #    - Col D (index 3): Model_Version dropdown
    #    - Col E..G (index 4..7): Clear any stale validation on URL columns
    #    - Col H (index 7): Supervisor_Email TEXT_IS_EMAIL
    #    - Col I (index 8): Trigger_Action dropdown (IDLE, START_EVAL, RUNNING, DONE, FAILED)
    tab3_id = next((title_to_id[t] for t in TAB3_ALIASES if t in title_to_id), None)
    if tab3_id is not None:
        requests.append(
            {
                "setDataValidation": {
                    "range": {
                        "sheetId": tab3_id,
                        "startRowIndex": 1,
                        "endRowIndex": 200,
                        "startColumnIndex": 4,
                        "endColumnIndex": 7,
                    },
                }
            }
        )
        if effective_prompt_versions:
            requests.append(
                {
                    "setDataValidation": {
                        "range": {
                            "sheetId": tab3_id,
                            "startRowIndex": 1,
                            "endRowIndex": 200,
                            "startColumnIndex": 2,
                            "endColumnIndex": 3,
                        },
                        "rule": _one_of_list_rule(
                            effective_prompt_versions,
                            "Select the Prompt_* rule tab name to evaluate",
                        ),
                    }
                }
            )
        if effective_models:
            requests.append(
                {
                    "setDataValidation": {
                        "range": {
                            "sheetId": tab3_id,
                            "startRowIndex": 1,
                            "endRowIndex": 200,
                            "startColumnIndex": 3,
                            "endColumnIndex": 4,
                        },
                        "rule": _one_of_list_rule(
                            effective_models,
                            "Select the governed Gemini model version to evaluate (AUTO_LATEST_FLASH is prohibited)",
                        ),
                    }
                }
            )
        requests.extend(
            [
                {
                    "setDataValidation": {
                        "range": {
                            "sheetId": tab3_id,
                            "startRowIndex": 1,
                            "endRowIndex": 200,
                            "startColumnIndex": 7,
                            "endColumnIndex": 8,
                        },
                        "rule": {
                            "condition": {"type": "TEXT_IS_EMAIL"},
                            "inputMessage": "Enter a valid supervisor email address (e.g. user@domain.com)",
                            "strict": True,
                            "showCustomUi": False,
                        },
                    }
                },
                {
                    "setDataValidation": {
                        "range": {
                            "sheetId": tab3_id,
                            "startRowIndex": 1,
                            "endRowIndex": 200,
                            "startColumnIndex": 8,
                            "endColumnIndex": 9,
                        },
                        "rule": _one_of_list_rule(
                            VALID_EVAL_TRIGGER_ACTIONS,
                            "Select START_EVAL to automatically start golden evaluation on the next scheduled tick (within 15 minutes); status transitions automatically to RUNNING -> DONE/FAILED",
                        ),
                    }
                },
            ]
        )

    return requests


class SheetClientProtocol(Protocol):
    """Protocol for reading and updating the Master Prompt & Model Config Google Sheet."""

    async def read_tab0_pointers(self, sheet_id: str) -> dict[str, str]: ...
    async def read_prompt_tab_rules(self, sheet_id: str, tab_name: str) -> List[SopRuleItem]: ...
    async def append_available_models(self, sheet_id: str, new_models: List[str]) -> None: ...


class GoogleSheetsConfigClient:
    """Production client for the Master Prompt & Model Config Sheet named by `MASTER_PROMPT_SHEET_ID`.

    Talks to Sheets API v4 with the same token-free Workspace identity as every Drive call
    (`gcp.workspace_credentials`: keyless DWD bot, or the runtime SA). No local CLI, no Cloudtop
    binary, no stored token -- identical behaviour on Cloud Run, ReasoningEngine and a laptop.
    """

    TAB0_TITLE = TAB0_TITLE
    TAB2_SCHEDULED_TITLE = TAB2_SCHEDULED_TITLE
    TAB3_EVAL_TITLE = TAB3_EVAL_TITLE
    TAB4_SPOT_CHECK_TITLE = TAB4_SPOT_CHECK_TITLE

    def __init__(self, gcs_gateway: Optional[object] = None) -> None:
        # Zero-GWS: `gs://` sheet IDs are read from GCS (`GcsStorageGateway.download_object_bytes`).
        self._gcs_gateway = gcs_gateway

    def _gcs(self):
        if self._gcs_gateway is None:
            from .gcs_gateway import GcsStorageGateway

            self._gcs_gateway = GcsStorageGateway()
        return self._gcs_gateway

    async def _read_gcs_workbook_tabs(self, sheet_id: str) -> dict[str, list[list[str]]]:
        """All tabs of a GCS `.xlsx` / `.json` SOP workbook (TTL-cached per URI)."""
        hit = _GCS_WORKBOOK_CACHE.get(sheet_id)
        if hit is not None and time.monotonic() - hit[0] < _GCS_WORKBOOK_CACHE_TTL_SEC:
            return hit[1]
        from .gcs_gateway import load_gcs_sop_tabs

        raw = await asyncio.to_thread(self._gcs().download_object_bytes, sheet_id)
        tabs = _populate_workbook_tab_aliases(load_gcs_sop_tabs(raw, sheet_id))
        _GCS_WORKBOOK_CACHE[sheet_id] = (time.monotonic(), tabs)
        return tabs

    @staticmethod
    def _sheets_service():
        from googleapiclient.discovery import build

        from .gcp import workspace_credentials

        return build("sheets", "v4", credentials=workspace_credentials(), cache_discovery=False)

    async def _read_range(self, sheet_id: str, a1_range: str) -> List[List[str]]:
        cache_key = f"{sheet_id}:{a1_range}"
        if cache_key in _SHEET_RANGE_CACHE:
            ts, val = _SHEET_RANGE_CACHE[cache_key]
            if time.monotonic() - ts < _SHEET_RANGE_CACHE_TTL_SEC:
                logger.debug("Hit local TTL cache for %s", cache_key)
                return [row[:] for row in val]

        candidate_ranges = [a1_range]
        if "!" in a1_range:
            raw_tab, cell_part = a1_range.split("!", 1)
            stripped_tab = raw_tab.strip("'")
            for alt_tab in _alias_candidates_for_tab(stripped_tab):
                if alt_tab != stripped_tab:
                    alt_a1 = f"'{alt_tab}'!{cell_part}" if raw_tab.startswith("'") else f"{alt_tab}!{cell_part}"
                    if alt_a1 not in candidate_ranges:
                        candidate_ranges.append(alt_a1)

        def _fetch_via_api() -> List[List[str]]:
            svc = self._sheets_service()
            last_exc: Optional[BaseException] = None
            for idx, cand_range in enumerate(candidate_ranges):
                try:
                    resp = (
                        svc.spreadsheets()
                        .values()
                        .get(spreadsheetId=sheet_id, range=cand_range)
                        .execute(num_retries=3)
                    )
                    return [[str(c) for c in r] for r in resp.get("values", [])]
                except Exception as exc:
                    last_exc = exc
                    if idx + 1 < len(candidate_ranges) and self._http_status_of(exc) == 400:
                        continue
                    raise
            if last_exc is not None:
                raise last_exc
            return []

        rows = await asyncio.to_thread(_fetch_via_api)
        _SHEET_RANGE_CACHE[cache_key] = (time.monotonic(), rows)
        return rows

    async def read_tab0_pointers(self, sheet_id: str) -> dict[str, str]:
        """Returns Tab 0 pointers verbatim. Read-only on purpose (REQ-013).

        The runtime never rewrites the customer's Active/Fallback pointer cells, even if the
        Workspace identity happens to be an Editor on the SOP Sheet. A stale or retired model
        name is handled by `PromptManager.resolve_and_probe_model`, which probes it and falls
        back to the configured fallback model with a warning shown to the supervisor.
        """
        if sheet_id.startswith("gs://"):
            tabs = await self._read_gcs_workbook_tabs(sheet_id)
            tab0_key = next((t for t in _alias_candidates_for_tab(self.TAB0_TITLE) if t in tabs), None)
            if tab0_key is None:
                raise ValueError(f"SOP workbook {sheet_id} has no tab {self.TAB0_TITLE!r}")
            rows = tabs[tab0_key]
        else:
            rows = await self._read_range(sheet_id, f"{self.TAB0_TITLE}!A1:D15")
        pointers: dict[str, str] = {}
        for row in rows[1:]:
            if len(row) >= 2 and row[0].strip():
                pointers[row[0].strip()] = row[1].strip()
        return pointers

    async def read_prompt_tab_rules(self, sheet_id: str, tab_name: str) -> List[SopRuleItem]:
        if sheet_id.startswith("gs://"):
            tabs = await self._read_gcs_workbook_tabs(sheet_id)
            rows: list[list[str]] = []
            for cand in _alias_candidates_for_tab(tab_name):
                if cand in tabs:
                    rows = tabs[cand]
                    break
        else:
            rows = await self._read_range(sheet_id, f"{tab_name}!A1:I100")
        return parse_rules_from_sheet_rows(rows)

    async def append_available_models(self, sheet_id: str, new_models: List[str]) -> None:
        """Appends newly discovered models to Tab 0's candidate row; never switches the active model."""
        if not new_models or sheet_id.startswith("gs://"):
            return  # a GCS workbook is customer-owned and read-only for the runtime
        catalog_note = " | ".join(f"{m} (🆕 New model available for testing)" for m in new_models)

        def _append() -> None:
            svc = self._sheets_service()
            candidates = _alias_candidates_for_tab(self.TAB0_TITLE)
            for idx, tab0_name in enumerate(candidates):
                try:
                    svc.spreadsheets().values().append(
                        spreadsheetId=sheet_id,
                        range=f"'{tab0_name}'!A1",
                        valueInputOption="RAW",
                        insertDataOption="INSERT_ROWS",
                        body={
                            "values": [
                                [
                                    "Candidate_Models_Discovered",
                                    catalog_note,
                                    "Appended to candidate list only (Active_Model_Version not switched automatically)",
                                ]
                            ]
                        },
                    ).execute(num_retries=3)
                    return
                except Exception as exc:
                    if idx + 1 < len(candidates) and self._http_status_of(exc) == 400:
                        continue
                    raise

        try:
            await asyncio.to_thread(_append)
        except Exception as exc:
            logger.warning("Failed to append candidate models to %s: %s", sheet_id, exc)

    async def read_scheduled_rules_with_diagnostics(
        self, sheet_id: str
    ) -> tuple[List[ScheduledAuditRule], List[ScheduledRuleRowError]]:
        """Reads `[Tab 2: Scheduled Inspection Jobs]` (`Tab2_Scheduled_Jobs`) rows and returns `(valid_rules, row_errors)`."""
        if not sheet_id:
            return [], []
        if sheet_id.startswith("gs://"):
            tabs = await self._read_gcs_workbook_tabs(sheet_id)
            rows = tabs.get(self.TAB2_SCHEDULED_TITLE, [])
            return parse_scheduled_rules_with_diagnostics(rows)
        try:
            a1 = f"{self.TAB2_SCHEDULED_TITLE}!A1:I200"
            _SHEET_RANGE_CACHE.pop(f"{sheet_id}:{a1}", None)
            rows = await self._read_range(sheet_id, a1)
            return parse_scheduled_rules_with_diagnostics(rows)
        except Exception as exc:
            logger.info(
                "Scheduled rules tab %s not present or unreadable on %s: %s",
                self.TAB2_SCHEDULED_TITLE,
                sheet_id,
                exc,
            )
            return [], []

    async def read_scheduled_rules(self, sheet_id: str) -> List[ScheduledAuditRule]:
        """Reads `[Tab 2: Scheduled Inspection Jobs]` (`Tab2_Scheduled_Jobs`) rows; returns `[]` if the tab does not exist yet."""
        valid_rules, _ = await self.read_scheduled_rules_with_diagnostics(sheet_id)
        return valid_rules

    async def read_scheduled_eval_rules_with_diagnostics(
        self, sheet_id: str
    ) -> tuple[List[ScheduledEvalRule], List[ScheduledEvalRowError]]:
        """Reads `[Tab 3: Golden Set Evaluation Workbench]` (`Tab3_Golden_Eval`) rows and returns `(valid_rules, row_errors)`."""
        if not sheet_id:
            return [], []
        if sheet_id.startswith("gs://"):
            tabs = await self._read_gcs_workbook_tabs(sheet_id)
            rows = tabs.get(self.TAB3_EVAL_TITLE, [])
            prompt_tabs = [t for t in tabs.keys() if t.startswith("Prompt_")]
            return parse_scheduled_eval_rules_with_diagnostics(
                rows, available_prompt_tabs=prompt_tabs
            )
        try:
            a1 = f"{self.TAB3_EVAL_TITLE}!A1:J200"
            _SHEET_RANGE_CACHE.pop(f"{sheet_id}:{a1}", None)
            rows = await self._read_range(sheet_id, a1)
            return parse_scheduled_eval_rules_with_diagnostics(rows)
        except Exception as exc:
            logger.info(
                "Scheduled eval tab %s not present or unreadable on %s: %s",
                self.TAB3_EVAL_TITLE,
                sheet_id,
                exc,
            )
            return [], []

    async def read_scheduled_eval_rules(self, sheet_id: str) -> List[ScheduledEvalRule]:
        """Reads `[Tab 3: Golden Set Evaluation Workbench]` (`Tab3_Golden_Eval`) rows; returns `[]` if the tab does not exist yet."""
        valid_rules, _ = await self.read_scheduled_eval_rules_with_diagnostics(sheet_id)
        return valid_rules

    async def update_scheduled_eval_row(
        self,
        sheet_id: str,
        row_index: int,
        *,
        trigger_action: Optional[str] = None,
        last_eval_report: Optional[str] = None,
    ) -> None:
        """Writes `trigger_action` (Col I) and/or `last_eval_report` (Col J) back to `Tab3_Golden_Eval` at `row_index`."""
        if not sheet_id or sheet_id.startswith("gs://") or row_index < 2:
            return
        if trigger_action is None and last_eval_report is None:
            return

        def _update() -> None:
            svc = self._sheets_service().spreadsheets().values()
            candidates = _alias_candidates_for_tab(self.TAB3_EVAL_TITLE)
            for idx, tab3_name in enumerate(candidates):
                try:
                    if trigger_action is not None and last_eval_report is not None:
                        svc.update(
                            spreadsheetId=sheet_id,
                            range=f"'{tab3_name}'!I{row_index}:J{row_index}",
                            valueInputOption="USER_ENTERED",
                            body={"values": [[trigger_action, last_eval_report]]},
                        ).execute(num_retries=3)
                    elif trigger_action is not None:
                        svc.update(
                            spreadsheetId=sheet_id,
                            range=f"'{tab3_name}'!I{row_index}",
                            valueInputOption="USER_ENTERED",
                            body={"values": [[trigger_action]]},
                        ).execute(num_retries=3)
                    else:
                        svc.update(
                            spreadsheetId=sheet_id,
                            range=f"'{tab3_name}'!J{row_index}",
                            valueInputOption="USER_ENTERED",
                            body={"values": [[last_eval_report]]},
                        ).execute(num_retries=3)
                    return
                except Exception as exc:
                    if idx + 1 < len(candidates) and self._http_status_of(exc) == 400:
                        continue
                    raise

        try:
            await asyncio.to_thread(_update)
            _SHEET_RANGE_CACHE.pop(f"{sheet_id}:{self.TAB3_EVAL_TITLE}!A1:J200", None)
        except Exception as exc:
            logger.warning(
                "Failed to update Tab3 row %d on %s: %s",
                row_index,
                sheet_id,
                exc,
            )

    @staticmethod
    def _http_status_of(exc: BaseException) -> Optional[int]:
        """HTTP status of a googleapiclient `HttpError` (`None` for anything else)."""
        status = getattr(getattr(exc, "resp", None), "status", None)
        try:
            return int(status) if status is not None else None
        except (TypeError, ValueError):
            return None

    def _read_model_dropdown_options_sync(self, svc, sheet_id: str) -> tuple[str, ...]:
        """Model dropdown options for `sheet_id`: the baseline plus every model Tab0 names
        (`model_dropdown_options`), from one uncached `values.get` so operator edits show up on
        the next tick.

        A missing Tab0 (HTTP 400 `Unable to parse range`) yields the baseline only. Any other
        failure (403 / 429 / 5xx / network) is raised so the caller leaves the sheet and the memo
        untouched this tick, instead of shrinking the strict dropdowns to the baseline and
        flagging every Tab3 row that already names a Tab0 candidate.
        """
        candidates = _alias_candidates_for_tab(self.TAB0_TITLE)
        resp = None
        for idx, tab0_name in enumerate(candidates):
            try:
                resp = (
                    svc.spreadsheets()
                    .values()
                    .get(
                        spreadsheetId=sheet_id,
                        range=f"'{tab0_name}'!{TAB0_MODEL_CANDIDATES_RANGE}",
                    )
                    .execute(num_retries=3)
                )
                break
            except Exception as exc:
                if self._http_status_of(exc) == 400:
                    if idx + 1 < len(candidates):
                        continue
                    logger.warning(
                        "Tab %r of %s is not readable (%s); model dropdowns keep the baseline list only",
                        self.TAB0_TITLE,
                        sheet_id,
                        exc,
                    )
                    return tuple(default_model_dropdown_options())
                raise
        rows = [[str(c) for c in r] for r in ((resp or {}).get("values") or [])]
        return tuple(model_dropdown_options(rows))

    async def ensure_master_sheet_validations(
        self, sheet_id: str, *, force: bool = False
    ) -> bool:
        """Idempotently migrates legacy `Tab2_Scheduled_Jobs` rows to the UTC dropdown schema,
        migrates or auto-creates `Tab3_Golden_Eval` (10-column 3-path schema), and applies native
        Google Sheets `setDataValidation` rules across Tab 0, Prompt_*, Tab 2, and Tab 3.

        The model dropdowns (Tab0 B3:B4, Tab3 column D) list the baseline models plus every model
        named in Tab0 (`collect_model_candidates_from_tab0`). Tab0 is re-read on every call; the
        whole-sheet work only runs when that list differs from the one last applied to `sheet_id`
        (`_VALID_SHEET_CACHE`) or `force=True`.
        """
        if not sheet_id or sheet_id.startswith("gs://"):
            return False

        def _migrate_and_validate_sync() -> tuple[tuple[str, ...], bool]:
            svc = self._sheets_service()
            models = self._read_model_dropdown_options_sync(svc, sheet_id)
            if not force and _VALID_SHEET_CACHE.get(sheet_id) == models:
                return models, False
            meta = svc.spreadsheets().get(spreadsheetId=sheet_id).execute(num_retries=3)
            tab_titles = {
                str((s.get("properties") or {}).get("title") or "")
                for s in (meta.get("sheets") or [])
            }
            tab2_existing = next((t for t in TAB2_ALIASES if t in tab_titles), None)
            if tab2_existing is not None:
                resp = (
                    svc.spreadsheets()
                    .values()
                    .get(spreadsheetId=sheet_id, range=f"'{tab2_existing}'!A1:I200")
                    .execute(num_retries=3)
                )
                raw_rows = [[str(c) for c in r] for r in resp.get("values", [])]
                changed, migrated_rows = migrate_tab2_rows_to_utc_schema(raw_rows)
                if changed and migrated_rows:
                    svc.spreadsheets().values().clear(
                        spreadsheetId=sheet_id,
                        range=f"'{tab2_existing}'!A1:I200",
                        body={},
                    ).execute(num_retries=3)
                    svc.spreadsheets().values().update(
                        spreadsheetId=sheet_id,
                        range=f"'{tab2_existing}'!A1",
                        valueInputOption="RAW",
                        body={"values": migrated_rows},
                    ).execute(num_retries=3)

            tab3_existing = next((t for t in TAB3_ALIASES if t in tab_titles), None)
            if tab3_existing is None:
                svc.spreadsheets().batchUpdate(
                    spreadsheetId=sheet_id,
                    body={
                        "requests": [
                            {
                                "addSheet": {
                                    "properties": {
                                        "title": self.TAB3_EVAL_TITLE,
                                        "gridProperties": {"frozenRowCount": 1},
                                    }
                                }
                            }
                        ]
                    },
                ).execute(num_retries=3)
                svc.spreadsheets().values().update(
                    spreadsheetId=sheet_id,
                    range=f"'{self.TAB3_EVAL_TITLE}'!A1",
                    valueInputOption="RAW",
                    body={
                        "values": [
                            list(EVAL_WORKBENCH_HEADERS),
                            list(DEFAULT_TAB3_SAMPLE_ROW),
                        ]
                    },
                ).execute(num_retries=3)
                meta = svc.spreadsheets().get(spreadsheetId=sheet_id).execute(num_retries=3)
            else:
                resp_t3 = (
                    svc.spreadsheets()
                    .values()
                    .get(spreadsheetId=sheet_id, range=f"'{tab3_existing}'!A1:J200")
                    .execute(num_retries=3)
                )
                raw_t3 = [[str(c) for c in r] for r in resp_t3.get("values", [])]
                changed_t3, migrated_t3 = migrate_tab3_rows_to_3path_schema(raw_t3)
                if changed_t3 and migrated_t3:
                    svc.spreadsheets().values().clear(
                        spreadsheetId=sheet_id,
                        range=f"'{tab3_existing}'!A1:J200",
                        body={},
                    ).execute(num_retries=3)
                    svc.spreadsheets().values().update(
                        spreadsheetId=sheet_id,
                        range=f"'{tab3_existing}'!A1",
                        valueInputOption="RAW",
                        body={"values": migrated_t3},
                    ).execute(num_retries=3)

            tab4_existing = next((t for t in TAB4_ALIASES if t in tab_titles), None)
            if tab4_existing is None:
                svc.spreadsheets().batchUpdate(
                    spreadsheetId=sheet_id,
                    body={
                        "requests": [
                            {
                                "addSheet": {
                                    "properties": {
                                        "title": self.TAB4_SPOT_CHECK_TITLE,
                                        "gridProperties": {"frozenRowCount": 1},
                                    }
                                }
                            }
                        ]
                    },
                ).execute(num_retries=3)
                svc.spreadsheets().values().update(
                    spreadsheetId=sheet_id,
                    range=f"'{self.TAB4_SPOT_CHECK_TITLE}'!A1",
                    valueInputOption="RAW",
                    body={
                        "values": [
                            list(SHEET_RULE_HEADER),
                            list(DEFAULT_TAB4_SAMPLE_ROW),
                        ]
                    },
                ).execute(num_retries=3)
                meta = svc.spreadsheets().get(spreadsheetId=sheet_id).execute(num_retries=3)

            reqs = build_master_sheet_validation_requests(meta, model_versions=list(models))
            if reqs:
                svc.spreadsheets().batchUpdate(
                    spreadsheetId=sheet_id,
                    body={"requests": reqs},
                ).execute(num_retries=3)
            logger.info(
                "Applied master sheet validations on %s; model dropdown options: %s",
                sheet_id,
                ", ".join(models),
            )
            return models, True

        try:
            models, applied = await asyncio.to_thread(_migrate_and_validate_sync)
            _VALID_SHEET_CACHE[sheet_id] = models
            if applied:
                _SHEET_RANGE_CACHE.pop(f"{sheet_id}:{self.TAB2_SCHEDULED_TITLE}!A1:I200", None)
                _SHEET_RANGE_CACHE.pop(f"{sheet_id}:{self.TAB3_EVAL_TITLE}!A1:J200", None)
                _SHEET_RANGE_CACHE.pop(f"{sheet_id}:{self.TAB4_SPOT_CHECK_TITLE}!A1:I100", None)
            return True
        except Exception as exc:
            logger.warning(
                "Could not apply master sheet validations on %s: %s", sheet_id, exc
            )
            return False

    async def update_scheduled_rule_status(
        self, sheet_id: str, row_index: int, status_text: str
    ) -> None:
        """Writes `status_text` back to column I (`Last_Run_Status`) of `Tab2_Scheduled_Jobs` at `row_index`."""
        if not sheet_id or sheet_id.startswith("gs://") or row_index < 2:
            return

        def _update() -> None:
            svc = self._sheets_service().spreadsheets().values()
            candidates = _alias_candidates_for_tab(self.TAB2_SCHEDULED_TITLE)
            for idx, tab2_name in enumerate(candidates):
                try:
                    svc.update(
                        spreadsheetId=sheet_id,
                        range=f"'{tab2_name}'!I{row_index}",
                        valueInputOption="USER_ENTERED",
                        body={"values": [[status_text]]},
                    ).execute(num_retries=3)
                    return
                except Exception as exc:
                    if idx + 1 < len(candidates) and self._http_status_of(exc) == 400:
                        continue
                    raise

        try:
            await asyncio.to_thread(_update)
            _SHEET_RANGE_CACHE.pop(f"{sheet_id}:{self.TAB2_SCHEDULED_TITLE}!A1:I200", None)
        except Exception as exc:
            logger.warning(
                "Failed to update Tab2 Last_Run_Status at row %d on %s: %s",
                row_index,
                sheet_id,
                exc,
            )


class PromptManager:
    """Loads active Prompt & Model versions from Google Sheets with 0.1s rollback."""

    def __init__(
        self,
        sheet_client: Optional[SheetClientProtocol] = None,
        known_valid_models: Optional[set[str]] = None,
        yaml_path: Optional[Path] = None,
    ) -> None:
        self._sheet_client: Optional[SheetClientProtocol] = (
            sheet_client if sheet_client is not None else GoogleSheetsConfigClient()
        )
        self._yaml_path = yaml_path
        # Models allowed to skip the live probe. Seeded from config (FALLBACK_MODEL_VERSION, set by
        # IaC) instead of a hardcoded version list; anything else is probed once, then remembered.
        self._known_valid_models: set[str] = (
            set(known_valid_models) if known_valid_models else {config.fallback_model_version}
        )
        self.last_scheduled_rule_errors: List[ScheduledRuleRowError] = []
        self.last_scheduled_eval_errors: List[ScheduledEvalRowError] = []

    async def _probe_single_model(self, candidate: str) -> bool:
        if not candidate:
            return False
        if candidate in self._known_valid_models:
            return True
        client = await get_genai_client()
        await asyncio.wait_for(
            asyncio.to_thread(client.models.generate_content, model=candidate, contents="1"),
            timeout=5.0,
        )
        self._known_valid_models.add(candidate)
        return True

    async def resolve_and_probe_model(
        self, requested_model: str, fallback_model: str
    ) -> tuple[str, Optional[str]]:
        """Verifies `requested_model` is valid and cascades to verified `fallback_model` or `config.fallback_model_version`."""
        cleaned = (requested_model or "").strip()
        effective_fallback = (fallback_model or "").strip() or config.fallback_model_version
        if not cleaned:
            return (
                effective_fallback,
                "⚠️ [Tab 0] Model version not specified; automatically using fallback model "
                + effective_fallback,
            )

        if cleaned.upper() == "AUTO_LATEST_FLASH":
            warning = (
                f"⚠️ Blocked AUTO_LATEST_FLASH blind auto-switch mode (prevents silent production accuracy drift); "
                f"locked to governed fallback model {effective_fallback}"
            )
            logger.warning(warning)
            return effective_fallback, warning

        try:
            await self._probe_single_model(cleaned)
            return cleaned, None
        except Exception as exc:
            # Also verify the Sheet-supplied fallback_model so a stale Sheet Fallback_Model_Version
            # (e.g. retired gemini-1.5-flash-002 in asia-southeast1) never slips through unprobed.
            chosen_fallback = effective_fallback
            if chosen_fallback == cleaned:
                chosen_fallback = config.fallback_model_version
            else:
                try:
                    await self._probe_single_model(chosen_fallback)
                except Exception:
                    chosen_fallback = config.fallback_model_version
            warning = (
                f"⚠️ Configured model '{cleaned}' failed health probe ({exc.__class__.__name__}); "
                f"automatically fell back to fallback model {chosen_fallback}"
            )
            logger.warning(warning)
            return chosen_fallback, warning

    async def load_active_config(self, sheet_id: Optional[str] = None) -> PromptModelConfig:
        """Reads `[Tab 0]` pointers (`Active_Prompt_Version` & `Active_Model_Version`) in real time."""
        # `None` => use the Terraform/Cloud Run-injected default sheet.
        # `""`   => caller deliberately opted out of Sheets; stay on the bundled baseline.
        if sheet_id is None:
            if config.master_prompt_sheet_id:
                target_sheet_id = config.master_prompt_sheet_id
            else:
                # Zero-GWS deployments (main.tf master_prompt_sheet_id = "") have no Sheet at all.
                logger.info(
                    "MASTER_PROMPT_SHEET_ID not configured (Zero-GWS mode); using built-in V25 baseline rule library directly."
                )
                target_sheet_id = ""
        elif sheet_id.strip():
            target_sheet_id = extract_spreadsheet_id(sheet_id)
        else:
            target_sheet_id = ""
        active_prompt_ver = config.default_prompt_version
        requested_model_ver = config.fallback_model_version
        fallback_model_ver = config.fallback_model_version
        scan_targets, rules = await asyncio.to_thread(load_rules_from_yaml, self._yaml_path)

        if target_sheet_id and self._sheet_client is not None:
            try:
                pointers = await asyncio.wait_for(
                    self._sheet_client.read_tab0_pointers(target_sheet_id),
                    timeout=10.0,
                )
                active_prompt_ver = pointers.get("Active_Prompt_Version") or active_prompt_ver
                requested_model_ver = pointers.get("Active_Model_Version") or requested_model_ver
                fallback_model_ver = pointers.get("Fallback_Model_Version") or fallback_model_ver

                tab_rules = await asyncio.wait_for(
                    self._sheet_client.read_prompt_tab_rules(target_sheet_id, active_prompt_ver),
                    timeout=10.0,
                )
                if tab_rules:
                    rules = tab_rules
            except Exception as exc:
                logger.error(
                    "Failed to read Master Prompt Sheet %s (%s); using full V10/V25 verbatim baseline.",
                    target_sheet_id,
                    exc,
                )

        resolved_model, model_warning = await self.resolve_and_probe_model(
            requested_model_ver, fallback_model_ver
        )
        sys_instruction = render_v25_system_instruction(
            active_prompt_ver, rules, visual_scan_targets=scan_targets
        )

        return PromptModelConfig(
            active_prompt_version=active_prompt_ver,
            active_model_version=resolved_model,
            fallback_model_version=fallback_model_ver,
            model_fallback_warning=model_warning,
            visual_scan_targets=scan_targets,
            rules=rules,
            system_instruction=sys_instruction,
        )

    async def sync_available_models_to_catalog(
        self,
        sheet_id: str,
        existing_catalog: List[str],
        discovered_models: Optional[List[str]] = None,
    ) -> List[str]:
        """Scans available Gemini Flash models and appends ONLY to candidate catalog."""
        if discovered_models is None:
            try:
                client = await get_genai_client()
                raw_models = await asyncio.wait_for(
                    asyncio.to_thread(lambda: list(client.models.list())),
                    timeout=10.0,
                )
                discovered_models = [
                    m.name.split("/")[-1]
                    for m in raw_models
                    if m.name and "gemini" in m.name.lower() and "flash" in m.name.lower()
                ]
            except Exception as exc:
                logger.warning("Could not list models from API: %s", exc)
                discovered_models = []

        existing_set = set(existing_catalog)
        newly_found = [m for m in discovered_models if m not in existing_set]
        if newly_found and self._sheet_client is not None and sheet_id:
            await asyncio.wait_for(
                self._sheet_client.append_available_models(sheet_id, newly_found),
                timeout=10.0,
            )
        return newly_found

    def _resolve_target_sheet_id(self, sheet_id: Optional[str] = None) -> str:
        if sheet_id is None:
            return config.master_prompt_sheet_id or ""
        if sheet_id.strip():
            return extract_spreadsheet_id(sheet_id)
        return ""

    async def ensure_master_sheet_validations(
        self, sheet_id: Optional[str] = None, *, force: bool = False
    ) -> bool:
        """Ensures `Tab2_Scheduled_Jobs` is migrated to the UTC schema and native Google Sheets
        dropdown/formula validations are applied across the Master Config Sheet.
        """
        target_sheet_id = self._resolve_target_sheet_id(sheet_id)
        if not target_sheet_id or self._sheet_client is None:
            return False
        ensurer = getattr(self._sheet_client, "ensure_master_sheet_validations", None)
        if not callable(ensurer):
            return False
        return await asyncio.wait_for(ensurer(target_sheet_id, force=force), timeout=20.0)

    async def load_scheduled_rules_with_diagnostics(
        self, sheet_id: Optional[str] = None
    ) -> tuple[List[ScheduledAuditRule], List[ScheduledRuleRowError]]:
        """Loads all rows from `[Tab 2: Scheduled Inspection Jobs]` (`Tab2_Scheduled_Jobs`) along with any row validation errors."""
        target_sheet_id = self._resolve_target_sheet_id(sheet_id)
        if not target_sheet_id or self._sheet_client is None:
            self.last_scheduled_rule_errors = []
            return [], []
        diag_reader = getattr(self._sheet_client, "read_scheduled_rules_with_diagnostics", None)
        if callable(diag_reader):
            rules, errors = await asyncio.wait_for(diag_reader(target_sheet_id), timeout=15.0)
            self.last_scheduled_rule_errors = list(errors)
            return rules, errors
        reader = getattr(self._sheet_client, "read_scheduled_rules", None)
        if callable(reader):
            rules = await asyncio.wait_for(reader(target_sheet_id), timeout=15.0)
            self.last_scheduled_rule_errors = []
            return rules, []
        self.last_scheduled_rule_errors = []
        return [], []

    async def load_scheduled_rules(self, sheet_id: Optional[str] = None) -> List[ScheduledAuditRule]:
        """Loads all rows from `[Tab 2: Scheduled Inspection Jobs]` (`Tab2_Scheduled_Jobs`)."""
        rules, errors = await self.load_scheduled_rules_with_diagnostics(sheet_id)
        self.last_scheduled_rule_errors = list(errors)
        return rules

    async def update_scheduled_rule_status(
        self, row_index: int, status_text: str, sheet_id: Optional[str] = None
    ) -> None:
        """Updates column I (`Last_Run_Status`) for a rule row in `[Tab 2: Scheduled Inspection Jobs]`."""
        target_sheet_id = self._resolve_target_sheet_id(sheet_id)
        if not target_sheet_id or self._sheet_client is None:
            return
        updater = getattr(self._sheet_client, "update_scheduled_rule_status", None)
        if not callable(updater):
            return
        await asyncio.wait_for(updater(target_sheet_id, row_index, status_text), timeout=10.0)

    async def load_config_for_tab(
        self,
        prompt_tab_name: str,
        model_version: str = "",
        sheet_id: Optional[str] = None,
        *,
        probe_model: bool = True,
    ) -> PromptModelConfig:
        """Loads a specific `Prompt_*` tab or `Tab4_Spot_Check` and `model_version` directly from the Master SOP Sheet.

        Used by `[Tab 3: Golden Set Evaluation Workbench]` (`Tab3_Golden_Eval`) and `[Tab 4: Targeted Spot-Check]` (`Tab4_Spot_Check`).
        When `model_version` is omitted/empty, inherits `Active_Model_Version` / `Fallback_Model_Version`
        from `[Tab 0]` so spot-checks and new-SOP tests automatically run on the active production model.
        """
        target_sheet_id = self._resolve_target_sheet_id(sheet_id)
        clean_tab = (prompt_tab_name or "").strip()
        is_spot_check_tab = clean_tab in TAB4_ALIASES
        clean_model = (model_version or "").strip()
        fallback_model_ver = config.fallback_model_version
        scan_targets, bundled_rules = await asyncio.to_thread(load_rules_from_yaml, self._yaml_path)

        rules: List[SopRuleItem] = []
        if target_sheet_id and self._sheet_client is not None:
            if not clean_model:
                try:
                    pointers = await asyncio.wait_for(
                        self._sheet_client.read_tab0_pointers(target_sheet_id),
                        timeout=10.0,
                    )
                    clean_model = (
                        pointers.get("Active_Model_Version")
                        or pointers.get("Fallback_Model_Version")
                        or config.fallback_model_version
                    )
                    fallback_model_ver = (
                        pointers.get("Fallback_Model_Version") or config.fallback_model_version
                    )
                except Exception as exc:
                    logger.warning("Could not read Tab0 model pointers for %s: %s", clean_tab, exc)
                    clean_model = config.fallback_model_version
            if is_spot_check_tab:
                for t4 in TAB4_ALIASES:
                    _SHEET_RANGE_CACHE.pop(f"{target_sheet_id}:{t4}!A1:I100", None)
            try:
                tab_rules = await asyncio.wait_for(
                    self._sheet_client.read_prompt_tab_rules(target_sheet_id, clean_tab),
                    timeout=15.0,
                )
            except Exception as exc:
                if is_spot_check_tab:
                    try:
                        await self.ensure_master_sheet_validations(sheet_id=target_sheet_id, force=True)
                        tab_rules = await asyncio.wait_for(
                            self._sheet_client.read_prompt_tab_rules(target_sheet_id, clean_tab),
                            timeout=15.0,
                        )
                    except Exception:
                        raise ValueError(
                            f"Cannot read tab `{clean_tab}` from the Master Sheet ({exc}). "
                            f"Please confirm that `{clean_tab}` exists in the Master Sheet and contains at least 1 rule."
                        ) from exc
                else:
                    raise
            if tab_rules:
                rules = tab_rules
        elif clean_tab == config.default_prompt_version or clean_tab in PROMPT_TAB_ALIASES:
            rules = bundled_rules
        elif is_spot_check_tab:
            rules = parse_rules_from_sheet_rows(
                [list(SHEET_RULE_HEADER), list(DEFAULT_TAB4_SAMPLE_ROW)]
            )

        if not clean_model:
            clean_model = config.fallback_model_version

        if not rules:
            if is_spot_check_tab:
                raise ValueError(
                    f"Master Sheet tab `{clean_tab}` (`{TAB4_SPOT_CHECK_TITLE}`) is currently empty "
                    f"(no valid SOP rule rows found). Please paste or enter at least 1 SOP rule to spot-check/test "
                    f"in tab `{clean_tab}` (Column A `Rule_ID` cannot be empty) before starting a targeted test."
                )
            raise ValueError(
                f"No valid SOP rule tab named '{clean_tab}' was found in the Master Sheet (or the tab is empty)"
            )

        if probe_model:
            resolved_model, model_warning = await self.resolve_and_probe_model(
                clean_model, fallback_model_ver
            )
        else:
            resolved_model, model_warning = clean_model, None
        sys_instruction = render_v25_system_instruction(
            clean_tab,
            rules,
            visual_scan_targets=scan_targets,
            is_spot_check=is_spot_check_tab,
        )
        return PromptModelConfig(
            active_prompt_version=clean_tab,
            active_model_version=resolved_model,
            fallback_model_version=fallback_model_ver,
            model_fallback_warning=model_warning,
            visual_scan_targets=scan_targets,
            rules=rules,
            system_instruction=sys_instruction,
        )

    async def load_scheduled_eval_rules_with_diagnostics(
        self, sheet_id: Optional[str] = None
    ) -> tuple[List[ScheduledEvalRule], List[ScheduledEvalRowError]]:
        """Loads all rows from `[Tab 3: Golden Set Evaluation Workbench]` (`Tab3_Golden_Eval`) along with any row validation errors."""
        target_sheet_id = self._resolve_target_sheet_id(sheet_id)
        if not target_sheet_id or self._sheet_client is None:
            self.last_scheduled_eval_errors = []
            return [], []
        diag_reader = getattr(self._sheet_client, "read_scheduled_eval_rules_with_diagnostics", None)
        if callable(diag_reader):
            rules, errors = await asyncio.wait_for(diag_reader(target_sheet_id), timeout=15.0)
            self.last_scheduled_eval_errors = list(errors)
            return rules, errors
        reader = getattr(self._sheet_client, "read_scheduled_eval_rules", None)
        if callable(reader):
            rules = await asyncio.wait_for(reader(target_sheet_id), timeout=15.0)
            self.last_scheduled_eval_errors = []
            return rules, []
        self.last_scheduled_eval_errors = []
        return [], []

    async def load_scheduled_eval_rules(
        self, sheet_id: Optional[str] = None
    ) -> List[ScheduledEvalRule]:
        """Loads all rows from `[Tab 3: Golden Set Evaluation Workbench]` (`Tab3_Golden_Eval`)."""
        rules, errors = await self.load_scheduled_eval_rules_with_diagnostics(sheet_id)
        self.last_scheduled_eval_errors = list(errors)
        return rules

    async def update_scheduled_eval_row(
        self,
        row_index: int,
        *,
        trigger_action: Optional[str] = None,
        last_eval_report: Optional[str] = None,
        sheet_id: Optional[str] = None,
    ) -> None:
        """Updates `Trigger_Action` (Col I) and/or `Last_Eval_Report` (Col J) in `[Tab 3: Golden Set Evaluation Workbench]`."""
        target_sheet_id = self._resolve_target_sheet_id(sheet_id)
        if not target_sheet_id or self._sheet_client is None:
            return
        updater = getattr(self._sheet_client, "update_scheduled_eval_row", None)
        if not callable(updater):
            return
        await asyncio.wait_for(
            updater(
                target_sheet_id,
                row_index,
                trigger_action=trigger_action,
                last_eval_report=last_eval_report,
            ),
            timeout=10.0,
        )
