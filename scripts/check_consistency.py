# -*- coding: utf-8 -*-
"""
节点 ③ 一致性审查 —— 生成后校验，决定是否回退重生成。

契约见 docs/ARCHITECTURE.md 第五节「节点 ③」。

输入：
    gen_document   完整 Markdown
    gen_elements   节点① 输出
    kb_material    素材三库召回结果（回溯基准）
    style_entities 风格库中出现过的企业名 / 专有名词（可选，用于第 7 项）
    section_min    章节最少字数，默认 300

输出 chk_report：
    pass     是否通过（只看阻断级 issue）
    issues   [{type, section, detail, severity}]
    stats    {tbd_count, word_count, blocking, warnings}

七项检查：
    1 全局数字一致      与 gen_elements 逐项比对        block
    2 数字可回溯        正文数字在 kb_material 命中      warn
    3 无杜撰资质        专利号/证书号可溯源              block
    4 占位符无残留      扫描 {{                         block
    5 空缺统计          统计【待补充】并提示人工          warn
    6 章节字数达标      各章节在要求区间                 warn
    7 风格未串事实      风格库企业名/数据未混入           block

两点实现约定：

  * 不抛异常中断流程。任何被审查的输入畸形，都转成一条 issue 返回 ——
    脚本抛异常会让整个工作流失败且无法定位。

  * 区分阻断与降级（docs/WORKFLOW.md 第五节）。
    block：续下去会产出「看似合理但错误」的结果 —— 必须回退，重试上限 2 次。
    warn ：结果明显不完整、人一眼看得出来 —— 放行，交人工处理。
    第 2 项之所以是 warn：正文里出现一个素材中查不到的数字，可能是合理派生
    （占比、合计、年份），一刀切成阻断会让回退循环停不下来。
"""

import json
import re

# 阻断级 issue 类型。其余为 warn。切换等级改这里，不改逻辑。
BLOCKING_TYPES = {
    "number_mismatch",
    "fabricated_qualification",
    "unresolved_placeholder",
    "style_leak",
    "empty_document",   # 空稿件放行 → 下游转出空 DOCX
    "check_error",      # 某项检查静默没跑，等于漏掉一道防线
}

DEFAULT_SECTION_MIN = 300

# 数字 + 单位。只认带单位的 —— 光秃秃的数字多为序号、年份、编号，判不了真伪。
UNIT_NUM = re.compile(
    r"(\d[\d,]*(?:\.\d+)?)\s*(%|万元|亿元|元|人|名|个月|年|项|件|个|ms|毫秒|秒|倍)")

# 资质编号：专利号、软著登记号、证书编号、报告编号
QUALIFICATION = re.compile(
    r"ZL[\d.X]+|软著登字第\s*\d+\s*号|登记号\s*[\d\-]+|"
    r"(?:证书|专利|报告|检测)\s*编?号\s*[:：]?\s*([A-Za-z0-9\-]{6,})")

CHAPTER = re.compile(r"^##\s+(.*)$")
ORDER_PREFIX = re.compile(r"^[一二三四五六七八九十]+、\s*")


def _normalize_inputs(args, kwargs):
    if kwargs:
        return kwargs
    if len(args) == 1 and isinstance(args[0], dict):
        return args[0]
    if not args:
        return {}
    raise TypeError("main() 只接受一个 dict 参数，或与输入变量同名的一组关键字参数")


def _as_json(v):
    """Dify 变量可能是 dict，也可能是 JSON 字符串。"""
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return {}
    return v or {}


def _blob(raw):
    """
    召回结果归一为一段纯文本 + 来源清单。
    形态不可预期（Dify 变量类型由界面决定），任何类型都必须能变成文本，不许抛异常。
    """
    if raw is None:
        return "", []
    if isinstance(raw, str):
        return raw, ["kb_material"]
    if not isinstance(raw, (list, tuple)):
        return str(raw), ["kb_material"]
    texts, sources = [], []
    for i, item in enumerate(raw):
        if isinstance(item, str):
            texts.append(item)
            sources.append("kb_material[%d]" % i)
        elif isinstance(item, dict):
            texts.append(item.get("content") or item.get("text") or "")
            meta = item.get("metadata") or {}
            sources.append(item.get("title") or meta.get("file_name") or "kb_material[%d]" % i)
    return "\n".join(texts), sources


def _chapters(doc):
    """按 ## 切章节，返回 [{"name", "text"}]。章节名去序号，与分块脚本一致。"""
    chapters = []
    current = {"name": "（前言）", "text": []}
    for line in doc.split("\n"):
        m = CHAPTER.match(line)
        if m:
            if current["text"]:
                chapters.append({"name": current["name"], "text": "\n".join(current["text"])})
            current = {"name": ORDER_PREFIX.sub("", m.group(1).strip()), "text": [line]}
        else:
            current["text"].append(line)
    if current["text"]:
        chapters.append({"name": current["name"], "text": "\n".join(current["text"])})
    return chapters


def _norm_num(s):
    """数字归一化后比较：去掉千分位与末尾 0，避免 3200 与 3,200.0 判成不符。"""
    s = str(s).replace(",", "").strip()
    try:
        f = float(s)
    except ValueError:
        return s
    return str(int(f)) if f == int(f) else str(f)


def _num(s):
    try:
        return float(str(s).replace(",", ""))
    except ValueError:
        return None


# ---------------------------------------------------------------- 逐项检查


def _check_1_number_consistency(doc, elements, add):
    """全局数字一致：文档断言了这些数，就必须与要素表一致。"""
    checks = [
        ("team_size", r"(?:团队|人员|研发人员|技术人员|职工|员工)\s*(?:共|合计|总计|约)?\s*"
                      r"[:：]?\s*(\d[\d,]*)\s*(?:人|名)",
         "投入人数", lambda v: _norm_num(v) == _norm_num(elements.get("team_size"))),
        ("duration_months", r"(?:周期|实施期|建设期|为期|历时)\s*[:：]?\s*(?:为|共)?\s*(\d+)\s*个月",
         "项目周期", lambda v: _norm_num(v) == _norm_num(elements.get("duration_months"))),
        ("total_budget", r"(?:总投资|项目投资|总投资额|经费合计|预算总额)\s*[:：]?\s*(?:为|共|约)?\s*"
                         r"(\d[\d,]*(?:\.\d+)?)\s*万元",
         "经费预算", lambda v: _norm_num(v) == _norm_num(elements.get("total_budget"))),
    ]
    for key, pat, section, ok in checks:
        expected = elements.get(key)
        if expected is None:
            continue
        for m in re.finditer(pat, doc):
            if not ok(m.group(1)):
                add("number_mismatch", section,
                    "正文「%s」为 %s，要素表为 %s" % (m.group(0).strip(), m.group(1), expected))

    # 经费明细逐科目比对
    breakdown = elements.get("budget_breakdown") or {}
    for subject, amount in breakdown.items():
        m = re.search(r"^\|\s*%s\s*\|\s*([\d,.]+)" % re.escape(subject), doc, re.M)
        if m and _norm_num(m.group(1)) != _norm_num(amount):
            add("number_mismatch", "经费预算",
                "科目「%s」正文为 %s，要素表为 %s" % (subject, m.group(1), amount))


def _check_2_traceable(doc, elements, blob, sources, add, style_entities):
    """
    数字可回溯：正文每个带单位的数字，都要能指回素材或用户输入。
    派生值（占比、合计）与前言区（用户输入）不算违规。
    """
    traceable = set()

    def remember(v):
        n = _num(v)
        if n is not None:
            traceable.add(_norm_num(n))

    for m in UNIT_NUM.finditer(blob):
        remember(m.group(1))
    # 用户输入与要素表本身也是合法出处（in_* 来自开始节点，不在素材里）
    for key in ("team_size", "duration_months", "total_budget"):
        if elements.get(key) is not None:
            remember(elements[key])
    for k, v in (elements.get("budget_breakdown") or {}).items():
        remember(v)
    for metric in elements.get("core_metrics") or []:
        for field in ("target", "baseline"):
            for m in UNIT_NUM.finditer(str(metric.get(field) or "")):
                remember(m.group(1))
    for ip in elements.get("ip_list") or []:
        remember(ip.get("count"))

    for chapter in _chapters(doc):
        if chapter["name"] == "（前言）":
            continue  # 封面区填的是用户输入，不是素材事实
        for line in chapter["text"].split("\n"):
            if "合计" in line:
                continue  # 合计行是派生值
            if line.startswith("|") and line.count("|") >= 4 and "%" in line:
                continue  # 预算占比列，派生值
            for m in UNIT_NUM.finditer(line):
                if _norm_num(m.group(1)) in traceable:
                    continue
                add("number_not_traceable", chapter["name"],
                    "数字「%s%s」在素材中未找到出处" % (m.group(1), m.group(2)))


def _check_3_qualification(doc, blob, add):
    """无杜撰资质：编号必须能溯源和素材。"""
    for m in QUALIFICATION.finditer(doc):
        token = (m.group(1) or m.group(0)).strip()
        if token not in blob:
            add("fabricated_qualification", "-",
                "资质编号「%s」在素材中不存在 —— 疑似杜撰，必须人工核对" % token)


def _check_4_placeholder(doc, add):
    for m in re.finditer(r"\{\{", doc):
        add("unresolved_placeholder", "-",
            "残留 {{ —— %s" % doc[m.start():m.start() + 30].replace("\n", " "))


def _check_5_tbd(doc, add):
    count = doc.count("【待补充")
    if count:
        add("missing_data", "-",
            "正文有 %d 处【待补充】，素材缺口需人工补全后重新生成" % count, severity="warn")
    return count


def _check_6_word_count(doc, add, section_min):
    counts = {}
    for chapter in _chapters(doc):
        name = chapter["name"]
        n = len(re.sub(r"\s", "", chapter["text"]))
        counts[name] = n
        if name == "（前言）":
            continue
        if n < section_min:
            add("word_count", name,
                "章节「%s」仅 %d 字，低于下限 %d 字，内容可能缺失" % (name, n, section_min),
                severity="warn")
    return counts


def _check_7_style_leak(doc, style_entities, add):
    for ent in style_entities or []:
        ent = str(ent).strip()
        if ent and ent in doc:
            add("style_leak", "-",
                "风格库中的「%s」出现在正文中 —— 风格只学表达，不搬事实" % ent)


# ---------------------------------------------------------------- 主流程


def main(*args, **kwargs):
    inputs = _normalize_inputs(args, kwargs)
    issues = []

    def add(itype, section, detail, severity=None):
        issues.append({
            "type": itype,
            "section": section,
            "detail": detail,
            "severity": severity or ("block" if itype in BLOCKING_TYPES else "warn"),
        })

    doc = inputs.get("gen_document") or ""
    if not doc.strip():
        add("empty_document", "-", "gen_document 为空，无可审查内容")

    elements = _as_json(inputs.get("gen_elements"))
    style_entities = inputs.get("style_entities") or []
    if isinstance(style_entities, str):
        style_entities = [s for s in re.split(r"[\n,，、]", style_entities) if s.strip()]
    try:
        section_min = int(inputs.get("section_min") or DEFAULT_SECTION_MIN)
    except (TypeError, ValueError):
        section_min = DEFAULT_SECTION_MIN

    blob, _sources = _blob(inputs.get("kb_material"))
    if not blob.strip():
        add("no_material", "-",
            "kb_material 为空，第 2、3 项检查无法执行（仅完成结构与占位符检查）", severity="warn")

    # 逐项执行。任何一项出错都转成 issue，不中断整体审查。
    steps = [
        ("check_1", lambda: _check_1_number_consistency(doc, elements, add)),
        ("check_2", lambda: _check_2_traceable(doc, elements, blob, _sources, add, style_entities)),
        ("check_3", lambda: _check_3_qualification(doc, blob, add) if blob.strip() else None),
        ("check_4", lambda: _check_4_placeholder(doc, add)),
        ("check_7", lambda: _check_7_style_leak(doc, style_entities, add)),
    ]
    for name, fn in steps:
        try:
            fn()
        except Exception as exc:  # 单项失败不拖垮整体
            add("check_error", "-", "%s 执行异常：%s: %s" % (name, type(exc).__name__, exc))

    tbd_count = _check_5_tbd(doc, add)
    try:
        word_count = _check_6_word_count(doc, add, section_min)
    except Exception as exc:
        add("check_error", "-", "check_6 执行异常：%s" % exc)
        word_count = {}

    blocking = [i for i in issues if i["severity"] == "block"]
    warnings = [i for i in issues if i["severity"] == "warn"]

    return {
        "pass": not blocking,
        "issues": issues,
        "stats": {
            "tbd_count": tbd_count,
            "word_count": word_count,
            "blocking": len(blocking),
            "warnings": len(warnings),
        },
    }


if __name__ == "__main__":
    import sys

    payload = json.loads(sys.stdin.read() or "{}")
    print(json.dumps(main(payload), ensure_ascii=False, indent=2))
