# -*- coding: utf-8 -*-
"""
节点 ② 变量替换 —— 把各章节生成结果套入 templates/ 模板。

契约见 docs/ARCHITECTURE.md 第五节「节点 ②」。

输入：
    template       模板全文（从 templates/申报书模板.md 读入，作为节点常量传入）
    gen_elements   节点① 输出
    in_*           开始节点用户输入
    gen_section_*  各章节 LLM 生成结果
    gen_milestones 里程碑数组（可选）[{"phase","time","goal","output"}]

输出：
    gen_document   完整 Markdown
    issues         替换阶段发现的问题，交节点③ 汇总
    stats          字数统计

四件事：
    1. 标量替换 —— in_* / gen_* 逐字替换
    2. 表格行展开 —— 一行占位符按数组长度展开成 N 行
    3. 空值处理 —— 缺失一律 【待补充：xxx】，不留空、不编（memory/AGENTS.md 第一条）
    4. 残留扫描 —— 替换后仍有 {{ 的记入 issues

替换基准：占位符名与 Dify 变量名逐字一致（docs/ARCHITECTURE.md 三）。
"""

import datetime
import re

# 表格行模板：族名 → (数据来源, [(字段, 占位符)])
# 判族用精确占位符名，不用前缀 —— 否则 {{gen_budget_total}} 会被当成明细行。
ROW_FAMILIES = {
    "metric": ("core_metrics", [
        ("name", "gen_metric_name"), ("target", "gen_metric_target"),
        ("baseline", "gen_metric_baseline"), ("source", "gen_metric_source"),
    ]),
    "milestone": ("milestones", [
        ("phase", "gen_ms_phase"), ("time", "gen_ms_time"),
        ("goal", "gen_ms_goal"), ("output", "gen_ms_output"),
    ]),
    "budget": ("budget_items", [
        ("item", "gen_budget_item"), ("amount", "gen_budget_amount"),
        ("ratio", "gen_budget_ratio"), ("note", "gen_budget_note"),
    ]),
}

# 占位符 → 可读名。给【待补充】提示用，让人一眼知道缺什么。
LABELS = {
    "gen_section_background": "项目背景与意义",
    "gen_section_tech": "技术方案",
    "gen_section_schedule": "实施计划",
    "gen_section_team": "团队与基础条件",
    "gen_section_outcome": "预期成果",
    "gen_section_budget": "经费预算",
    "gen_section_risk": "风险与应对",
    "gen_budget_total": "经费合计金额",
}

# 模板里 <!-- 模板说明 --> 那段，成稿不得出现
COMMENT_PAT = re.compile(r"<!--.*?-->", re.S)
PLACEHOLDER_PAT = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")

TBD = "【待补充：%s】"


# ---------------------------------------------------------------- 辅助


def _is_blank(v):
    return v is None or (isinstance(v, str) and not v.strip()) or v == []


def _as_text(v):
    if isinstance(v, (list, tuple, dict)):
        return str(v)
    return str(v)


def _build_budget_items(elements):
    """
    明细行数据。占比是派生显示值，不是素材事实 ——
    算不出（缺合计）就不给占比，不编一个数。
    """
    breakdown = elements.get("budget_breakdown") or {}
    total = elements.get("total_budget")
    items = []
    for subject, amount in breakdown.items():
        ratio = ""
        if isinstance(total, (int, float)) and total:
            ratio = "%.1f%%" % (amount / total * 100)
        items.append({"item": subject, "amount": amount, "ratio": ratio, "note": ""})
    return items


def _normalize_inputs(args, kwargs):
    if kwargs:
        return kwargs
    if len(args) == 1 and isinstance(args[0], dict):
        return args[0]
    if not args:
        return {}
    raise TypeError("main() 只接受一个 dict 参数，或与输入变量同名的一组关键字参数")


# ---------------------------------------------------------------- 主流程


def main(*args, **kwargs):
    inputs = _normalize_inputs(args, kwargs)
    issues = []

    template = inputs.get("template") or ""
    if not template.strip():
        return {
            "gen_document": "",
            "issues": [{"type": "missing_template", "section": "-",
                        "detail": "template 为空，未传入模板全文"}],
            "stats": {"total_chars": 0, "section_chars": {}, "tbd_count": 0},
        }

    elements = inputs.get("gen_elements") or {}
    if isinstance(elements, str):
        import json
        try:
            elements = json.loads(elements)
        except ValueError:
            issues.append({"type": "bad_input", "section": "-",
                           "detail": "gen_elements 不是合法 JSON，已按空表处理"})
            elements = {}

    # --- 替换表：除 gen_elements 外，所有 in_* / gen_* 标量都进
    values = {}
    for key, val in inputs.items():
        if key in ("template", "gen_elements"):
            continue
        if key.startswith("in_") or key.startswith("gen_"):
            values[key] = val
    if _is_blank(values.get("gen_date")):
        values["gen_date"] = datetime.date.today().isoformat()

    # --- 数组：表格行数据
    arrays = {
        "core_metrics": elements.get("core_metrics") or [],
        "milestones": inputs.get("gen_milestones") or [],
        "budget_items": _build_budget_items(elements),
    }
    for key in ("core_metrics", "milestones"):
        if _is_blank(arrays[key]):
            pass  # 空数组的处理在各行展开处，记 issue

    # --- 表格行展开
    out_lines = []
    for line in template.split("\n"):
        hit = None
        for family, (source_key, fields) in ROW_FAMILIES.items():
            if any("{{%s}}" % ph in line for _, ph in fields):
                hit = (family, source_key, fields)
                break

        if hit is None:
            out_lines.append(line)
            continue

        _, source_key, fields = hit
        rows = arrays.get(source_key) or []
        if not rows:
            issues.append({
                "type": "empty_table",
                "section": source_key,
                "detail": "表格数据为空，已删除该行（模板行：%s）" % line.strip()[:60],
            })
            continue

        for row in rows:
            filled = line
            for field, ph in fields:
                val = row.get(field)
                if _is_blank(val):
                    # 基线可能真的没有（新产品无现有基线），用「无」比【待补充】贴切
                    val = "无" if field in ("baseline", "ratio", "note") else TBD % field
                filled = filled.replace("{{%s}}" % ph, _as_text(val))
            out_lines.append(filled)

    document = COMMENT_PAT.sub("", "\n".join(out_lines))

    # --- 标量替换 + 空值处理
    def _sub(m):
        name = m.group(1)
        label = LABELS.get(name, name)
        if name not in values:
            # 名字压根不符合命名规则 —— 多半是模板笔误，不是素材缺口，得分开报
            if not name.startswith(("in_", "gen_")):
                issues.append({
                    "type": "unknown_variable",
                    "section": "-",
                    "detail": "占位符 {{%s}} 不符合 in_/gen_ 命名规则，请检查模板" % name,
                })
                return TBD % name
            issues.append({
                "type": "missing_data",
                "section": label,
                "detail": "变量 %s 未传入（工作流未赋值），已填 %s" % (name, TBD % label),
            })
            return TBD % label
        val = values.get(name)
        if _is_blank(val):
            issues.append({
                "type": "missing_data",
                "section": label,
                "detail": "变量 %s 为空，已填 %s" % (name, TBD % label),
            })
            return TBD % label
        return _as_text(val)

    document = PLACEHOLDER_PAT.sub(_sub, document)

    # --- 残留扫描：直接扫 {{（一致性审查第 4 项）
    # 用松散匹配 —— 占位符可能因格式错误没被上面的正则吃掉（{{123}}、{{ a-b }}）
    for m in re.finditer(r"\{\{", document):
        snippet = document[m.start():m.start() + 30].replace("\n", " ")
        issues.append({
            "type": "unresolved_placeholder",
            "section": "-",
            "detail": "残留 {{ —— %s" % snippet,
        })

    # --- 统计
    section_chars = {}
    current = "（前言）"
    for line in document.split("\n"):
        if line.startswith("## "):
            # 去掉「一、」这类序号，审查节点按章节名比对
            current = re.sub(r"^[一二三四五六七八九十]+、\s*", "", line[3:].strip())
        section_chars[current] = section_chars.get(current, 0) + len(line)

    return {
        "gen_document": document,
        "issues": issues,
        "stats": {
            "total_chars": len(document),
            "section_chars": section_chars,
            "tbd_count": document.count("【待补充"),
        },
    }


if __name__ == "__main__":
    import json
    import sys

    payload = json.loads(sys.stdin.read() or "{}")
    result = main(payload)
    print(result["gen_document"])
    print("\n--- issues ---")
    print(json.dumps(result["issues"], ensure_ascii=False, indent=2))
