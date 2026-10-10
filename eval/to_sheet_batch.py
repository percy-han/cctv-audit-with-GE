"""Render a score.json into a gsheets `mutate batch` payload (one new tab).

Usage:
  python eval/to_sheet_batch.py <score.json> <tab title> > batch.json
  gsheets mutate add-sheet <SHEET_ID> --title '<tab title>'
  gsheets mutate batch <SHEET_ID> -f batch.json
"""

import json
import sys

MARK = {1.0: "✓ 命中 (1)", 0.5: "◐ 半对 (0.5)", 0.0: "✗ 漏掉 (0)"}


def rows_for(rep: dict) -> list[list[str]]:
    r, ad = rep["recall"], rep["alert_density"]
    out = [
        [f"尺子打分：{rep['run_label']}"],
        [f"加权召回（{r['all']['rows']} 行）= {r['all']['points']:g} / {r['all']['rows']} = {r['all']['recall']:.1%}",
         f"开卷 dev = {r['dev']['points']:g} / {r['dev']['rows']} = {r['dev']['recall']:.1%}",
         f"检查 holdout = {r['holdout']['points']:g} / {r['holdout']['rows']} = {r['holdout']['recall']:.1%}",
         f"告警密度 = 平均 {ad['mean_per_clip']:.1f} 条/段，最多 {ad['max_per_clip']} 条/段（{ad['findings']} 条 / {ad['clips']} 段）"],
        [f"裁判 = {rep['judge_model']}（Vertex AI Eval SDK LLMMetric，每题 {rep.get('judge_passes', 1)} 次投票取中位）；"
         f"代码硬筛 = 同一视频 + OSD 相差 ≤{rep['window_sec']} 秒；打分时间 {rep['scored_at']}"],
        [],
        ["表格行号", "开卷/检查", "主题", "门店", "客户标注原文", "对应视频（完整文件名）",
         "判分", "裁判理由（逐件）", "命中的 AI 条目", "窗口外近邻（仅供校准，不计分）", "你的判断（请填：同意 / 应为 1 / 应为 0.5 / 应为 0）"],
    ]
    for it in rep["items"]:
        reasons = "\n".join(f"{p['part_id']}: {p['explanation']}" for p in it["parts"])
        hits = "\n".join(f"{m['finding_id']} OSD {m['osd']} [{m['rule_id']}] {m['evidence']}"
                         for m in it["matched"]) or "—"
        near = "\n".join(n for p in it["parts"] for n in p.get("near_misses", [])) or "—"
        out.append([
            str(it["sheet_row"]), "开卷" if it["split"] == "dev" else "检查",
            it["focus"], it["outlet_name"], it["finding_verbatim"],
            "\n".join(it["video_filenames"]), MARK.get(it["score"], str(it["score"])),
            reasons, hits, near, "",
        ])
    return out


def main() -> int:
    rep = json.load(open(sys.argv[1], encoding="utf-8"))
    tab = sys.argv[2]
    rows = rows_for(rep)
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    last_col = chr(ord("A") + width - 1)
    json.dump([{"op": "write", "range": f"'{tab}'!A1:{last_col}{len(rows)}", "data": rows}],
              sys.stdout, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
