"""Builds the r02 candidate snapshot (Layer 2 only, parent r00).

Why r02 looks like this (diagnosis of r00/r01 dev misses, see ledger):
- The 6 dev misses are already spelled out in the r00 rules (A1 lists hair/spectacles/spit guard,
  A3 lists soap-before-wet and not drying, B3 lists multipurpose chemicals, C8 the 30 s stir).
  So missing rule text is not the cause.
- In the missed windows the model's findings are dominated by repeated C4 items (no hat/hairnet,
  phone use) about one person, while a second person's short sink or contact actions go
  unreported (e.g. R04/R05 happen while the other partner is on the phone).
- r01 tried to fix this with generic Layer 1 scanning instructions and lost ground (35.5%).

Hypothesis: cut C4 output noise (one grooming row per person per clip, one phone row per
continuous episode) and make every sink visit / chemical application a per-person independent
check, so attention is spent on short hand actions instead of repeated grooming/phone rows.
Layer 1 is unchanged (single-layer mutation rule). Wording stays SOP-generic: no store, time or
clip-specific details.
"""
from __future__ import annotations

import json
from pathlib import Path

from eval.tune_loop import DEFAULT_ROUNDS_DIR, create_round_snapshot, deserialize_rules_json

C4_ADD = (
    "\n- **【输出去重，防止刷屏】**：\n"
    "  - 仪容着装类（未戴帽/发网/口罩、配饰）：同一员工在本段内**只报 1 条**，证据 1 句话写清缺什么即可，不要每隔几十秒重复上报。\n"
    "  - 使用私人手机：同一员工**一次连续使用只报 1 条**（写起止时刻）；中间放下超过 60 秒后再拿起才算新的一次。\n"
    "  - 这两类问题报完后，**注意力要回到画面里其他员工的双手动作**（水槽、吧台、设备接触），不得因为有人在玩手机或没戴帽就停止扫描其他人。"
)

A3_ADD = (
    "\n- **【每一次水槽访问都要单独核查，按人计】**：画面中**任何一名员工**（不只是当前主要关注的那名）只要走到水槽前，"
    "就要按上表 5 步独立核查并写出该员工的起止时刻；即使同一时段另一名员工正在玩手机、搬货或有其他违规，也不得跳过。"
    "水槽被遮挡或员工背对镜头看不清步骤 → 记 `UNVERIFIED · 洗手步骤不可见`，不要默认合规。"
)

A1_ADD = (
    "\n- **【按人独立追踪】**：画面中有多名员工时，每个人的污染源动作分别追踪、分别开监视窗；"
    "一名员工的显眼违规（如玩手机）不能代替或掩盖另一名员工在同一时段摸头发/眼镜/防飞沫挡板后直接接触设备或食品的动作。"
)

B3_ADD = (
    "\n- **【先查来源容器，不只看颜色】**：凡是看到员工往制冰机任何部件（外盖门板、布水管、挡水帘、内腔）上喷、倒、涂、刷液体，"
    "都要**回看这液体是从哪个容器取的**（喷壶/瓶/罐/桶），并描述容器外观；来源不是标准 Sanitiser 喷壶或透明量桶的 → 按 B3 上报；"
    "看不清来源 → `SUSPECTED · 化学品身份不明`。"
)


def build(rounds_dir: Path = DEFAULT_ROUNDS_DIR, overwrite: bool = False) -> Path:
    parent = rounds_dir / "r00"
    layer1 = (parent / "layer1_template.md").read_text(encoding="utf-8")
    rules = deserialize_rules_json(json.loads((parent / "layer2_rules.json").read_text(encoding="utf-8")))
    add = {"C4": C4_ADD, "A3": A3_ADD, "A1": A1_ADD, "B3": B3_ADD}
    new_rules = []
    for r in rules:
        if r.rule_id in add:
            r = r.model_copy(update={"check_instruction": r.check_instruction + add[r.rule_id]})
        new_rules.append(r)
    return create_round_snapshot(
        rounds_dir=rounds_dir,
        round_id="r02",
        parent_round_id="r00",
        layer1_template=layer1,
        layer2_rules=new_rules,
        hypothesis=(
            "Layer 2 attention re-budget (parent r00; r01 Layer 1 rejected). Dev-miss rules already exist in r00, "
            "misses coincide with repeated C4 grooming/phone rows about one partner while another partner's short "
            "sink/contact actions go unreported. Changes: C4 dedup (1 grooming row per person per clip, 1 phone row per "
            "continuous episode, then resume scanning others); A3 every sink visit by any partner checked independently; "
            "A1 per-person independent contamination tracking; B3 trace source container of any liquid applied to ice "
            "maker parts instead of relying on bottle colour."
        ),
        target_dev_misses=["R02", "R03", "R04", "R05", "R20"],
        overwrite=overwrite,
    )


if __name__ == "__main__":
    print(build())
