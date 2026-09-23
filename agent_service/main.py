# -*- coding: utf-8 -*-
"""
申报书 Agent 产品服务 —— 脱离 Dify 的交付形态。

企业浏览器填表 → 本服务在后台串行跑全链路（素材编号 → 要素抽取 →
七章 LLM → 拼接 → 十项一致性审查）→ 网页看进度、看 issues、下载 docx。

引擎 = scripts/run_pipeline.py（与命令行共用同一份）；模型与推理机地址走
环境变量 —— 换 32b、指局域网 GPU 机，改配置不改代码。

启动：
    python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
    .venv/bin/uvicorn main:app --host 127.0.0.1 --port 8000
    或  .venv/bin/python main.py

安全边界（照 memory_service，三条，别松）：
    1. 默认只监听 127.0.0.1。要监听局域网必须同时设 AGENT_SERVICE_TOKEN ——
       见 __main__ 里的硬校验，无令牌绑非回环地址直接拒绝启动。
    2. 令牌校验覆盖全部 /api/* 数据接口（compare_digest 比较）。
       / 与 /runs/* 只给页面壳，壳里没有任何数据，数据全靠受保护的接口拉。
    3. 只连 OLLAMA_BASE_URL（默认 127.0.0.1:11434），不发任何其他网络请求。

任务模型：单 worker 线程 + 队列。Ollama 本来就串行（7 章并行会撞超时，
2026-09-21 Dify 首跑实测），队列长度 1 是事实约束不是偷懒。
状态落 RUNS_DIR/<run_id>/state.json，服务重启后 running/queued 标 interrupted。
"""

import io
import json
import os
import queue
import re
import secrets
import sys
import threading
import time
from datetime import datetime
from typing import Optional
from urllib.parse import quote

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, "scripts")
sys.path.insert(0, SCRIPTS)

import md_to_docx  # noqa: E402
import run_pipeline  # noqa: E402

DEFAULT_PORT = 8000
HERE = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(HERE, "static")
RUNS_DIR = os.environ.get("RUNS_DIR") or os.path.join(HERE, "runs")
CONVERT_DIR = os.path.join(RUNS_DIR, "_convert")

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

app = FastAPI(
    title="项目申报书 Agent · 产品服务",
    description="申报书自动撰写（初稿 + 机械审查）。AI 出初稿，人工做最终审核。仅内网使用。",
    version="1.0.0",
)

# ---------------------------------------------------------------- 鉴权


def require_token(x_agent_token: str = Header(default="")):
    """设了 AGENT_SERVICE_TOKEN 才校验。没设 = 只监听 localhost 的开发模式。"""
    expected = os.environ.get("AGENT_SERVICE_TOKEN") or ""
    if not expected:
        return
    if not secrets.compare_digest(x_agent_token, expected):
        raise HTTPException(status_code=401, detail="缺少或错误的 X-Agent-Token")


# ---------------------------------------------------------------- 任务队列

JOBS = {}          # run_id -> job dict
QUEUE = queue.Queue()
LOCK = threading.Lock()
WORKER_STARTED = False

ENGINE = run_pipeline.run_workflow   # 测试时替换成假引擎

TERMINAL = ("done", "failed", "interrupted")


def _job_dir(run_id):
    return os.path.join(RUNS_DIR, run_id)


def _new_run_id():
    base = "run_" + time.strftime("%Y%m%d-%H%M%S")
    run_id = base
    n = 2
    while os.path.exists(_job_dir(run_id)):
        run_id = "%s-%d" % (base, n)
        n += 1
    return run_id


def _persist(job):
    """状态落盘。先写临时文件再改名 —— 半截状态文件比慢一点更糟。"""
    tmp = os.path.join(job["dir"], "state.json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({k: v for k, v in job.items() if k not in ("inputs", "dir")},
                  fh, ensure_ascii=False, indent=2)
    os.replace(tmp, os.path.join(job["dir"], "state.json"))


def _transition(job, state, error=None):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    job["state"] = state
    if state == "running" and not job.get("started_at"):
        job["started_at"] = now
    if state in TERMINAL:
        job["finished_at"] = now
    if error:
        job["error"] = error
    _persist(job)


def _worker():
    while True:
        run_id = QUEUE.get()
        job = JOBS.get(run_id)
        if not job:
            continue
        _transition(job, "running")

        def on_progress(step, state, detail):
            job["progress"].append({
                "step": step, "state": state, "detail": detail,
                "at": time.strftime("%H:%M:%S"),
            })
            _persist(job)

        try:
            result = ENGINE(job["inputs"], on_progress=on_progress)
            run_pipeline.archive(result, job["inputs"], job["dir"])
            job["pass"] = result["pass"]
            job["blocking"] = result["check_stats"]["blocking"]
            _transition(job, "done")
        except Exception as exc:
            _transition(job, "failed", "%s: %s" % (type(exc).__name__, exc))


def _ensure_worker():
    global WORKER_STARTED
    with LOCK:
        if WORKER_STARTED:
            return
        threading.Thread(target=_worker, daemon=True, name="pipeline-worker").start()
        WORKER_STARTED = True


def submit(inputs):
    run_id = _new_run_id()
    job_dir = _job_dir(run_id)
    os.makedirs(job_dir, exist_ok=True)
    job = {
        "id": run_id,
        "state": "queued",
        "project_name": inputs.get("project_name") or "",
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "started_at": None,
        "finished_at": None,
        "error": None,
        "pass": None,
        "blocking": None,
        "progress": [],
        "inputs": inputs,
        "dir": job_dir,
    }
    with LOCK:
        JOBS[run_id] = job
    _persist(job)
    _ensure_worker()
    QUEUE.put(run_id)
    return job


def recover_interrupted():
    """重启后 running/queued 都是再也不会动的尸体 —— 标 interrupted，让人看得见。"""
    if not os.path.isdir(RUNS_DIR):
        return 0
    fixed = 0
    for name in sorted(os.listdir(RUNS_DIR)):
        path = os.path.join(RUNS_DIR, name, "state.json")
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                state = json.load(fh)
        except (ValueError, OSError):
            continue
        if state.get("state") in ("queued", "running"):
            state["state"] = "interrupted"
            state["error"] = "服务重启，任务中断"
            state["finished_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(state, fh, ensure_ascii=False, indent=2)
            fixed += 1
    return fixed


def _read_state(run_id):
    job = JOBS.get(run_id)
    if job:
        return {k: v for k, v in job.items() if k not in ("inputs", "dir")}
    path = os.path.join(_job_dir(run_id), "state.json")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="没有这个任务：" + run_id)
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _result_file(run_id, name):
    path = os.path.join(_job_dir(run_id), name)
    if not os.path.exists(path):
        raise HTTPException(status_code=404,
                            detail="任务 %s 还没有产出 %s（可能尚未完成）" % (run_id, name))
    return path


# ---------------------------------------------------------------- 请求模型


class RunRequest(BaseModel):
    project_name: str = Field(..., max_length=200)
    declaration_type: str = Field(...)
    tech_direction: str = Field(..., max_length=200)
    project_leader: str = Field(..., max_length=100)
    team_size: int = Field(..., ge=1, le=10000)
    budget_range: str = Field(..., max_length=100)
    expected_outcome: str = Field(..., max_length=2000)
    project_highlights: str = Field(..., max_length=2000)
    special_requirements: str = Field("", max_length=2000)
    material_tech: str = Field("", max_length=8000)
    material_ip: str = Field("", max_length=8000)
    material_finance: str = Field("", max_length=8000)
    style_input: str = Field("", max_length=4000)


# ---------------------------------------------------------------- 页面


@app.get("/", include_in_schema=False)
def index_page():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/runs/{run_id}", include_in_schema=False)
def run_page(run_id: str):
    return FileResponse(os.path.join(STATIC_DIR, "run.html"))


@app.get("/runs/{run_id}/result", include_in_schema=False)
def result_page(run_id: str):
    return FileResponse(os.path.join(STATIC_DIR, "result.html"))


# ---------------------------------------------------------------- API


@app.get("/api/form")
def form_fields():
    """表单字段定义 —— 从 build_workflow.START_VARS 出，前端动态建表单，单一真相源。"""
    import build_workflow
    return {"fields": [
        {"var": v[0], "label": v[1], "type": v[2], "max_length": v[3],
         "required": v[4], "options": v[5]}
        for v in build_workflow.START_VARS
    ]}


@app.post("/api/runs", dependencies=[Depends(require_token)])
def create_run(req: RunRequest):
    job = submit(req.model_dump())
    return {"run_id": job["id"], "url": "/runs/%s" % job["id"]}


@app.get("/api/runs", dependencies=[Depends(require_token)])
def list_runs():
    if not os.path.isdir(RUNS_DIR):
        return {"runs": []}
    runs = []
    for name in sorted(os.listdir(RUNS_DIR), reverse=True):
        path = os.path.join(RUNS_DIR, name, "state.json")
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                state = json.load(fh)
        except (ValueError, OSError):
            continue
        runs.append({
            "run_id": state.get("id", name),
            "state": state.get("state"),
            "project_name": state.get("project_name"),
            "created_at": state.get("created_at"),
        })
    return {"runs": runs}


@app.get("/api/runs/{run_id}", dependencies=[Depends(require_token)])
def run_status(run_id: str):
    return _read_state(run_id)


@app.get("/api/runs/{run_id}/document", dependencies=[Depends(require_token)])
def run_document(run_id: str):
    path = _result_file(run_id, "document.md")
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    return Response(text, media_type="text/plain; charset=utf-8")


@app.get("/api/runs/{run_id}/report", dependencies=[Depends(require_token)])
def run_report(run_id: str):
    path = _result_file(run_id, "check_report.json")
    with open(path, encoding="utf-8") as fh:
        return JSONResponse(json.load(fh))


def _docx_response(data, filename):
    return Response(
        data, media_type=DOCX_MIME,
        headers={"Content-Disposition":
                 "attachment; filename*=UTF-8''%s" % quote(filename)})


@app.get("/api/runs/{run_id}/docx", dependencies=[Depends(require_token)])
def run_docx(run_id: str):
    state = _read_state(run_id)
    with open(_result_file(run_id, "document.md"), encoding="utf-8") as fh:
        text = fh.read()

    try:
        doc = md_to_docx.convert(text, title=state.get("project_name") or "项目申报书")
    except RuntimeError:
        raise HTTPException(status_code=501,
                            detail="服务端没装 python-docx —— 见 agent_service/requirements.txt")
    buf = io.BytesIO()
    md_to_docx.save(doc, buf)
    title = state.get("project_name") or run_id
    return _docx_response(buf.getvalue(), "申报书-%s.docx" % title)


# ---------------------------------------------------------------- docx 转换（Dify 节点调这里）


@app.post("/api/convert", dependencies=[Depends(require_token)])
async def convert_markdown(request: Request):
    """Markdown 进，docx 出 —— Dify 工作流的 docx 节点（HTTP 请求节点）调这里。

    请求体就是 Markdown 原文（raw text），title 不单独传：md 首行 `# 标题`
    自带标题。不用 JSON 是因为正文里的引号/换行进 JSON 要转义，Dify 的变量
    替换只做字符串替换不会转义 —— 传 raw text 才能避开整个坑。

    ?mode=url：docx 落盘、返回下载链接（Dify 版本收不了二进制文件时的备选）。
    ?title=：可选，覆盖 md 首行标题。
    """
    text = (await request.body()).decode("utf-8")
    title = request.query_params.get("title") or None
    mode = request.query_params.get("mode", "binary")

    try:
        doc = md_to_docx.convert(text, title=title)
    except RuntimeError:
        raise HTTPException(status_code=501,
                            detail="服务端没装 python-docx —— 见 agent_service/requirements.txt")
    buf = io.BytesIO()
    md_to_docx.save(doc, buf)
    filename = "申报书-%s.docx" % (title or "未命名")

    if mode == "url":
        os.makedirs(CONVERT_DIR, exist_ok=True)
        cid = secrets.token_hex(8)
        with open(os.path.join(CONVERT_DIR, cid + ".docx"), "wb") as fh:
            fh.write(buf.getvalue())
        return {"url": "/api/convert/%s/download" % cid, "filename": filename}

    return _docx_response(buf.getvalue(), filename)


@app.get("/api/convert/{cid}/download", dependencies=[Depends(require_token)])
def convert_download(cid: str):
    if not re.fullmatch(r"[0-9a-f]{16}", cid):
        raise HTTPException(status_code=404, detail="没有这个转换结果")
    path = os.path.join(CONVERT_DIR, cid + ".docx")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="没有这个转换结果（可能已过期）")
    return FileResponse(path, media_type=DOCX_MIME, filename="申报书.docx")


recover_interrupted()


if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", DEFAULT_PORT))
    if host not in ("127.0.0.1", "localhost", "::1") and not os.environ.get("AGENT_SERVICE_TOKEN"):
        raise SystemExit(
            "绑定非回环地址（%s）必须设 AGENT_SERVICE_TOKEN —— 服务能读企业申报数据，"
            "无令牌对局域网开放等于裸奔" % host)
    import uvicorn
    uvicorn.run(app, host=host, port=port)
