# 项目：项目申报书自动撰写 AI Agent

## 一、项目目标

为科创企业搭建一个本地部署、可自动执行、可远程监看、可扩充的
项目申报书自动撰写 Agent。核心原则：AI 出初稿，人工做最终审核。

## 二、技术栈

- 交付形态：FastAPI 自建服务（`agent_service/`），企业浏览器直用
- 推理：Ollama（本地 / 局域网 GPU）或 OpenAI 兼容云 API（DeepSeek 等）——
  `LLM_PROVIDER` 切换，`OLLAMA_*` / `OPENAI_*` 环境变量接线
- 引擎：`scripts/` 单测覆盖的管线脚本，提示词在 `scripts/prompts/`
- 文档输出：Markdown → DOCX（python-docx，标题黑体 / 正文宋体）
- 邮件自动接单（可选）：`agent_service/email_intake.py`，stdlib imaplib/smtplib
  + pypdf（PDF 附件），EMAIL_ENABLED=1 才启用
- 调度 / 监控 / 知识库 RAG：后续 hook，不在当前版本
- Agent 记忆：`memory/` 文件式记忆（运行时 Agent 读写）
- 开发工具：Claude Code

## 三、团队分工

- 同学 A（我）：引擎、提示词、服务、docx 输出、记忆系统
- 同学 B：知识库素材收集、脱敏、分块、检索测试

## 四、核心架构

### 4.1 引擎链路（scripts/ 管线）

```
表单 13 字段（网页 / inputs.json）
→ ⓪素材编号 number_material.py     素材逐条编 (S#) 号，产出 kb_material + kb_index
→ ①要素抽取 extract_elements.py    全局要素表 gen_elements（预算/周期/团队/指标）
→ 七章 LLM 串行生成                 build_workflow.CHAPTERS × prompts/02–08
→ ②章节拼接 assemble_document.py   拼全文，剥 <think>，降级正文标题
→ ③一致性审查 check_consistency.py 十项机械检查 → block/warn/待补充
→ docx 转换 md_to_docx.py
```

`build_workflow.py` 是**单一真相源**：章节定义、表单变量、模型参数、
提示词加载 —— `run_pipeline.py`（CLI）与 `agent_service/`（网页）共同 import，
不复制。提示词运行时直接读，改完立即生效。

### 4.2 输入变量（13 字段）

| 变量名 | 类型 | 必填 | 说明 |
|---|---|---|---|
| project_name | 文本 | 是 | 项目名称 |
| declaration_type | 下拉 | 是 | 申报类型（科技型中小企业 / 高新技术企业 / 专精特新 / 其他） |
| tech_direction | 文本 | 是 | 技术方向 |
| project_leader | 文本 | 是 | 项目负责人 |
| team_size | 数字 | 是 | 投入人数 |
| budget_range | 文本 | 是 | 预算规模 |
| expected_outcome | 多行 | 是 | 预期成果 |
| project_highlights | 多行 | 是 | 项目亮点 |
| special_requirements | 多行 | 否 | 特殊要求 |
| material_tech | 多行 | 否 | 素材·企业技术参数（一行一条） |
| material_ip | 多行 | 否 | 素材·知识产权（一行一条） |
| material_finance | 多行 | 否 | 素材·财务数据（一行一条） |
| style_input | 多行 | 否 | 风格样例（只学表达，可留空） |

另有「粘贴识别」：`scripts/parse_inputs.py` 把自由文本抽成表单字段，
只抽文本明说的、没提就空，识别结果只回填表单、提交权在人。

### 4.3 知识库（B 负责建设，规划中）

- 模板-科技型中小企业 / 模板-高新技术企业
- 素材-企业技术参数 / 素材-知识产权 / 素材-财务数据
- 风格-优秀申报书

素材规范见 `knowledge/README.md`（元信息头、数字规范、脱敏、三类分库）。
**接入前**素材走表单三栏（临时入口）；接入 = 检索结果替代表单素材进
`number_material`，链路其余不动。

### 4.4 运行时记忆（memory/）

- CONTEXT.md：运行时 Agent 身份与情境（注入其 system prompt）
- AGENTS.md：行为规则（不得编造、材料不足要说明）
- decisions.md / lessons.md：决策与经验

**`memory/` 由运行时申报书 Agent 写入，Claude Code 不写。**
开发进度记本文档第六节。

## 五、开发约定

- 所有 Python 脚本放在 scripts/ 下；提示词统一放 scripts/prompts/
- 变量名全小写、下划线分隔；脚本仅依赖标准库（md_to_docx 例外）
- 引擎脚本之间不互相 import（保持可独立测试）；
  本地工具（run_pipeline / parse_inputs / build_workflow）例外
- 记忆文件用 Markdown，人类可直接读改
- 不编造数据，材料不足时在输出中明确标注

## 六、当前任务（每次开发时更新）

进度状态记这里，**不记 `memory/CONTEXT.md`**。

### 已完成

- [x] **产品化 MVP：脱离编排平台的交付服务**（2026-09-23）
      `scripts/run_pipeline.py` 引擎 runner + `agent_service/` FastAPI 服务
      （表单 → 串行队列 → 进度页 → 结果页 → docx 下载；状态落盘重启可恢复）
      `scripts/md_to_docx.py` Markdown→docx（宋体/黑体）
      配置即形态：OLLAMA_MODEL 换 32b 零代码
- [x] **表单粘贴识别 parse_inputs**（2026-10-05）
      自由文本 → 13 表单字段，只抽明说的、没提就空；接进 agent_service
      `/api/parse` + 网页「粘贴识别」区；新建 `docs/代码地图.md`；
      `docs/使用指南.md` 重写对齐实际产品
- [x] **架构瘦身：Dify 全部移除**（2026-10-05）
      删 dify/（工作流 yml、config、archive）、templates/、memory_service/、
      call_dify / fill_template / chunk_document 及测试、Dify 时代规划文档；
      `build_workflow.py` 剥掉 yml 发射器只留引擎真相源；
      提示词移至 `scripts/prompts/`；CLAUDE.md / scripts/README.md / 代码地图重写
      ✅ scripts 187 全绿、agent_service 24 全绿
- [x] **邮件自动接单**（2026-10-06）
      `agent_service/email_intake.py`：IMAP 轮询收信（不置已读）→ 正文 + 附件
      （.txt/.docx/.pdf）拼文本 → parse_inputs 抽 13 字段 → 8 必填齐全才
      `main.submit()`，回执客户（含识别字段摘要可纠错）；缺必填回信列缺失项；
      出稿转 docx 邮件通知审核人，失败/中断也通知；UID journal 去重落
      `runs/email_journal.json`；EMAIL_ENABLED=1 才启用，配错不拖垮服务；
      带 `--dry-run 某.eml` 不碰网络的调试入口
      ✅ email_intake 24 全绿、test_service 25 全绿、scripts 188 全绿；
      dry-run 真 Ollama 验证：完整邮件 13 字段全对、缺失邮件正确拒绝
- [x] **云 API 推理接线（DeepSeek）**（2026-10-06）
      `run_pipeline.chat()` 加 `provider="openai"` 分支（/chat/completions +
      Bearer），`resolve_llm()` 读 `LLM_PROVIDER` 切 ollama/openai；
      deepseek-reasoner 的 reasoning_content 只取 content 正文；
      run_workflow / parse_inputs 无参时自动走环境变量，agent_service 零改动；
      ✅ scripts 194 全绿（含 openai 假服务器全链路）、agent_service 49 全绿；
      真机验证待 API key 到位后跑一轮
- [x] 引擎历史里程碑（细节见 git 历史）：
      素材编号节点、十项机械审查、think 剥离、few-shot 提示词、
      五轮 14b 复跑（block 84→10 到平台期）、Dify 工作流阶段 v0.7–v0.9

### 当前这一步（2026-10-06）

**邮件自动接单已落地（见上方完成项）。真机验证到 dry-run 为止，真邮箱链路**
**等接单邮箱开通后验收（使用指南 3.3 有开通步骤）。**

**下一步：**
1. 配 `LLM_PROVIDER=openai` + `OPENAI_API_KEY` 真机跑一轮 DeepSeek（真实申报单），
   对比 14b 质量；出稿后接真邮箱端到端验收（发测试单 → 回执 → 出稿 → 审核人收 docx）
2. 质量待拍板：deepseek-chat 质量过关则目标机不再需要本地推理
   （1GB 显存机器只跑 agent_service 的形态成立）
3. 后续 hook（不在本次）：知识库/RAG 接入、定时触发、记忆回写、
   审核流意见回写、邮件接单 References 追踪

单测：scripts 194（docx 组 venv 下 9 真跑）、agent_service 25 + email_intake 24。

### 阻塞项

| 阻塞 | 影响 | 责任 |
|---|---|---|
| **推理机未定**（局域网 GPU？云实例？） | 交付形态定不了，合规边界定不了 | A ← 最优先 |
| 目标运行机内存/CPU 未知 | 判断能否承载服务 + 向量检索 | 待定 |
| 最小素材集未到位 | 知识库无法开始建 | B |
| 申报类型未最终确认 | 模板与知识库无法定稿 | 待定 |

**开发阶段不受阻塞** —— 开发机（Mac）跑全栈，推理同机，现在就能开工。

## 七、关键约束

- **数据边界：** 优先"数据不出内网"。目标运行机显存仅 1GB，推理层必须外置 ——
  外置到局域网 GPU 服务器则承诺成立；外置到云 GPU 实例则承诺改为
  "数据不出专属实例"，**须先与企业书面确认**。云 API（LLM_PROVIDER=openai，
  DeepSeek 等）会把申报数据发到外部云上，同样须先与企业书面确认才可用于
  真实企业数据；开发/演示阶段不受此限
- **三机形态：** 开发机（Mac）/ 目标运行机（1GB 显存，只跑 agent_service）/
  推理机（外置 GPU，Ollama）
- AI 输出必须经人工审核后才能对外使用
- 知识库素材由 B 提供，A 不直接修改知识库内容
- Embedding 模型必须与 B 保持一致（`qwen3-embedding:0.6b-fp16`，digest 锁定）
- **`memory/` 由运行时申报书 Agent 写入，Claude Code 不写。**
  开发进度记本文档第六节

## 八、参考文档

**先看哪本：** 想知道"下一步干什么" → 本文档第六节；想知道"代码在哪" → `docs/代码地图.md`

| 文档 | 讲什么 |
|---|---|
| `docs/代码地图.md` | **每份代码干什么、谁调谁、数据怎么流、改动顺序** |
| `docs/产品化方案.md` | **可交付服务形态**：接口契约、环境变量、存档、后续路线 |
| `docs/使用指南.md` | 给企业的操作手册 |
| `docs/对接清单.md` | **与 B 的对接：要什么、给什么、什么时候要** |
| `knowledge/README.md` | 知识库建设方法与素材规范 |
| `scripts/README.md` | **脚本清单、输入输出契约、豁免规则**（改脚本前先读） |
| `scripts/prompts/` | 11 个提示词全文（00_system + 01–09 + 10_parse_input） |
| `memory/CONTEXT.md` | 运行时 Agent 身份与情境（**注入其 system prompt**） |
| `memory/AGENTS.md` | 运行时 Agent 行为规则 |
| `memory/decisions.md` | 决策记录 |
| `memory/lessons.md` | 经验教训 + 知识库缺口 |
