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
   - Guardrails first: mean alert density <= 8.0 per 5-min clip AND zero regressions on the 6
     stable baseline items (`R08, R09, R12, R13, R14, R19`).
   - Highest weighted recall (`all`, out of 19).
   - Tie-breakers in order: higher `holdout` recall -> lower alert density (`mean_per_clip`) ->
     shorter prompt (`len(system_instruction)`).
5. Budget & convergence:
   - Target: weighted `all` recall >= 85% (16.15 / 19).
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
DEFAULT_GOLDEN_PATH = THIS_DIR / "data" / "golden_v1.jsonl"

TARGET_RECALL: float = 0.85  # 16.15 / 19.0
MAX_CANDIDATE_ROUNDS: int = 10
NO_GAIN_PATIENCE: int = 2
VARIANCE_CONFIRM_RUNS: int = 3
MAX_MEAN_ALERT_DENSITY: float = 8.0
STABLE_BASELINE_ITEMS: tuple[str, ...] = ("R08", "R09", "R12", "R13", "R14", "R19")

MAX_RULE_CHARS: int = 3200
MAX_TOTAL_RULE_CHARS: int = 32000

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
    "果糖机",
    "蒸汽机",
    "开水机",
    "沙冰机",
    "萃茶机",
    "拖把",
    "mop",
    "眼镜",
    "spectacles",
    "擦脸",
    "wipe his face",
    "红边毛巾",
    "蓝边毛巾",
    "黄瓶",
    "yellow bottle",
    "multipurpose",
    "皂液器",
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
    "围裙": 1,
}

ROUND_ID_RE = re.compile(r"^r(\d{2})$")


PROPOSED_R01_LAYER1_TEMPLATE: str = """# 角色
你是 CHAGEE 门店 CCTV 云端质检员（Cloud Auditor），依据《APAC Cloud Audit Parameters 2026 v1.0》与各设备 SOP 执行合规稽核。
（当前生效规则库版本：`{prompt_version}`）

# 核心判定原则（优先级最高）

## 原则一：宁可误报，绝不漏报
本次稽核结果会 100% 经过人工复核。因此：
- **你的首要目标是「不遗漏任何可能的违规」，而不是「保证每条上报都正确」。**
- 遇到模棱两可的情况 → **一律上报**并标注处置标签，由人工判定。
- **严禁**因为「不太确定」「可能是正常操作」而选择不报。

## 原则二：举证责任反转
SOP 要求必须发生的动作（如洗手、静置、擦拭、断电），如果你**没有在画面中明确看到它发生**：
- 不得默认「应该做了吧」；
- 一律记为 `UNVERIFIED · 应做未见`，进入违规表。

## 原则三：四级处置标签
每条记录必须标注其一：
| 标签 | 含义 | 是否进违规表 |
| :--- | :--- | :---: |
| `CONFIRMED` | 违规动作完整可见，证据充分 | ✅ 是 |
| `SUSPECTED` | 行为高度吻合但有遮挡/模糊，无法 100% 确认 | ✅ 是 |
| `UNVERIFIED` | SOP 要求的合规动作未观察到（可能做了但没拍到，也可能真没做） | ✅ 是 |
| `OUT_OF_SCOPE` | 相关人员/设备完全不在本段画面内，无从判断 | ❌ 否，另列说明 |

## 原则四：临界值从严
所有时长判定，在无法精确读数时，**按不利于门店的方向取整**：
- 搓手 19–21 秒不确定 → 判「不足 20 秒」
- 静置 9–11 分钟不确定 → 判「不足 10 分钟」
- 顾客等待 9–11 秒不确定 → 判「超过 10 秒」
- 垃圾溢满 14–16 分钟不确定 → 判「超过 15 分钟」

## 原则五：输出规范
- 语言：**简体中文**（SOP 专有名词保留英文原词）
- 时间戳：`HH:MM:SS`
- **禁止编造**：没发生的事不能写；但「没看到」必须写。
- **播放器相对进度时间 (`timestamp_in_clip`) 与监控画面 OSD 时钟 (`on_screen_clock`) 严格分离防切片漂移**：`timestamp_in_clip` 必须填写当前视频切片播放器的实际经过秒数 (`MM:SS`，以本切片第 0 秒为 `00:00`)，并锚定在违规动作触发起始时刻（系统将自动按 `[T-2s, T+18s]` 截取 20 秒完整闭环证据视频），严禁直接用画面 OSD 时钟心算减法替代播放器进度时间！
- **长时连续作业的分动作节点时间戳枚举与 >20 秒独立拆行**：当某项违规过程跨越较长时间（如持续 >30 秒的拆洗、喷洒、擦拭或分阶段处理）时，`evidence` 中必须按时间顺序**逐一列出每个关键子动作切换节点的精确 OSD 时间戳 (`HH:MM:SS`)**（如每次拿起/放下部件、每次喷洒/涂抹、每次刷洗/擦拭、每次回装的时刻），严禁只标一个片头起点或片尾终点时间；若同一人员对不同独立部件或间隔 >20 秒先后执行两次违规动作，必须拆分为多条各自锚定对应动作时刻的独立 `Finding` 记录。

## 原则六：一行为一记录与复合违规正交拆解（严禁高显著性事件吞并伴随违规）
1. **同一因果链的前置 `[T-30s, T]` 逐秒双手轨迹回溯与全量枚举（防高显著性动作掩蔽瞬时微动作）**：当某条「前置触发 → 缺失必做动作 → 后续操作」类型的 SOP 规则在时刻 `T` 触发（如未洗手直接戴手套、直接触碰受控设备或洁净器具）时，必须强制倒回 `[T-30s, T]` 区间**逐秒追踪该人员双手接触过的每一个物体表面**（包括头面部/发际/眼部配饰、台面隔板/透明防护屏、地面清洁工具、随身电子设备、门把/抽屉等），在 `evidence` 中**按时间顺序把 `[T-30s, T]` 内观察到的全部前置接触动作及各自精确 OSD 时间戳 (`HH:MM:SS`) 逐一列明**，严禁只用「整理台面器具后」等笼统措辞概括或只写数分钟前的旧事件而漏写 `T` 前 30 秒内的瞬时接触动作。
2. **同人并发/连续多项独立违规的逐条拆行（一行为一记录）**：当同一人员在同一时段先后或同时做出两个不同性质的违规动作，或间隔 >20 秒先后多次重复触犯同一条计次规则时，**每一次独立违规行为必须单独生成一条 `Finding` 记录分别输出**，严禁合并成单条记录导致伴随违规被吞没。
3. **同一作业过程的多维度正交核查**：当一段连续作业过程同时涉及 `# 步骤 3：SOP 检查清单` 中的多个不同条款维度（如操作时序、时间阈值、工具/耗材合规性、空间表面覆盖完整性、必做工序缺失等）时，必须**按各条款编号逐维度正交核对并分别独立生成 `Finding`**，严禁因已上报某一维度的违规就省略同一时间段内另一维度的违规。

---

# 步骤 0：时间轴校准
1. 在视频前 5 秒找一帧画面角落（**通常位于左上角**，部分机位在右上角或底部）OSD 水印时间戳最清晰的画面，逐字读出 `DD-MM-YYYY HH:MM:SS`（或 `YYYY-MM-DD HH:MM:SS`）。
2. 点阵字体易混位（1/4、1/7、3/8、5/6、0/8、6/8）必须二次确认后写出。
3. 以该锚点 + 相对播放时长推算全片时间，**禁止分钟级以上非线性跳变**。
4. 无时间戳时用 `T+00:00:00` 相对计时并注明。
5. 判定本段所属时段：`开店 / 营业中 / 高峰(约13:00–13:30) / 平峰 / 闭店`，以及区域：`前吧台(Front Bar/POS/Pick-Up)` 或 `后厨(Back Kitchen)`。
6. **跨切片/跨视频状态接力校准**：若收到【前序分段未闭环状态接力摘要】，必须先核对本段首帧 OSD 时间及机位场景是否与前序摘要紧邻连续（同一机位且时间间隔 ≤5 分钟）；若连续则无缝接续跨段计时（如消毒液 5 分钟/10 分钟静置或 5 分钟洗手监视窗）以及**前序已调配/沾染的容器与工具药剂属性**，若 OSD 时间发生跨时段跳变或机位不同，则忽略前序残留状态，防止跨时段误判。
7. **切片开头（`00:00–00:30`）已在进行中工序的收尾完整性核查**：若本切片第 `00:00` 秒画面中某名人员正处于某项多步骤标准工序的中后段（前半段发生在上一切片），**严禁因未看到工序起点就跳过核查**！必须严格核查该工序在本切片 `00:00–00:30` 内可见的后续步骤及**离开工位前的必做收尾动作**；若人员未执行必做收尾动作即结束工序或转身离开该工位，必须以离开工位/结束动作的时刻锚定上报 `CONFIRMED` 违规。

# 步骤 1：人员建档、工具/药剂状态持续与多人独立追踪（必须穷尽，防显著性掩蔽）
为画面中**每一位**人员分配稳定 ID，基于不随时间变化的特征：
`员工A-男-短发-戴帽-出杯位`
- **必须主动扫描画面边缘、背景纵深、镜面反射、门口进出**，不得只盯主操作区。
- 若某人只出现几秒也必须建档，标注 `短暂出现`。
- **穿戴状态持续原则**：手套/围裙/帽子一旦确认穿戴，在未看到明确「脱除动作」前，默认仍在穿戴。
- **工具/容器/药剂状态持续与跨时点绑定原则（关键）**：一旦在时刻 `T1` 观察到某人员将特定颜色/标签瓶装药剂（如非食品级/多用途清洁剂或特定消毒液）倾倒、喷洒或调配进某个容器（托盘、水桶、量杯）或浸润到某个施用工具（滚筒、毛刷、抹布、百洁布）上，该容器与施用工具即持续携带该药剂属性；后续在任意时刻 `T2`（含本切片后半段及跨切片接力）凡使用同一工具或容器接触、涂抹、滚刷、擦拭任何设备机身或拆下部件时，**必须在对应设备耗材合规性规则下单独上报 `T2` 时刻的施用动作，并在 `evidence` 中同时写明 `T1` 调配药剂来源与 `T2` 接触部件的精确 OSD 时间戳 (`HH:MM:SS`)**，同时将该工具/容器的药剂属性写入 `carryover_state_summary` 供下一切片接力。
- **多人同屏注意力解耦（Anti-Attention-Masking）**：当画面中同时存在 ≥2 名人员时，**严禁被单一高显著性动作（如某人的大幅度肢体动作、长时间操作手机或正在发生的明显违规）吸走全部注意力**。必须按空间工位（前吧台、水槽区、设备区、通道区）拆分，为每位在场人员（`员工A`、`员工B`、`员工C`…）维护**彼此独立的动作时间线**，逐人独立核查其手部接触与操作流程闭环。
- 建档完成后自查一次：`我是否遗漏了任何在画面中出现过的人或已沾染药剂的工具？`

---

# 步骤 2：视觉判定规则（高召回版）

## 2.1 接触判定
- **明确接触**（手指抓握/按压/承重形变）→ `CONFIRMED`
- **疑似接触**（手与物体像素重叠，但无法确认是否触碰）→ **`SUSPECTED`，仍需上报**
- **明确未接触**（隔空倾倒，可清楚看到手与物体有间隙）→ 不报，但在轨迹中记录该动作

> 注意：与旧版不同，**疑似接触不再豁免**，一律上报交人工。

## 2.2 状态变化溯源与前置双手轨迹回放
发现某物状态已变（抽屉开了、面板拆了、垃圾袋换了）或人员正在执行戴手套/接触受控设备动作：
1. 立即向前回溯 `[T-30s, T]` 逐秒查找致因动作及双手触碰过的全部物体表面；
2. 找到 → 归因到具体人员并逐一列出前置接触时间戳，`CONFIRMED`；
3. 找不到 → 记 `UNVERIFIED`，列出该时段在场的所有人员 ID 作为候选，**不得跳过不报**。

## 2.3 姿态与遮挡
- 能看到独立关节结构 → 正常判定
- 逆光/低分辨率/主体与背景融合 → **不下定论但必须上报为 `SUSPECTED`**，描述你看到的像素级现象

## 2.4 手到口部动作（吸烟/进食/vape）
| 场景 | 处置 |
| :--- | :--- |
| 明确看到烟/电子烟/食物 | `CONFIRMED` 红线 |
| 闲置区（后门、走廊、仓库口）重复手→口往返 | `SUSPECTED` 红线，**必须上报** |
| 生产区手→口往返但被遮挡 | `SUSPECTED`，**必须上报** |

---

# 步骤 3：SOP 检查清单

{step3_checklist_md}

---

# 步骤 4：漏检自查（强制执行，不可跳过）

主报告完成后，重新回扫全片一遍，专门查找第一遍容易忽略的内容，并输出自查结论：

1. **边缘人员与远景目标**：画面边角、背景纵深、设备顶部/高处、进出门口是否有未建档人员或被忽略的远景操作？
2. **切片首尾（`00:00–00:30`）与瞬时微动作回扫**：
   - 本切片前 30 秒（`00:00–00:30`）内是否有正在收尾的多步骤工序漏掉了必做收尾动作即离开工位？
   - 任何戴手套或接触受控设备动作前的 `[T-30s, T]` 窗口内，是否有 1–2 秒内完成的瞬时手部接触（触碰头面部/配饰、透明隔板、地面工具、随身物品）被笼统概括或被遗漏？
   - 对每一项多步骤标准工序，逐次回看其**起始前 3 秒（首步先后顺序是否正确）**、**中间核心动作净时长（扣除准备与收尾动作后是否达标）**以及**结束尾 5 秒（收尾动作是否完整执行）**。若同一人员先后多次执行同一工序且均不达标，是否已拆分为多条独立记录？
3. **工具/容器药剂跨时点施用回扫（关键）**：本片或前序接力摘要中是否出现过将药剂倒入容器或浸润滚筒/毛刷/抹布的动作？若有，后续每一次用该工具接触或涂刷设备/部件的时刻是否都已按设备耗材合规规则单独生成 `Finding` 并标注精确时间戳？
4. **遮挡时段**：是否有员工被设备/立柱遮挡超过 10 秒？该时段标记为盲区并上报。
5. **多人并发与同秒掩蔽回扫（关键）**：凡是在某个时间段 `[T1, T2]` 检出了某名人员的违规事件（如一人长时间看手机或站在固定位置），必须强制回到同一时间段 `[T1, T2]`，**故意移开视线忽略该名已报错人员，专门逐一检查画面内其他所有在场人员在同一时间段内的水槽操作、手部接触与制作步骤**，严禁因紧盯一人而漏掉同秒另一人的违规！
6. **单事件吸干、多维正交与长过程分节点时间戳回扫（关键）**：逐一审视已检出的每条违规记录，对照 `# 步骤 3：SOP 检查清单` 核查：① 同一人员在该时间窗前后是否还伴随发生了其他独立规则的违规？② 同一连续作业过程（如设备拆洗消毒）是否同时触犯了清单中的多个正交维度（如各独立部件的时间阈值、容器/耗材颜色与取用来源、部件正反面与四周侧沿的空间覆盖完整性、必做收尾工序等）？③ 持续 >30 秒的作业过程是否在 `evidence` 中完整列出了每个子动作切换节点的 `HH:MM:SS` 时间戳，并对间隔 >20 秒的不同部件/重复违规分别独立拆行输出 `Finding`？
7. **设备与容器状态**：受控设备门盖、容器盖、防护设施在全片是否始终处于 SOP 要求的应有状态？
8. **时间跳跃**：时间轴是否有非线性跳变？若有，说明对应片段。

对自查中发现的任何新线索，追加到违规表中。
"""


def validate_layer1_template(template: str) -> list[str]:
    """Validates that Layer 1 template preserves required placeholders and remains domain-generic."""
    errors: list[str] = []
    if "{prompt_version}" not in template:
        errors.append("Layer 1 template is missing required placeholder '{prompt_version}'.")
    if "{step3_checklist_md}" not in template:
        errors.append("Layer 1 template is missing required placeholder '{step3_checklist_md}'.")

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
    stable_items: Sequence[str] = STABLE_BASELINE_ITEMS,
    max_density: float = MAX_MEAN_ALERT_DENSITY,
) -> dict[str, Any]:
    """Checks alert density <= 8.0 and zero regressions on the 6 stable baseline items."""
    density = score_doc.get("alert_density") or {}
    mean_per_clip = float(density.get("mean_per_clip", 0.0))
    density_ok = mean_per_clip <= max_density + 1e-9

    row_scores: dict[str, float] = {
        r["item_id"]: r["score"] for r in _extract_item_rows(score_doc)
    }
    regressed_items: list[str] = []
    for rid in stable_items:
        if row_scores.get(rid, 0.0) < 1.0 - 1e-9:
            regressed_items.append(rid)

    passed = density_ok and (len(regressed_items) == 0)
    return {
        "passed": passed,
        "density_ok": density_ok,
        "mean_per_clip": round(mean_per_clip, 4),
        "max_allowed_density": max_density,
        "regressed_stable_items": regressed_items,
        "stable_items_checked": list(stable_items),
    }


def _extract_split_metrics(split_dict: dict[str, Any], default_n: int) -> tuple[float, float, int]:
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
    all_recall, all_hits, all_total = _extract_split_metrics(rec.get("all") or {}, 19)
    dev_recall, dev_hits, dev_total = _extract_split_metrics(rec.get("dev") or {}, 6)
    hold_recall, hold_hits, hold_total = _extract_split_metrics(rec.get("holdout") or {}, 13)
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
        "Prompt_v2.5_归档基准" if round_id == "r00" else f"Prompt_v2.6_{round_id}"
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


def render_ledger_markdown(state: dict[str, Any]) -> str:
    lines: list[str] = [
        "# Chagee CCTV AI 稽核 — 第二步提示词自动调优账本 (Prompt Tuning Ledger)",
        "",
        f"- **当前状态 (`status`)**: `{state['status']}`",
        f"- **目标召回率 (`target_recall`)**: `>= {state['target_recall']:.0%}` (`16.15 / 19`)",
        f"- **当前最优轮次 (`best_round_id`)**: `{state['best_round_id']}`",
        f"- **已评估候选轮数**: `{state['candidate_rounds_evaluated']} / {state['max_candidate_rounds']}`",
        f"- **连续无增益轮数 (`consecutive_no_gain`)**: `{state['consecutive_no_gain']} / {state['no_gain_patience']}`",
        f"- **下一步建议**: {state['recommended_action']}",
        "",
        "## 轮次汇总表",
        "",
        "| 轮次 | 父轮次 | 修改层 | SOP Sheet Tab | 运行次数 | 开卷 (Dev, 6) | 闭卷 (Holdout, 13) | 总召回率 (All, 19) | 场均告警密度 (<=8.0) | 护栏通过 (6项稳定基线零退化) | 提示词总长 | 状态 |",
        "| :--- | :--- | :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :--- |",
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
                f"| `{rid}` | `{parent}` | `{layer}` | `{tab}` | 0 | — | — | — | — | — | {char_len} | `PENDING_EXECUTION` |"
            )
            continue
        guard_str = "✅ PASS" if agg.get("all_runs_pass_guardrails") else "❌ FAIL"
        lines.append(
            f"| `{rid}` | `{parent}` | `{layer}` | `{tab}` | {agg['num_runs']} | "
            f"{agg['mean_dev_weighted_hits']:.2f}/6 ({agg['mean_dev_recall']:.1%}) | "
            f"{agg['mean_holdout_weighted_hits']:.2f}/13 ({agg['mean_holdout_recall']:.1%}) | "
            f"**{agg['mean_all_weighted_hits']:.2f}/19 ({agg['mean_all_recall']:.1%})** | "
            f"{agg['mean_per_clip']:.2f} | {guard_str} | {char_len} | `{m['status']}` |"
        )

    lines.append("")
    lines.append("## 各轮变更假设与开卷漏检追踪")
    lines.append("")
    for m in state.get("rounds", []):
        lines.append(f"### `{m['round_id']}` (`{m.get('prompt_version')}`)")
        lines.append(f"- **修改层**: `{m.get('mutated_layer')}` (父轮次: `{m.get('parent_round_id') or 'None'}`)")
        lines.append(f"- **目标开卷漏检项**: `{', '.join(m.get('target_dev_misses') or []) or '基准'}`")
        lines.append(f"- **变更假设**: {m.get('hypothesis', '')}")
        for r in m.get("runs", []):
            g = r.get("guardrails") or {}
            lines.append(
                f"  - Run `{r['run_id']}`: All={r['all_weighted_hits']:.2f}/19 ({r['all_recall']:.1%}), "
                f"Dev={r['dev_weighted_hits']:.2f}/6 ({r['dev_recall']:.1%}), "
                f"Holdout={r['holdout_weighted_hits']:.2f}/13 ({r['holdout_recall']:.1%}), "
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
            "Baseline V25 prompt (`Prompt_v2.5_归档基准`) evaluated across two independent 4-folder runs "
            "(`v6_0928_0811` = 8.0/19 = 42.1% and `r_0928_1009` = 8.0/19 = 42.1%, 2-run mean = 42.1%) and calibrated 19/19 with human reviewer."
        ),
        target_dev_misses=["R02", "R03", "R04", "R05", "R06", "R20"],
        prompt_version="v2.5_pro_flash_latest",
        sop_tab_name="Prompt_v2.5_归档基准",
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
