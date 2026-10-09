# -*- coding: utf-8 -*-
"""
表单自动识别 —— 自由文本进，表单字段出（字段集合按 profile 从 build_workflow 取）。

产品侧工具（本地工具）：用户在网页「粘贴识别」框里直接丢项目信息与想法，
本脚本调 Ollama 抽成结构化字段，用户核对后提交。

不编造是硬约束：只抽文本里明确提到的，没提到就空 —— 提示词
（scripts/prompts/10_parse_input.md）写死，单测也卡这一条。LLM 输出只当建议，
**表单提交权在人**：识别结果回填到表单，用户核对修改后才提交。

与 run_pipeline 同属本地工具，import 它是 scripts/README「本地工具例外」许可的。
"""

import argparse
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

sys.path.insert(0, HERE)
import build_workflow  # noqa: E402  字段/选项按 profile 从单一真相源取
import run_pipeline  # noqa: E402


def _fields(profile="enterprise"):
    """start_vars → (变量名, 标签, 类型) 三元组。类型归并为 text/select/number。"""
    return [(v[0], v[1],
             "select" if v[2] == "select" else
             "number" if v[2] == "number" else "text")
            for v in build_workflow.get_profile(profile)["start_vars"]]


# 企业别名：旧测试与调用方引用的模块级名字，字段真相源已收口到 build_workflow。
FIELDS = _fields("enterprise")
DECLARATION_OPTIONS = build_workflow.PROFILES["enterprise"]["declaration_options"]

_FENCE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")
_INT = re.compile(r"^\s*(\d+)")

def _user_prompt(text, profile="enterprise"):
    cfg = build_workflow.get_profile(profile)
    lines = ["表单字段（只抽这些）："]
    for var, label, _ftype in _fields(profile):
        note = ""
        if var == cfg["declaration_var"]:
            options = cfg["declaration_options"]
            note = "（%s选一：%s）" % (build_workflow._cn(len(options)),
                                      " / ".join(options))
        if _ftype == "number":
            note = "（整数，没提填 0）"
        lines.append("- %s %s：%s" % (var, label, note) if note
                     else "- %s %s" % (var, label))
    lines.append("")
    lines.append("文本：")
    lines.append(text)
    lines.append("")
    lines.append("输出 JSON：")
    keys = ", ".join('"%s": "…"' % v for v, _l, _t in _fields(profile))
    lines.append("{%s}" % keys)
    return "\n".join(lines)


def _extract_json(text):
    """模型不总给干净 JSON：剥代码围栏、从第一个 { 切到最后一个 }。"""
    stripped = _FENCE.sub("", text.strip())
    try:
        return json.loads(stripped)
    except ValueError:
        pass
    start, end = stripped.find("{"), stripped.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("输出里没有 JSON 对象")
    return json.loads(stripped[start:end + 1])


def _normalize(raw, profile="enterprise"):
    """模型输出 → 干净字段。类型不对、超界、选项对不上都按「没提到」处理。"""
    cfg = build_workflow.get_profile(profile)
    fields = {v: "" for v, _l, _t in _fields(profile)}
    for var, _label, ftype in _fields(profile):
        value = raw.get(var)
        if value is None:
            continue
        if ftype == "number":
            if isinstance(value, (int, float)):
                value = str(value)
            m = _INT.match(str(value))
            if m:
                n = int(m.group(1))
                fields[var] = n if 1 <= n <= 10000 else 0
            continue
        value = str(value).strip()
        if not value:
            continue
        if var == cfg["declaration_var"]:
            if value not in cfg["declaration_options"]:
                matched = None
                for keyword, option in cfg["declaration_fuzzy"].items():
                    if keyword in value:
                        matched = option
                        break
                # 企业：提了但对应不上 → 其他；学生：不编造，对应不上就空
                value = matched or ("其他" if cfg["declaration_fuzzy"] else "")
            fields[var] = value
        else:
            fields[var] = value
    return fields


def parse_inputs(text, model=None, base_url=None, temperature=None,
                 num_ctx=None, timeout=None, profile="enterprise"):
    """
    自由文本 → {"fields", "missing", "raw"}。字段集合由 profile 决定。

    fields 全部键与 profile 的 start_vars 一致；missing 是没提到的字段标签列表；
    模型输出解析失败时返回 fields 全空 + parse_error（不抛异常 —— 识别只是辅助，
    识别失败表单照填）。
    """
    empty = {v: "" for v, _l, _t in _fields(profile)}
    if not (text or "").strip():
        return {"fields": empty, "missing": [l for _v, l, _t in _fields(profile)],
                "raw": text, "error": "empty"}

    cfg = build_workflow.get_profile(profile)
    system = build_workflow.read_prompt(cfg["parse_prompt"], profile=profile).rstrip("\n")

    try:
        if model is None and base_url is None:
            base_url, model, provider, api_key = run_pipeline.resolve_llm()
        else:
            base_url = base_url or os.environ.get("OLLAMA_BASE_URL") \
                or run_pipeline.DEFAULT_BASE_URL
            model = model or os.environ.get("OLLAMA_MODEL") \
                or run_pipeline.DEFAULT_MODEL
            provider, api_key = "ollama", None
        content = run_pipeline.chat(
            base_url, model, system, _user_prompt(text, profile=profile),
            temperature if temperature is not None else 0.0,
            num_ctx or 8192,
            timeout or run_pipeline.TIMEOUT,
            provider=provider, api_key=api_key,
        )
        raw = _extract_json(content)
    except Exception as exc:
        return {"fields": empty, "missing": [l for _v, l, _t in _fields(profile)],
                "raw": text, "error": "parse_failed",
                "parse_error": "%s: %s" % (type(exc).__name__, exc)}

    fields = _normalize(raw, profile=profile)
    missing = [label for var, label, _ftype in _fields(profile)
               if fields[var] in ("", 0)]
    return {"fields": fields, "missing": missing, "raw": text}


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="自由文本 → 表单字段（本地调试用）")
    parser.add_argument("text_file", help="自由文本文件（UTF-8）")
    parser.add_argument("--profile", default="enterprise",
                        choices=build_workflow.profile_ids(),
                        help="申报对象（enterprise 科技企业 / student 大学生科研）")
    args = parser.parse_args(argv)

    with open(args.text_file, encoding="utf-8") as fh:
        text = fh.read()
    result = parse_inputs(text, profile=args.profile)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if "error" not in result else 1


if __name__ == "__main__":
    sys.exit(main())
