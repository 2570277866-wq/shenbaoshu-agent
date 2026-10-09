# -*- coding: utf-8 -*-
"""
管线运行器 —— 脱离 Dify 直接跑全链路：素材编号 → 要素抽取 → 七章串行 LLM →
章节拼接 → 一致性审查。产品服务（agent_service）与命令行共用同一引擎。

为什么有它：
    引擎逻辑本就在 `scripts/`，本脚本把节点串成一段串行主循环 ——
    跑全流程最快、最透明，也是命令行与网页服务共用的引擎入口。

⚠ 一条约定例外：
    scripts/ 的「脚本之间不互相 import」是为贴进 Dify 代码节点而设；
    本脚本与 build_workflow.py 一样是**本地工具，不进 Dify**，故允许 import
    同目录脚本，并直接 import `build_workflow` 取 CHAPTERS / START_VARS /
    user_prompt —— 单一真相源，不复制（改了章节定义而不改本脚本，不会分叉）。

用法：
    python3 run_pipeline.py inputs.json            # scripts/inputs.sample.json 有样例
    python3 run_pipeline.py --model qwen3:32b --base-url http://192.168.1.10:11434 inputs.json

存档：`output/run_YYYYmmdd-HHMMSS/` 五件套 —— inputs.json / kb_index.json /
gen_elements.json / document.md / check_report.json。kb_index 必须随稿存档：
没有它，事后没人能回答「（S3）指的是什么」（scripts/README.md 素材编号契约）。

模型与地址走参数/环境变量，不写死 —— 换 32b、指局域网推理机，改配置不改代码。
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import assemble_document  # noqa: E402
import build_workflow  # noqa: E402
import check_consistency  # noqa: E402
import extract_elements  # noqa: E402
import number_material  # noqa: E402

TIMEOUT = 900  # 单章生成实测最长 254s（llm_03，14b），留足余量

PLACEHOLDER = re.compile(r"\{\{#([A-Za-z0-9_]+)\.([A-Za-z0-9_]+)#\}\}")

DEFAULT_MODEL = build_workflow.MODEL["name"]
DEFAULT_BASE_URL = "http://127.0.0.1:11434"

OPENAI_DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
OPENAI_DEFAULT_MODEL = "deepseek-chat"


def resolve_llm():
    """(base_url, model, provider, api_key) —— 推理层接线，环境变量决定。

    LLM_PROVIDER=openai 走 OpenAI 兼容云 API（默认 DeepSeek），
    否则走 Ollama（本机 / 局域网 GPU）。改配置不改代码。
    """
    if os.environ.get("LLM_PROVIDER", "ollama") == "openai":
        return (os.environ.get("OPENAI_BASE_URL") or OPENAI_DEFAULT_BASE_URL,
                os.environ.get("OPENAI_MODEL") or OPENAI_DEFAULT_MODEL,
                "openai",
                os.environ.get("OPENAI_API_KEY") or "")
    return (os.environ.get("OLLAMA_BASE_URL") or DEFAULT_BASE_URL,
            os.environ.get("OLLAMA_MODEL") or DEFAULT_MODEL,
            "ollama", None)


def _render_prompt(text, inputs, gen_elements, grouped):
    """
    user_prompt(chapter) 的占位符 → 实际内容。

    Dify 在运行时注入变量，这里复刻同样的注入：
      {{#start_node.X#}}           表单值（str）
      {{#elements_node.gen_elements#}}  要素表 JSON
      {{#number_node.kb_material_G#}}   本章素材分组视图
    """
    def repl(match):
        node, var = match.group(1), match.group(2)
        if node == "start_node":
            return str(inputs.get(var) or "")
        if node == "elements_node" and var == "gen_elements":
            return json.dumps(gen_elements, ensure_ascii=False)
        if node == "number_node":
            return grouped.get(var, "")
        raise ValueError("未知占位符 {{#%s.%s#}} —— 提示词与生成器不一致？" % (node, var))

    return PLACEHOLDER.sub(repl, text)


def chat(base_url, model, system, user, temperature, num_ctx,
         timeout=TIMEOUT, provider="ollama", api_key=None):
    """
    单次 LLM 调用，返回回答正文。

    provider="ollama" 走 /api/chat；provider="openai" 走 OpenAI 兼容的
    /chat/completions（DeepSeek 等云 API，Authorization: Bearer）。
    deepseek-reasoner 的思考在 reasoning_content 字段 —— 只取正文 content。

    think 照开（质量来源，v0.8 复跑结论）；`<think>` 块由 assemble_document
    统一剥离，这里不处理。参数与 Dify 节点一致（COMPLETION_PARAMS）。
    """
    if provider == "openai":
        if not api_key:
            raise ValueError("LLM_PROVIDER=openai 必须设 OPENAI_API_KEY")
        payload = json.dumps({
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "stream": False,
        }).encode("utf-8")
        req = urllib.request.Request(
            base_url.rstrip("/") + "/chat/completions",
            data=payload,
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + api_key},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""

    payload = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "options": {"temperature": temperature, "num_ctx": num_ctx},
        "stream": False,
    }).encode("utf-8")

    req = urllib.request.Request(
        base_url.rstrip("/") + "/api/chat",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return (data.get("message") or {}).get("content") or ""


def run_workflow(inputs, model=None, base_url=None, temperature=None,
                 num_ctx=None, on_progress=None, timeout=TIMEOUT,
                 profile="enterprise"):
    """
    全链路。`inputs` = 开始节点变量（scripts/inputs.sample.json 格式），
    字段集合由 `profile`（build_workflow.PROFILES）决定：enterprise 13 字段 /
    student 16 字段。

    on_progress(step, state, detail)：step 为「素材编号 / 要素抽取 / NN 章节名 /
    章节拼接 / 一致性审查」，state 为 start / done / fail。

    单章失败不中断全流程：该章记 error、输出空串，拼接层放
    「本章生成失败，需人工撰写」占位符 —— 稿子少一章必须让人看得见
    （docs/WORKFLOW.md 5.1 E5）。

    返回：document / pass / issues / check_stats / gen_elements / kb_index /
    number_stats / elements_stats / assemble_issues / assemble_stats / sections
    / profile
    """
    cfg = build_workflow.get_profile(profile)
    if model is None and base_url is None:
        base_url, model, provider, api_key = resolve_llm()
    else:
        model = model or os.environ.get("OLLAMA_MODEL") or DEFAULT_MODEL
        base_url = base_url or os.environ.get("OLLAMA_BASE_URL") or DEFAULT_BASE_URL
        provider, api_key = "ollama", None
    temperature = build_workflow.COMPLETION_PARAMS["temperature"] if temperature is None else temperature
    num_ctx = build_workflow.COMPLETION_PARAMS["num_ctx"] if num_ctx is None else num_ctx

    def progress(step, state, detail=""):
        if on_progress:
            on_progress(step, state, detail)

    system_text = build_workflow.read_prompt(
        build_workflow.SYSTEM_PROMPT, profile=profile).rstrip("\n")

    # 节点⓪ 素材编号
    progress("素材编号", "start")
    try:
        number_result = number_material.main(**{
            "kb_material_" + group: str(inputs.get(src) or "")
            for group, src in sorted(cfg["material_source"].items())
        })
        kb_block = number_result["kb_material"]
        progress("素材编号", "done",
                 "编号 %d 条，丢弃 %d 条" % (number_result["stats"]["count"],
                                        number_result["stats"]["dropped"]))
    except Exception as exc:
        progress("素材编号", "fail", "%s: %s" % (type(exc).__name__, exc))
        raise

    # 节点① 要素抽取。in_* 参数按 profile 的 elements_vars 映射；
    # 映射值为 None 的键不传（如学生无 team_size），缺项落 gen_elements.missing。
    progress("要素抽取", "start")
    try:
        element_inputs = {
            param: inputs.get(var)
            for param, var in cfg["elements_vars"].items() if var
        }
        if cfg.get("budget_subjects"):
            element_inputs["budget_subjects"] = cfg["budget_subjects"]
        elements_result = extract_elements.main(kb_material=kb_block, **element_inputs)
        gen_elements = elements_result["gen_elements"]
        progress("要素抽取", "done",
                 "缺 %d 项：" % elements_result["stats"]["missing"]
                 + "、".join(gen_elements["missing"][:5])
                 + ("…" if len(gen_elements["missing"]) > 5 else ""))
    except Exception as exc:
        progress("要素抽取", "fail", "%s: %s" % (type(exc).__name__, exc))
        raise

    # 各章节 LLM，串行。并行会 N 个请求同时打单机 Ollama，后面的吃读超时
    # （2026-09-21 Dify 首跑实测）—— 故顺序逐章，别无选择。
    sections = {}
    section_errors = {}
    for chapter in cfg["chapters"]:
        label = "%s %s" % (chapter["num"], chapter["title"])
        progress(label, "start")
        # 提示词渲染也在 try 里：章节提示词文件丢了同样降级本章，
        # 不能让一次 FileNotFoundError 杀掉整轮（跑中途搬 prompts 目录的教训）。
        try:
            user = _render_prompt(
                build_workflow.user_prompt(chapter, profile=profile),
                inputs, gen_elements, number_result)
            sections[chapter["out"]] = chat(
                base_url, model, system_text, user, temperature, num_ctx, timeout,
                provider=provider, api_key=api_key)
            progress(label, "done", "%d 字" % len(sections[chapter["out"]]))
        except (urllib.error.URLError, urllib.error.HTTPError,
                TimeoutError, ConnectionError, ValueError, OSError) as exc:
            sections[chapter["out"]] = ""
            section_errors[label] = "%s: %s" % (type(exc).__name__, exc)
            progress(label, "fail", section_errors[label])

    # 节点② 章节拼接（think 剥离在此层）
    progress("章节拼接", "start")
    try:
        assemble_result = assemble_document.main(
            **sections, in_project_name=inputs.get("project_name"),
            in_sections=build_workflow.section_spec(profile))
        gen_document = assemble_result["gen_document"]
        progress("章节拼接", "done",
                 "%d 字，剥 think %d 块" % (assemble_result["stats"]["chars"],
                                        assemble_result["stats"]["think_stripped"]))
    except Exception as exc:
        progress("章节拼接", "fail", "%s: %s" % (type(exc).__name__, exc))
        raise

    # 节点③ 一致性审查。表单字段出处白名单与检查项按 profile 配置。
    progress("一致性审查", "start")
    try:
        check_kwargs = {
            "gen_document": gen_document,
            "gen_elements": gen_elements,
            "kb_material": kb_block,
            "in_form_fields": cfg["check_form_fields"],
        }
        if cfg.get("checks"):
            check_kwargs["in_checks"] = cfg["checks"]
        for f in cfg["check_form_fields"]:
            check_kwargs["user_form_" + f] = inputs.get(f) or ""
        check_result = check_consistency.main(**check_kwargs)
        stats = check_result["stats"]
        progress("一致性审查", "done",
                 "block %d / warn %d / 待补充 %d" % (stats["blocking"],
                                                stats["warnings"], stats["tbd_count"]))
    except Exception as exc:
        progress("一致性审查", "fail", "%s: %s" % (type(exc).__name__, exc))
        raise

    return {
        "document": gen_document,
        "pass": check_result["pass"],
        "issues": check_result["issues"],
        "check_stats": check_result["stats"],
        "gen_elements": gen_elements,
        "kb_index": number_result["kb_index"],
        "number_stats": number_result["stats"],
        "elements_stats": elements_result["stats"],
        "assemble_issues": assemble_result["issues"],
        "assemble_stats": assemble_result["stats"],
        "sections": sections,
        "section_errors": section_errors,
        "profile": profile,
        "model": model,
        "base_url": base_url,
        "provider": provider,
    }


def _run_id():
    return "run_" + time.strftime("%Y%m%d-%H%M%S")


def archive(result, inputs, out_dir, profile="enterprise"):
    """存档六件套。kb_index 随稿存档 —— 没有它（S3）无从核对；profile.json 记录申报对象。"""
    cfg = build_workflow.get_profile(profile)
    files = {
        "inputs.json": json.dumps(inputs, ensure_ascii=False, indent=2),
        "profile.json": json.dumps({"profile": profile, "label": cfg["label"]},
                                   ensure_ascii=False, indent=2),
        "kb_index.json": json.dumps(result["kb_index"], ensure_ascii=False, indent=2),
        "gen_elements.json": json.dumps(result["gen_elements"], ensure_ascii=False, indent=2),
        "document.md": result["document"],
        "check_report.json": json.dumps({
            "pass": result["pass"],
            "issues": result["issues"],
            "stats": result["check_stats"],
        }, ensure_ascii=False, indent=2),
    }
    for name, text in files.items():
        with open(os.path.join(out_dir, name), "w", encoding="utf-8") as fh:
            fh.write(text)
    return sorted(files)


def main(argv=None):
    parser = argparse.ArgumentParser(description="脱离 Dify 跑全链路申报书生成")
    parser.add_argument("inputs", nargs="?", help="inputs.json 路径；缺省读 stdin")
    parser.add_argument("--model", default=None, help="Ollama 模型名（默认 %s）" % DEFAULT_MODEL)
    parser.add_argument("--base-url", default=None, help="Ollama 地址（默认 %s）" % DEFAULT_BASE_URL)
    parser.add_argument("--out-dir", default=None, help="存档目录；缺省 output/<run_id>/")
    parser.add_argument("--profile", default="enterprise",
                        choices=build_workflow.profile_ids(),
                        help="申报对象（enterprise 科技企业 / student 大学生科研）")
    args = parser.parse_args(argv)

    raw = sys.stdin.read() if not args.inputs else open(args.inputs, encoding="utf-8").read()
    inputs = json.loads(raw)

    if args.out_dir:
        out_dir = args.out_dir
    else:
        out_dir = os.path.join(ROOT, "output", _run_id())

    def on_progress(step, state, detail):
        mark = {"start": "…", "done": "✅", "fail": "❌"}.get(state, "·")
        line = "%s %-10s %s" % (mark, step, detail)
        print(line, file=sys.stderr, flush=True)

    result = run_workflow(inputs, model=args.model, base_url=args.base_url,
                          on_progress=on_progress, profile=args.profile)

    os.makedirs(out_dir, exist_ok=True)
    files = archive(result, inputs, out_dir, profile=args.profile)

    print("存档：%s" % out_dir)
    for name in files:
        print("  %s" % name)
    print("申报对象：%s" % build_workflow.get_profile(args.profile)["label"])
    print("审查：pass=%s block=%d warn=%d 待补充=%d" % (
        result["pass"], result["check_stats"]["blocking"],
        result["check_stats"]["warnings"], result["check_stats"]["tbd_count"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
