# -*- coding: utf-8 -*-
"""
表单自动识别 —— 自由文本进，13 个表单字段出。

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
PROMPT_PATH = os.path.join(ROOT, "scripts", "prompts", "10_parse_input.md")

sys.path.insert(0, HERE)
import run_pipeline  # noqa: E402

DECLARATION_OPTIONS = ("科技型中小企业", "高新技术企业", "专精特新", "其他")

# (变量名, 标签, 类型) —— 顺序即提示词里的顺序。
FIELDS = [
    ("project_name", "项目名称", "text"),
    ("declaration_type", "申报类型", "select"),
    ("tech_direction", "技术方向", "text"),
    ("project_leader", "项目负责人", "text"),
    ("team_size", "投入人数", "number"),
    ("budget_range", "预算规模", "text"),
    ("expected_outcome", "预期成果", "text"),
    ("project_highlights", "项目亮点", "text"),
    ("special_requirements", "特殊要求", "text"),
    ("material_tech", "技术素材", "text"),
    ("material_ip", "知识产权素材", "text"),
    ("material_finance", "财务素材", "text"),
    ("style_input", "风格样例", "text"),
]

_FENCE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")
_INT = re.compile(r"^\s*(\d+)")

def _user_prompt(text):
    lines = ["表单字段（只抽这些）："]
    for var, label, _ftype in FIELDS:
        note = ""
        if var == "declaration_type":
            note = "（四选一：" + " / ".join(DECLARATION_OPTIONS) + "）"
        if var == "team_size":
            note = "（整数，没提填 0）"
        lines.append("- %s %s：%s" % (var, label, note) if note
                     else "- %s %s" % (var, label))
    lines.append("")
    lines.append("文本：")
    lines.append(text)
    lines.append("")
    lines.append("输出 JSON：")
    keys = ", ".join('"%s": "…"' % v for v, _l, _t in FIELDS)
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


def _normalize(raw):
    """模型输出 → 干净字段。类型不对、超界、选项对不上都按「没提到」处理。"""
    fields = {v: "" for v, _l, _t in FIELDS}
    for var, _label, ftype in FIELDS:
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
        if var == "declaration_type":
            if value not in DECLARATION_OPTIONS:
                low = value
                if "专精特新" in low:
                    value = "专精特新"
                elif "高新技术" in low or "高企" in low:
                    value = "高新技术企业"
                elif "科技型中小" in low:
                    value = "科技型中小企业"
                elif low:  # 提了但对应不上 → 其他
                    value = "其他"
            fields[var] = value
        else:
            fields[var] = value
    return fields


def parse_inputs(text, model=None, base_url=None, temperature=None,
                 num_ctx=None, timeout=None):
    """
    自由文本 → {"fields", "missing", "raw"}。

    fields 全部 13 个键；missing 是没提到的字段标签列表；模型输出解析失败时
    返回 fields 全空 + parse_error（不抛异常 —— 识别只是辅助，识别失败表单照填）。
    """
    empty = {v: "" for v, _l, _t in FIELDS}
    if not (text or "").strip():
        return {"fields": empty, "missing": [l for _v, l, _t in FIELDS],
                "raw": text, "error": "empty"}

    with open(PROMPT_PATH, encoding="utf-8") as fh:
        system = fh.read().rstrip("\n")

    try:
        content = run_pipeline.chat(
            base_url or os.environ.get("OLLAMA_BASE_URL") or run_pipeline.DEFAULT_BASE_URL,
            model or os.environ.get("OLLAMA_MODEL") or run_pipeline.DEFAULT_MODEL,
            system, _user_prompt(text),
            temperature if temperature is not None else 0.0,
            num_ctx or 8192,
            timeout or run_pipeline.TIMEOUT,
        )
        raw = _extract_json(content)
    except Exception as exc:
        return {"fields": empty, "missing": [l for _v, l, _t in FIELDS],
                "raw": text, "error": "parse_failed",
                "parse_error": "%s: %s" % (type(exc).__name__, exc)}

    fields = _normalize(raw)
    missing = [label for var, label, _ftype in FIELDS
               if fields[var] in ("", 0)]
    return {"fields": fields, "missing": missing, "raw": text}


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="自由文本 → 表单字段（本地调试用）")
    parser.add_argument("text_file", help="自由文本文件（UTF-8）")
    args = parser.parse_args(argv)

    with open(args.text_file, encoding="utf-8") as fh:
        text = fh.read()
    result = parse_inputs(text)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if "error" not in result else 1


if __name__ == "__main__":
    sys.exit(main())
