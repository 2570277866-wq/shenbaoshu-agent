# dify/ — 工作流导出与配置

## 目录

| 路径 | 内容 |
|---|---|
| `workflow_v<主>.<次>.yml` | **当前**工作流 |
| `archive/` | 历史版本，只作记录，**不要导入** |
| `prompts/` | 各节点提示词，按章节命名 —— **唯一真相** |
| `config.md` | 模型、知识库、环境变量清单 |

## 当前版本

| 版本 | 日期 | 节点 | 状态 |
|---|---|---|---|
| `workflow_v0.8.yml` | 2026-09-20 | 13 | ⬜ **尚未导入过 Dify** |
| `workflow_v0.7.yml` | 2026-09-20 | 3 | ⚠ 最后一次在真实运行里验证过的版本，留作回退 |

v0.8 是第一条完整链路：开始 → ⓪素材编号 → ①要素抽取 → llm_02…llm_08（七章）
→ ②章节拼接 → ③一致性审查 → 结束。知识检索与 DOCX 输出未接。

## ★ v0.8 起：yml 是产物，不是手写的

从前是在 Dify 界面改、再导出 yml。**现在反过来**：

```
dify/prompts/*.md  +  scripts/*.py        ← 唯一真相，改这里
            │
            ▼  python3 scripts/build_workflow.py
   dify/workflow_v0.8.yml                 ← 产物，勿手改
```

`scripts/build_workflow.py` 把提示词与脚本内联进 yml。好处：
`00_system.md` 存一份而不是抄七遍，改提示词不会漏掉某一章。

**改了 `dify/prompts/` 或任一被内联的脚本，必须重跑生成器。**
`test_build_workflow.py::TestOutputFile` 卡这一条 ——
不重跑就报红，因为产物和真相已经不一致了。

```bash
python3 scripts/build_workflow.py            # 写文件
python3 scripts/build_workflow.py --stdout   # 只打印，用于比对
```

导出的方向也变了：**从 Dify 导出只用于留档**（Dify 升级、误操作、容器重建都可能丢工作流）。
若在界面里手改了，导出为次版本号 +1 并**回头同步到 prompt 或脚本** ——
否则下次重跑生成器，手改的那点东西会无声消失。

## 改动流程（v0.8 起）

1. 改 `dify/prompts/` 或 `scripts/`（**不是**改 yml）
2. 跑单测：`cd scripts && python3 -m unittest discover -s . -p "test_*.py"`
3. 重跑 `python3 scripts/build_workflow.py`
4. 导入 Dify 实跑验证
5. 更新 `docs/WORKFLOW.md`（节点结构）与 `docs/ARCHITECTURE.md`（若涉及编排）
6. 更新 `CLAUDE.md` 第六节「当前任务」

> 开发过程记 `docs/开发日志.md`。**`memory/` 是运行时申报书 Agent 的，不由开发写**
> —— 见 `CLAUDE.md` 第八节。

## 环境变量（只记名字，不记值）

在 Dify 的 `.env` 或环境变量配置中维护，**值不入库**：

| 变量名 | 用途 |
|---|---|
| `OLLAMA_BASE_URL` | 本地模型服务地址 |
| `DOCX_SERVICE_URL` | Markdown to DOCX 服务地址（若自建） |
| `XXL_JOB_*` | 调度中心配置 |

> ⚠ Docker 部署时 `.env` 不要提交。本仓库**已初始化 git 且为 PUBLIC**
> （见 `CLAUDE.md` 第六节），`.env` 与 `dify/docker/` 不入库。
