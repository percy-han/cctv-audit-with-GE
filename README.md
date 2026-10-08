# 门店 CCTV AI 稽核（Gemini Enterprise + Gemini 视频理解）

门店督导在 **Gemini Enterprise（GE）** 里把一个 Google Drive 视频文件夹的链接发给稽核 Agent，系统会在后台逐个视频做 AI 稽核，按 SOP 规则找出违规，最后在同一个文件夹里生成一张稽核报告 Google Sheet（每条违规带画面时间和 20 秒证据片段链接）。

本仓库包含部署一套完整环境需要的全部内容：Terraform 基础设施、Cloud Build 流水线、服务代码、SOP 规则表快照和 Prompt 调优工具。

## 1. 架构一览

```
督导 ──► Gemini Enterprise 应用 ──► 稽核 Agent
                                      │  (Vertex AI Agent Engine / ReasoningEngine，负责对话：预检、确认、查进度)
                                      ▼
                             Cloud Run Worker（<p>-worker，每个任务独占一个实例，最多 20 个并发）
                                      │  下载 Drive 视频 → 去音轨、按 SEGMENT_DURATION_SEC 切片 → Gemini 逐片分析
                                      │  → 片间状态接力 → 去重 → 写报告
                                      ▼
            GCS staging bucket（任务状态 JSON、切片缓存）   Google Drive / Sheets（报告、证据片段）

SOP 规则 = 一张 Google Sheet（运行时读取，改规则不用重新部署）
Cloud Scheduler 看门狗（<p>-watchdog）：定时唤醒 Worker，续跑被中断的任务
```

- 模型版本不写死：从 SOP Sheet 的 `Tab0_版本总控与回滚开关` 读取 `Active_Model_Version` / `Fallback_Model_Version`，启动时先探测可用性。
- 访问 Drive/Sheets 用的是**域委派（DWD）**：Worker 的服务账号以一个 Workspace "机器人账号" 的身份读视频、写报告，不需要任何密钥文件。

## 2. 目录结构

| 路径 | 内容 |
|---|---|
| `bootstrap/` | 一次性初始化（项目 Owner 执行）：部署服务账号、Worker 服务账号、镜像仓库、Cloud Build 触发器、所需 API |
| `main.tf` | 主体资源：staging bucket、Cloud Run Worker、看门狗、Agent Engine、GE 应用与 Agent |
| `cloudbuild.yaml` + `deploy/` | 部署流水线：构建镜像 → terraform apply → 创建/更新 Agent Engine 并绑定到 GE |
| `cctv_audit/` | 服务代码 |
| `sop/master_sheet.json` + `scripts/init_sop_sheet.py` | SOP 规则表快照和导入脚本 |
| `eval/` | Prompt 调优/召回率评测工具（评测数据不随仓库分发，见 `eval/data/README.md`） |
| `tests/`, `eval/tests/` | 单元测试 |

## 3. 前提条件

1. 一个已开通结算的 GCP 项目（可以是已有项目；多套环境可共用一个项目，只要 `name_prefix` 不同）。执行人需要项目 Owner。
2. Gemini Enterprise 许可（用来访问 GE 应用的用户需要分配许可）。
3. 一个 Google Workspace **机器人账号**（如 `cctv-bot@<你的域名>`），需有 Drive 存储空间：报告和证据片段存在它名下。
4. 一位 Workspace 超级管理员，用来添加一次域委派（第 6 步）。
5. 本地工具：`gcloud`、Terraform ≥ 1.7、Python 3.12+。

> 所有资源名都由 `name_prefix` 派生（`<p>-worker`、`<p>-deployer`、`<p>-images`、`<p>-watchdog`、`<p>-agent`、`<p>-ge`、`<项目ID>-<p>-staging` 等），换一个前缀就能在同一个项目里再建一套，互不冲突。

## 4. 部署步骤

下面用 `<env>` 表示这套环境的名字（任意，例如 `prod`，但不能叫 `example`），`<p>` 表示 `name_prefix`。

### 第 1 步：创建 Terraform 状态桶（每个项目一次）

```bash
gcloud auth login
bootstrap/create_state_bucket.sh <项目ID> <region>     # 桶名默认 <项目ID>-tfstate
```

### 第 2 步：导入 SOP 规则表

Sheet 是 Drive 文件，Terraform 建不了，手动准备一次即可（任选以下一种方式，**不需要**申请额外的 OAuth 敏感权限）：

- **方式 A（推荐，浏览器直接导入或上传转换，0 命令行认证）**：
  1. **做法 A1（在空白表格内导入）**：新建一个空白 Google Sheet，点击菜单栏 **文件 (File) → 导入 (Import) → 上传 (Upload)**，选择本仓库里的 `sop/master_sheet.xlsx`，导入位置选 **替换电子表格 (Replace spreadsheet)** → 点击 **导入数据**。
  2. **做法 A2（直接上传 `.xlsx` 到 Google Drive）**：如果你直接把 `sop/master_sheet.xlsx` 上传到了 Google Drive，双击打开它后，注意看左上角文件名旁边是否有绿色的 **`.XLSX`** 标记（有该标记代表它仍是 Excel 二进制格式，Google Sheets API 无法直接读取）。如果有 `.XLSX` 标记，只需在左上角菜单点击一次 **文件 (File) → 另存为 Google 表格 (Save as Google Sheets)**，浏览器会弹出一个**没有 `.XLSX` 标记**的新标签页，**后续请使用这个新标签页的表格和 `<SheetID>`**。
  3. 把最终这张原生 Google 表格（左上角无 `.XLSX` 标记）共享给你的**机器人账号**（即 `workspace_impersonate_user` 填写的邮箱，查看者即可；如果希望服务自动把新模型名追加到 Tab0，给编辑者）。
  4. 记下地址栏里的 `<SheetID>`，第 3 步填进 `<env>.tfvars` 的 `master_prompt_sheet_id`。

- **方式 B（命令行脚本写入，在第 4 步 `bootstrap` 建好服务账号后执行）**：
  > 注意：Google 默认会拦截 `gcloud` 内置客户端 ID 直接向个人账号申请 `spreadsheets` 敏感范围（报 `Google blocked this access`）。因此命令行方式改为直接复用标准 `gcloud auth application-default login`（仅 `cloud-platform` 范围），通过模拟 `<p>-worker` 服务账号写入：
  1. 在浏览器里新建空白 Google Sheet，共享给 `<p>-worker@<项目ID>.iam.gserviceaccount.com` 为**编辑者**（同时共享给机器人账号）。
  2. 完成第 4 步 `bootstrap` 后执行：
     ```bash
     gcloud auth application-default login
     python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
     .venv/bin/python scripts/init_sop_sheet.py --sheet-id <SheetID> --tfvars <env>.tfvars
     ```

以后改规则直接改 Sheet：新建一个规则页签，把 Tab0 的 `Active_Prompt_Version` 改成新页签名即可生效（改回旧名就是回滚），不用重新部署。

### 第 3 步：填写两套配置文件

```bash
cp example.tfvars <env>.tfvars                     # 主体配置
cp example.gcs.tfbackend <env>.gcs.tfbackend       # prefix = "<p>/main"
cp bootstrap/example.tfvars bootstrap/<env>.tfvars
cp bootstrap/example.gcs.tfbackend bootstrap/<env>.gcs.tfbackend   # prefix = "<p>/bootstrap"
```

把文件里所有 `<...>` 换成实际值。两边的 `project_id`、`name_prefix`、`region` 必须一致；`bootstrap/<env>.tfvars` 里设 `trigger_env = "<env>"`。
`master_prompt_sheet_id` 填第 2 步的 Sheet ID。

这些文件不含密钥，**需要提交到 Git**（推送触发部署时流水线从仓库读取它们）。

### 第 4 步：初始化（项目 Owner 执行一次）

```bash
cd bootstrap
terraform init -backend-config=<env>.gcs.tfbackend
terraform apply -var-file=<env>.tfvars
```

完成后会输出 `manual_deploy_command`，就是第 5 步要执行的命令。

### 第 5 步：部署主体

**等 2 分钟左右**再执行（新建的部署服务账号的权限需要时间生效，立即执行可能报 403 `storage.objects.get`，稍等重试即可）。在仓库根目录执行第 4 步输出的命令，形如：

```bash
gcloud builds submit --project=<项目ID> --region=<region> --config=cloudbuild.yaml \
  --gcs-source-staging-dir=gs://<项目ID>-tfstate/source \
  --service-account=projects/<项目ID>/serviceAccounts/<p>-deployer@<项目ID>.iam.gserviceaccount.com \
  --substitutions=_ENV=<env>,_REGION=<region>,_REPO=<p>-images,_IMAGE=cctv-audit-worker,_DEPLOYER_SA_ID=<p>-deployer
```

整个过程约 15–30 分钟（Agent Engine 创建最慢）。它会自动完成：构建镜像、创建 Cloud Run Worker/看门狗/bucket、创建 Agent Engine、创建 GE 应用并注册 Agent。之后改代码或配置，重新执行这条命令即可（只加 `_APPLY=false` 则只预览不修改）。

### 第 6 步：Workspace 管理员添加域委派（一次）

1. 查 Worker 服务账号的数字 ID（Client ID）：

```bash
gcloud iam service-accounts describe <p>-worker@<项目ID>.iam.gserviceaccount.com --format='value(uniqueId)'
```

2. Workspace 管理控制台 → 安全 → 访问权限和数据控制 → API 控制 → **管理全网域委派** → 添加：
   - 客户端 ID：上一步的数字
   - OAuth 范围：`https://www.googleapis.com/auth/drive,https://www.googleapis.com/auth/spreadsheets,https://www.googleapis.com/auth/userinfo.profile`
   （其中 `userinfo.profile` 仅在开启 Google Chat 完成通知时，用于把发起人邮箱解析为 Google Chat 所需的数字用户 ID 实现 `@发起人`；若未授权该范围，核心稽核不受影响，仅通知回退为显示纯邮箱文本。）

一般几分钟内生效。没加 `drive` / `spreadsheets` 或范围不对时，GE 里预检会直接提示写入探测失败。

### 第 7 步：共享视频文件夹

把要稽核的 Drive 文件夹共享给机器人账号，权限选**编辑者**（报告和证据片段要写回这个文件夹）。

### （可选）开启 Google Chat 完成通知（自动 `@发起人`）

默认关闭（`enable_google_chat_notification = false`）。如果希望后台稽核完成后自动往指定的 Google Chat 聊天室发卡片并 `@` 发起任务的督导：

1. 在目标 Google Chat 聊天室里创建一个 Incoming Webhook（聊天室名称旁下拉菜单 → 应用和集成 → Webhook）。
2. 在 `<env>.tfvars`（或本地不提交 Git 的 `*.auto.tfvars`）中设置：
   ```hcl
   enable_google_chat_notification = true
   google_chat_webhook_url         = "https://chat.googleapis.com/v1/spaces/.../messages?key=...&token=..."
   ```
3. 重新执行第 5 步部署命令即可生效；随时把 `enable_google_chat_notification` 改回 `false` 即可关闭通知。

### （可选）推送即部署

在 Cloud Build 控制台把本 GitHub 仓库连接为第二代仓库，然后在 `bootstrap/<env>.tfvars` 设置 `trigger_repository = "projects/<项目ID>/locations/<region>/connections/<连接名>/repositories/<仓库名>"`，重新 apply bootstrap。之后推送到 `main` 分支会自动部署。如果代码不在仓库根目录，设置 `iac_dir`。

## 5. 使用

1. 打开 GE 应用（控制台 → Gemini Enterprise → 应用 `<p>-ge`），给用户分配许可。
2. 在 GE 里选择稽核 Agent，发送 Drive 文件夹链接。
3. Agent 先做预检（权限、视频数量/时长、切片计划、生效规则和模型版本），回复"确认开始"后任务在后台运行，可以关掉页面。
4. 随时问"好了么"查进度；完成后回复报告 Sheet 链接（若开启了 Google Chat 通知，也会在聊天室自动 `@` 发起人并附上报告链接）。

## 6. 运行测试

```bash
.venv/bin/python -m pytest tests/ -q -p no:cacheprovider
.venv/bin/python -m pytest eval/tests -q -p no:cacheprovider     # 两个目录分开跑
```

`eval/tests` 中依赖私有评测数据的用例在没有数据时会自动跳过。

## 7. Prompt 调优工具（eval/）

用人工稽核结果做"标准答案"，评估新规则/新 Prompt 的召回率：

- `eval/build_dataset.py`：把人工稽核表冻结成 `eval/data/golden_v1.jsonl`（格式见 `eval/data/README.md`）。
- `eval/run_gcp_round.py`：用一个调优轮次（`eval/rounds/rNN/`）的规则跑全部验证视频，并打分；也可通过 `eval/cloudbuild_round.yaml` 在 Cloud Build 上运行。
- `eval/score_run.py`：给一次运行结果打分（Gemini 作裁判）。
- `eval/tune_loop.py`：自动调优循环的控制器和守则（单层修改、防过拟合词表、早停）。
- `eval/run_visibility_probe.py`：针对单条漏检，裁出前后 60–75 秒的短片，检查模型能否"看见"该动作。

每个脚本都支持 `--help`。

## 8. 删除环境

以下命令都需要项目 Owner 凭据，在仓库根目录按顺序执行。`terraform destroy` 会通过 `terraform_data.vertex_reasoning_engine` 的销毁钩子（`deploy/deploy_reasoning_engine.py destroy-stack`）自动清理该环境专属的 Gemini Enterprise Agent / 应用 / 数据存储、Vertex AI ReasoningEngine、Staging Bucket 内的缓存对象以及 Cloud Run 服务（严格校验 `service_account` 归属，绝不触碰同项目下的其他环境）：

1. **销毁主环境资源**：
   ```bash
   # 若在 Cloud Shell 中执行，先禁用不通的 IPv6 以免 Terraform 报 dial tcp [2600:...]:443 cannot assign requested address
   sudo sysctl -w net.ipv6.conf.all.disable_ipv6=1 net.ipv6.conf.default.disable_ipv6=1 2>/dev/null || true

   terraform init -reconfigure -backend-config=<env>.gcs.tfbackend
   terraform destroy -var-file=<env>.tfvars
   ```
   > **注**：若该环境是在旧版本代码下创建的（当时 `google_cloud_run_v2_service` 在状态文件里默认记录了 `deletion_protection = true`），先执行一行 `terraform state rm google_cloud_run_v2_service.cctv_audit_worker` 再执行上面的 `terraform destroy -var-file=<env>.tfvars`（`destroy-stack` 钩子会自动通过 API 删除该 Cloud Run 服务）。

2. **销毁初始化资源（Bootstrap）**：
   ```bash
   cd bootstrap
   terraform init -reconfigure -backend-config=<env>.gcs.tfbackend
   terraform destroy -var-file=<env>.tfvars
   cd ..
   ```

3. （可选）删除状态桶里这套环境的状态文件：`gcloud storage rm -r gs://<项目ID>-tfstate/<p>/`，并让 Workspace 管理员移除第 6 步添加的域委派条目。

API 在删除时不会被关闭（`disable_on_destroy = false`），不影响同项目里的其他服务。项目级 IAM 授权都绑定在这套环境自己的服务账号上，会随 destroy 一起删除，不影响其他环境。


