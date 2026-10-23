# 门店 CCTV AI 稽核（Gemini Enterprise + Gemini 视频理解）

门店督导在 **Gemini Enterprise（GE）** 里把一个 **Google Drive 视频文件夹链接** 或 **Google Cloud Storage（GCS）存储桶目录链接（`gs://...` 或 GCP 控制台链接）** 发给稽核 Agent，系统会在后台逐个视频做 AI 稽核，按 SOP 规则找出违规，并根据输入链接类型**自动切换输出方式**：
- **Google Workspace 模式（输入 Google Drive 文件夹）**：在同一个 Drive 文件夹里生成一张稽核报告 **Google Sheet** 及 `📁 违规证据切片_Evidence/`（每条违规带画面时间和 20 秒证据片段链接），SOP 从 Google Sheet 读取。
- **Zero-GWS / 纯 GCS 兜底模式（输入 `gs://` 或 GCS 控制台目录，无需 Google Workspace）**：在同一个 GCS 目录里直接生成一份双工作表 Excel 报告 **`📊 AI稽核报告与Token账单.xlsx`** 及 `📁 违规证据切片_Evidence/`，SOP 直接从 Terraform 自动创建的版本控制存储桶（`gs://<项目ID>-<p>-workspace/sop/master_sheet.xlsx`）读取。两种模式可**同时并存、按单自动路由**，也可独立使用。

本仓库包含部署一套完整环境需要的全部内容：Terraform 基础设施、Cloud Build 流水线、服务代码、SOP 规则表快照（`.json` 与 `.xlsx`）和 Prompt 调优工具。

## 1. 架构一览

```
督导 ──► Gemini Enterprise 应用 ──► 稽核 Agent
                                      │  (Vertex AI Agent Engine / ReasoningEngine，负责对话：预检、确认、查进度；按 Session 严格隔离并发任务)
                                      ▼
                             Cloud Run Worker（<p>-worker，每个任务独占一个实例，最多 20 个并发）
                                      │  下载 Drive / GCS 视频 → 去音轨、按 SEGMENT_DURATION_SEC 切片 → Gemini 逐片分析
                                      │  → 片间状态接力（carryover_state_summary）→ 去重 → 按输入源自动路由写报告
                                      ├───────────────────────────────────────────────┐
                                      ▼                                               ▼
            [模式 A：Google Workspace 模式]                 [模式 B：Zero-GWS / GCS 模式（无需 GWS）]
            • Google Drive / Sheets（3-Tab 报告、证据切片）  • GCS Workspace Bucket（<项目ID>-<p>-workspace，开启对象版本控制）
            • SOP 规则 = Google Sheet（实时读取）            • 输出：📊 AI稽核报告与Token账单.xlsx + 📁 违规证据切片_Evidence/
                                                            • SOP 规则 = gs://<项目ID>-<p>-workspace/sop/master_sheet.xlsx

            [公共底座]
            • GCS Staging Bucket（<项目ID>-<p>-staging，30 天 TTL）：任务状态 JSON（按用户+会话隔离）、临时无音切片缓存
            • Cloud Scheduler 看门狗（<p>-watchdog）：每 2 分钟巡检，自动从已完成切片的 Checkpoint 断点续跑中断任务
```

- **模型版本与 SOP 规则不写死**：从 SOP 表格（Google Sheet 或 GCS 上的 `master_sheet.xlsx`）的 `Tab0_版本总控与回滚开关` 读取 `Active_Model_Version` / `Fallback_Model_Version` 与生效规则页签，改规则或回滚版本**不用重新部署**。
- **双模自适应**：当输入 `gs://...`（或 GCS 控制台链接）时，系统自动使用 `GCS_SOP_URI`（默认 `gs://<项目ID>-<p>-workspace/sop/master_sheet.xlsx`），全程 **0 次 Google Workspace API 调用**；当输入 Google Drive 文件夹时，通过**全网域委派（DWD）**以 Workspace「机器人账号」身份读写 Drive/Sheets，不需要任何静态密钥文件。

## 2. 目录结构

| 路径 | 内容 |
|---|---|
| `bootstrap/` | 一次性初始化（项目 Owner 执行）：部署服务账号、Worker 服务账号、镜像仓库、Cloud Build 触发器、所需 API |
| `main.tf` | 主体资源：staging bucket（30 天缓存）、workspace bucket（开启版本控制 + 自动注入初始 `sop/master_sheet.xlsx`）、Cloud Run Worker、看门狗、Agent Engine、GE 应用与 Agent |
| `cloudbuild.yaml` + `deploy/` | 部署流水线：构建镜像 → terraform apply → 创建/更新 Agent Engine、种子化 SOP 并绑定到 GE |
| `cctv_audit/` | 服务代码（含 Drive/Sheets 网关与 GCS/Excel 网关双模自动路由） |
| `sop/master_sheet.xlsx` + `sop/master_sheet.json` + `scripts/init_sop_sheet.py` | SOP 规则表 Excel 种子文件、JSON 快照和导入/同步脚本 |
| `eval/` | Prompt 调优/召回率评测工具（评测数据不随仓库分发，见 `eval/data/README.md`） |
| `tests/`, `eval/tests/` | 单元测试 |

## 3. 前提条件

1. 一个已开通结算的 GCP 项目（可以是已有项目；多套环境可共用一个项目，只要 `name_prefix` 不同）。执行人需要项目 Owner。
2. Gemini Enterprise 许可（用来访问 GE 应用的用户需要分配许可）。
3. **（仅 Google Drive / Sheets 模式需要；纯 GCS 模式可跳过）** 一个 Google Workspace **机器人账号**（如 `cctv-bot@<你的域名>`），需有 Drive 存储空间：Drive 模式下的报告和证据片段存在它名下。
4. **（仅 Google Drive / Sheets 模式需要；纯 GCS 模式可跳过）** 一位 Workspace 超级管理员，用来添加一次域委派（部署第 6 步）。
5. 本地工具：`gcloud`、Terraform ≥ 1.7、Python 3.12+。

> 所有资源名都由 `name_prefix` 派生（`<p>-worker`、`<p>-deployer`、`<p>-images`、`<p>-watchdog`、`<p>-agent`、`<p>-ge`、`<项目ID>-<p>-staging`、`<项目ID>-<p>-workspace` 等），换一个前缀就能在同一个项目里再建一套，互不冲突。

## 4. 账号体系与跨系统打通说明（GCP × Gemini Enterprise × Google Workspace / GCS）

> 💡 **提示**：如果你们当前**没有 Google Workspace（或暂时采用纯 GCS 模式）**，Terraform 会自动建好 `<项目ID>-<p>-workspace` 存储桶并放入初始 SOP `sop/master_sheet.xlsx`，`<p>-worker` 服务账号已自动拥有该桶读写权限，**无需配置下文的 GWS 机器人账号与 DWD 域委派**，可直接跳到「第 5 节 部署步骤」。

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

### 第 2 步：准备 SOP 规则表（纯 GCS 模式可跳过此步）

- **如果你使用 Zero-GWS / 纯 GCS 模式**：**直接跳过第 2 步**。第 5 步执行 Terraform 部署时，会自动创建 `<项目ID>-<p>-workspace` 存储桶，并把本仓库的 `sop/master_sheet.xlsx` 初始化到 `gs://<项目ID>-<p>-workspace/sop/master_sheet.xlsx`（且后续部署绝不会覆盖你修改过的版本）。
- **如果你使用 Google Workspace（Drive / Sheets）模式**，Sheet 是 Drive 文件，Terraform 建不了，手动准备一次即可（任选以下一种方式，**不需要**申请额外的 OAuth 敏感权限）：

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
- **双模并存 / Google Workspace 模式**：`master_prompt_sheet_id` 填第 2 步的 `<SheetID>`，`workspace_impersonate_user` 填机器人邮箱（此时发 Drive 链接自动读 Google Sheet，发 `gs://` 链接自动读 `gs://<项目ID>-<p>-workspace/sop/master_sheet.xlsx`）。
- **纯 GCS（无 Google Workspace）模式**：`master_prompt_sheet_id` 填 `"gs://<项目ID>-<p>-workspace/sop/master_sheet.xlsx"`（或直接留空 `""`），`workspace_impersonate_user` 填 `""`。

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

整个过程约 15–30 分钟（Agent Engine 创建最慢）。它会自动完成：构建镜像、创建 Cloud Run Worker/看门狗/staging 与 workspace 双存储桶、初始化 `gs://<项目ID>-<p>-workspace/sop/master_sheet.xlsx`、创建 Agent Engine、创建 GE 应用并注册 Agent。之后改代码或配置，重新执行这条命令即可（只加 `_APPLY=false` 则只预览不修改）。

### 第 6 步：Workspace 管理员添加域委派（仅 Drive/Sheets 模式需要，纯 GCS 模式可跳过）

1. 查 Worker 服务账号的数字 ID（Client ID）：

```bash
gcloud iam service-accounts describe <p>-worker@<项目ID>.iam.gserviceaccount.com --format='value(uniqueId)'
```

2. Workspace 管理控制台 → 安全 → 访问权限和数据控制 → API 控制 → **管理全网域委派** → 添加：
   - 客户端 ID：上一步的数字
   - OAuth 范围：`https://www.googleapis.com/auth/drive,https://www.googleapis.com/auth/spreadsheets,https://www.googleapis.com/auth/userinfo.profile`
   （其中 `userinfo.profile` 仅在开启 Google Chat 完成通知时，用于把发起人邮箱解析为 Google Chat 所需的数字用户 ID 实现 `@发起人`；若未授权该范围，核心稽核不受影响，仅通知回退为显示纯邮箱文本。）

一般几分钟内生效。没加 `drive` / `spreadsheets` 或范围不对时，GE 里预检会直接提示写入探测失败。

### 第 7 步：准备并授权待稽核的视频目录

- **方式 A（使用 Google Drive 文件夹）**：把要稽核的 Drive 文件夹共享给机器人账号，权限选**编辑者**（报告和证据片段要写回这个文件夹）。
- **方式 B（使用 GCS 目录，无需 Google Workspace）**：直接把门店视频上传到 Terraform 自动创建的版本控制工作桶（例如 `gs://<项目ID>-<p>-workspace/stores/<门店名>/`），Worker 服务账号已自动拥有该桶读写权限，无需额外执行任何授权命令。

### （可选）开启 Google Chat 双阶段通知（启动通知 + 完成通知，自动 `@督导本人`）

默认关闭（`enable_google_chat_notification = false`）。开启后，无论是 **GE 对话发起的单店稽核**、**`Tab2` 每日定时巡检批次**，还是 **`Tab3` 黄金集评测任务**，系统都会在**任务启动瞬间（`🚀 已启动`）**和**任务完成瞬间（`✅ 已完成`）**自动向指定的 Google Chat 聊天室推送卡片通知，并精准 `@` 对应督导本人：

1. 在目标 Google Chat 聊天室里创建一个 Incoming Webhook（聊天室名称旁下拉菜单 → 应用和集成 → Webhook）。
2. **推荐做法（仓库零密钥，通过 GCS 托管 Webhook 地址）**：
   - 将 Webhook URL 写入受版本控制的专属工作区存储桶（该桶仅项目内服务账号可读，避免把含 `key` 和 `token` 的明文 URL 提交到 Git）：
     ```bash
     printf '%s' 'https://chat.googleapis.com/v1/spaces/.../messages?key=...&token=...' \
       | gcloud storage cp - gs://<项目ID>-<p>-workspace/config/google_chat_webhook.txt
     ```
   - 在 `<env>.tfvars` 中填入该 `gs://` 路径引用（系统发通知时会在内存中实时读取并解析为真实 HTTPS 地址）：
     ```hcl
     enable_google_chat_notification = true
     google_chat_webhook_url         = "gs://<项目ID>-<p>-workspace/config/google_chat_webhook.txt"
     ```
   - *（备选）* 若无需脱敏，也可直接在 `google_chat_webhook_url` 中填写 `https://chat.googleapis.com/v1/spaces/...` 原始地址。
3. 重新执行第 5 步部署命令即可生效；随时把 `enable_google_chat_notification` 改回 `false` 即可关闭通知。

### （可选）推送即部署

在 Cloud Build 控制台把本 GitHub 仓库连接为第二代仓库，然后在 `bootstrap/<env>.tfvars` 设置 `trigger_repository = "projects/<项目ID>/locations/<region>/connections/<连接名>/repositories/<仓库名>"`，重新 apply bootstrap。之后推送到 `main` 分支会自动部署。如果代码不在仓库根目录，设置 `iac_dir`。

## 6. 日常使用与定时自动巡检操作手册

### 6.1 方式一：在 Gemini Enterprise 对话框手动发起单店全量稽核

1. 打开 GE 应用（控制台 → Gemini Enterprise → 应用 `<p>-ge`），给用户分配许可。
2. **⚠️ 关键第一步：在 GE 中选中或 `@` 稽核 Agent**：
   - 在 GE 主界面若不指定 Agent，默认是由通用大模型回答（无法调用后台视频预检与 Cloud Run 算力池）。因此，**请务必先在左侧/顶部「Agents（智能体）」列表中点击进入 `CHAGEE 门店视频 AI 稽核专家` 专属对话页，或者在主聊天框输入 `@` 并选中 `@CHAGEE 门店视频 AI 稽核专家`**：
   - **输入框防混淆提示（`customPlaceholderText`）**：进入该 Agent 对话页后，输入框内置常驻引导——`请确认已选中或 @CHAGEE 门店视频 AI 稽核专家：完整稽核整个视频请输入「@CHAGEE 门店视频 AI 稽核专家 请帮忙稽核视频，<视频链接>」；专项抽检或测新SOP请点【仅测 Tab4_专项测试表】气泡`；
   - **一键气泡（`starterPrompts` 第 1 个气泡）**：点击 **`请帮忙稽核视频：<视频文件夹链接>`**（或在主对话框直接输入 `@CHAGEE 门店视频 AI 稽核专家 请帮忙稽核视频，<Google Drive 门店视频文件夹链接 或 gs:// 目录>`）。
3. Agent 先做秒级预检（顶部显著展示 **`✅ 稽核模式：【日常全量稽核】`** 横幅，并返回专属**预检单号**、权限检查、分辨率检查、视频数量/时长、切片计划、生效规则和模型版本），回复「确认开始」（若在主对话框请带 `@CHAGEE 门店视频 AI 稽核专家 确认开始`）后任务在后台运行，可以随时关掉页面（支持同时开启多个会话窗口并发稽核不同目录，单号与进度按会话严格隔离）。
4. 任务启动瞬间与完成瞬间均会在 Google Chat 群推送通知卡片（含 **`🖥️ 打开 HTML 督导复核工作台`** 与 **`📊 打开原 Google Sheet 账单`** 双按钮）；也可随时在 GE 对话框 `@` 该 Agent 问「好了么」查进度。

---

### 6.2 方式二：特定 SOP 专项抽检与新 SOP 提示词测试优化（总控表 `Tab4_专项测试表`）

当业务团队遇到以下两类高频场景时，**无需准备人工标注表（Golden Set），也无需改动 `Tab0` 线上正式生产开关**，可直接使用总控表内置的固定工作台 **`Tab4_专项测试表`**：
- **场景 A（特定 SOP 专项抽检）**：只想针对门店视频重点核查某 1–N 条现有规则（如只抽检 `B4` 红边毛巾消毒桶溯源或 `A1/A2` 洗手规范），不希望其他条款混杂干扰；
- **场景 B（新 SOP 上线前提示词测试与调优）**：业务新增了一条 SOP（如 `C9 · 新增果糖机喷嘴清洁测试`），在合入主表前，希望先拿真实门店视频反复修改提示词、在 **HTML 督导复核工作台 (`/portal`)** 边看 20 秒证据切片边人工评判效果。

#### 操作三步曲（零提示词注入风险、零全量报告误覆盖）：
1. **第一步：在总控表 `Tab4_专项测试表` 填入待测规则**
   - 打开总控表 `CHAGEE_SOP_与模型版本总控台` 的 **`Tab4_专项测试表`** 页签（表头 `A–I 列` 与正式 `Prompt_*` 规则分表完全一致，已内置下拉框校验）；
   - 将需要专项抽检的规则从主表复制过来，或直接编写需要测试调优的新 SOP 规则（可放 1 条或多条；每次加载实时拉取，绕过 300s 缓存，改完表格立即生效）。
2. **第二步：在 Gemini Enterprise 选中/`@` 稽核 Agent，点击专属气泡并发起预检**
   - 在 GE 左侧点击进入 **`CHAGEE 门店视频 AI 稽核专家`** 专属对话页（只有进入 Agent 专属页才会显示预设气泡与专属输入框提示），点击第 2 个预设气泡：**`【仅测 Tab4_专项测试表（非全量）】请帮忙稽核视频：`**，并在冒号后粘贴门店视频文件夹链接（或者在主聊天框直接输入 `@CHAGEE 门店视频 AI 稽核专家 【仅测 Tab4_专项测试表（非全量）】请帮忙稽核视频：<视频文件夹链接>`）；
   - **双重防误触保护**：
     - 预检回复顶部会弹出醒目黄色警示横幅：**`⚠️ 【当前为专项抽检 / 新 SOP 测试模式（非全量稽核）】`**，明确列出本次仅核查 `Tab4_专项测试表` 中的哪几条规则，并提示若误点气泡可直接 `@CHAGEE 门店视频 AI 稽核专家 请帮忙稽核视频，<视频链接>` 重发全量指令；
     - 若 `Tab4_专项测试表` 当前为空，预检会以 **0 Token 消耗** 直接拦截并提示先填表。
3. **第三步：回复「确认开始」，在 HTML 督导工作台直观评判效果**
   - 系统自动保留完整的 Layer 1 底层防幻觉内核（OSD 时钟校准、多人独立追踪、20s 证据切片锚定），同时注入 **`【专项抽检 / 新 SOP 测试范围锁定（Scope Lock）】`**，严格锁定只输出 `Tab4_专项测试表` 内的规则；
   - **按规则维度同表智能追加（绝不冲掉全量报告）**：若该门店文件夹此前已跑过日常全量稽核，专项抽检仅精准替换本次被测 `rule_id`（如 `B4` 或 `C9`）在报告表中的记录，该视频下其余全量 SOP 条款（如 `A1`、`C4` 等）的历史违规记录与全量 Token 账单 **100% 完好保留**；
   - 跑完后直接点击通知卡片进入 **HTML 督导复核工作台 (`/portal`)**，右侧自动展示 `Tab4_专项测试表` 中自定义新规则的完整名称与合格/不合格标准，边看 20 秒视频切片边验证提示词效果。

---

### 6.3 方式三：每日定时自动巡检操作手册（总控表 `Tab2_定时巡检配置表`）

除了在 GE 聊天框手动发链接外，客户可在 SOP 总控表（`CHAGEE_SOP_与模型版本总控台` Google Sheet 或 GCS `master_sheet.xlsx`）的 **`Tab2_定时巡检配置表`** 页签中配置**每日无人值守定时巡检**。门店或区域人员白天只需往 Google Drive 大目录（或其下的门店/日期子文件夹）上传监控视频，**无需每天手动修改表格里的文件夹链接**，系统每天到点自动扫描新增视频、按门店并发执行稽核，并推送汇总报告。

#### 第一步：规划并授权监控视频根目录（`Root_Folder_URL`）
1. 在 Google Drive（或 GCS 存储桶）建立大区/总部监控根目录，子目录可按多层嵌套自由组织，系统会自动以**最末级放视频的子文件夹（Leaf Folder）**为单位拆分为独立的门店稽核任务：
   ```text
   📁 越南大区每日监控根目录 (Root_Folder_URL，共享给机器人账号「编辑者」权限)
   ├── 📁 1-Cantavil D2/
   │   ├── 🎬 Footage1.mov
   │   ├── 🎬 Footage2.mov
   │   ├── 📊 AI稽核报告与Token账单          <-- 系统自动在该门店目录内生成/同表智能追加
   │   └── 📁 违规证据切片_Evidence/         <-- 系统自动生成 20s 违规证据 MP4（巡检自动跳过此目录）
   └── 📁 2-Bau Cat/
       ├── 🎬 Footage1.mov
       └── ...
   ```
2. **权限要求**：将顶层根目录共享给你的 **GWS 机器人账号（`workspace_impersonate_user`）**，权限设为 **「编辑者 (Editor)」**（子文件夹会自动继承该权限，无需逐个设置）。

#### 第二步：在总控表 `Tab2_定时巡检配置表` 填写定时规则（`A–I 列` 逐列说明）

打开总控表的 **`Tab2_定时巡检配置表`** 页签，从第 2 行起每行配置一条定时规则（系统已自动绑定原生下拉框与防呆校验 `strict: True`）：

| 列号 | 列名 | 填写要求与校验规则 | 填写示例 |
| :--- | :--- | :--- | :--- |
| **A 列** | `Rule_ID` | **必填**，全局唯一的规则编号（不可与其他行重复） | `SCHED-VN-01` |
| **B 列** | `Rule_Name` | 任务或区域名称（显示在 Google Chat 通知卡片上） | `越南大区门店每日晚间巡检` |
| **C 列** | `Root_Folder_URL` | **必填**，第 1 步准备的 Google Drive 根文件夹链接/ID 或 `gs://桶/前缀`（内置公式自动拦截误填的 `/file/d/` 单视频链接） | `https://drive.google.com/drive/folders/1abc...` |
| **D 列** | `Trigger_Hour_UTC` | **必填（下拉框）**，每日触发小时（**统一采用 UTC 时区**，`00`–`23` 共 24 档）：<br/>• 越南时间 (`UTC+7`) `22:00` ➔ 选 **`15`**<br/>• 新加坡/北京时间 (`UTC+8`) `22:00` ➔ 选 **`14`** | `15` |
| **E 列** | `Trigger_Minute_UTC` | **必填（下拉框）**，每日触发分钟，仅 **`00`、`15`、`30`、`45`** 四档（对齐 Cloud Scheduler 每 15 分钟轮询刻度；支持当天修改时间后立即按新时间重排） | `00` |
| **F 列** | `Lookback_Days` | **必填（下拉框）**，向前追溯扫描天数，可选 **`0.5`、`1`、`2`、`3`、`4`、`5`、`6`、`7`** 天（`0.5` 天 = 12 小时，`1` 天 = 24 小时）：仅稽核在该时间窗内新上传且尚未稽核过的视频 | `1` |
| **G 列** | `Supervisor_Email` | **必填**，负责该区域的督导企业邮箱（系统在启动和完成通知中自动高亮 `@` 该邮箱） | `auditor@chagee.com` |
| **H 列** | `Enabled` | **必填（下拉框）**：选 **`ENABLED`** 立即生效；选 **`PAUSED`** 暂停该规则 | `ENABLED` |
| **I 列** | `Last_Run_Status` | **系统自动回写列（请勿手填）**：每次触发或完成后自动回写状态摘要；若某行通过复制粘贴填入了非法值，系统也会在此列自动回写 `❌ 配置错误：具体原因` | `✅ 批次 batch_... 已完成：共 2 家门店...` |

#### 第三步：定时巡检的五大生产级自动化机制
1. **双阶段 Google Chat 批次通知（绝不刷屏）**：
   - 到点触发时：推送 **1 张 `⏰ 定时批量视频 AI 稽核已启动` 卡片**（列出本批次包含的所有门店子目录、视频数及预检状态）；
   - 所有门店跑完后：由最后一个完成的门店容器通过 GCS 乐观锁触发推送 **唯一 1 张 `✅ 定时巡检批次已完成` 汇总卡片**（汇总各门店违规数与成本，顶部提供一键打开多门店联合复核工作台按钮）。
2. **单视频级 `<720P` 低清自动过滤（`REJECTED_LOW_RES`，不阻断同目录高清视频）**：
   - 若某个门店文件夹内混入了低于 `720P`（短边 `< 720` 像素）的低清视频，系统会将该低清视频单独标记为 `REJECTED_LOW_RES` 自动跳过（并在通知卡片与 `Last_Run_Status` 中明确提示跳过了哪几支低清视频及分辨率），同文件夹内其余 `>= 720P` 的高清视频照常完成稽核，且次日不会对已跳过的低清视频死循环重复报错。
3. **文件级防重账本与母子目录就近归属（Carve-Out）**：
   - 每支视频在 GCS 拥有独立三态原子账本（`DISPATCHED` / `COMPLETED` / `FAILED`），已成功稽核的视频绝不重复扣费；
   - 若母目录设在 `21:00` 巡检，而其中某个重点门店子目录单独配了一行 `22:00` 的 `ENABLED` 规则，系统在 `21:00` 扫描时会自动将该子目录豁免保留给 `22:00` 的专属规则执行。
4. **24 小时超长任务支持与同表智能追加（一个门店永远只有 1 张报告表）**：
   - 后台采用 **`Cloud Run Jobs`（`<p>-worker-job`，单任务最长支持 24 小时）**，多门店之间 100% 并发执行，单门店内部视频按文件名自然顺序严格串行（确保跨视频的 5–10 分钟消毒浸泡计时接力不断裂）；
   - 次日同一门店目录上传新视频后，系统自动将新视频的违规记录与 Token 账单智能追加到该门店原有的 `📊 AI稽核报告与Token账单` 末尾（若重跑同名视频则原地替换该视频旧行），不会每天碎片化生成新表。
5. **督导 HTML 卡片式复核工作台（`/portal`）**：
   - 点击 Google Chat 完成卡片上的 **`🌐 打开本批次多门店 HTML 复核工作台`**（或单店 **`🖥️ 打开 HTML 督导复核工作台`**，HMAC-SHA256 签名链接默认 **30 天**有效），即可在浏览器中：
     - 顶部一键切换同批次不同门店；
     - 边看 20 秒违规证据视频、边对照右侧违反的 SOP 原文；
     - 点击 **`✅ 确认违规`** 或 **`❌ 排除误报`** 并填写整改备注，结果实时同步写回原 Google Sheet 的第 J、K 列；
     - 一键切换到 **「📋 门店 48h 整改通知单」** 页签复制标准整改通知下发店长。
6. **工作台访问控制与复核审计日志（`enable_portal_iap = true`，默认开启）**：
   - **谁能打开**：工作台由独立的 Cloud Run 服务 `<p>-portal` 提供，前面挂 **Cloud Run 原生 IAP**，打开链接先走 Google 登录页。只有 tfvars 里 `portal_allowed_members`（如 `["domain:<客户域名>"]` 或 `["group:督导组@<客户域名>"]`）中的账号能通过登录；`enable_portal_iap = true` 时该列表必填。
   - **能看哪些门店**：登录后再按 **Google Drive 权限** 逐店判断——只有能在 Drive 里打开该门店报告 Sheet（报告未生成时为门店文件夹）的人才能看/复核该门店；批次工作台只显示有权限的门店，一个都没有则 403。取消共享后最多 5 分钟生效。登录账号须属于机器人账号所在的 Workspace 域（靠域委派以该账号身份查 Drive）。gs:// 门店（纯 GCS 模式）没有 Drive 权限可查，只靠 IAP 登录名单把关。
   - 旧的 `<p>-worker/portal?...` 链接会自动 302 跳到 `<p>-portal`（签名链接本身不变）；worker 上的 `/api/portal/*` 直接 403。`terraform output portal_url` 查看地址。
   - **审计日志**：每次在工作台改状态/备注，都会**先**往 `gs://<workspace 桶>/audit_logs/reviews/<YYYY>/<MM>/<DD>/<job 或 batch ID>/<UTC 时间>_<8 位随机>.json` 写一条只增不改的记录（`if_generation_match=0`），**写不进去就拒绝修改 Sheet 并返回 500**（没有日志就不改）。字段：`ts_utc`、`reviewer_email`（IAP 验证过的登录邮箱）、`reviewer_source`、`job_id` / `batch_id` / `store_job_id`、`composite_key`、`rule_id` / `video_filename` / `timestamp_in_clip`、`old_status` → `new_status`、`old_note` → `new_note`、`report_sheet_url`、`client_ip`、`user_agent`、`phase`（`requested`；Sheet 写失败时另记 `sheet_write_failed`）。状态和备注都没变的提交不记录。
   - **已知缺口**：直接在 Google Sheet 里改 J/K 列不会产生审计日志（Sheets 没有回调）。两位稽核员几乎同时改同一条时，后写者日志里的 `old_status` 可能不是最新值（Sheets 不支持条件写入），但两次修改都会各留一条记录、按 `ts_utc` 排序即可还原先后。Drive 权限检查遇到 Google 侧 5xx 时返回 500（不放行、也不误报"无权限"），刷新重试即可。
   - 关闭（`enable_portal_iap = false`）时恢复旧行为：worker 直接提供工作台，只靠签名链接，无登录身份、无 Drive 权限校验。已部署的 `<p>-portal` 不会被 Terraform 自动删除，如不再需要请手动 `gcloud run services delete <p>-portal`。

### Zero-GWS / GCS 兜底模式详解（没有 Google Workspace 时）

没有 Google Workspace（或暂时不想配 Drive 域委派）时，可以把视频放在 GCS 里，用法几乎一样。

> **Terraform 会自动准备好 GCS 模式要用的东西**（第 5 步部署时创建）：
> - 一个开启了对象版本控制的存储桶 `<项目ID>-<p>-workspace`（可用 `workspace_bucket_name` 改名），Worker 服务账号自动拥有读写权限。里面放门店视频（如 `gs://<项目ID>-<p>-workspace/stores/<门店>/`）、Excel 报告、证据切片和 SOP 规则表。这个桶没有按时间自动删除的规则，只会清理每个文件第 30 个之前的旧版本。
> - 默认 SOP 规则表 `gs://<项目ID>-<p>-workspace/sop/master_sheet.xlsx`，由仓库里的 `sop/master_sheet.xlsx` 在建桶时通过原子条件上传（`ifGenerationMatch=0`）写入一次，之后客户改过的版本绝不会被 Terraform 覆盖。地址可以用 `terraform output gcs_sop_uri` 查看。
> - 这个地址会以 `GCS_SOP_URI` 的形式注入 Worker 和 Agent Engine。督导发 `gs://` 目录时，如果 `master_prompt_sheet_id` 本身不是 `gs://` 地址，就自动用这个规则表，全程不调用 Google Workspace。发 Drive 文件夹时仍用 `master_prompt_sheet_id` 指向的 Google Sheet；没填 Sheet 时 Drive 文件夹也用这个规则表。
>
> 所以用 Terraform 部署后，把视频上传到 `gs://<项目ID>-<p>-workspace/stores/<门店>/`，在 GE 里发这个地址就能直接稽核。下面第 1 步说明怎么改规则、怎么回滚；如果想把 SOP 放在客户自己的其他桶里，也按这一步操作。

1. **SOP 规则放哪里：GCS `master_sheet.xlsx` + 存储桶版本控制（推荐标准方案）**（第 6、7 步的域委派、共享 Drive 文件夹都可以跳过）：
   1. 在客户自己的存储桶上开启对象版本控制（Object Versioning），之后每次覆盖上传，旧版本都会自动归档：
      ```bash
      gcloud storage buckets update gs://<桶> --versioning
      ```
      下一步的脚本默认也会尝试开启。如果当前账号没有 `storage.buckets.update` 权限，脚本会提示上面这条命令，然后继续上传。
   2. 上传初始 SOP 规则表（脚本会上传 `.xlsx` 后再读回来逐格核对，并打印本次版本号）：
      ```bash
      gcloud auth application-default login
      python3 scripts/init_sop_sheet.py --gcs-uri gs://<桶>/sop/master_sheet.xlsx --tfvars <env>.tfvars
      ```
   3. 在 `<env>.tfvars` 里设置 `master_prompt_sheet_id = "gs://<桶>/sop/master_sheet.xlsx"`（也可以填这个文件在控制台的链接，或者 `.json` 快照），然后重新执行第 5 步部署。Worker 服务账号对这个桶至少要有 `roles/storage.objectViewer`。不能放在本服务自己的暂存桶里，那个桶的文件 30 天后会被自动删除，部署和预检都会拒绝。
   4. 平时改规则：从控制台把 `master_sheet.xlsx` 下载下来，用 Excel 或 WPS 修改，可以改规则文字、删掉某条规则所在的行来停用它、改 `Tab0_版本总控与回滚开关` 里的 `Active_Prompt_Version` / `Active_Model_Version`。改完另存为 `.xlsx`，用同一个文件名上传覆盖。**不用重新部署**，最多 5 分钟（300 秒）后新任务就按新规则跑。不要改页签名称和表头。预检时会先确认这个文件能读、有 Tab0 页签，读不到会直接提示怎么处理，不会扣 Token。
   5. 两种管理和回滚 SOP 版本的方式：
      - **方式 A（推荐，不用改配置，1 秒回滚）**：`master_prompt_sheet_id` 一直保持 `gs://<桶>/sop/master_sheet.xlsx`，平时同名上传覆盖，GCS 自动保留历史版本。需要回滚时，在 GCP 控制台打开这个文件的 **「版本历史记录 (Version history)」**，对想要的版本点 **「恢复 (Restore)」**；或者用命令 `gcloud storage cp gs://<桶>/sop/master_sheet.xlsx#<旧版本号> gs://<桶>/sop/master_sheet.xlsx`。最多 5 分钟（300 秒）内自动生效。
      - **方式 B（在配置里锁死某一个版本）**：在 `master_prompt_sheet_id` 末尾带上版本号，比如 `gs://<桶>/sop/master_sheet.xlsx#1728547200123456`，系统就固定读取这个历史版本，之后再上传覆盖也不影响。控制台链接里带 `?generation=<版本号>` 也可以。版本号可以在「版本历史记录」里看到，上传脚本也会打印出来。

   不配外部 SOP 文件时，可以设置 `master_prompt_sheet_id = ""`，直接用内置的 V25 基准规则库，但规则要改就只能改代码重新部署。
2. **授权**：如果使用 Terraform 自动创建的 `<项目ID>-<p>-workspace` 桶，权限已自动配好；如果使用客户自己的其他存储桶，把 `roles/storage.objectAdmin` 授予 Worker 服务账号（`<p>-worker@<项目ID>.iam.gserviceaccount.com`）：
   ```bash
   gcloud storage buckets add-iam-policy-binding gs://<视频桶> \
     --member="serviceAccount:<p>-worker@<项目ID>.iam.gserviceaccount.com" --role="roles/storage.objectAdmin"
   ```
3. **使用**：在 GE 里发送 `gs://<视频桶>/<门店目录>`，或者控制台 Storage 页面的链接（`https://console.cloud.google.com/storage/browser/<视频桶>/<门店目录>`）。系统只读取该目录下直接放着的视频文件，不进子目录。
4. **结果放在哪**（都在同一个 GCS 目录里）：
   - `📊 AI稽核报告与Token账单.xlsx`：Excel 报告，两个工作表分别是「违规事件 3 秒复核台」和「本次视频 Token 消耗与耗时账单」，证据链接可以直接点开；
   - `📁 违规证据切片_Evidence/`：每条违规对应的 20 秒 MP4 证据切片。

   任务完成时，GE 会回复 Excel 报告链接和这个 GCS 目录的控制台链接。
5. **不能用的存储桶**：本服务自己的暂存桶（`STAGING_BUCKET`，`<项目ID>-<p>-staging`）整个桶都不能放门店视频、报告或 SOP 文件。它存的是任务状态和临时切片，而且所有文件 30 天后会被自动删除，系统会直接拒绝。请使用 `<项目ID>-<p>-workspace` 桶或客户自己的存储桶（例如 `gs://<客户桶>/stores/<门店>/`）。

## 7. 运行测试

```bash
.venv/bin/python -m pytest tests/ -q -p no:cacheprovider
.venv/bin/python -m pytest eval/tests -q -p no:cacheprovider     # 两个目录分开跑
```

`eval/tests` 中依赖私有评测数据的用例在没有数据时会自动跳过。

## 8. 黄金数据集准备与自助评测操作手册（总控表 `Tab3_黄金集评测工作台` & `eval/`）

当需要**测试新发布的 Gemini 模型版本**，或者**业务团队修改了某份 SOP 提示词分表后验证召回率是否提升、有无误报劣化**时，可在总控表的 **`Tab3_黄金集评测工作台`** 页签中**零代码一键发起黄金集回归评测**。

### 8.1 零代码自助评测操作手册（推荐：总控表 `Tab3_黄金集评测工作台`）

整个评测准备与触发只需 4 步，**无需修改任何代码，也无需在人工标注表内部添加任何隐藏配置页**：

#### 第一步：准备物理隔离的黄金集视频目录（`Golden_Videos_Folder_URL`）
1. **为什么要物理隔离**：黄金集视频是长期对比的基准标尺，**必须单独存放在专属的 Google Drive 文件夹中**（例如 `evaluation/Golden_Videos`），与日常门店上传/清理的监控文件夹彻底分开，防止日常清理误删基准视频。
2. **目录结构要求**：在 `Golden_Videos` 根目录下，支持按 `稽核重点 / 门店名`（两层）或直接按 `门店名`（一层）组织子文件夹，将对应的基准视频（`.mov` / `.mp4`）放入门店子文件夹中：
   ```text
   📁 Golden_Videos (Golden_Videos_Folder_URL，共享给机器人账号「查看者」或「编辑者」)
   ├── 📁 1. Handwashing Monitoring/
   │   ├── 📁 1-Cantavil D2/
   │   │   ├── 🎬 Footage2-...mov
   │   │   ├── 🎬 Footage3-...mov
   │   │   └── 🎬 Footage4-...mov
   │   └── 📁 2-Bau Cat/
   │       ├── 🎬 Footage1-...mov
   │       └── ...
   └── 📁 2. Ice Maker Weekly Cleaning/
       ├── 📁 1-Cantavil D2/
       └── 📁 2-Bau Cat/
   ```
   > **注**：评测过程只读拉取这些视频（首次运行后会将去音轨切片缓存在持久化桶 `gs://<项目ID>-<p>-workspace/eval/media/` 中加速后续评测），**绝不会**往黄金集视频目录里写入日常稽核报告或证据切片，保持基准目录 100% 纯净。

#### 第二步：准备人工标注结果表（`Human_Labels_Sheet_URL`）
1. 准备一张原生 Google Sheet（左上角无 `.XLSX` 标记），共享给 **GWS 机器人账号（`查看者` 权限即可）**。
2. 表格的第一个页签（或名为 `Manual Audit Result` / `Sheet1` 的页签）直接存放人工稽核标注行，系统原生兼容以下 **5 列格式** 或 **6 列格式**（第 1 行为表头，从第 2 行起每行一条人工发现的违规）：

   | 列 | 字段名 | 填写说明 | 示例 |
   | :--- | :--- | :--- | :--- |
   | **A 列** | `Audit Focus` | 稽核重点 / 场景名称（与视频子目录对应，同列下行留空会自动向上继承） | `Handwashing Monitoring` |
   | **B 列** | `Outlet` | 门店编号（可选，留空自动继承上一行） | `1` |
   | **C 列** | `Outlet Name` | **必填**，门店名称（需与第一步的子文件夹门店名匹配，如 `Cantavil D2` 匹配 `1-Cantavil D2`；同门店下行留空会自动继承） | `Cantavil D2` |
   | **D 列** | `Audit Clause` | 违反的 SOP 条款大类名称 | `1.5 Handwashing and Sanitation Standard` |
   | **E 列** | `Audit Result` / `Findings` | **必填**，人工标注原文，格式为 `Footage <序号>: <画面OSD时间 HHMMSS> <违规描述>`（若同一视频内有多个时间点，会自动拆分为多个得分点） | `Footage 2: 080518 Partner not dry hand with hand towel after handwashing` |
   | **F 列** | `对应的视频文件名称` *(可选第 6 列)* | 可显式填写视频完整文件名；**若采用 5 列格式不填此列**，系统会自动根据 C 列门店名 + E 列开头的 `Footage <序号>` 在第一步的文件夹中精准匹配唯一视频文件 | *(可不填，自动按 `Footage 2` 匹配)* |

#### 第三步：准备测评报告归档目录（`Results_Folder_URL`）
1. 在 Google Drive 中新建一个用于存放历次测评报告的文件夹（如 `evaluation`），将其共享给 **GWS 机器人账号**，权限必须设为 **「编辑者 (Editor)」**（因为每次评测都会在该文件夹下新建一份带时间戳的 5-Tab Google Sheet 测评报告）。

#### 第四步：在总控表 `Tab3_黄金集评测工作台` 配置并一键触发（`A–J 列` 逐列说明）

打开总控表 `CHAGEE_SOP_与模型版本总控台` 的 **`Tab3_黄金集评测工作台`** 页签，按列填写并触发：

| 列号 | 列名 | 填写要求与作用 | 填写示例 |
| :--- | :--- | :--- | :--- |
| **A 列** | `Eval_ID` | **必填**，唯一的评测任务编号 | `EVAL-01` |
| **B 列** | `Eval_Name` | 评测说明名称（显示在 Google Chat 卡片与报告中） | `V2.5标准版提示词回归评测` |
| **C 列** | `Prompt_Tab_Name` | **必填（下拉框）**，选择本次要评测的 SOP 规则页签（直接读取该页签规则评测，**无需改动 `Tab0` 线上生产开关，零生产影响**） | `Prompt_v2.5_V10全量17条标准版` |
| **D 列** | `Model_Version` | **必填（下拉框）**，选择本次要评测的模型版本（候选项 = 内置基线 + `Tab0` `候选列表` 中的模型名，新模型先加到 `Tab0` 即可选，见 8.2；不支持 agentic 视频理解的模型会自动以 static 模式评测并如实记录） | `gemini-3.8-flash` |
| **E 列** | `Golden_Videos_Folder_URL` | **必填**，第一步准备的黄金集视频根目录 Google Drive 链接 | `https://drive.google.com/drive/folders/1tAX...` |
| **F 列** | `Human_Labels_Sheet_URL` | **必填**，第二步准备的人工标注结果 Google Sheet 链接 | `https://docs.google.com/spreadsheets/d/1VBu...` |
| **G 列** | `Results_Folder_URL` | **必填**，第三步准备的测评报告归档文件夹 Google Drive 链接 | `https://drive.google.com/drive/folders/1ky2...` |
| **H 列** | `Supervisor_Email` | **必填**，接收评测启动/完成 Google Chat `@` 通知的邮箱 | `admin@chagee.com` |
| **I 列** | `Trigger_Action` | **必填（触发控制下拉框）**：<br/>• 平时保持 `IDLE` 或 `DONE`；<br/>• **需要跑评测时，下拉切换为 `START_EVAL`**；<br/>• 系统在下一个 15 分钟调度刻度检测到 `START_EVAL` 后，自动将其改为 `RUNNING`，跑完自动改为 `DONE`（异常则为 `FAILED`） | `START_EVAL` |
| **J 列** | `Last_Eval_Report` | **系统自动回写列（请勿手填）**：自动回写启动状态、预检报错原因，或评测完成后的召回率摘要与 **5-Tab 测评报告 Sheet 直链** | `✅ 评测完成 (...)：总体召回率 52.6% (仅 CONFIRMED 52.6%) \| 成本 $4.60 \| 报告: https://...` |

#### 第五步：解读生成的 5-Tab 测评报告 Sheet 与跨轮次历史对比

每次评测完成后，系统会在 `Results_Folder_URL` 文件夹中生成一份全新的 Google Sheet（文件名形如 `CHAGEE AI稽核测评_<round_id>_<run_id>_<YYYYMMDD-HHMMSS>`），并在 Google Chat 推送 **`✅ 黄金集评测已完成`** 卡片。报告包含 **5 个页签**（面向生产环境统一采用全量黄金集指标，无内部开发集/留出集切分）：
1. **`Tab 1: 本次测评概览`**：汇总本次评测的 **`总体召回率`**、**`仅 CONFIRMED 召回率`**、平均每视频告警数（护栏 `≤ 8.0`）、告警命中率、瞬时动作时间定位误差（秒）、单视频平均成本与总成本（USD），以及 Input / Thinking / Output Token 拆分。
2. **`Tab 2: 本次逐题结果`**：逐题列出每道人工标注题目的得分（`1.0` 命中 / `0.5` 半对 / `0.0` 漏检）、匹配的 AI 告警详情及 Gemini Pro 裁判模型的给分理由，方便业务人员针对 `0` 分漏检题精准优化 SOP 描述。
3. **`Tab 3: 历史轮次对比`（含 3 张原生对比图表）**：
   - **为什么每次新建一张 Sheet，里面却有完整的历史对比？** 系统会将每次评测的汇总指标持久化追加到 GCS 账本 `gs://<项目ID>-<p>-workspace/eval/rounds/eval_history.jsonl`。每次生成新 Sheet 时，容器会自动拉取该账本，把**同一套黄金集（相同 `golden_version`）**下的所有历史轮次与当前轮次汇总在同一张表内，并自动绘制 3 张原生柱状/折线图：
     - **图 1**：各轮次核心召回率（`总体召回率`、`仅 CONFIRMED 召回率`）与 `告警命中率` 演进趋势；
     - **图 2**：各轮次按 **SOP 大类**（洗手规范 / 制冰机清洁 / 吧台卫生等）召回率对比；
     - **图 3**：各轮次按 **门店 × 稽核重点** 召回率对比。
4. **`Tab 4: 历史单跑明细`（含原生图表）**：列出历次单跑记录（当前运行以 **`★`** 标记）及 **图 4：每次运行的告警密度 / 时间漂移 / 单视频成本对比图**（带 `8.0 条/视频` 防误报爆炸守卫线）。
5. **`Tab 5: 本次黄金集快照`**：完整备份本次评测读取的人工标注题目、时间判定模式（`POINT ±20s` vs `WINDOW ±60s`）与对应视频文件名，确保每次评测 100% 可审计、可追溯。

---

### 8.2 进阶方式二：开发者 CLI 与 Cloud Build 脚本（`eval/`）

除总控表 `Tab3` 零代码触发外，工程团队也可通过命令行或 Cloud Build 直接调用 `eval/` 工具链：
- **评分标尺与双时间窗机制（`eval/score_run.py`）**：确定性时间预筛 + Gemini Pro 3 次投票取中位数裁判打分：
  - **瞬时定点动作（`POINT` 模式：`1.5 Handwashing and Sanitation Standard` 洗手台瞬时动作）**：采用 **`±20 秒`（前后共 40 秒跨度）** 严格时间窗口，并计算 AI 报出时刻与人工标注时刻的 **时间戳定位误差（`timestamp_drift_sec`）**。
  - **持续过程 / 静置计时 / 跨阶段因果链（`WINDOW` 模式：制冰机 5–10 分钟静置、泡茶 30 秒内搅拌、摸头发后未洗手复工等）**：保留 **`±60 秒` 窗口（及起止时间段包含匹配）**，且**豁免单点时间戳误差统计**。
- **黄金集版本防串台（`golden_version`，`ruler-v2`）**：每次评测自动计算 `golden_version` 哈希；当且仅当两轮评测使用完全相同的黄金集题目与视频集合时，`Tab 3: 历史轮次对比` 才会将它们纳入同一张图表对比。
- **模型下拉框候选来自 `Tab0`**：`Tab0` 的 `B3/B4`（`Active_Model_Version` / `Fallback_Model_Version`）与 `Tab3` 的 `D 列 Model_Version` 下拉框选项 = 内置基线模型 + `Tab0_版本总控与回滚开关` 中出现的所有模型名（`Active_Model_Version` / `Fallback_Model_Version` / `Candidate_Models_Discovered` 行的 B 列，以及任意行的 `D 列 候选列表`，以 `|` 分隔，括注如 `(🆕 新模型可用待测试)` 会被自动剥离）。**想评测一个新模型（如 `gemini-3.5-flash`、`gemini-3.1-pro-preview`），只需把它追加到 `Tab0` 的 `候选列表` 单元格里**，下一次 15 分钟调度刻度即自动刷新两处下拉框，无需重启或改代码。
- **不支持 agentic 视频理解的模型自动回退 static**：Vertex 对不支持 agentic 视频理解的模型（实测 `gemini-3.5-flash`、`gemini-3.1-pro-preview`）返回 `400 INVALID_ARGUMENT: Agentic video processing is not enabled for this model`；系统在首个切片收到该错误后自动改为 static 抽帧并对本进程内该模型的后续切片直接走 static，该次评测在 `eval_history.jsonl`、轮次均值与测评报告 Sheet 中记录为 **`media_mode=static`**（`Tab3` 的 `J 列` 摘要与 Chat 卡片同样标注 `视频模式`）。**要做模型间的同口径对比，请让所有候选模型跑同一种模式**：Cloud Build 传 `_MEDIA_MODE=static`（`eval/cloudbuild_round.yaml` 的 `VIDEO_MEDIA_PROCESSING=${_MEDIA_MODE}`，默认 `agentic`）。
- **常用模块 CLI（均支持 `--help`）**：
  - `eval/run_gcp_round.py`：在本地或通过 `eval/cloudbuild_round.yaml` 运行评测轮次；
  - `eval/sheet_report.py`：为已有运行补生成或原地刷新 5-Tab Google Sheet 报告（`python -m eval.sheet_report --history eval/rounds/eval_history.jsonl --score eval/results/<run_id>/score.json --folder-id <文件夹ID>`）；
  - `eval/run_visibility_probe.py`：针对单条漏检裁出前后 60–75 秒短片，快速诊断模型视觉可见性。

## 9. 删除环境

以下命令都需要项目 Owner 凭据，在仓库根目录按顺序执行。`terraform destroy` 会通过 `terraform_data.stack_teardown` 的销毁钩子（`deploy/deploy_reasoning_engine.py destroy-stack`）自动清理该环境专属的 Gemini Enterprise Agent / 应用 / 数据存储、Vertex AI ReasoningEngine、Staging Bucket 内的缓存对象以及 Cloud Run 服务（严格校验 `service_account` 归属，绝不触碰同项目下的其他环境）：

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


