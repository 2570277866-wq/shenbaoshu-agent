# memory_service/ — 本地记忆服务

把 5 个记忆工具做成 HTTP 端点，给 Dify 的 HTTP 节点调用。

**为什么非得有这一层：** Dify 的 LLM 节点读不到本地文件系统，代码节点的沙箱也不保证
可持久读写。Agent 想往 `memory/` 里写决策和经验，中间必须有一条通路。见
`docs/ARCHITECTURE.md` 9.2。

```
Dify 工作流 ──HTTP──► 本地记忆服务 ──► memory/*.md
```

## 启动

```bash
cd memory_service
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

export MEMORY_DIR=/path/to/project/memory    # 不设则用仓库内的 memory/
export MEMORY_SERVICE_TOKEN=$(openssl rand -hex 16)   # 可选，设了就要求带令牌
.venv/bin/python main.py
```

默认监听 `127.0.0.1:8787`。改端口用 `MEMORY_SERVICE_PORT`，改地址用 `MEMORY_SERVICE_HOST`
（**无令牌时绑非本机地址会直接拒绝启动**，见下文「安全边界」）。

## 端点

| 方法 | 路径 | 对应工具 | 说明 |
|---|---|---|---|
| POST | `/memory/read` | `memory_read` | 读文件 |
| POST | `/memory/write` | `memory_write` | 覆盖 / 追加 |
| POST | `/memory/replace` | `memory_replace` | 替换片段 |
| POST | `/memory/insert` | `memory_insert` | 插入条目 |
| GET | `/memory/list` | `memory_list` | 列出记忆文件 |
| GET | `/memory/inject` | — | 拼 CONTEXT + AGENTS，供系统提示词注入 |
| POST | `/memory/rollback` | — | 从备份恢复（契约外补充，见下） |
| GET | `/healthz` | — | 健康检查，不校验令牌 |

成功一律 `{"ok": true, ...}`，失败一律
`{"ok": false, "error": {"code": "...", "message": "..."}}` + 对应 HTTP 状态码。
**Dify 的条件分支读 `error.code` 判分支**，别去解析 message。

### 错误码

| code | HTTP | 什么时候 |
|---|---|---|
| `unknown_file` | 400 | 文件名不在白名单（含一切路径穿越尝试） |
| `file_not_found` | 404 | 白名单内但文件不存在 |
| `bad_request` | 400 | 参数不合法（mode/position 取值、old 为空、条目带一级标题…） |
| `content_too_large` | 413 | 单次写入超 512 KB |
| `file_too_large` | 413 | 写完全超 4 MB |
| `not_found` | 409 | `replace` 的 `old` 一处都没匹配上 |
| `ambiguous` | 409 | `replace` 的 `old` 匹配到多处，且未传 `allow_multiple` |
| `anchor_not_found` | 409 | `insert` 的锚点行不存在 |
| `need_anchor` | 400 | 往 CONTEXT/AGENTS 插入但没给锚点 |
| `unauthorized` | 401 | 令牌缺失或不对 |

### 例子

```bash
B=http://127.0.0.1:8787

curl -s $B/memory/list
curl -s $B/memory/inject
curl -s -X POST $B/memory/read -H 'Content-Type: application/json' \
     -d '{"file":"decisions.md"}'

# 写决策（生成完成后）。dry_run 先看一眼要改成什么样
curl -s -X POST $B/memory/write -H 'Content-Type: application/json' \
     -d '{"file":"lessons.md","content":"...","mode":"overwrite","dry_run":true}'

# 往 decisions.md 插一条 —— 不给 position 默认插在第一条之前（最新在上）
curl -s -X POST $B/memory/insert -H 'Content-Type: application/json' \
     -d '{"file":"decisions.md","content":"## 2026-09-18 — 标题\n\n**决策：** …\n\n---"}'

# 往 lessons.md 插一条 —— 默认锚点是「## 已记录」，自动插进那一节
curl -s -X POST $B/memory/insert -H 'Content-Type: application/json' \
     -d '{"file":"lessons.md","content":"### 2026-09-18 · 技术方案\n\n| 字段 | 说明 |"}'
```

**条目末尾的 `---` 自己带。** 服务不猜文档风格 —— 它只做「插到第 N 行 + 空行规整」，
分隔符、字段表这些属于内容。decisions.md 的条目之间要 `---`，所以上面例子里带了。

## 插到哪：position 与默认锚点

| position | 落点 |
|---|---|
| `before_first_heading` | 第一个 `## ` 之前 |
| `after_anchor` | 锚点行之后（先吃掉紧跟的空行再插） |
| `before_anchor` | 锚点行之前 |
| `bottom` | 文件末尾 |

不传 `position` 时按文件取默认：

| 文件 | 默认 | 为什么 |
|---|---|---|
| `decisions.md` | `before_first_heading` | 最新在上 |
| `lessons.md` | `after_anchor` → `## 已记录` | 插进「已记录」小节，不落在「记录时机」那些结构段里 |
| `CONTEXT.md` / `AGENTS.md` | **拒绝**（`need_anchor`） | 规则文件，结构由人维护，不接受自动插入 |

## 安全边界

1. **只监听 127.0.0.1。** 要绑别的地址必须同时设 `MEMORY_SERVICE_TOKEN`，
   否则 `main.py` 拒绝启动。这不是洁癖：这服务能改记忆文件，而记忆文件会
   **注入 system prompt** —— 暴露到局域网等于把提示词注入的入口交出去。
2. **文件名白名单精确匹配**，不做路径拼接。`../`、`a/b`、绝对路径、`.backups/x.bak`
   全部在 `store.resolve()` 门口返回 `unknown_file`。
3. **写前备份。** 每次改动先存一份到 `memory/.backups/`，每个文件保留最近 20 份。
   有了备份就得有恢复手段，所以补了 `/memory/rollback`（设计契约外的端点）。
4. **原子写。** 临时文件 + `os.replace`，读者看不到写一半的文件。
   只有「读—改—写」的复合操作加锁（线程锁 + `fcntl` 文件锁）。
5. **审计。** 每次写操作往 `memory/.audit.jsonl` 追加一行（时间、操作、文件、
   字节数变化、备份名、actor）。远程监看和事后追责都靠它。`dry_run` 不记。

## 在 Dify 里怎么接

HTTP 节点，方法 POST，Body 选 JSON：

```json
{
  "file": "decisions.md",
  "content": "## {{#start.project_name#}} — {{#start.declaration_type#}}\n\n**决策：** 本次采用 …\n\n---",
  "actor": "dify-workflow"
}
```

**网络连通性有个坑。** Dify 跑在 Docker 里，容器内的 `127.0.0.1` 是容器自己，不是宿主机：

| 部署 | 服务绑哪 | Dify 里填什么 |
|---|---|---|
| Docker Desktop（Mac/Win，开发机） | `127.0.0.1:8787`（默认） | `http://host.docker.internal:8787` |
| Linux 目标机，容器 bridge 网络 | `172.17.0.1:8787`（docker0 网关） | `http://172.17.0.1:8787` |
| Linux 目标机，容器 `network_mode: host` | `127.0.0.1:8787`（默认） | `http://127.0.0.1:8787` |

Linux 绑 `172.17.0.1` 时 `_check_bind` 会要求设令牌 —— 设上，别绕。
`172.17.0.1` 不对外网暴露，但它对**所有**本地容器可见，令牌是应有的一道。

## 产出物

```
memory/
├── CONTEXT.md  AGENTS.md  decisions.md  lessons.md    ← 四个记忆文件，人可直接改
├── .backups/      每次写入前的快照，保留最近 20 份/文件
├── .locks/        跨进程文件锁，0 字节，别删也别提交
└── .audit.jsonl   写操作审计，一行一条 JSON
```

`memory/.backups/`、`memory/.locks/`、`memory/.audit.jsonl` 是**服务产物，不是记忆内容** ——
真要用 git 管仓库，把它们 ignore 掉，并在交付前清理。

## 测试

```bash
cd memory_service
.venv/bin/python -m unittest test_service -v
```

51 项。每个测试用独立临时目录、复制一份真记忆文件进去，**不碰仓库里的真文件**；
`_REPO_SNAPSHOT` 在导入时拍快照，跑完比对，防止 `MEMORY_DIR` 没生效导致静默污染。

## 实测发现（2026-09-18）

写这个服务时踩到的，都是「不报错但错」那一类：

| 发现 | 影响 |
|---|---|
| 回滚的路径校验把自己返回的格式拒了 | 备份名是 `.backups/xxx.bak`，含 `/`；「含斜杠就拒」的土办法把合法输入也挡了。改用整串正则匹配 |
| 锁文件落在 `memory/` 根下 | `fcntl` 要一个实体文件，0 字节的 `.decisions.md.lock` 和记忆文件混在一层，人看见会当成垃圾删掉。挪进 `.locks/` |
| 「找不到就静默跳过」比报错更危险 | `replace` 匹配 0 处若静默通过，调用方以为改了、其实没改，且人读文档看不出来。所以 0 处与多处都返回 409 |
| 测试可能改到真记忆文件 | 每个测试用临时目录 + 导入时快照比对（`_REPO_SNAPSHOT`）。`MEMORY_DIR` 一旦没生效，测试会静默污染 Agent 记忆且不报错 |

## 待确认

- [ ] **记忆写入是否在生成流程内自动触发，还是人工确认后写入**
      （设计文档第十节遗留问题）。服务两边的能力都有：直接写，或先 `dry_run` 看一眼。
- [ ] Dify HTTP 节点的超时设置 —— 本服务毫秒级返回，但节点默认超时值要确认不会
      比工作流里其他节点短。
- [ ] 备份是否需要跨机保留（现在只在本地，目标机磁盘挂了就一起没了）。
- [ ] 多轮生成并发写同一文件时，`ambiguous` 的触发频率是否高到需要放宽。
