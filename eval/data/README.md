# eval/data（不随仓库分发）

这个目录放**调优工具用的私有评测数据**：客户人工稽核结果冻结成的"标准答案"（golden dataset）和历次跑批的原始结果。里面有门店名、Drive 视频 ID 和人工稽核原文，所以没有提交到 Git（见根目录 `.gitignore`）。

没有这些数据时，`eval/tests` 里依赖它们的 10 个用例会被自动跳过（见 `eval/tests/conftest.py`），其余用例照常跑。

## 怎么生成自己的 golden 数据

1. 把人工稽核结果表读成二维 JSON（第 0 行是表头，列依次为 Focus / Outlet / Outlet Name / Audit Clause / Findings / 视频文件名）。
2. 用一次正式稽核任务的 job JSON（`gs://<staging bucket>/jobs/<user>/<job>.json`，里面的 `preflight_report.videos` 是 Drive 文件名到 file_id 的对照表）做映射：

```bash
python3 -m eval.build_dataset --manual-json manual_audit_result_raw.json \
  --job job_a.json --job job_b.json --source-sheet-id <人工稽核表ID> \
  --out eval/data/golden_v1.jsonl
```

`build_dataset.py` 里的 `SOURCE_SHEET_ID`、`EXPECTED_ROWS`、`DEV_GROUPS`、`TEXT_COMPOUND_PARTS` 是第一批验证数据的设置，换数据集时要一起改。

## golden_v1.jsonl 格式

一行一条人工标注的违规：

| 字段 | 类型 | 含义 |
|---|---|---|
| `item_id` | str | 条目编号，如 `R02` |
| `sheet_row` | int | 在人工稽核表里的行号 |
| `focus` | str | 稽核场景，如 `Handwashing Monitoring` |
| `outlet_no` / `outlet_name` | str | 门店编号 / 名称 |
| `audit_clause` | str | 条款 |
| `finding_verbatim` | str | 人工稽核原文 |
| `video_filenames` / `video_file_ids` | list[str] | 对应视频的 Drive 文件名 / file_id |
| `parts` | list[obj] | 一行里有几个独立违规就拆成几个 part：`{part_id, description, osd_times: ["HH:MM:SS", ...]}` |
| `split` | str | `dev`（调优时可以看）或 `holdout`（只用来验收） |
| `source_sheet_id` / `source_sha256` | str | 来源表 ID 和原始数据的哈希，防止标准答案被悄悄改动 |

示例（虚构数据）：

```json
{"item_id": "R01", "sheet_row": 2, "focus": "Handwashing Monitoring", "outlet_no": "S001", "outlet_name": "Demo Store", "audit_clause": "1.5 Handwashing and Sanitation Standard", "finding_verbatim": "Footage 1: 080518 Partner not dry hand with hand towel after handwashing", "video_filenames": ["Footage 1.mp4"], "video_file_ids": ["<drive-file-id>"], "parts": [{"part_id": "R01", "description": "Footage 1: 080518 Partner not dry hand with hand towel after handwashing", "osd_times": ["08:05:18"]}], "split": "dev", "source_sheet_id": "<sheet-id>", "source_sha256": "<sha256>"}
```
