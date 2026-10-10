"""Module 3: Google Sheets Master Prompt & Model Version Control Engine (`prompt_manager.py`).

Implements `REQ-013` (Dual-Layer Sheet Version Control & 0.1s 1-Click Rollback) and embeds
100% verbatim the user's canonical `CHAGEE_CCTV_Cloud_Auditor_Prompt_v1.0`:
1. **Layer 1: Code-Fixed Engine Kernel (固化在代码中的底座)**:
   - `# 角色`
   - `# 核心判定原则（优先级最高）`: 原则一（宁可误报，绝不漏报）、原则二（举证责任反转）、
     原则三（四级处置标签）、原则四（临界值从严）、原则五（输出规范）+ 原则六（播放器相对进度
     `timestamp_in_clip` 与监控画面 OSD 时钟分离防切片漂移）。
   - `# 步骤 0：时间轴校准` (1–5)
   - `# 步骤 1：人员建档（必须穷尽）`
   - `# 步骤 2：视觉判定规则（高召回版）` (`2.1 接触判定: 疑似接触不再豁免 -> SUSPECTED`,
     `2.2 状态变化溯源`, `2.3 姿态与遮挡`, `2.4 手到口部动作`)
   - `# 步骤 4：漏检自查（强制执行，不可跳过）` (1–6)
2. **Layer 2: Sheet-Managed Dynamic SOP Checklist (`SOP` 文件夹 Google Sheet 动态维护的业务规则)**:
   - `# 步骤 3：SOP 检查清单` 的全部 **24 个完整子模块**（`A1`–`A5`、`B0`、`B1-1` Langtuo 15 项表、
     `B1-2` Manitowoc 13 项表、`B2`–`B5`、`C1`–`C8`、`D1`–`D4` 运营与人效 IPLH）存储在
     `CHAGEE_SOP_与模型版本总控台_Master_Config`（ID 由 `MASTER_PROMPT_SHEET_ID` 指定）中，
     实时拉取后与第 1 层底座无缝拼装，100% 逐字还原您的最新提示词！
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import List, Optional, Protocol

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

    subject: str = Field(description="Observation subject, e.g., 人员, 手部与手套")
    question: str = Field(description="Mandatory visual grounding question before verdict")


class SopRuleItem(BaseModel):
    """One structured SOP rule row read from a versioned Prompt Tab or bundled baseline."""

    rule_id: str = Field(description="Rule ID, e.g., A1, A2, B1-1, C1, D1")
    name: str = Field(description="Rule display name")
    category: str = Field(default="SOP", description="Category name, e.g., 【A】洗手与手部卫生")
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


DEFAULT_VISUAL_SCAN_TARGETS: List[VisualScanTarget] = [
    VisualScanTarget(subject="人员", question="画面中一共出现过几个人？逐个描述性别、发型、衣着护具特征与所处工位。"),
    VisualScanTarget(subject="手部与手套", question="每个人的手是裸手还是戴手套？手套颜色、有无破损污渍、此刻手持何物？"),
    VisualScanTarget(subject="头面部与配饰", question="帽网/口罩等护具穿戴是否完整规范？手部与腕部有无佩戴饰品？"),
    VisualScanTarget(subject="着装与护具存放", question="在岗着装是否符合规范？有无脱下工作服/围裙等护具未归位存放的情况？"),
    VisualScanTarget(subject="手部接触序列", question="按时间顺序列出每个人的手先后接触过哪些非食品接触表面、个人物品、清洁工具、设备部件或食品工器具。"),
    VisualScanTarget(subject="水槽与清洁工序", question="画面内是否有实体水槽或清洁作业？逐一记录起始动作顺序、持续操作净时长及收尾擦干动作。"),
    VisualScanTarget(subject="设备、容器与器具", question="画面中受控设备（含顶部/内部可拆部件）、容器、量具、取用工具处于什么开闭或存放状态？"),
    VisualScanTarget(subject="垃圾与地面环境", question="垃圾容器是否闭合、有无溢满？地面与操作区环境是否整洁？"),
    VisualScanTarget(subject="耗材、容器色标与私人物品", question="作业中使用的抹布色标、取用来源容器、喷壶/药剂瓶身颜色与标签分别是什么？操作区有无私人物品？"),
    VisualScanTarget(subject="顾客与出品流转", question="有无顾客交互或出品流转？记录各环节响应与操作衔接时间点。"),
]


# 24 SOP sections: the 23 verbatim ones from Percy's canonical CHAGEE_CCTV_Cloud_Auditor_Prompt_v1.0,
# plus C8 (customer SOP 6.2 Automated Tea Maker Machine, "Filter Tea into an Ice-Prepared Container").
DEFAULT_CHAGEE_V25_RULES: List[SopRuleItem] = [
    # ───────────────────────── 【A】洗手与手部卫生（Week 1 主模块） ─────────────────────────
    SopRuleItem(
        rule_id="A1",
        name="A1 · 强制洗手触发场景（参数表 1.5.5 + Reference Case 1）",
        category="【A】洗手与手部卫生（Week 1 主模块）",
        severity="RED_LINE",
        detection_type="absence",
        requires_full_context=True,
        check_instruction=(
            "出现以下任一情形，**立即开启 5 分钟监视窗**，逐秒追踪该员工：\n\n"
            "**高危污染类（红线级）**\n"
            "- **触碰头面部与个人饰物**：摸头发（touches hair）、扶/调整眼镜（spectacles/glasses）、摸脸/口鼻、触碰或拉扯口罩、打喷嚏/咳嗽掩口\n"
            "- **用同一张擦手纸先擦脸/汗，再继续擦手**\n"
            "- **触碰非食品接触隔离设施与把手**：触碰前吧台透明防飞沫挡板/护罩（spit guard）、门把手/门帘、设备外框\n"
            "- 处理垃圾袋、碰触垃圾桶或垃圾抽屉（**含把手**）、给垃圾桶套袋\n"
            "- 从地面拾取物品 / 触碰地面或地面杂物筐\n"
            "- **使用或握持清洁工具**：握持拖把（mop）、刮水板、扫把、清洗隔油池\n"
            "- 踩踏操作台面\n\n"
            "**常规触发类**\n"
            "- **上岗 / 当班开始（Partner enter work, on the switch：员工进入工作区、开启设备电源开关；洗手步骤必须发生在戴手套及接触任何食品接触工具之前）**\n"
            "- **休息后返岗 / 使用手机后**\n"
            "- **如厕后**\n"
            "- **接触现金 / POS 收银后**\n"
            "- 完成任何清洁作业后\n"
            "- 手套破损或被污染后\n\n"
            "- ⚠️ **【多污染源并存全量枚举原则（严禁主事件吞并微动作）】**：若同一员工在未洗手窗口内先后发生了多个触碰污染源动作（例如：既玩了手机，又在前后几秒摸了头发/眼镜、触碰了吧台防飞沫罩 spit guard、摸了门把手或拿了拖把 mop），在输出 `A1`/`A2` 证据描述时**必须把观察到的每一个污染源动作及时间戳全部写明**，严禁只写最显眼的「玩手机」而漏写摸头发、扶眼镜、碰防飞沫罩或拿拖把！"
        ),
        pass_criteria="上岗开启设备开关后（戴手套前）及触发上述任一污染源后5分钟监视窗内完成标准五步洗手",
        fail_criteria="上岗进入工作区开启设备开关后未洗手即戴手套或接触食品工具（before starting shift），或触碰头发/眼镜/防飞沫罩(spit guard)/拖把(mop)/手机/垃圾桶后未见洗手（多污染源并存时须全量列出；离开画面记 UNVERIFIED·污染后去向不明；窗口截断记 UNVERIFIED·监视窗被截断）",
    ),
    SopRuleItem(
        rule_id="A2",
        name="A2 · 违规判定（三要素）",
        category="【A】洗手与手部卫生（Week 1 主模块）",
        severity="RED_LINE",
        detection_type="absence",
        requires_full_context=True,
        check_instruction=(
            "在 5 分钟监视窗内，若**同时满足**：\n"
            "1. 发生上述 `A1` 触发事件（记录**全部**前置污染源动作及时间戳，如摸头发/扶眼镜、碰吧台防飞沫挡板 spit guard、握持拖把 mop、用擦脸后的同一张纸巾擦手、玩手机等）\n"
            "2. **未观察到完整五步洗手流程**\n"
            "3. 员工**直接戴手套** 或 **恢复接触食品接触工具/设备/出餐**（记录时间戳）\n"
            "   > 食品接触工具与设备清单：制冰机门板/冰仓(ice maker machine / ice bin)、冰桶(ice bucket)、立式冷柜/冷藏柜门及内部食材(chiller / food ingredients)、"
            "泡茶壶(tea brewer pot)、滤网(tea filter net)、茶基容器(tea base container)、雪克杯(shaker cup)、冰铲(ice scoop)、CHAGEE 纸杯、吸管、量杯、吧勺、滤器\n\n"
            "→ **红线违规**\n\n"
            "> [!高召回规则]\n"
            "> - **【擦脸纸巾复用擦手 = 交叉污染必报】**：若员工洗完手后拿擦手纸**先擦拭脸部/口鼻，又继续用同一张纸巾擦手并穿戴手套、打开制冰机取冰或恢复制作**，手部已被面部二次污染，必须判定 `A2`（`CONFIRMED`）红线违规！\n"
            "> - **【多触发源全量写明】**：若员工在直接戴手套或触碰制冰机/冷柜/吧台器具前既有玩手机、又有摸头发/眼镜/防飞沫挡板(spit guard)/拿拖把(mop)，必须在同一条 `A2` 证据中把所有污染源全部列出（或分别独立成行），严禁因只写玩手机而掩盖其他污染触发源。\n"
            "> - 若员工在触发后**离开画面**，标记 `UNVERIFIED · 污染后去向不明`，**仍需上报**。\n"
            "> - 若 5 分钟窗口内视频结束，标记 `UNVERIFIED · 监视窗被截断`，**仍需上报**。\n"
            "> - **「直接戴手套」本身就是独立违规项**（参数表 1.5.3），即使后续没碰食品也要报。"
        ),
        pass_criteria="触发事件后先完成五步洗手并用干净纸巾单向擦干双手，再戴新手套及接触制冰机/冷柜/食品接触工具",
        fail_criteria="触碰头发/眼镜/防飞沫罩(spit guard)/拖把(mop)/手机后未洗手直接戴手套（1.5.3）或接触制冰机/冷柜食材/吧台器具（1.5.5 红线），或洗完手用同一张纸先擦脸再擦手后戴手套恢复作业",
    ),
    SopRuleItem(
        rule_id="A3",
        name="A3 · 洗手动作五步核对",
        category="【A】洗手与手部卫生（Week 1 主模块）",
        severity="NORMAL",
        detection_type="absence",
        requires_full_context=True,
        check_instruction=(
            "每次洗手都要逐项核对并输出：\n"
            "| # | 步骤 | 判定要点 | 常见违规 |\n"
            "|---|---|---|---|\n"
            "| 1 | **湿手（必须先于打皂液）** | **双手到达水槽的前 3 秒，必须先伸入水龙头流水下冲淋润湿（rinse/wet hands first）**，然后才能抬手按压皂液器 | **未先冲水湿手而直接干手按压皂液器（apply soap before wet hand / not rinse hand before applying handwashing gel）= 顺序错误必报** |\n"
            "| 2 | 取洗手液 | 湿手后有按压皂液器/取用洗手液动作 | 直接冲水不打液（如仅冲水 2 秒即离开） |\n"
            "| 3 | **搓洗 ≥20 秒** | **必须剥离湿手、按液与冲水时间，单独计算「纯泡沫对搓净时长」**（`按完皂液开始双手对搓时刻 → 双手伸入水流冲洗泡沫时刻`） | **纯搓洗净时长 <20 秒（含 15–22 秒临界区间，依原则四从严必报）** |\n"
            "| 4 | 清水冲洗 | 冲掉泡沫 | 打完液直接擦干 |\n"
            "| 5 | **纸巾擦干（必查结尾动作）** | 冲净泡沫后**必须从擦手纸架抽取干净纸巾将双手彻底擦干（dry hands with hand towel）** | **洗完手未用擦手纸擦干（直接甩手/自然晾干/用抹布围裙擦/直接戴手套）= 缺步骤必报；或用同一张纸先擦脸再擦手** |\n\n"
            "- 洗手必须在**实体水槽 + 可见流水**处；用毛巾擦手 ≠ 洗手；洗手池洗手套 ≠ 洗手。\n"
            "- **【步骤先后时序与首尾 3 秒慢放核查】**：无论在洗手专项还是制冰机周清视频中，凡观察到任何员工走到水槽洗手，必须重点慢放核查：\n"
            "  1. **首 3 秒（湿手 vs 按皂液）**：手是先伸到水龙头下方接水润湿，还是**手部干燥状态直接先伸向皂液器按压洗手液（apply soap before wet hand）**？凡未先冲水湿手直接按皂液器，立即独立上报 `A3`（`CONFIRMED`）顺序错误违规！\n"
            "  2. **中段（纯泡沫对搓净时长）**：严禁把「在水槽前站立总时长（含开水、湿手、按液、冲水、擦手）」当作搓洗时长；扣除湿手与冲水后，凡纯泡沫对搓净时长不足 20 秒（含 15–22 秒临界区间），判定 `A3`（`CONFIRMED`）。**若同一员工在片中先后进行了 2 次洗手（例如相隔数十秒先后两次冲水/搓洗均不足 20 秒），必须分别生成 2 条独立的 `A3` 记录，严禁合并为 1 条！**\n"
            "  3. **尾 5 秒（是否用擦手纸擦干）**：关水/冲水结束后，员工双手是否明确抽取擦手纸擦干？若冲完水后**未拿擦手纸擦干双手**便直接离开水槽或直接拿取物品/戴手套，立即独立上报 `A3`（`CONFIRMED`，洗完手未用擦手纸擦干 `not dry hand with hand towel after handwashing`）！\n"
            "- 缺任意步骤或**顺序错误**（如先打液后湿手、未用擦手纸擦干）→ 🟡 Common Deviation（非红线，但必报；若一次洗手中同时存在「干手先按皂液」与「搓洗<20秒」或「未用纸巾擦干」，必须在证据中同时写明或分别独立上报）。"
        ),
        pass_criteria="实体水槽先流水湿手→再取洗手液→纯泡沫搓洗净时长≥20秒（不含湿手与冲水时间）→清水冲洗→抽取干净擦手纸彻底擦干双手（每次洗手独立核算）",
        fail_criteria="未先冲水湿手直接干手按压皂液器（apply soap before wet hand）、纯搓洗净时长不足20秒（含15-22秒临界值；多次洗手须逐次独立上报）、洗完手未用擦手纸擦干（not dry hand with hand towel）、用同一张纸先擦脸再擦手、或用抹布/围裙擦手",
    ),
    SopRuleItem(
        rule_id="A4",
        name="A4 · 手套规范（参数表 1.6）",
        category="【A】洗手与手部卫生（Week 1 主模块）",
        severity="RED_LINE",
        detection_type="absence",
        requires_full_context=True,
        check_instruction=(
            "- 处理食品必须戴手套\n"
            "- **洗手后必须立即更换新手套**（洗完手继续用旧手套 = 违规）\n"
            "- 手套被污染后必须立即更换"
        ),
        pass_criteria="处理食品全程戴手套，洗手后及手套污染后立即更换新手套",
        fail_criteria="裸手处理食品、洗完手继续戴旧手套、手套污染破损后未更换",
    ),
    SopRuleItem(
        rule_id="A5",
        name="A5 · 2 小时周期洗手（仅当输入覆盖 ≥2 小时时启用）",
        category="【A】洗手与手部卫生（Week 1 主模块）",
        severity="NORMAL",
        detection_type="absence",
        requires_full_context=True,
        check_instruction=(
            "- 追踪每位员工的洗手时间点，计算相邻间隔\n"
            "- 间隔 >2 小时 → 违规\n"
            "- **若本段视频总时长 <2 小时，明确输出：`本段时长不足，2 小时周期规则不适用`**，不得强行判定"
        ),
        pass_criteria="在岗每2小时内至少完成一次合规洗手（时长<2小时则注明不适用）",
        fail_criteria="连续在岗超过2小时未洗手",
    ),
    # ───────────────────────── 【B】制冰机周清洁（Week 2 主模块） ─────────────────────────
    SopRuleItem(
        rule_id="B0",
        name="B0 · 机型识别与立式分体制冰机视觉先验（第一步，必须输出）",
        category="【B】制冰机周清洁（Week 2 主模块）",
        severity="NORMAL",
        detection_type="presence",
        requires_full_context=False,
        check_instruction=(
            "观察机身品牌标识、面板形状、水帘结构、格栅布局、门板开合方式：\n"
            "- 输出：`机型 = Langtuo` / `Manitowoc (HT1000)` / `Manitowoc (Indigo NXT)` / `无法确认`\n"
            "- 写出你的识别依据\n"
            "- **无法确认时，按 Langtuo 最严标准核验（静置 ≥10 分钟），并标注 `机型未确认`**\n"
            "- ⚠️ **【官方 SOP 两大制冰机机型结构与可拆部件视觉锚定（源自 CHAGEE APAC 官方 SOP）】**：\n"
            "  1. **`Langtuo` 双门立式分体制冰机（Model: Langtuo，SOP 21）**：\n"
            "     - **外观结构**：高约 1.8–2.0 米的高大立式分体机，上半部制冰机头为**左右两块不锈钢前门面板（`left/right door panels`，内衬白色食品接触面 `white cover`）+ 中间立柱（`middle pillar`）+ 右上角蓝色数字液晶屏**，下半部为斜掀盖储冰仓（`ice bin`，蓝色内胆）。机头内部上方有横向白色**布水管（`water distribution tube`）**、金属**蒸发器制冰格栅（`evaporator cube`）**、下方**水槽（`water trough`）**，外覆两块带 V 型导流槽的白色**挡水帘（`water curtain`）**。\n"
            "     - **顶部平面暂存拆洗特征（SOP 21 第 5–10 步关键视觉先验）**：按照官方 SOP，员工会踩梯子/高凳，用螺丝刀拆下左右两块前门盖板（`door panels / white cover`）、中间立柱及两块白色挡水帘（`water curtain`），**直接平铺放置在制冰机顶部平面（`top surface of the ice maker`，白色食品接触内表面朝上）喷洒消毒液静置 10 分钟**，并在机器顶部或取下后用白色长柄弯头刷（`cleaning brush`）和红边毛巾刷洗擦拭后装回。\n"
            "  2. **`Manitowoc` 立式制冰机（Model: Choice HT1000 / Indigo NXT，SOP 8）**：\n"
            "     - **外观结构**：拆下前门面板（`door panel / white cover`，外层银色不锈钢、内层白色塑料壁）及百叶/导流状**挡水帘（`water curtain`）**后，内部正上方为可拧螺丝拆下的横向白色**布水管（`water distribution tube`）**，中部为金属**制冰格栅（`ice making plates`）**，右侧为三个白色**浮球开关与水泵（`float switches / water pump`）**，底部为横向可卡扣拆下的**水槽（`water trough`）**，右上角为 `ICE / OFF / CLEAN` 拨杆开关（HT1000）或 Indigo NXT 触摸屏。\n"
            "  - **严禁将员工登梯在制冰机顶部平面喷洒/刷洗/擦拭白色门盖板（`white cover`）与挡水帘（`water curtain`）、擦拭上方布水管（`water distribution tube`）或向水槽倒消毒液误判为「清洁排风网/空调口」或「擦拭冷柜顶部」而漏检 `B1`–`B4`！**"
        ),
        pass_criteria="准确识别 Langtuo 或 Manitowoc 制冰机及其五大核心部件（外盖门板 door panel/white cover、挡水帘 water curtain、上方横向布水管 water distribution tube、蒸发器制冰格 evaporator grid、水槽 water trough）并按对应静置阈值核验",
        fail_criteria="未按对应机型标准核验，或将制冰机顶部平面上的白色门盖板/挡水帘及内部布水管误判为其他设备而漏检 B1–B4",
    ),
    SopRuleItem(
        rule_id="B1-1",
        name="B1-1 · Langtuo 完整流程核对（15 项，SOP 21）",
        category="【B】制冰机周清洁（Week 2 主模块）",
        severity="RED_LINE",
        detection_type="absence",
        requires_full_context=True,
        check_instruction=(
            "| # | 步骤 | 关键判定 | 依据 |\n"
            "|---|---|---|---|\n"
            "| 1 | **关闭主电源开关（main switch）并清空冰仓** | 按屏幕 `[Power]->[Off]` 排水排冰后，**必须戴干燥手套关闭机身背面/顶部红色主电源旋钮开关**，再铲空储冰仓残冰 | SOP 21 Step 1–4 |\n"
            "| 2 | **备料：白底透明喷壶灌满无色消毒液** | 全程仅限使用 Ecolab KAY-5 消毒液，**严禁使用黄色瓶身多功能清洁剂（Not using multipurpose solution in yellow bottle）** | SOP 21 Step 4 & B3 |\n"
            "| 3 | 备料：配置 **8L 消毒液**（透明带刻度量桶） | 用于后续倒入水槽右侧启动自清洁 | SOP 21 Step 4 & 26 |\n"
            "| 4 | 备料：白色长柄弯头清洁刷浸泡于消毒液 | | SOP 21 Step 4 |\n"
            "| 5 | **喷洒并用蓝边毛巾擦净制冰机顶部平面，再用螺丝刀拆下左右门面板与中间立柱** | 拆下的螺丝须放入不锈钢方盘妥善保管 | SOP 21 Step 5–6 |\n"
            "| 6 | **门面板/白色外盖（door panel / white cover）正反双面喷洒消毒液并静置 ≥10 分钟** | 放置在制冰机顶部平面时**白色食品接触内表面必须朝上**；喷洒后**必须静置满 10 分钟**，未满严禁提前擦拭、刷洗或回装 | SOP 21 Step 7–8 |\n"
            "| 7 | **拆下两块白色挡水帘（Remove water curtain）喷洒消毒液并静置 ≥10 分钟** | 拆下挡水帘喷洒消毒液后置于顶部**静置满 10 分钟**，未满严禁提前刷洗、擦拭或回装 | SOP 21 Step 9–10 |\n"
            "| 8 | **喷洒内部制冰格表面、侧边格栅与上方布水管** | 必须大量喷洒：① **内部蒸发器制冰格（evaporator cube / interior surface of ice maker machine grid）**；② **侧边格栅（side grid）**；③ **上方横向布水管（water distribution tube）与管壁孔洞** | SOP 21 Step 11–14 |\n"
            "| 9 | **喷洒储冰仓所有内壁、顶部、边角及盖板内侧（all interior & interior of door）** | 喷完所有内部、外部与可拆件后，喷壶内消毒液应仅剩 ≤1/4 或喷空 | SOP 21 Step 15–18 |\n"
            "| 10 | **关闭冰仓**，再喷**机身外部**表面 | 顺序不可颠倒 | SOP 21 Step 18 |\n"
            "| 11 | **各部件独立静置 ≥10 分钟（Allow it to sit for 10 minutes）** | 必须对**布水管（distribution tube）、白色外盖门板（white cover / door panel）、挡水帘（water curtain）、舱门内壁（interior of door）及机身内腔**分别核对 `喷完时刻 → 首次擦拭/刷洗/回装时刻`，任一部件不足 10 分钟即报 `B2` | SOP 21 Step 19 |\n"
            "| 12 | **静置满 10 分钟后，用从消毒桶取出的干净红边毛巾及刷子清洁内腔、布水管与可拆件（含四周边框/侧面边缘）** | ① 用湿红边毛巾包裹吧勺/封口夹及长柄刷清洁蒸发器格栅、**上方布水管（distribution tube）**、缝隙孔洞与水槽；② 对挡水帘和白色外盖门板必须彻底刷洗/擦拭**正反面及四周边框、四个侧面厚度边缘与边角（especially at the corners, sides and edges）**，挡水帘经滤过水冲洗后再用干红边毛巾擦干；③ 门面板与中间立柱的**白色内表面用干红边毛巾擦拭**，**不锈钢外表面与底边框（bottom border）用干蓝边毛巾擦拭** | SOP 21 Step 19–35 |\n"
            "| 13 | **向水槽右侧倒入 8L 消毒液（Pour 8L of sanitizer solution into the right side of the water trough / Pour sanitiser solution into water trough）** | **必须在内腔擦洗完毕后执行**：持大容量量桶向制冰机上部水槽（water trough）倾倒 8L 消毒液（漏倒必报） | SOP 21 Step 26 |\n"
            "| 14 | **按序装回挡水帘、中间立柱、左门面板、右门面板并拧紧螺丝（Close back ice maker machine）**，开启主电源与自清洁，用**干燥蓝边毛巾**擦净机身外壳 | 可拆件必须完整装回并关好制冰机门板 | SOP 21 Step 29–38 |\n"
            "| 15 | **约 2 小时 / 5 批冰后：用干红边毛巾擦净储冰仓盖板孔洞，铲除丢弃全部含消毒液的冰块 + 用滤过水冲洗冰仓并用干红毛巾擦干缝隙** | 若视频未覆盖此时段，记 `OUT_OF_SCOPE` 并提示需另调后续录像 | SOP 21 Step 39–42 |"
        ),
        pass_criteria="完整执行 Langtuo 周清流程：断主电源、仅用白底透明瓶无色消毒液、拆门面板(white cover)与挡水帘(water curtain)置顶喷洒静置≥10分钟、喷内部制冰格/侧边格栅/布水管(distribution tube)/冰仓内壁并静置≥10分钟、用消毒桶取出的干净红毛巾擦拭布水管/内腔及可拆件（含四周边框与侧面边缘）、向水槽倒入8L消毒液、按序装回门板关好机器、蓝毛巾擦外部",
        fail_criteria="未关主电源、使用黄色瓶身多功能清洁剂（B3）、任一部件（布水管 distribution tube / 白色外盖门板 white cover / 挡水帘 water curtain / 舱门内壁 / 机身内腔）静置不足10分钟即提前擦拭/刷洗/回装（B2）、未使用消毒桶取出的干净红毛巾（B4）、漏擦白色外盖门板或挡水帘的四周边框/侧面边缘（sides/edges）、漏向水槽倒消毒液、或未关回制冰机",
    ),
    SopRuleItem(
        rule_id="B1-2",
        name="B1-2 · Manitowoc 完整流程核对（13 项，SOP 8）",
        category="【B】制冰机周清洁（Week 2 主模块）",
        severity="RED_LINE",
        detection_type="absence",
        requires_full_context=True,
        check_instruction=(
            "| # | 步骤 | 关键判定 |\n"
            "|---|---|---|\n"
            "| 1 | **拆下前门板，先拨 CLEAN 冲水 5 分钟，再将 toggle switch 拨至 OFF 断电清空残冰** | 拆内部组件和铲冰前开关必须在 `OFF` 位；全程**严禁使用黄色瓶身多功能清洁剂（Not using multipurpose solution in yellow bottle）**（SOP 8 Step 1–3） |\n"
            "| 2 | **拆下内部可拆三件套：布水管（distribution tube）、水槽（water trough）、挡水帘（water curtain）** | 拧松螺丝拆下上方横向布水管（distribution tube）、拔开水泵连接滑套、按压左右卡扣拉出底部水槽（water trough）与挡水帘（SOP 8 Step 4–6） |\n"
            "| 3 | **对门面板/白色外盖（door panel / white cover）喷洒消毒液并静置 ≥5 分钟** | 喷洒消毒液后须**静置满 5 分钟**，未满严禁提前擦拭、刷洗或回装（SOP 8 Step 7） |\n"
            "| 4 | **对挡水帘（water curtain）、水槽（water trough）、布水管（distribution tube）喷洒消毒液并静置 ≥5 分钟** | 所有可拆件喷洒消毒液后须**静置满 5 分钟**，未满严禁提前擦拭、刷洗或回装（SOP 8 Step 7 & 12） |\n"
            "| 5 | **喷洒内部制冰格表面（ice making plates / grid）、浮球开关（float switches）与侧壁（side wall）** | 大量喷洒消毒液并**静置满 5 分钟**（SOP 8 Step 8） |\n"
            "| 6 | **喷洒储冰仓内部（interior ice bin）、舱门内壁（interior of door）及机身外表面** | 大量喷洒消毒液并**静置满 5 分钟**（SOP 8 Step 9–10） |\n"
            "| 7 | 处理**空气滤网（air filter）** | 拆下水洗并用干净蓝边毛巾擦干装回；若滤网在机器背面无法触及则豁免（须注明）（SOP 8 Page 7） |\n"
            "| 8 | **各部件独立静置 ≥5 分钟（After 5 minutes, proceed to wipe all sanitized components）** | 必须对**布水管（water distribution tube）、白色外盖门板（door panel / white cover）、挡水帘（water curtain）、舱门内壁（interior of door）及机身内腔**分别核对 `喷完时刻 → 首次擦拭/刷洗/回装时刻`，任一部件不足 5 分钟即独立报 `B2` |\n"
            "| 9 | **用从消毒桶取出的干净红边毛巾依次擦拭浮球开关、水泵、挡水帘、水槽、布水管及储冰仓死角（含四周边框与侧面边缘）** | 必须使用从专用消毒桶（sanitizing bucket）取出的干净红边毛巾；擦拭白色外盖门板（white cover）、挡水帘（water curtain）、布水管（distribution tube）时**必须完整擦拭正反双面、四周边框与侧面厚度边缘（sides/edges）及边角**（SOP 8 Step 11–15） |\n"
            "| 10 | **先装回布水管（distribution tube）、水槽（water trough）与挡水帘（water curtain）** | 擦净后按序装回机身内部（SOP 8 Step 13） |\n"
            "| 11 | **拨至 CLEAN 并在首次排水后掀开挡水帘 45° 向水槽倒入 3L 消毒液（Pour 3L of sanitizer solution into water trough）** | ⚠️ **Audit Template 必查动作**：启动约 35 分钟自清洁循环，完成后拨回 `ICE`（SOP 8 Step 16–18） |\n"
            "| 12 | **红边毛巾擦拭门面板白色内表面（white surface）并装回门板（Close back ice maker machine）** | ⚠️ **Audit Template 必查动作**：门面板白色内壁（含四周侧沿）用**红边毛巾**擦净后装回机身（SOP 8 Step 19） |\n"
            "| 13 | **用蓝边毛巾擦拭门面板银色外表面（silver surface）及机身外壳** | 去除外部残留消毒液水痕（SOP 8 Step 20–21） |"
        ),
        pass_criteria="拨OFF断电、拆门板/布水管(distribution tube)/水槽(water trough)/挡水帘(water curtain)、全部件与内腔喷洒无色消毒液并独立静置≥5分钟、用消毒桶取出的干净红毛巾对布水管/挡水帘/水槽/白色外盖门板（正反面+四周侧面边缘）与内腔全表面无死角擦拭、装回内部三件套并倒入3L消毒液启动Self-Cleaning、红毛巾擦门板白色内壁后装回关好机器、蓝毛巾擦银色外表面",
        fail_criteria="未拨OFF开关、使用黄色瓶身多功能清洁剂（B3）、任一部件（布水管 distribution tube / 白色外盖门板 white cover / 挡水帘 water curtain / 舱门内壁 / 机身内腔）喷洒消毒液后静置不足5分钟即提前擦拭/刷洗/回装（B2）、未使用从消毒桶取出的干净红毛巾（B4）、未彻底擦拭白色外盖门板或挡水帘的四周边框/侧面边缘（sides of white cover）、漏向水槽倒消毒液、或未关回制冰机",
    ),
    SopRuleItem(
        rule_id="B2",
        name="B2 · 分部件静置锁定期与全表面（含四周侧面边缘）覆盖完整性（必查红线）",
        category="【B】制冰机周清洁（Week 2 主模块）",
        severity="RED_LINE",
        detection_type="absence",
        requires_full_context=True,
        check_instruction=(
            "- 必须**按以下 5 类核心部件逐件独立追踪并分别计算**消毒液停留静置时长（输出格式：`[部件名] 喷洒 HH:MM:SS → 首次擦拭/刷洗/回装 HH:MM:SS = 实际 X 分 Y 秒`）：\n"
            "  1. **上方横向白色布水管/分配管（water distribution tube）**\n"
            "  2. **前门面板/白色外盖（door panel / white cover，含放置在制冰机顶部平面或台面上的白色盖板）**\n"
            "  3. **白色带槽挡水帘/水帘板（water curtain）**\n"
            "  4. **制冰机舱门内壁/储冰仓门内侧（interior of ice maker door）**\n"
            "  5. **机身内腔与制冰蒸发器格栅（all interior / evaporator grid / water trough）**\n"
            "  - **Manitowoc 最低静置阈值 ≥5 分钟**；**Langtuo / 机型未确认最低静置阈值 ≥10 分钟**。\n"
            "- 🔴 **【部件静置锁定期阻断原则（提前擦拭/刷洗/装回任一部件 = 立即对该部件单独报 B2 CONFIRMED）】**：\n"
            "  - 上述 5 类部件中的**任何一个**一旦喷洒消毒液（或从拆下开始清洁），必须静置满规定时长（5 分钟或 10 分钟）后才能开始用毛巾擦拭、用刷子刷洗、用水冲洗或装回机器！\n"
            "  - 凡观察到员工在静置未满时就：① **提前用红毛巾擦拭布水管（wipe water distribution tube without waiting 5/10 min）**；② **提前刷洗/擦拭/装回白色外盖门板（white cover / door panel）**；③ **提前刷洗/擦拭/装回挡水帘（water curtain）**；或 ④ **喷洒舱门内壁（interior of ice maker door）后未满静置时间即擦拭并关门回装**，无需等待其他部件，**必须立即针对该具体部件独立输出一条 `B2`（`CONFIRMED`）红线违规记录**！\n"
            "  - **【严禁被其他条款吞并】**：即便同一时间段已检出 `A4`（未戴手套徒手清洁）、`B4`（毛巾未从消毒桶取出）或 `B3`（用错化学瓶），只要该段内存在未满静置时间即擦拭/刷洗/回装部件的行为，**必须额外并列输出独立的 `B2` 违规记录，绝不可省略 `B2`！**\n"
            "- 🔴 **【可拆部件「四周边框与侧面厚度边缘」全覆盖原则（Wipe the sides and edges thoroughly）】**：\n"
            "  - 根据官方 SOP（Langtuo Step 27 `especially at the corners and edges`），在刷洗和擦拭**白色外盖门板（white cover / door panel）**与**挡水帘（water curtain）**时，必须完整擦拭正反两面以及**四周边框、四个侧面厚度边缘（sides of the white cover）和四角凹槽**。\n"
            "  - 若员工仅快速擦拭/刷洗白色外盖门板或挡水帘的正面平坦区域，**未彻底擦拭其四周侧面边缘（do not wipe the sides of white cover thoroughly）**，必须单独上报一条 `B2`（或 `B4`）违规记录，明确写明「未彻底擦拭白色外盖门板/水帘板的四周侧面与边缘（sides/edges）」！\n"
            "- **视频在静置期内结束（且未见提前擦拭）→ `UNVERIFIED · 静置未完成即截断`，仍需上报**并注明需调取后续录像。\n"
            "- 无法精确读时 → 按从严原则判「不足」。"
        ),
        pass_criteria="布水管(water distribution tube)、白色外盖门板(white cover/door panel)、挡水帘(water curtain)、舱门内壁(interior of door)及机身内腔各自的消毒液静置时长均 ≥ 机型阈值（Manitowoc 5分钟 / Langtuo 10分钟），且擦拭白色外盖与挡水帘时完整覆盖正反面与四周侧面边缘(sides/edges)",
        fail_criteria="任一部件（布水管 water distribution tube、白色外盖门板 white cover、挡水帘 water curtain、舱门内壁 interior of ice maker door、机身内腔）喷洒消毒液后静置未满5/10分钟即提前擦拭/刷洗/回装（红线，须按部件独立上报 B2），或擦拭白色外盖门板/挡水帘时未彻底擦拭四周侧面边缘（do not wipe the sides of white cover thoroughly）",
    ),
    SopRuleItem(
        rule_id="B3",
        name="B3 · 化学品容器视觉鉴别（参数表 '0'Tolerance + 官方 SOP 喷壶规范）",
        category="【B】制冰机周清洁（Week 2 主模块）",
        severity="RED_LINE",
        detection_type="presence",
        requires_full_context=False,
        check_instruction=(
            "- ✅ **唯一允许的周清洁化学品（Sanitiser）视觉特征**：根据 Manitowoc 与 Langtuo 官方 SOP 图示，仅允许使用 **Ecolab KAY-5 Sanitizer** 配制的**无色透明溶液**，盛装在**白底透明/半透明喷壶（贴有粉红色/红色 Sanitizer 标签）**或**透明带刻度量桶（3L/8L）**中（月度除垢允许使用透明量桶配制的 Delimer）。\n"
            "- 🔴 **严禁使用黄色瓶身多功能清洁剂（Multipurpose chemical in yellow bottle = 零容忍红线）**：\n"
            "  - 门店的**鲜黄色瓶身喷壶（yellow bottle / yellow spray bottle）**或**黄色液体**为**多功能清洁剂（Multipurpose detergent / cleaner）**，严禁用于制冰机任何部件！\n"
            "  - **【必报硬性规则】**：凡在画面中观察到员工手持**黄色喷壶/黄色瓶子（yellow bottle）**或非标化学品容器，向制冰机**白色外盖门板（white cover / door panel）、上方布水管（water distribution tube）、挡水帘（water curtain）或内腔**喷洒/涂抹并擦拭刷洗，**必须立即独立生成一条 `B3`（`CONFIRMED` 红线）违规记录**（明确写明：使用黄色瓶装多功能清洁剂 `multipurpose chemical` 清洁制冰机白色外盖/布水管等部件）！\n"
            "  - **【严禁漏报】**：即使同一时间段已上报了 `B2`（静置时间不足）或 `A1`（触碰地面杂物未洗手），**也必须同时独立输出这条 `B3` 化学品违规记录，绝不可被 `B2`/`A1` 吞并！**\n"
            "- 🔴 **禁止：混用多种化学品**（Mixed use of chemicals = 红线）\n"
            "- 追踪画面中出现的**每一个化学瓶/喷壶**，逐一描述其瓶身颜色（白底透明 vs **黄色瓶身**）、液体颜色与标签；若颜色可疑或无法辨识则记 `SUSPECTED · 化学品身份不明`。"
        ),
        pass_criteria="全程仅使用白底透明瓶身（粉红标签）盛装的无色 Ecolab KAY-5 Sanitiser 消毒液（及月度除垢剂 Delimer），绝未使用黄色瓶身多功能清洁剂",
        fail_criteria="使用黄色瓶身多功能清洁剂（multipurpose chemical in yellow bottle）清洁制冰机白色外盖(white cover)、布水管(water distribution tube)或内腔（红线，必须独立上报 B3）、混用多种化学品、或化学品身份不明（SUSPECTED）",
    ),
    SopRuleItem(
        rule_id="B4",
        name="B4 · 三色毛巾、消毒桶取用起点溯源与四周边框擦拭（参数表 3.1 + 官方 SOP）",
        category="【B】制冰机周清洁（Week 2 主模块）",
        severity="RED_LINE",
        detection_type="presence",
        requires_full_context=False,
        check_instruction=(
            "| 颜色 | 官方 SOP 指定用途 | 存放与取用要求 |\n"
            "| :---: | :--- | :--- |\n"
            "| 🔴 **红边毛巾 (Red-Edged Towel)** | **直接食品接触面**：制冰机内腔、蒸发器格栅、**上方布水管 (distribution tube)**、水槽 (water trough)、浮球开关、**挡水帘 (water curtain)**、**门面板白色内表面 (white interior surface of door panel)**、中间立柱内侧面 | **必须存放在盛有消毒液的专用贴标消毒桶（sanitizing bucket）内，使用时必须从消毒桶中随用随取干净红边毛巾**，**严禁直接从操作台面、机器顶部、梯子旁或围裙上拿取未浸泡于消毒桶的毛巾** |\n"
            "| 🔵 **蓝边毛巾 (Blue-Edged Towel)** | **间接/非食品接触外表面**：制冰机不锈钢外壳、制冰机顶部平面、**门面板银色不锈钢外表面 (silver exterior surface of door panel)**、中间立柱外侧面及底边框 (bottom border)、空气滤网 | 从蓝色消毒桶取用，可放操作区台面 |\n"
            "| 🟢 **绿边毛巾** | 户外与顾客区 | **严禁进入操作区台面**，须悬挂或独立容器存放 |\n\n"
            "- 🔴 **【毛巾取用起点逆向溯源原则】**：凡观察到员工持毛巾擦拭制冰机内腔、布水管、舱门内壁、白色外盖门板（white cover）或挡水帘（water curtain），必须**向前逆推 5 秒核查该毛巾的取用起点**——合规操作必须是从**专用消毒桶（sanitizing bucket）**中取出的干净红边毛巾；若员工直接从操作台面、水槽边缘、制冰机顶部、梯子旁或身上随手拿起未从消毒桶取出的毛巾（或用错颜色毛巾）去擦拭制冰机，立即独立上报 `B4`（`CONFIRMED`）红线违规！\n"
            "- 🔴 **【白色外盖门板与挡水帘「四周侧面边缘」擦拭核查】**：用红边毛巾或刷子清洁白色外盖门板（white cover / door panel）和挡水帘（water curtain）时，必须完整擦拭正反面及**四周边框、四个侧面厚度边缘（sides of white cover）与边角凹槽（corners and edges）**；若仅擦拭正面中间区域而漏擦四周侧面边缘，必须在报告中明确指出该违规！\n"
            "- 容器须贴专用标签（贴在外盖上的定位标签视为无效标签）\n"
            "- 用错颜色 / 未从消毒桶取用干净红边毛巾 / 漏擦可拆件四周侧面边缘 / 存放不当 → 违规"
        ),
        pass_criteria="擦拭制冰机内腔、布水管、挡水帘及门面板白色内表面时全程使用从专用贴标消毒桶（sanitizing bucket）中取出的干净红边毛巾并完整擦拭四周侧面边缘，蓝边毛巾擦门面板银色外表面与机身外壳，绿边绝不进操作区",
        fail_criteria="未使用从专用消毒桶（sanitizing bucket）中取出的干净红边毛巾擦拭制冰机（not used a clean red edge towel from sanitising bucket）、擦拭白色外盖门板/挡水帘时漏擦四周侧面边缘（sides of white cover）、三色毛巾混用、红边毛巾直接放操作台面、绿边毛巾出现在操作区",
    ),
    SopRuleItem(
        rule_id="B5",
        name="B5 · 冰铲与冰处理（参数表 4.1 / 4.5）",
        category="【B】制冰机周清洁（Week 2 主模块）",
        severity="RED_LINE",
        detection_type="presence",
        requires_full_context=False,
        check_instruction=(
            "- 🔴 **冰铲存放于冰仓内（stored in ice bins）= 红线**\n"
            "- 冰铲使用前后都必须放回**专用收纳盒**，用后立即盖好盒盖\n"
            "- 只能用冰铲取冰，**严禁徒手抓冰**\n"
            "- 搬运冰块时必须盖上冰桶/制冰机盖\n"
            "- 🔴 **多余的冰倒回制冰机或冰仓 = 违规**\n"
            "- 冰仓、制冰机盖用后须及时盖回"
        ),
        pass_criteria="仅用冰铲取冰，用后立即放回专用收纳盒并盖好盒盖，冰仓盖及时关闭",
        fail_criteria="冰铲存放在冰仓内（红线）、徒手抓冰、多余冰块倒回制冰机/冰仓、冰仓盖未及时盖回",
    ),
    # ───────────────────────── 【C】常态红线扫描（所有周次都跑） ─────────────────────────
    SopRuleItem(
        rule_id="C1",
        name="C1 · 交叉污染（'0' Tolerance）",
        category="【C】常态红线扫描（所有周次都跑）",
        severity="RED_LINE",
        detection_type="presence",
        requires_full_context=False,
        check_instruction=(
            "- 工具/餐具掉地后未彻底清洗消毒即复用\n"
            "- 员工**踩踏操作台面**后未消毒即恢复作业\n"
            "- 🔴 **在食品水槽清洗拖把、刮水板、隔油池**\n"
            "- 用食品包装材料盛装化学品\n"
            "- 高危行为后未洗手（→ 见【A】）\n"
            "- 多余食材倒回原包装\n"
            "- 🔴 **手指碰触客用杯内壁**\n"
            "- 🔴 **徒手抓取即食半成品**（冰块、切片水果）\n"
            "- 掉在操作台上的即食食品未丢弃仍继续使用\n"
            "- 掉地/掉污水区的食材包装未消毒即复用\n"
            "- 自动化设备软管直接放入物料盒"
        ),
        pass_criteria="无任何交叉污染行为，食品水槽专槽专用，手不触碰客用杯内壁，掉地器具彻底消毒",
        fail_criteria="食品水槽洗拖把/刮水板/隔油池、手指碰触客用杯内壁、徒手抓冰块/水果、掉地器具未消毒复用",
    ),
    SopRuleItem(
        rule_id="C2",
        name="C2 · 杯具使用（'0' Tolerance）",
        category="【C】常态红线扫描（所有周次都跑）",
        severity="RED_LINE",
        detection_type="presence",
        requires_full_context=False,
        check_instruction=(
            "- 🔴 重复使用饮品杯\n"
            "- 🔴 顾客未取/取消的饮品未丢弃、杯子被复用\n"
            "- 🔴 杯身贴多重标签（未撕旧标签就复用）\n"
            "- 🔴 收银小票、笔、文具放入饮品杯或食品容器"
        ),
        pass_criteria="饮品杯一次性使用、单张当次标签、无小票或笔放入杯具/食品容器",
        fail_criteria="重复使用饮品杯、杯身贴多层标签、收银小票/笔/文具放入饮品杯或食品容器",
    ),
    SopRuleItem(
        rule_id="C3",
        name="C3 · 不当行为（'0' Tolerance）",
        category="【C】常态红线扫描（所有周次都跑）",
        severity="RED_LINE",
        detection_type="presence",
        requires_full_context=False,
        check_instruction=(
            "- 🔴 店内吸烟 / vape\n"
            "- 🔴 **玩扑克牌**\n"
            "- 🔴 营业时间内睡觉\n"
            "- 🔴 在备餐时或顾客面前进食/饮水\n"
            "- 🔴 **拍摄视频**（顾客面前拍摄或拍摄非工作内容）\n"
            "- 🔴 对顾客表现敌意、讽刺、轻慢\n"
            "- 🔴 **无视顾客在场或需求**"
        ),
        pass_criteria="在岗期间无吸烟/vape、玩牌、睡觉、备餐区进食饮水、拍摄非工作视频或冷落顾客行为",
        fail_criteria="店内吸烟/vape、玩牌、睡觉、备餐或顾客面前进食饮水、拍摄非工作视频、无视顾客",
    ),
    SopRuleItem(
        rule_id="C4",
        name="C4 · 仪容、围裙/私人物品存放与在岗行为（1.1–1.4）",
        category="【C】常态红线扫描（所有周次都跑）",
        severity="RED_LINE",
        detection_type="presence",
        requires_full_context=False,
        check_instruction=(
            "- 配饰：耳环、手表、戒指、手链、项链、假指甲、耳钉、**装饰发夹** —— 一律禁止\n"
            "- 着装：制服 + 发网 + 帽子 + 口罩 + 手套；制服围裙须洁净无污渍；制服上不得有线头、头发、有效期标签\n"
            "- 发网外不得露出散发\n"
            "- 不得穿亮色裤/短裤、露趾鞋\n"
            "- 🔴 **在顾客面吧台或厨房使用手机处理非工作事务**\n"
            "- 🔴 **等客时倚靠台面（Leaning on the counter）**\n"
            "- 🟡 **【围裙与个人物品指定区域存放规范（Partner apron not kept at designated storage area）】**：\n"
            "  - 员工在后厨或吧台**脱下工作围裙（apron）后，必须挂放在指定的围裙/员工物品存放区（designated storage area）**；**严禁将脱下的围裙随手放置在操作台面、设备台面、货架、矮凳/圆凳或角落里**！\n"
            "  - **【禁止被「玩手机」吞并硬性规则】**：若同一名员工先后发生了「**脱下围裙未挂入指定存放区（随手放在台面/圆凳/角落）**」与「**坐下或站在后厨/吧台看手机**」两个违规行为，**必须拆分为两条独立的 `C4` 违规记录分别输出**——第 1 条专门报告「员工脱下围裙后未放回指定存放区（apron not kept at designated storage area）」，第 2 条专门报告「在工作区使用私人手机（`RED_LINE`）」，严禁把脱围裙乱放吞并进玩手机的单条记录里！\n"
            "- 个人物品未放指定区域；个人食品饮料放在生产区"
        ),
        pass_criteria="无任何禁戴配饰，制服/发网/帽子/口罩/手套规范齐全，脱下的围裙与个人物品均存放在指定存放区（不乱丢在台面或凳子上），不在吧台或厨房玩手机，不倚靠台面",
        fail_criteria="佩戴手表/戒指/耳钉/装饰发夹、未戴帽或口罩下滑露散发、脱下围裙未放在指定存放区（Partner apron not kept at designated storage area，须独立成行上报）、吧台或后厨使用私人手机（红线）、等客时倚靠台面（红线）、个人饮料放生产区",
    ),
    SopRuleItem(
        rule_id="C5",
        name="C5 · 垃圾与地面（4.3 / 4.4）",
        category="【C】常态红线扫描（所有周次都跑）",
        severity="RED_LINE",
        detection_type="presence",
        requires_full_context=True,
        check_instruction=(
            "- 垃圾桶盖用后未盖 / 桶盖缺失或损坏\n"
            "- 🔴 **垃圾溢满超过 15 分钟**\n"
            "- 未使用脚踏式垃圾桶 / 踏板失灵 / 未套垃圾袋\n"
            "- 用水槽、纸箱、无标签食品容器当垃圾容器\n"
            "- **临时垃圾袋未扎口**\n"
            "- 备餐区 / 吧台地面积水或垃圾**超过 15 分钟**未清理"
        ),
        pass_criteria="脚踏垃圾桶带盖闭合、套袋规范、临时垃圾袋扎口、无垃圾溢满或地面积水超时",
        fail_criteria="垃圾桶未盖盖、垃圾溢满超15分钟（红线）、地面垃圾或积水超15分钟未清理、临时垃圾袋未扎口",
    ),
    SopRuleItem(
        rule_id="C6",
        name="C6 · 设备与器具（6.x / 7.x / 8.x）",
        category="【C】常态红线扫描（所有周次都跑）",
        severity="NORMAL",
        detection_type="presence",
        requires_full_context=False,
        check_instruction=(
            "- 自动泡茶机：茶桶底部有可见余水 / 茶网未沥干 / **热水口与卡扣冲泡前后未擦拭** / 冲泡流程不符 SOP"
            "（出茶入冰、30 秒内搅拌单独按 C8 核查，不在 C6 重复报）\n"
            "- 冷藏门：安装不规范（错轨、未随手关闭）、有缝隙漏冷\n"
            "- 搅拌机存放时不得密封\n"
            "- 蒸汽机：蒸汽棒**使用前后都要放气**；用后擦净无堵塞\n"
            "- 混合机（Mixer）：搅拌杆**使用前后都要清洁**\n"
            "- 茶具（茶桶、茶网、吧勺、滤器）冲泡前须洁净；茶桶与滤袋须**拆开分别清洗**\n"
            "- 开封茶叶袋须挤出空气并密封\n"
            "- 冷藏物料在常温放置**超过 15 分钟**\n"
            "- 未使用电子秤称量食材\n"
            "- 未做温度计晨间校准 / 手冲未测温 / 泡茶机温度水量未核对或造假"
        ),
        pass_criteria="泡茶机/蒸汽机/混合机使用前后按SOP擦拭放气清洁，茶桶无余水，冷藏门随手关严，食材上电子秤称量",
        fail_criteria="热水口与卡扣未擦拭、茶桶有余水、茶桶与滤袋未拆开洗、冷藏门未随手关闭、冷藏物料常温超15分钟、未用电子秤",
    ),
    SopRuleItem(
        rule_id="C7",
        name="C7 · 顾客服务（9.x）",
        category="【C】常态红线扫描（所有周次都跑）",
        severity="RED_LINE",
        detection_type="absence",
        requires_full_context=True,
        check_instruction=(
            "- 🔴 顾客在区域内停留**超过 10 秒**未获主动招呼\n"
            "- 付款后未**双手**递交小票/找零；未引导顾客至等候区或座位区\n"
            "- 取餐时未核对小票或手机订单\n"
            "- 出品未密封加盖、方向不正、未配齐吸管/纸巾/汤匙"
        ),
        pass_criteria="顾客到店10秒内主动招呼，双手递交小票/找零，取餐核对订单，出品密封端正配齐吸管纸巾",
        fail_criteria="顾客停留超过10秒未获主动招呼（红线）、单手递交、取餐未核对订单、出品未密封或漏配吸管纸巾",
    ),
    SopRuleItem(
        rule_id="C8",
        name="C8 · 泡茶机出茶入冰与 30 秒内搅拌（6.2 Automated Tea Maker Machine）",
        category="【C】常态红线扫描（所有周次都跑）",
        severity="NORMAL",
        detection_type="absence",
        requires_full_context=True,
        check_instruction=(
            "客户 SOP 原文（6.2 Automated Tea Maker Machine · Filter Tea into an Ice-Prepared Container）：\n"
            "- Pour the filtered tea into a container with ice, prepared 2-3 minutes beforehand "
            "(but not more than 3 minutes in advance).\n"
            "- Stir the tea within 30 seconds of pouring to ensure even cooling. "
            "(30s from the machine prompt: “Tea is ready” / dripping stage)\n"
            "\n"
            "泡茶机每出一次茶都要单独核查、单独记录（同一段视频里出茶多次就逐次核查）：\n"
            "1. **先放冰、后倒茶**：过滤好的茶汤必须倒进（或滴入）**事先已经装好冰**的容器；先倒茶、后补冰 → 违规。\n"
            "2. **备冰 2–3 分钟**：从往该容器里加冰，到茶汤开始倒入（或开始滴入），间隔应为 2–3 分钟。"
            "**超过 3 分钟**（冰放太久）→ 违规；不足 2 分钟也不符合 SOP 的 2–3 分钟要求，同样上报并写明实际间隔。"
            "加冰发生在本段开始之前：接力摘要里有加冰时刻就接着算；没有 → 无法计时，"
            "这一项记 `OUT_OF_SCOPE`（不进违规表），不要猜时间。\n"
            "3. **30 秒内搅拌**：\n"
            "   - 计时起点 = 泡茶机提示「Tea is ready」/ 进入滴滤（dripping）阶段的那一刻。视频没有声音，听不到提示音，"
            "改用画面上**最早能看到**的信号：机器屏幕或指示灯显示完成、出茶口的茶汤从连续流出变成滴落、"
            "或员工开始把茶汤倒进装冰容器。\n"
            "   - 计时终点 = 员工第一次把长勺 / 搅拌棒 / 搅拌桨伸进容器里搅动茶汤。\n"
            "   - 容器一直在画面里、起点后超过 30 秒还没开始搅拌 → `CONFIRMED`（临界值从严）；"
            "30 秒内容器被端出画面或被挡住、看不到有没有搅拌 → `UNVERIFIED · 应做未见`。\n"
            "   - 起点在本段开始之前：接力摘要里有起点（且写明还没搅拌）就接着计时；没有 → 看不到起点，"
            "也看不到开头之前是否已经搅过，这一项记 `OUT_OF_SCOPE`（不进违规表），不要猜。\n"
            "4. 证据里写清：起点时刻（OSD）和依据（看到的是哪个信号）、开始搅拌时刻（或「未见搅拌」）、实际间隔秒数、操作员工。\n"
            "5. 本段结尾还没闭环的计时（已加冰还没倒茶、已出茶还没搅拌）写进接力摘要，注明 OSD 时刻和是哪个容器。"
        ),
        pass_criteria=(
            "过滤茶汤倒进提前 2–3 分钟（不超过 3 分钟）装好冰的容器；"
            "从泡茶机提示「Tea is ready」/ 滴滤阶段起 30 秒内开始搅拌"
        ),
        fail_criteria=(
            "先倒茶后加冰；备冰到倒茶超过 3 分钟或不足 2 分钟；"
            "泡茶机提示「Tea is ready」/ 滴滤阶段起超过 30 秒才搅拌或未见搅拌"
        ),
    ),
    # ───────────────────────── 【D】运营与人效（Week 3 主模块） ─────────────────────────
    SopRuleItem(
        rule_id="D1",
        name="D1 · 有效出杯计数",
        category="【D】运营与人效（Week 3 主模块）",
        severity="NORMAL",
        detection_type="presence",
        requires_full_context=True,
        check_instruction=(
            "一杯有效产出必须在画面中完整跨越四工位：\n"
            "`Station 1 贴标 + AI 机扫码` → `Station 2 加冰 + 摇混机混制` → "
            "`Station 3 倒入客用杯（热饮含加热步骤）` → `Station 4 封口 + 出餐`\n"
            "- 只完成部分工位 → 单独记「未闭环 N 杯」，不计入总数\n"
            "- 单一注水或单一封口 **不计为一杯**"
        ),
        pass_criteria="完整跨越 Station 1→2→3→4 四工位计为有效出杯",
        fail_criteria="未跨越完整四工位（记入未闭环杯数，不计入有效出杯总数）",
    ),
    SopRuleItem(
        rule_id="D2",
        name="D2 · IPLH 计算（Week 3 必交）",
        category="【D】运营与人效（Week 3 主模块）",
        severity="NORMAL",
        detection_type="presence",
        requires_full_context=True,
        check_instruction=(
            "```\n"
            "IPLH (Items Per Labour Hour) = 有效出杯总数 ÷ 实际在岗人时\n"
            "实际在岗人时 = Σ(每位员工在画面内的在岗时长，换算为小时)\n"
            "```\n"
            "输出：**总 IPLH** + **各员工个人小时出杯率**。样本不足 1 小时按线性折算并注明。"
        ),
        pass_criteria="准确输出总 IPLH 与各员工个人小时出杯率",
        fail_criteria="未按公式折算实际在岗人时与有效出杯数",
    ),
    SopRuleItem(
        rule_id="D3",
        name="D3 · 人力投放",
        category="【D】运营与人效（Week 3 主模块）",
        severity="NORMAL",
        detection_type="presence",
        requires_full_context=True,
        check_instruction=(
            "1. 判定时段（开店 / 高峰 13:00–13:30 / 平峰 / 闭店）\n"
            "2. 输出在岗人数随时间的变化\n"
            "3. 判断异常：高峰期人手不足导致积压 / 平峰闭店期人力冗余闲置"
        ),
        pass_criteria="人力排班与时段客流匹配",
        fail_criteria="高峰期人手不足导致订单积压，或平峰/闭店期人力冗余闲置",
    ),
    SopRuleItem(
        rule_id="D4",
        name="D4 · 出品规范抽查（参数表 8.4，至少 3 杯）",
        category="【D】运营与人效（Week 3 主模块）",
        severity="NORMAL",
        detection_type="presence",
        requires_full_context=True,
        check_instruction=(
            "- 是否按配方 SOP 制作\n"
            "- 量杯与雪克杯使用是否规范\n"
            "- **加冰与配料顺序是否正确**\n"
            "- 是否完全倒净（刮除多余泡沫后，雪克杯内液体与果肉须全部倒入成品杯）"
        ),
        pass_criteria="抽查≥3杯均按配方SOP制作、量杯雪克杯规范、加冰配料顺序正确、刮泡并完全倒净",
        fail_criteria="加冰与配料顺序错误、未使用量杯、未刮除多余泡沫或雪克杯内液体果肉未完全倒净",
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


DEFAULT_LAYER1_TEMPLATE: str = """# 角色
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

## 原则六：一行为一记录与复合违规正交拆解（严禁高显著性事件吞并伴随违规）
1. **同一因果链的多前置触发动作全量枚举（防高显著性动作掩蔽瞬时微动作）**：当某条「前置触发 → 缺失必做动作 → 后续操作」类型的 SOP 规则在同一监视时间窗内先后发生多个前置触发动作时，必须在 `evidence` 中**按时间顺序把观察到的全部前置触发动作及各自时间戳逐一列明**，严禁只写持续时间最长、视觉最显眼的单一动作而漏写前后 1–2 秒内的瞬时微动作。
2. **同人并发/连续多项独立违规的逐条拆行（一行为一记录）**：当同一人员在同一时段先后或同时做出两个不同性质的违规动作，或先后多次重复触犯同一条计次规则时，**每一次独立违规行为必须单独生成一条 `Finding` 记录分别输出**，严禁合并成单条记录导致伴随违规被吞没。
3. **同一作业过程的多维度正交核查**：当一段连续作业过程同时涉及 `# 步骤 3：SOP 检查清单` 中的多个不同条款维度（如操作时序、时间阈值、工具/耗材合规性、空间表面覆盖完整性、必做工序缺失等）时，必须**按各条款编号逐维度正交核对并分别独立生成 `Finding`**，严禁因已上报某一维度的违规就省略同一时间段内另一维度的违规。

---

# 步骤 0：时间轴校准
1. 在视频前 5 秒找一帧画面角落（**通常位于左上角**，部分机位在右上角或底部）OSD 水印时间戳最清晰的画面，逐字读出 `DD-MM-YYYY HH:MM:SS`（或 `YYYY-MM-DD HH:MM:SS`）。
2. 点阵字体易混位（1/4、1/7、3/8、5/6、0/8、6/8）必须二次确认后写出。
3. 以该锚点 + 相对播放时长推算全片时间，**禁止分钟级以上非线性跳变**。
4. 无时间戳时用 `T+00:00:00` 相对计时并注明。
5. 判定本段所属时段：`开店 / 营业中 / 高峰(约13:00–13:30) / 平峰 / 闭店`，以及区域：`前吧台(Front Bar/POS/Pick-Up)` 或 `后厨(Back Kitchen)`。
6. **跨切片/跨视频状态接力校准**：若收到【前序分段未闭环状态接力摘要】，必须先核对本段首帧 OSD 时间及机位场景是否与前序摘要紧邻连续（同一机位且时间间隔 ≤5 分钟）；若连续则无缝接续跨段计时（如消毒液 5 分钟/10 分钟静置或 5 分钟洗手监视窗），若 OSD 时间发生跨时段跳变或机位不同，则忽略前序残留状态，防止跨时段误判。

# 步骤 1：人员建档与多人独立追踪（必须穷尽，防显著性掩蔽）
为画面中**每一位**人员分配稳定 ID，基于不随时间变化的特征：
`员工A-男-短发-戴帽-出杯位`
- **必须主动扫描画面边缘、背景纵深、镜面反射、门口进出**，不得只盯主操作区。
- 若某人只出现几秒也必须建档，标注 `短暂出现`。
- **状态持续原则**：手套/围裙/帽子一旦确认穿戴，在未看到明确「脱除动作」前，默认仍在穿戴。
- **多人同屏注意力解耦（Anti-Attention-Masking）**：当画面中同时存在 ≥2 名人员时，**严禁被单一高显著性动作（如某人的大幅度肢体动作、长时间停留或正在发生的明显违规）吸走全部注意力**。必须按空间工位（前吧台、水槽区、设备区、通道区）拆分，为每位在场人员（`员工A`、`员工B`、`员工C`…）维护**彼此独立的动作时间线**，逐人独立核查其手部接触与操作流程闭环。
- 建档完成后自查一次：`我是否遗漏了任何在画面中出现过的人？`

---

# 步骤 2：视觉判定规则（高召回版）

## 2.1 接触判定
- **明确接触**（手指抓握/按压/承重形变）→ `CONFIRMED`
- **疑似接触**（手与物体像素重叠，但无法确认是否触碰）→ **`SUSPECTED`，仍需上报**
- **明确未接触**（隔空倾倒，可清楚看到手与物体有间隙）→ 不报，但在轨迹中记录该动作

> 注意：与旧版不同，**疑似接触不再豁免**，一律上报交人工。

## 2.2 状态变化溯源
发现某物状态已变（抽屉开了、面板拆了、垃圾袋换了）却没看到过程：
1. 向前回溯查找致因动作；
2. 找到 → 归因到具体人员，`CONFIRMED`；
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
2. **瞬时微动作与工序首尾节点回扫**：
   - 画面中是否有 1–2 秒内完成的瞬时手部接触、物品/护具随手放置等微动作被略过或被长耗时主事件吞并？
   - 对每一项多步骤标准工序，逐次回看其**起始前 3 秒（首步先后顺序是否正确）**、**中间核心动作净时长（扣除准备与收尾动作后是否达标）**以及**结束尾 5 秒（收尾动作是否完整执行）**。若同一人员先后多次执行同一工序且均不达标，是否已拆分为多条独立记录？
3. **遮挡时段**：是否有员工被设备/立柱遮挡超过 10 秒？该时段标记为盲区并上报。
4. **多人并发与同秒掩蔽回扫（关键）**：凡是在某个时间段 `[T1, T2]` 检出了某名人员的违规事件，必须强制回到同一时间段 `[T1, T2]`，**故意移开视线忽略该名已报错人员，专门逐一检查画面内其他所有在场人员在同一时间段内的手部动作与操作步骤**，严禁因紧盯一人而漏掉同秒另一人的违规！
5. **单事件吸干与多维正交漏报回扫（关键）**：逐一审视已检出的每条违规记录，对照 `# 步骤 3：SOP 检查清单` 核查：① 同一人员在该时间窗前后是否还伴随发生了其他独立规则的违规？② 同一连续作业过程（如设备拆洗消毒）是否同时触犯了清单中的多个正交维度（如各独立部件的时间阈值、容器/耗材颜色与取用来源、部件正反面与四周侧沿的空间覆盖完整性、必做收尾工序等）？凡触犯多个条款维度的，必须按各自 `rule_id` 分别独立输出 `Finding`，严禁一条记录吸干！
6. **设备与容器状态**：受控设备门盖、容器盖、防护设施在全片是否始终处于 SOP 要求的应有状态？
7. **时间跳跃**：时间轴是否有非线性跳变？若有，说明对应片段。

对自查中发现的任何新线索，追加到违规表中。

---

# 输出结构与生成顺序（严格遵循 JSON 字段顺序，先记人、再记事、后自查、最后定性）

你必须严格按照以下 JSON 字段的先后顺序生成内容，**严禁跳过前置轨迹记录与自查步骤直接跳写违规表**：

1. **`calibrated_wall_clock_start`（步骤 0 校准信息）**：写出首帧锚定帧的 OSD 绝对时间（`DD-MM-YYYY HH:MM:SS`）、易混数字复核结果、时段与区域判定。
2. **`people`（步骤 1 人员档案）**：穷尽列出本段内出现过的每一位人员 ID、体貌/穿戴特征及首次—最后出现时间（含短暂路过人员）。
3. **`person_trajectories`（逐人独立事件轨迹：事件驱动 + 合并心跳）**：
   - **必须与 `people` 列表一一对应（每人独立一条轨迹字符串）**，严禁把所有人的动作混成一条时间线导致次要工位人员被遗漏！
   - 每位人员的轨迹按时间顺序记录：`员工ID [所在工位]: HH:MM:SS (播放器 MM:SS) 具体动作、手部接触物体表面与工具状态 -> ...`。
   - 凡触发高危污染接触、多步骤连续工序（含起始前 3 秒、持续净时长、结束尾 5 秒）或容器/工具药剂调配施用，必须在对应人员轨迹中完整记下起止时间戳与耗时。
   - 对于连续几分钟无异常的常规合规操作时段，可合并输出心跳（例如 `10:00:00-10:05:00 [HEARTBEAT] 正常在岗`），不必机械化强制每 60 秒写一条以节约输出长度。
4. **`self_check_notes`（步骤 4 漏检自查结论）**：
   - 在输出最终违规表之前，必须对照上方每位人员的 `person_trajectories` 逐条回答 `# 步骤 4：漏检自查` 的核对问题（在此记录中请统一用中文「违规表」，严禁书写英文键名）。
   - 重点核查：当某名人员在 `[T1, T2]` 发生显眼事件时，其他在场人员在同一时段 `[T1, T2]` 的轨迹中是否存在未洗手接触、工序时长不足、少做收尾步骤或工具混用等漏检线索。
5. **违规汇总数组（主扫描 + 漏检自查全量合集）**：
   - 将主扫描与 `self_check_notes` 自查中发现的全部 `CONFIRMED`、`SUSPECTED`、`UNVERIFIED` 记录逐条输出为独立的违规记录对象。
6. **`carryover_state_summary`（跨段状态接力摘要）**：记录本段结尾 OSD 时间及所有尚未闭环的跨段监视窗、静置计时起点与工具药剂属性。
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
    """Parses 9-column Google Sheet rows (`A1:I100`, skipping header row) into `SopRuleItem` list."""
    if len(rows) <= 1:
        return []
    rules: List[SopRuleItem] = []
    for row in rows[1:]:
        if not row or not row[0].strip():
            continue
        padded = row + [""] * max(0, 9 - len(row))
        rules.append(
            SopRuleItem(
                rule_id=padded[0].strip(),
                name=padded[1].strip() or padded[0].strip(),
                category=padded[2].strip() or "SOP",
                severity=padded[3].strip() or "RED_LINE",
                detection_type=padded[4].strip() or "presence",
                requires_full_context=padded[5].strip().upper() in {"TRUE", "1", "YES"},
                check_instruction=padded[6].strip(),
                pass_criteria=padded[7].strip(),
                fail_criteria=padded[8].strip(),
            )
        )
    return rules


def render_v25_system_instruction(
    prompt_version: str,
    rules: List[SopRuleItem],
    visual_scan_targets: Optional[List[VisualScanTarget]] = None,
    layer1_template: Optional[str] = None,
) -> str:
    """Renders 100% verbatim the canonical `CHAGEE_CCTV_Cloud_Auditor_Prompt_v1.0` with dynamic Sheet rules."""
    # Group rules by category (`【A】...`, `【B】...`, `【C】...`, `【D】...`) to preserve exact markdown hierarchy
    grouped_blocks: List[str] = []
    current_category = None
    for r in rules:
        if r.category != current_category:
            current_category = r.category
            grouped_blocks.append(f"\n## {current_category}\n")
            if "【C】" in current_category:
                grouped_blocks.append(
                    "> 以下每一项都必须在报告中显式给出「已核查·未发现」或具体违规，**不得省略**。\n"
                )
        grouped_blocks.append(f"### {r.name}\n{r.check_instruction}\n")

    step3_checklist_md = "\n".join(grouped_blocks).strip()
    tpl = layer1_template if layer1_template is not None else DEFAULT_LAYER1_TEMPLATE
    if "{prompt_version}" not in tpl or "{step3_checklist_md}" not in tpl:
        raise ValueError(
            "layer1_template must contain both {prompt_version} and {step3_checklist_md} placeholders"
        )
    return tpl.replace("{prompt_version}", prompt_version).replace(
        "{step3_checklist_md}", step3_checklist_md
    )


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

    TAB0_TITLE = "Tab0_版本总控与回滚开关"

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
        tabs = load_gcs_sop_tabs(raw, sheet_id)
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

        def _fetch_via_api() -> List[List[str]]:
            resp = (
                self._sheets_service()
                .spreadsheets()
                .values()
                .get(spreadsheetId=sheet_id, range=a1_range)
                .execute(num_retries=3)
            )
            return [[str(c) for c in r] for r in resp.get("values", [])]

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
            if self.TAB0_TITLE not in tabs:
                raise ValueError(f"SOP workbook {sheet_id} has no tab {self.TAB0_TITLE!r}")
            rows = tabs[self.TAB0_TITLE]
        else:
            rows = await self._read_range(sheet_id, f"{self.TAB0_TITLE}!A1:D15")
        pointers: dict[str, str] = {}
        for row in rows[1:]:
            if len(row) >= 2 and row[0].strip():
                pointers[row[0].strip()] = row[1].strip()
        return pointers

    async def read_prompt_tab_rules(self, sheet_id: str, tab_name: str) -> List[SopRuleItem]:
        if sheet_id.startswith("gs://"):
            rows = (await self._read_gcs_workbook_tabs(sheet_id)).get(tab_name, [])
        else:
            rows = await self._read_range(sheet_id, f"{tab_name}!A1:I100")
        return parse_rules_from_sheet_rows(rows)

    async def append_available_models(self, sheet_id: str, new_models: List[str]) -> None:
        """Appends newly discovered models to Tab 0's candidate row; never switches the active model."""
        if not new_models or sheet_id.startswith("gs://"):
            return  # a GCS workbook is customer-owned and read-only for the runtime
        catalog_note = " | ".join(f"{m} (🆕 新模型可用待测试)" for m in new_models)

        def _append() -> None:
            self._sheets_service().spreadsheets().values().append(
                spreadsheetId=sheet_id,
                range=f"'{self.TAB0_TITLE}'!A1",
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
                body={
                    "values": [
                        [
                            "Candidate_Models_Discovered",
                            catalog_note,
                            "仅追加至候选列表（未自动切换 Active_Model_Version）",
                        ]
                    ]
                },
            ).execute(num_retries=3)

        try:
            await asyncio.to_thread(_append)
        except Exception as exc:
            logger.warning("Failed to append candidate models to %s: %s", sheet_id, exc)


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
            return effective_fallback, "⚠️ [Tab 0] 未填写模型版本，已自动使用保底模型 " + effective_fallback

        if cleaned.upper() == "AUTO_LATEST_FLASH":
            warning = (
                f"⚠️ 已拦截 AUTO_LATEST_FLASH 自动盲切模式（防止生产准确率静默漂移），"
                f"当前锁定使用受控保底模型 {effective_fallback}"
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
                f"⚠️ 配置表指定的模型 '{cleaned}' 探活不可用 ({exc.__class__.__name__})，"
                f"本次已自动回退使用保底模型 {chosen_fallback}"
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
                logger.info("MASTER_PROMPT_SHEET_ID 未配置（Zero-GWS 模式），将直接使用内置 V25 基准规则库。")
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
