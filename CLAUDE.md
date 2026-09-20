# 项目：项目申报书自动撰写 AI Agent

## 一、项目目标

为科创企业搭建一个本地部署、可自动执行、可远程监看、可扩充的
项目申报书自动撰写 Agent。核心原则：AI 出初稿，人工做最终审核。

## 二、技术栈

- 工作流编排：Dify（本地 Docker 部署）
- 本地模型：Ollama（DeepSeek/Qwen 量化版）
- 知识库：Dify 内置 RAG
- 文档输出：Markdown to DOCX 插件，备选 python-docx 自建 API
- 自动调度：Dify Schedule Trigger 或 XXL-JOB
- 远程监控：Dify Logs + XXL-JOB Dashboard
- Agent 记忆：Harness 文件式记忆（memory/ 目录）
- 开发工具：Claude Code

## 三、团队分工

- 同学 A（我）：工作流设计、Dify 节点编排、提示词设计、
  docx 输出、触发器配置、Harness 记忆系统
- 同学 B：知识库建设、素材收集、脱敏、分块、索引、检索测试

## 四、核心架构

### 4.1 Dify 工作流节点

开始节点（输入变量）
→ 文档提取器（解析上传的申报指南/模板）
→ 知识检索（绑定多个知识库）
→ 多个 LLM 节点（分章节生成）
→ 代码执行节点（变量替换、长文档分块）
→ 一致性审查节点
→ HTTP 节点（Markdown to DOCX）
→ 结束节点（文件下载）

### 4.2 输入变量（开始节点定义）

| 变量名 | 类型 | 必填 | 说明 |
|---|---|---|---|
| declaration_type | 下拉选择 | 是 | 申报类型 |
| project_name | 文本 | 是 | 项目名称 |
| tech_direction | 文本 | 是 | 技术方向 |
| project_leader | 文本 | 是 | 项目负责人 |
| team_size | 数字 | 是 | 投入人数 |
| budget_range | 文本 | 是 | 预算规模 |
| expected_outcome | 多行文本 | 是 | 预期成果 |
| project_highlights | 多行文本 | 是 | 项目亮点 |
| special_requirements | 多行文本 | 否 | 特殊要求 |
| reference_files | 文件上传 | 否 | 申报指南/项目文档 |

### 4.3 知识库（由 B 负责建设，A 负责绑定）

- 模板-科技型中小企业
- 模板-高新技术企业
- 素材-企业技术参数
- 素材-知识产权
- 素材-财务数据
- 风格-优秀申报书

### 4.4 Harness 记忆系统

memory/ 目录下的文件：
- CONTEXT.md：启动时自动注入 system prompt
- AGENTS.md：声明式规则（不得编造、材料不足要说明）
- decisions.md：每次生成后的关键决策
- lessons.md：人工修改后的经验教训

Agent 通过 memory_read / memory_write / memory_replace /
memory_insert / memory_list 五个工具主动管理这些文件。

## 五、开发阶段

### 第 1 周：环境与最小链路
- Docker Compose 部署 Dify
- Ollama 拉取量化模型，Dify 接入
- 创建空白工作流：开始 → 知识检索 → LLM → 结束
- 跑通单章节（技术方案）生成

### 第 2 周：完整工作流与自动化
- 扩展多章节 LLM 节点
- 加入代码执行节点做变量替换
- 接入 Markdown to DOCX
- 启用迭代节点处理长文档
- 配置 Schedule Trigger

### 第 3 周：监控 + 手动触发 + 测试
- 部署 XXL-JOB 或配置 Dify API
- 配置告警
- 用 3 个真实项目测试
- 建立记忆回写机制

### 第 4 周：包装与交付
- 配置 Dify 应用输入表单
- 编写使用指南
- 培训企业

## 六、当前任务（每次开发时更新）

进度状态记这里，**不记 `memory/CONTEXT.md`**（那是运行时 Agent 的身份与规则）。

- [x] 创建项目目录结构
- [x] 编写 memory/CONTEXT.md 和 memory/AGENTS.md
- [x] 准备 templates/ 下的 Markdown 模板（骨架完成，待按官方指南校准）
- [x] 编写 docs/WORKFLOW.md
- [x] 编写 docs/ARCHITECTURE.md、docs/PLAN.md、docs/技术选型.md、docs/使用指南.md
- [x] 编写 dify/prompts/ 提示词（00_system + 01–09 全部）
- [x] 定 Embedding 型号：`qwen3-embedding:0.6b-fp16`（生成模型同选 Qwen3）
- [ ] **拉取 embedding 并记 digest**：`ollama pull qwen3-embedding:0.6b-fp16` → `ollama list`
- [ ] **建测试集**（20–30 题）跑召回验证，≥ 90% 才算通过
- [ ] 通知 B 对齐 digest 与指令前缀
- [x] 编写 scripts/ 五个脚本（要素抽取 / 变量替换 / 分块 / 一致性审查 / 调试调用）
      ✅ 82 项单测全绿，`python3 -m unittest discover scripts/`
- [x] **机械校验层：一致性审查从七项扩到十项**（2026-09-20）
      新增 8 数字来源标记闭环 / 9 无据佐证声称 / 10 模型自我认证，均为 block
      对 v0.7 实测输出回放，5 类违规全中，合法项零误报
      ⚠ **未覆盖**：编造技术细节、编造专利内容（语义层，仍归人工）
      依据见 `docs/开发日志.md` 2026-09-20 §13 §14
- [ ] **素材编号节点** ← 第 8 项检查的前提
      素材必须在进入提示词前按 `S1：…` 逐条编号，否则第 8 项报 block 拒绝执行
      放在节点①（要素抽取）或新增一个代码节点
- [ ] **在 Dify 代码节点里实跑脚本** ← 待 Dify 部署后验证
      （代码节点能否读知识库原始条目、超时与内存上限，见 `scripts/README.md` 待确认）
- [x] **本地记忆服务** `memory_service/`（FastAPI，5 个记忆工具 + 回滚 + 审计）
      ✅ 51 项单测全绿，`cd memory_service && .venv/bin/python -m unittest test_service`
      ⬜ 待做：在 Dify HTTP 节点里真接一次（Docker 网络连通性见其 README）
- [ ] 部署 Dify（Docker Compose）
- [ ] 导出 Dify 工作流配置到 dify/
- [x] **建 git 仓库并推 GitHub**
      `github.com/2570277866-wq/shenbaoshu-agent`
      ⚠ **2026-09-19 起为 PUBLIC**（原 PRIVATE，经确认后公开，不可逆）
      git 身份只配在本仓库：noreply 邮箱，未动全局配置
      `memory/.backups` `.locks` `.audit.jsonl` `.venv` 已 ignore
      ⚠ **企业素材不得推入本仓库** —— 见 `docs/开发日志.md` 2026-09-19 条

### 当前这一步（2026-09-20）

**提示词路线已到头，改走机器兜底。** 依据：
同一种「结构要求 → 素材填不满 → 编造填充」的机制已出现五次（见开发日志 §13），
每修一种模型换一种形式；v0.7 更学会**用合规格式包装编造**
（`降低至 8 ms（【待补充：优化目标值来源】）`—— 格式全对，实质是编造）。

机械校验层已建成并对 v0.7 回放验证。**下一步：**
1. **补素材编号节点** —— 第 8 项检查的前提，否则新层跑不起来
2. 铺多章节（02–08 的 LLM 节点）

单测：`python3 -m unittest discover scripts/` → 82 项。

### 阻塞项

| 阻塞 | 影响 | 责任 |
|---|---|---|
| **推理机未定**（局域网 GPU？云实例？） | 交付形态定不了，合规边界定不了 | A ← 最优先 |
| 目标运行机内存/CPU 未知 | 判断能否承载 Dify + 向量库 | 待定 |
| Embedding 模型未确认 | 知识库无法开始建索引 | A + B |
| 最小素材集未到位 | 无法跑通单章节生成 | B |
| 申报类型未最终确认 | 模板与知识库无法定稿 | 待定 |

**开发阶段不受阻塞** —— 用开发机（Mac / 独显机）跑全栈，推理同机，现在就能开工。

## 七、开发约定

- 所有 Python 脚本放在 scripts/ 下，函数入口为 main()
- 变量名全小写、下划线分隔
- 提示词统一放在 dify/prompts/ 下，按章节命名
- 每次修改工作流后，导出配置到 dify/ 并更新 docs/WORKFLOW.md
- 记忆文件用 Markdown，人类可直接读改
- 不编造数据，材料不足时在输出中明确标注

## 八、关键约束

- **数据边界：** 优先"数据不出内网"。目标运行机显存仅 1GB，推理层必须外置 ——
  外置到局域网 GPU 服务器则承诺成立；外置到云 GPU 实例则承诺改为
  "数据不出专属实例"，**须先与企业书面确认**。云 API 方案不用。
- **三机形态：** 开发机（Mac + 独显）/ 目标运行机（1GB 显存，只做编排）/
  推理机（外置 GPU）。见 `docs/ARCHITECTURE.md` 1.1
- AI 输出必须经人工审核后才能对外使用
- 知识库素材由 B 提供，A 不直接修改知识库内容
- Embedding 模型必须与 B 保持一致
- **`memory/` 由运行时申报书 Agent 写入，Claude Code 不写。**
  开发进度记本文档第六节

## 九、参考文档

**先看哪本：** 想知道"下一步干什么" → `docs/执行路线图.md`

| 文档 | 讲什么 |
|---|---|
| `docs/执行路线图.md` | **每一步谁做、用什么、产出什么、交给谁** |
| `docs/开发日志.md` | **每次开发做了什么、用了什么命令、得到什么数据**（最新在上） |
| `docs/PLAN.md` | 定位、三阶段路径、20–30 天排期、Demo 方案、风险 |
| `docs/ARCHITECTURE.md` | 系统全貌：编排、变量、知识库、代码节点、输出、触发、监控、记忆 |
| `docs/WORKFLOW.md` | 节点内部细节：提示词索引、变量传递、分块、审查组装、异常处理 |
| `docs/技术选型.md` | 硬件规划、Dify vs LangChain、云 GPU 方案 |
| `docs/对接清单.md` | **与 B 的对接：要什么、给什么、什么时候要** |
| `docs/使用指南.md` | 给企业的操作手册 |
| `memory_service/README.md` | **本地记忆服务**：端点契约、Dify 接法、安全边界 |
| `dify/prompts/` | 10 个提示词全文 |
| `dify/config.md` | 模型与知识库配置记录 |
| `knowledge/README.md` | 知识库建设方法与素材规范 |
| `memory/CONTEXT.md` | 运行时 Agent 身份与情境（**注入其 system prompt**） |
| `memory/AGENTS.md` | 运行时 Agent 行为规则 |
| `memory/decisions.md` | 决策记录 |
| `memory/lessons.md` | 经验教训 + 知识库缺口 |
