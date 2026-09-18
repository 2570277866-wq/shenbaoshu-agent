# -*- coding: utf-8 -*-
"""
本地记忆服务 —— 把 5 个记忆工具暴露成 HTTP 端点，供 Dify HTTP 节点调用。

设计见 docs/ARCHITECTURE.md 9.2 / 9.4。这是 Dify 与本地文件系统之间唯一的通路：
Dify 的 LLM 节点读不到文件，代码节点沙箱不保证可持久读写，只能走 HTTP。

启动：
    pip install -r requirements.txt
    export MEMORY_DIR=/path/to/project/memory     # 不设则用仓库内的 memory/
    export MEMORY_SERVICE_TOKEN=$(openssl rand -hex 16)   # 可选，设了就要求带令牌
    python3 main.py                                # 或 uvicorn main:app --host 127.0.0.1 --port 8787

安全边界（三条，别松）：
    1. 默认只监听 127.0.0.1。要监听局域网必须同时设 MEMORY_SERVICE_TOKEN ——
       见 __main__ 里的硬校验，无令牌绑 0.0.0.0 直接拒绝启动。
    2. 文件名白名单精确匹配，不做路径拼接（store.resolve）。
    3. 不记录、不回传请求体之外的内容；审计日志只记操作元数据。
"""

import os
import secrets
from typing import Optional

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

import store
from store import MemoryServiceError

DEFAULT_PORT = 8787

app = FastAPI(
    title="项目申报书 Agent · 本地记忆服务",
    description="5 个记忆工具的 HTTP 实现。仅内网使用。",
    version="1.0.0",
)


# ---------------------------------------------------------------- 鉴权


def require_token(x_memory_token: str = Header(default="")):
    """
    设了 MEMORY_SERVICE_TOKEN 才校验。没设 = 只监听 localhost 的开发模式。

    用 compare_digest 而非 ==，避免按字符比较的时序差泄漏令牌。
    """
    expected = os.environ.get("MEMORY_SERVICE_TOKEN") or ""
    if not expected:
        return
    if not secrets.compare_digest(x_memory_token, expected):
        raise MemoryServiceError("unauthorized", "缺少或错误的 X-Memory-Token", 401)


@app.exception_handler(MemoryServiceError)
async def _memory_error_handler(request: Request, exc: MemoryServiceError):
    """服务自己抛的错统一出口。绝不让 FastAPI 的默认 500 页面漏出去 —— Dify 读不懂 HTML。"""
    return JSONResponse(
        status_code=exc.status,
        content={"ok": False, "error": {"code": exc.code, "message": exc.message}},
    )


# ---------------------------------------------------------------- 请求模型


class ReadReq(BaseModel):
    file: str = Field(..., description="记忆文件名，白名单内四个之一")


class WriteReq(BaseModel):
    file: str
    content: str
    mode: str = Field("overwrite", description="overwrite 覆盖 | append 追加")
    actor: Optional[str] = Field(None, description="谁写的，写进审计日志")
    dry_run: bool = Field(False, description="只回报将要发生什么，不落盘")


class ReplaceReq(BaseModel):
    file: str
    old: str = Field(..., description="待替换片段，精确匹配")
    new: str
    allow_multiple: bool = Field(False, description="匹配到多处时是否全部替换")
    actor: Optional[str] = None
    dry_run: bool = False


class InsertReq(BaseModel):
    file: str
    content: str = Field(..., description="条目正文，用 ## 开头")
    anchor: Optional[str] = Field(None, description="锚点行，精确匹配整行")
    position: Optional[str] = Field(
        None, description="after_anchor | before_anchor | before_first_heading | bottom")
    actor: Optional[str] = None
    dry_run: bool = False


class RollbackReq(BaseModel):
    file: str
    backup: str = Field(..., description="写操作返回的 backup 字段")


# ---------------------------------------------------------------- 端点


@app.get("/healthz")
def healthz():
    root = store.memory_root()
    return {"ok": True, "dir": str(root), "dir_exists": root.is_dir(),
            "auth": bool(os.environ.get("MEMORY_SERVICE_TOKEN")),
            "files": [f["file"] for f in store.list_files()["files"] if f["exists"]]}


@app.post("/memory/read", dependencies=[Depends(require_token)])
def memory_read(req: ReadReq):
    """memory_read —— 读文件。生成前回顾历史决策用。"""
    return store.read(req.file)


@app.post("/memory/write", dependencies=[Depends(require_token)])
def memory_write(req: WriteReq):
    """memory_write —— 覆盖 / 追加。"""
    return store.write(req.file, req.content, mode=req.mode, actor=req.actor, dry_run=req.dry_run)


@app.post("/memory/replace", dependencies=[Depends(require_token)])
def memory_replace(req: ReplaceReq):
    """memory_replace —— 替换片段。找不到或匹配多处都报错，不静默。"""
    return store.replace(req.file, req.old, req.new,
                         allow_multiple=req.allow_multiple, actor=req.actor, dry_run=req.dry_run)


@app.post("/memory/insert", dependencies=[Depends(require_token)])
def memory_insert(req: InsertReq):
    """memory_insert —— 插入条目。decisions/lessons 有默认位置，规则文件必须给锚点。"""
    return store.insert(req.file, req.content, anchor=req.anchor,
                        position=req.position, actor=req.actor, dry_run=req.dry_run)


@app.get("/memory/list", dependencies=[Depends(require_token)])
def memory_list():
    """memory_list —— 列出记忆文件。"""
    return store.list_files()


@app.get("/memory/inject", dependencies=[Depends(require_token)])
def memory_inject():
    """拼 CONTEXT + AGENTS，供系统提示词装配。"""
    return store.inject()


@app.post("/memory/rollback", dependencies=[Depends(require_token)])
def memory_rollback(req: RollbackReq):
    """误写回滚。设计契约外的补充端点 —— 有了备份就得有恢复手段，否则备份是摆设。"""
    return store.rollback(req.file, req.backup)


# ---------------------------------------------------------------- 启动


def _check_bind(host):
    """
    无令牌不得绑非本机地址。这条不是洁癖：服务能改 Agent 的记忆文件，
    而记忆文件会注入 system prompt —— 暴露到局域网等于把提示词注入的入口交出去。
    """
    if host in ("127.0.0.1", "localhost", "::1"):
        return
    if not os.environ.get("MEMORY_SERVICE_TOKEN"):
        raise SystemExit(
            "拒绝启动：MEMORY_SERVICE_HOST=%s 会对外暴露，但未设 MEMORY_SERVICE_TOKEN。\n"
            "该服务可写 Agent 记忆，记忆会注入 system prompt。要么绑回 127.0.0.1，"
            "要么先设令牌。" % host)


if __name__ == "__main__":
    import uvicorn

    host = os.environ.get("MEMORY_SERVICE_HOST", "127.0.0.1")
    port = int(os.environ.get("MEMORY_SERVICE_PORT", DEFAULT_PORT))
    _check_bind(host)
    print("记忆目录：%s" % store.memory_root())
    print("监听：http://%s:%d   鉴权：%s" % (host, port, "开" if os.environ.get("MEMORY_SERVICE_TOKEN") else "关"))
    # 单 worker。多 worker 虽有多进程文件锁兜底，但没必要 —— 这服务是低频调用，
    # 瓶颈在 LLM 不在这里。
    uvicorn.run(app, host=host, port=port)
