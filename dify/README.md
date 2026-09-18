# dify/ — 工作流导出与配置

## 目录

| 路径 | 内容 |
|---|---|
| `workflow_v<主>.<次>.yml` | Dify 工作流导出文件 |
| `prompts/` | 各节点提示词，按章节命名 |
| `config.md` | 模型、知识库、环境变量清单 |

## 改动流程

1. 在 Dify 界面修改工作流
2. 导出为 `workflow_vX.Y.yml`（次版本号 +1）
3. 提示词改动同步更新 `prompts/`
4. `memory/decisions.md` 记录改了什么、为什么
5. 更新 `docs/WORKFLOW.md`
6. 更新 `memory/CONTEXT.md` 状态

**导出文件入库是保险。** Dify 版本升级、误操作、容器重建都可能丢工作流。有了导出文件可重建。

## 版本记录

| 版本 | 日期 | 改动 | 备注 |
|---|---|---|---|
| — | — | — | 尚无导出 |

## 环境变量（只记名字，不记值）

在 Dify 的 `.env` 或环境变量配置中维护，**值不入库**：

| 变量名 | 用途 |
|---|---|
| `OLLAMA_BASE_URL` | 本地模型服务地址 |
| `DOCX_SERVICE_URL` | Markdown to DOCX 服务地址（若自建） |
| `XXL_JOB_*` | 调度中心配置 |

## 待办

- [ ] Dify 部署完成后记录版本号
- [ ] 导出首个 `workflow_v0.1.yml`
- [ ] 编写 `config.md`

> ⚠️ 注意：Docker 部署时注意 `.env` 不要提交到仓库。此仓库当前**不是 git 仓库**，若初始化 git，需一并写 `.gitignore`。
