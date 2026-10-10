# eval/data（不随仓库分发）— 黄金集格式与配置

这个目录放**调优评测用的私有数据**：把客户人工稽核结果冻结成的"标准答案"（黄金集，golden set）、它的配置文件，以及历次跑批的原始结果。里面有门店名、Drive 视频 ID 和人工稽核原文，所以不提交到 Git（见根目录 `.gitignore`）。

没有这些数据时，`eval/tests` 里依赖它们的用例会被自动跳过（见 `eval/tests/conftest.py`），其余用例照常跑（`eval/tests/test_golden_agnostic.py` 全部使用虚构数据）。

## 1. 评测流水线不写死任何黄金集内容

题目编号、题数、dev/holdout 划分、SOP 大类、门店、视频、文件夹、"稳定基线题"全部来自黄金集，不写死在代码里。黄金集有两种来源，只能配置其中一种；两个都设时，评测在调用任何模型之前就报错退出。换一套黄金集只改配置，不改代码：

| 位置 | 来源 A：客户的标注表（推荐） | 来源 B：JSONL 文件 |
| :-- | :-- | :-- |
| Terraform（`<env>.tfvars`） | `eval_golden_sheet_id = "<标注表 ID>"` | `eval_golden_uri = "gs://<私有桶>/<路径>/<name>.jsonl"` |
| Cloud Build（`eval/cloudbuild_round.yaml`） | `_GOLDEN_SHEET_ID=$(terraform output -raw eval_golden_sheet_id)` → `EVAL_GOLDEN_SHEET_ID` | `_GOLDEN_URI=$(terraform output -raw eval_golden_uri)` → `EVAL_GOLDEN_URI` |
| 本地运行 | `--golden-sheet-id <ID>` | `--golden <路径或 gs://...>` |

> [!IMPORTANT]
> 本仓库**不附带**黄金集。两个变量都为空时，评测会去找 `eval/data/golden_v1.jsonl`，找不到就直接报错退出，并提示怎么设置；这时还没有调用任何模型，不产生费用。

黄金集是客户私有数据：不要放进公开仓库，也不要放进 staging bucket（那里的对象 30 天后自动删除，Terraform 会拒绝这种配置）。如果用 gs://，运行评测的 worker 服务账号需要有该对象的读权限。

## 1.1 来源 A：客户标注表 +「测评配置」页（`eval/golden_sheet.py`）

客户继续维护自己的标注页（例如 `Sheet1`，列为 Focus / Outlet / Outlet Name / Audit Clause / Findings / 对应的视频文件名称）。程序从不改动这一页。

表格放在和测评结果同一个 Drive 文件夹（`eval_results_folder_id`）里，再加一页「测评配置」，告诉评测怎么把标注页转成黄金集。每次评测实时读取这两页，并列出配置的 Drive 文件夹里的**全部**视频；没有标注的视频也算，因为它们计入告警密度。然后生成和第 3、4 节完全一样的 JSONL + manifest。

每次评测留下的记录：

- 结果目录里的 `golden_snapshot/`：实际使用的 `<name>.jsonl`、`<name>.manifest.json`、`golden_origin.json`；
- 结果 Sheet 里的「本次黄金集快照」页，概览页写明来源标注表的链接。

「测评配置」页格式（虚构示例）。A 列是分区标记，标记的下一行是该分区的表头；空行和以 `#` 开头的行会被忽略：

| A | B | C | D | E |
| :-- | :-- | :-- | :-- | :-- |
| `[基本设置]` | | | | |
| 项目 | 值 | | | |
| 黄金集名称 | golden_v2 | | | |
| 标注页范围 | Sheet1!A1:F80 | | | |
| 期望题数 | 25（可空；不符直接失败） | | | |
| 备注 | （可空；不影响版本号） | | | |
| `[视频文件夹]` | | | | |
| 分组ID | 门店 | 稽核重点 | Drive 文件夹 ID | 缓存任务 ID（可空，逗号分隔） |
| store-a-hw | Demo Store A | Handwashing Monitoring | `<drive-folder-id>` | |
| `[开发集分组]` | | | | |
| 稽核重点 | 门店 | | | |
| Handwashing Monitoring | Demo Store A | | | |
| `[复合题拆分]` | | | | |
| 表格行号 | 拆分描述 1 | 拆分描述 2 | | |
| 18 | did not wait 5 min | did not wipe the sides | | |
| `[逐题设置]` | | | | |
| 题号 | SOP 大类（可空） | 时间判定模式（POINT/WINDOW，可空） | 稳定基线题（是/否，可空） | |
| R02 | A_Handwashing | POINT | 是 | |

规则：

- **必填分区**：`[基本设置]`（黄金集名称、标注页范围）和 `[视频文件夹]`，其余分区都可以不写。
- **视频文件夹**：每一行是一个顺序任务，分组名为「门店 | 稽核重点」。
- **文件名必须完全一致**：标注里的视频文件名必须在这些文件夹里找得到，连空格和括号都要一样。同一个文件名在多个文件夹里出现也算错误。
- **题号**：为 `R<表格行号>`，和 `eval/build_dataset.py` 的编号规则相同。
- **逐题设置**：`[逐题设置]` 会写进 manifest 的 `item_overrides` / `stable_baseline_items`。
- **报错位置**：错误信息会指出具体单元格，例如 `测评配置!D12`。

Workspace 身份需要对表格和这些视频文件夹至少有「查看者」权限；运行 `init-config` 需要对表格有「编辑者」权限。

客户的日常流程：

1. 修改标注页（增删改行），或者往文件夹里加、删视频。
2. 下次评测自动使用新内容，并在评分标尺变化时生成新的 `golden_version`（见第 5 节）。

工具（以 Workspace 身份运行；Cloud Build 用 `eval/cloudbuild_golden_sheet.yaml`，以 worker 服务账号运行）：

```bash
python -m eval.golden_sheet check --sheet-id <ID> [--compare <上次的 score.json 或版本号>]   # 只读自检
python -m eval.golden_sheet init-config --sheet-id <ID> \
  --from-build-config <name>.build.json --manifest <name>.manifest.json [--dry-run] [--overwrite]
```

`init-config` 只写「测评配置」这一页；已经存在时，不加 `--overwrite` 会拒绝改动。

## 2. 用人工稽核表生成黄金集文件（来源 B）：`eval/build_dataset.py --config`

1. 把人工稽核结果表读成二维 JSON。第 0 行是表头，列依次为 Focus / Outlet / Outlet Name / Audit Clause / Findings / 视频文件名。
2. 准备一次正式稽核任务的 job JSON（`gs://<staging bucket>/jobs/<user>/<job>.json`），里面的 `preflight_report.videos` 是 Drive 文件名到 file_id 的对照表。
3. 写一份构建配置 `<name>.build.json`（下面是虚构示例），然后运行：

```bash
python3 -m eval.build_dataset --manual-json manual_audit_result_raw.json \
  --job job_a.json --job job_b.json --config eval/data/<name>.build.json \
  --out eval/data/<name>.jsonl
```

```json
{
  "source_sheet_id": "<人工稽核表ID>",
  "expected_rows": 7,
  "dev_groups": [["Handwashing Monitoring", "Demo Store A"]],
  "text_compound_parts": {
    "5": ["After spraying sanitiser, did not wait 5 minutes", "Did not wipe the sides thoroughly"]
  },
  "item_fields": {
    "R02": {"sop_category": "A_Handwashing", "temporal_mode": "POINT", "stable_baseline": true},
    "R05": {"sop_category": "B_IceMaker", "temporal_mode": "WINDOW"}
  }
}
```

配置里的每个键都可以不写：

- `dev_groups`：列出的 (Focus, Outlet Name) 组合归入 `dev`（调优时可以看），其余全部是 `holdout`（只用来验收）。
- `text_compound_parts`：同一个时间点里有多个独立违规的行，按表格行号拆成多个 part。带两个时间戳的行会自动拆分，不需要写在这里。
- `item_fields`：给某些题补充可选字段（见第 3 节），按 `R<表格行号>` 编号。
- `expected_rows`：行数不符时直接失败，防止读错表。

命令行的 `--source-sheet-id`、`--expected-rows`、`--dev-group "Focus|Outlet Name"` 可以覆盖配置里的同名设置。

## 3. `<name>.jsonl` 格式（一行一条人工标注）

| 字段 | 必填 | 含义 / 缺省时的行为 |
| :-- | :-- | :-- |
| `item_id` | 是 | 唯一编号，如 `R02`、`G01` |
| `split` | 是 | `dev` 或 `holdout`；召回率按全部 / dev / holdout 分别报告 |
| `outlet_name`、`focus` | 是 | 门店、稽核场景；用于"门店 × 场景"分组 |
| `finding_verbatim` | 是 | 人工稽核原文（给裁判模型和报表看） |
| `video_filenames`、`video_file_ids` | 是 | 等长列表；对应视频的 Drive 文件名和 file_id |
| `parts[]` | 是 | 至少一个：`part_id`（唯一）、`osd_times`（`HH:MM:SS` 列表，可为空，表示整段）、`description`（可选）。整题得分 = 各 part 平均 |
| `audit_clause` | 否 | 条款原文；没有 `sop_category` 时用它当分组名 |
| `sop_category` | 否 | 报表分组。缺省时依次取 `audit_clause` 原文、`focus`、`未分类`（结果里记为 `sop_category_source`） |
| `temporal_mode` | 否 | `POINT`（±20 秒，统计时间偏差）或 `WINDOW`（±60 秒，允许时间段包含）。可写在题上或 part 上。缺省时用代码里的关键词规则推断，结果里记为 `temporal_mode_source: heuristic`。新黄金集建议显式填写 |
| `stable_baseline` | 否 | `true` 表示这题在基线里稳定答对，任何候选提示词都不能让它退化（护栏） |
| `drive_folder_id` | 否 | 没有 manifest 的 `folders` 时，用它把视频分到同一个顺序任务里 |
| `sheet_row`、`outlet_no`、`source_sheet_id`、`source_sha256` | 否 | 来源信息，只做追溯 |

示例（虚构数据）：

```json
{"item_id": "R02", "sheet_row": 2, "split": "dev", "focus": "Handwashing Monitoring", "outlet_no": "S001", "outlet_name": "Demo Store A", "audit_clause": "1.5 Handwashing and Sanitation Standard", "sop_category": "A_Handwashing", "temporal_mode": "POINT", "stable_baseline": true, "finding_verbatim": "Footage 1: 080518 Partner not dry hand with hand towel after handwashing", "video_filenames": ["Footage 1.mp4"], "video_file_ids": ["<drive-file-id-1>"], "parts": [{"part_id": "R02", "description": "Partner not dry hand with hand towel after handwashing", "osd_times": ["08:05:18"]}], "source_sheet_id": "<sheet-id>", "source_sha256": "<sha256>"}
```

## 4. `<name>.manifest.json`（可选，和黄金集放在同一目录或同一 gs:// 前缀）

```json
{
  "stable_baseline_items": ["R02", "R07"],
  "item_overrides": {"R05": {"sop_category": "B_IceMaker", "temporal_mode": "WINDOW"}},
  "folders": [
    {
      "group_id": "store-a-handwash",
      "folder_id": "<drive-folder-id>",
      "label": "Demo Store A | Handwashing Monitoring",
      "videos": [
        {"file_id": "<drive-file-id-1>", "filename": "Footage 1.mp4", "duration_sec": 300.0, "width": 1920, "height": 1080},
        {"file_id": "<drive-file-id-2>", "filename": "Footage 2.mp4"}
      ]
    }
  ]
}
```

- **`folders`**：要跑的全部视频，按顺序任务分组，**包括没有标注的视频**。这些视频也计入告警密度和命中率，所以负样本要写进来。
  - `duration_sec`、`width`、`height` 可以不写，首次切片时会用 ffprobe 补上。
  - 不写 `folders` 时，只跑有标注的视频，按 `drive_folder_id`（没有则按"门店 | 场景"）分组。
  - 任何有标注的视频不在清单里，加载时直接报错。
- **`stable_baseline_items`**：稳定基线题，和题目上的 `stable_baseline: true` 合并。两处都没有时，护栏显示"未配置"，不会静默通过或失败。
- **`item_overrides`**：给已冻结、不想改动的黄金集补 `sop_category`、`temporal_mode`、`stable_baseline`。

## 5. `golden_version`：不同版本的召回率不可比

`golden_version = <name>@<sha256 前 10 位>`，只对**评分标尺**取哈希（方案 `ruler-v2`，记录为 `golden_version_scheme`）。

计入哈希（这些一改，版本号就变）：

- 黄金集中评分会读到的全部字段：题号、划分、门店/场景、条款、标注原文、视频、每个 part 的时间点和描述，以及可选的 sop_category / temporal_mode / stable_baseline / drive_folder_id；
- manifest 中每个文件夹的分组/名称及其视频集合（file_id + 文件名），以及 `item_overrides`、`stable_baseline_items`。

不计入哈希：

- 来源信息：`source_sha256`、`source_sheet_id`、`outlet_no`；
- 备注；
- 视频时长、大小、分辨率；
- Drive 文件夹 ID、缓存任务 ID；
- 表格修改时间。

键的顺序、空格、换行风格也不影响结果。审计用的全文哈希（`golden_sha256_full` / `manifest_sha256_full`）另外记在 score.json 里，`golden_sheet check --compare` 会说明是否只有非评分字段变了。

版本号会写进：

- 每份 `score.json`（`golden` 段）
- `eval/rounds/eval_history.jsonl` 的每条记录
- Google Sheet 测评报告

按轮次取均值时会按版本分组。报告里的「历史轮次对比 / 历史单跑明细」和图表只对比和本次同一版本的运行，并注明有多少其他版本的运行没有纳入。换了新黄金集后，应在新版本上重新跑一次 r00 基线。
