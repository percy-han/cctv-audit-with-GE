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
4. 一位 Workspace 超级管理员，用来添加一次域委派（部署第 6 步）。
5. 本地工具：`gcloud`、Terraform ≥ 1.7、Python 3.12+。

> 所有资源名都由 `name_prefix` 派生（`<p>-worker`、`<p>-deployer`、`<p>-images`、`<p>-watchdog`、`<p>-agent`、`<p>-ge`、`<项目ID>-<p>-staging` 等），换一个前缀就能在同一个项目里再建一套，互不冲突。

## 4. 账号体系与跨系统打通说明（GCP × Gemini Enterprise × Google Workspace）

本方案横跨 **GCP（云基础设施与模型推理）**、**Gemini Enterprise（对话入口）** 和 **Google Workspace / GWS（Drive 视频存取、Sheets 报告与 Google Chat 通知）** 三套系统。在开始部署前，先理清三边的账号关系和打通方式：

### 4.1 核心原则：GE 与 GWS 共享同一套企业员工账号，GCP 服务账号通过「机器人账号」读写 Drive

1. **员工账号同源（GWS = Gemini Enterprise 登录身份）**：
   - Gemini Enterprise 本身**不单独建一套账号密码**，它直接复用你们企业的 Google Workspace / Cloud Identity 域账号体系。
   - 门店督导用自己的企业邮箱（如 `auditor@<你的域名>`）登录 Google Drive 上传门店视频，并用**同一个邮箱账号**登录 Gemini Enterprise 网页端发起稽核对话、在 Google Chat 接收 `@` 完成通知。
2. **为什么要配一个「GWS 机器人账号（`workspace_impersonate_user`）」？**
   - GCP 自动创建的 IAM 服务账号（`<p>-worker@<项目ID>.iam.gserviceaccount.com`）不是人类员工账号，它在 Google Drive 里的个人存储配额是 **0 字节**——如果直接用服务账号去新建 Google Sheet 报告或上传 20 秒证据视频，Google Drive API 会直接拒绝并报错 `storageQuotaExceeded`。
   - 因此，需要在 GWS 里准备一个带 Drive 存储空间的普通域账号作为**机器人账号**（如 `cctv-bot@<你的域名>`），并通过 **Google Workspace 全网域委派（DWD）** 允许 GCP 的 `<p>-worker` 服务账号在运行时以无密钥（IAM `signJwt`）方式「化身为（Impersonate）」这个机器人账号去读视频、写报告和查用户 ID。

### 4.2 三套系统中的 6 类账号/角色对照表

| 所属系统 | 账号 / 身份名称 | 类型 | 谁来创建 / 在哪配置 | 核心职责与所需权限 |
|---|---|---|---|---|
| **GCP** | **1. GCP 项目管理员** | 人工账号 | 企业 Cloud 管理员 | 拥有 GCP 项目的 `roles/owner` 权限；仅在首次部署时执行第 1 步（建状态桶）和第 4 步（`bootstrap` 初始化）。 |
| **GCP** | **2. 部署服务账号**<br>`<p>-deployer@<项目ID>.iam...` | 机器账号<br>(IAM SA) | 第 4 步 `bootstrap` **自动创建** | 供 Cloud Build 流水线使用；自动赋予构建镜像、管理 Cloud Run / GCS / Scheduler / Vertex AI Agent Engine 及注册 Gemini Enterprise 应用的权限。 |
| **GCP** | **3. 运行时服务账号**<br>`<p>-worker@<项目ID>.iam...` | 机器账号<br>(IAM SA) | 第 4 步 `bootstrap` **自动创建** | 同时挂载在 **Agent Engine (`<p>-agent`)** 和 **Cloud Run Worker (`<p>-worker`)** 上：<br>• **对内（GCP）**：调用 Vertex AI Gemini 多模态模型、读写 GCS 状态桶、签发 OIDC Token 触发 Cloud Run；<br>• **对外（GWS）**：通过 DWD 模拟下面的「GWS 机器人账号」访问 Drive / Sheets / People API。 |
| **GWS** | **4. GWS 超级管理员** | 人工账号 | 企业 IT 管理员 | 仅在部署第 6 步登录 `admin.google.com` 执行一次操作：将 `<p>-worker` 服务账号的数字 `Client ID` 加入「全网域委派（DWD）」白名单。 |
| **GWS** | **5. GWS 机器人账号**<br>（如 `cctv-bot@<你的域名>`） | 专用域账号<br>(有 Drive 配额) | 企业 IT 在 GWS 创建，邮箱填入 `<env>.tfvars` 的 `workspace_impersonate_user` | **跨云桥梁身份**：拥有 Google Workspace 基础许可（含 Drive 存储空间）。被 `<p>-worker` 通过 DWD 模拟后，负责读取 SOP 规则表、从督导文件夹下载源视频、上传 20 秒违规证据 MP4，并创建 Google Sheet 稽核报告（文件归属在该机器人名下，不占服务账号 0 字节配额）。 |
| **GWS & GE** | **6. 门店督导 / 业务用户**<br>（如 `auditor@<你的域名>`） | 人工账号 | 门店督导本人 | • **在 GWS Drive 中**：新建视频文件夹并上传监控视频，将该文件夹共享给上面的 **GWS 机器人账号（编辑者）**；<br>• **在 Gemini Enterprise 中**：需要被分配 **Gemini Enterprise License（席位许可）**，登录 GE 网页端粘贴文件夹链接发起稽核；<br>• **在 Google Chat 中**：稽核完成后在群里收到机器人自动 `@` 本人的卡片通知。 |

### 4.3 三套系统是怎么打通并联动工作的？

```
┌─────────────────────────────────────────────────────────────────────────────────────────┐
│ 1. 用户侧（同源 GWS 员工账号：auditor@<你的域名>）                                        │
│    • 在 Google Drive 上传视频，把文件夹共享给【GWS 机器人账号 cctv-bot@】（编辑者）         │
│    • 用同一个邮箱登录 Gemini Enterprise Web UI（需在 GCP 控制台分配 GE 席位许可）          │
└───────────────────────────────┬─────────────────────────────────────────────────────────┘
                                │ ① 在 GE 聊天框发送 Drive 文件夹链接 / 回复"确认开始"
                                ▼
┌─────────────────────────────────────────────────────────────────────────────────────────┐
│ 2. Gemini Enterprise 应用 (<p>-ge) ──[Discovery Engine 自动绑定]──► Agent Engine (<p>-agent)│
│    • 部署流水线自动创建 GE 应用并将 Vertex AI ReasoningEngine (<p>-agent) 注册挂载进去      │
│    • GE 把当前登录督导的身份传给 <p>-agent（按用户隔离任务状态，并记录任务发起人邮箱）       │
└───────────────────────────────┬─────────────────────────────────────────────────────────┘
                                │ ② Google 签名 OIDC ID Token（零静态密钥，roles/run.invoker）
                                ▼
┌─────────────────────────────────────────────────────────────────────────────────────────┐
│ 3. GCP Cloud Run Worker (<p>-worker，挂载运行时服务账号 <p>-worker@<项目ID>.iam...)       │
│    • 调用 Vertex AI Gemini 模型执行多模态视频稽核，任务断点实时写入 GCS staging bucket      │
│    • Cloud Scheduler 看门狗 (<p>-watchdog) 每 5 分钟通过 OIDC 唤醒 Worker 续跑中断任务      │
└──────────────┬───────────────────────────────────────────────────┬──────────────────────┘
               │ ③ 全网域委派 (DWD，IAM signJwt 免密钥模拟)          │ ④ Incoming Webhook POST
               │   化身【GWS 机器人账号 cctv-bot@<你的域名>】        │   (结合 DWD userinfo.profile)
               ▼                                                   ▼
┌──────────────────────────────────────────┐     ┌────────────────────────────────────────┐
│ 4A. Google Drive & Google Sheets         │     │ 4B. Google Chat 聊天室（可选完成通知）   │
│ • 读取 SOP 总控表（加载最新规则与模型名）  │     │ • 先通过 DWD 调 People API 把发起督导   │
│ • 从督导文件夹流式下载视频                 │     │   邮箱解析成 GWS 数字用户 ID             │
│ • 将 20s 违规证据 MP4 与 3-Tab 稽核报告   │     │ • 往群 Webhook 发卡片，精准 @发起督导    │
│   Sheet 直接写回督导的同一 Drive 文件夹   │     │   本人并附上 Google Sheet 报告直链       │
└──────────────────────────────────────────┘     └────────────────────────────────────────┘
```

要让上面四条链路全部跑通，只需要完成以下 **4 处对接配置**（均已包含在下文「第 5 节 部署步骤」中）：

1. **打通 GE 与 GCP Agent Engine（流水线自动完成 + 管理员分配 License）**：
   - **应用与 Agent 绑定**：无需手动配置。执行部署命令（第 5 步）时，`deploy/deploy_reasoning_engine.py` 会自动创建 Vertex AI `ReasoningEngine`（`<p>-agent`）、创建 Gemini Enterprise 应用（`<p>-ge`），并将 Agent 注册到 GE 应用中。
   - **给督导开通访问权**：管理员进入 **GCP 控制台 → Gemini Enterprise → License（许可管理）**，勾选需要使用系统的督导邮箱（`auditor@<你的域名>`）分配许可。
2. **打通 GCP Agent Engine 与 Cloud Run Worker（Terraform 自动完成）**：
   - 无需手动配置。`bootstrap` 和 `main.tf` 会自动为 `<p>-worker` 服务账号授予 `roles/run.invoker`、`roles/aiplatform.user`、`roles/storage.objectAdmin` 和 `roles/iam.serviceAccountTokenCreator`（允许服务账号调用 IAM `signJwt` 签发 DWD 令牌）。
3. **打通 GCP 服务账号与 Google Workspace Drive/Sheets（第 3、6、7 步）**：
   - **配置被模拟的机器人邮箱（第 3 步）**：在 `<env>.tfvars` 中设置 `workspace_impersonate_user = "cctv-bot@<你的域名>"`。
   - **配置 GWS 全网域委派 DWD（第 6 步，超管一次操作）**：GWS 超级管理员在 `admin.google.com` 的「管理全网域委派」中，填入 `<p>-worker` 服务账号的数字 `uniqueId`（Client ID），并授权以下 3 个 OAuth Scope：
     - `https://www.googleapis.com/auth/drive`（读源视频、创建证据子目录、上传 20s 证据 MP4）
     - `https://www.googleapis.com/auth/spreadsheets`（读 SOP 规则总控表、写入 3-Tab 稽核报告 Sheet）
     - `https://www.googleapis.com/auth/userinfo.profile`（把发起督导邮箱解析为 Google Chat `@` 所需的数字用户 ID）
   - **共享文件夹权限（第 2、7 步）**：将 SOP 规则表和督导的待稽核视频文件夹共享给 `cctv-bot@<你的域名>`（视频文件夹需给**编辑者**权限，这样机器人生成的报告 Sheet 和证据视频才能直接落盘在该文件夹内，督导无需二次授权即可直接点开观看）。
4. **打通 Cloud Run Worker 与 Google Chat 通知群（可选配置）**：
   - 在需要接收通知的 Google Chat 聊天室创建 **Incoming Webhook**，将 URL 填入 `<env>.tfvars` 的 `google_chat_webhook_url` 并设 `enable_google_chat_notification = true`。
   - 只要第 6 步的域委派里包含了 `userinfo.profile`，Worker 在发通知前就会自动以机器人身份查询 People API，把发起任务的督导邮箱转为 `<users/数字ID>`，在群消息里直接高亮 `@` 到督导本人。

## 5. 部署步骤

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

## 6. 使用

1. 打开 GE 应用（控制台 → Gemini Enterprise → 应用 `<p>-ge`），给用户分配许可。
2. 在 GE 里选择稽核 Agent，发送 Drive 文件夹链接。
3. Agent 先做预检（权限、视频数量/时长、切片计划、生效规则和模型版本），回复"确认开始"后任务在后台运行，可以关掉页面。
4. 随时问"好了么"查进度；完成后回复报告 Sheet 链接（若开启了 Google Chat 通知，也会在聊天室自动 `@` 发起人并附上报告链接）。

## 7. 运行测试

```bash
.venv/bin/python -m pytest tests/ -q -p no:cacheprovider
.venv/bin/python -m pytest eval/tests -q -p no:cacheprovider     # 两个目录分开跑
```

`eval/tests` 中依赖私有评测数据的用例在没有数据时会自动跳过。

## 8. 模型与 SOP 评测体系及 Cloud Monitoring 监控大盘（eval/）

用人工稽核结果做"黄金基准集（Golden Dataset）"，评估不同模型版本（`model_version`）和不同 SOP 提示词版本（`sop_version`）的召回率、误报密度、定位误差与推理成本，并自动将每次评测结果推送到 **GCP Cloud Monitoring 监控大盘**（原生保留 **24 个月 / 2 年**，同时全量归档至 `eval/rounds/eval_history.jsonl`）：

- **核心脚本**：
  - `eval/build_dataset.py`：把人工稽核表冻结成 `eval/data/golden_v1.jsonl`（格式见 `eval/data/README.md`）。
  - `eval/run_gcp_round.py`：用一个调优轮次（`eval/rounds/rNN/`）的规则跑全部验证视频、自动打分、追加写入 `eval/rounds/eval_history.jsonl`，并自动推送自定义指标到 Cloud Monitoring（可通过 `--skip-monitoring-publish` 跳过）；支持通过 `eval/cloudbuild_round.yaml` 在 Cloud Build 上运行。
  - `eval/score_run.py`：确定性时间预筛 + Gemini Pro 3 次投票取中位数裁判打分。针对不同 SOP 性质采用**分化的时间容差与定位误差策略**：
    - **瞬时定点动作（`POINT` 模式：`1.5 Handwashing and Sanitation Standard` 洗手台瞬时动作）**：采用 **`±20 秒`（前后共 40 秒跨度）** 严格时间窗口，并计算 AI 报出时刻与人工标注时刻的 **时间戳定位误差（`timestamp_drift_sec`）**。
    - **持续过程 / 静置计时 / 跨阶段因果链（`WINDOW` 模式：制冰机 5–10 分钟静置、泡茶 30 秒内搅拌、摸头发后未洗手复工等）**：保留 **`±60 秒` 窗口（及起止时间段包含匹配）**，且**豁免单点时间戳误差统计**。
  - `eval/monitoring_publisher.py`：将每次评测的多维指标（带 `model_version`、`sop_version`、`media_mode`、`round_id`、`run_id`、`sop_category`、`outlet_focus`、`video_name` 标签）写入 Cloud Monitoring 自定义指标 `custom.googleapis.com/cctv_audit/eval/*`。
  - `eval/tune_loop.py`：自动调优循环的控制器和守则（单层修改、防过拟合词表、早停守卫：`findings_per_clip <= 8.0` 且 `regressed_stable_items == 0`）。
  - `eval/run_visibility_probe.py`：针对单条漏检，裁出前后 60–75 秒的短片，检查模型能否"看见"该动作。

- **Cloud Monitoring 评测与运行监控大盘（由 `main.tf` 的 `google_monitoring_dashboard.cctv_audit_dashboard` 自动部署）**：
  - 部署后可通过 `terraform output -raw monitoring_dashboard_console_url` 打开大盘，顶部支持按 `model_version`、`sop_version`、`media_mode`、`round_id`、`sop_category`、`outlet_focus`、`video_name` 自由筛选与对比：
    1. **第 1 行（全局模型 × SOP 版本对比与防劣化守卫）**：总体/留出集（Holdout）/开发集（Dev）召回率趋势、置信度双工作点 PR 对比（`CONFIRMED+SUSPECTED` vs 严格模式 `仅 CONFIRMED` + 有效告警命中率 `hit_rate`）、单视频平均告警数（`<= 8.0` 红线）与稳定项回退数（`== 0`）、瞬时洗手动作时间戳定位误差（`±20s` 窗口内均值）与同配置跨 Run 翻转率（`flip_rate`）。
    2. **第 2 行（按 SOP 大类 & 门店机位分组对比）**：按 `sop_category`（`A_Handwashing` 洗手、`B_IceMaker` 制冰机、`C_TeaBar_Hygiene` 吧台卫生）和 `outlet_focus`（门店 × 机位）分组柱状对比。
    3. **第 3 行（单段视频细粒度透视）**：按 `video_name` 展示每段视频的召回率与告警输出条数，一眼定位哪段视频提升或误报偏高。
    4. **第 4 行（单视频 Token 成本、耗时与容器健康度）**：单段 5 分钟视频平均成本（USD）、平均耗时（秒）以及 Cloud Run Worker 活跃实例数。

每个脚本都支持 `--help`。

## 9. 删除环境

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


