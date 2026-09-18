# -*- coding: utf-8 -*-
"""
节点 ① 要素抽取 —— 把散落的素材结构化为全局要素表。

契约见 docs/ARCHITECTURE.md 第五节「节点 ①」。

输入：
    kb_material   素材三库召回结果（技术参数 / 知识产权 / 财务数据）
                  接受三种形态：list[str] / list[dict] / str
    in_*          开始节点的用户输入（可选，优先于素材）

输出：gen_elements
    project_name / team_size / duration_months / total_budget /
    budget_breakdown / core_metrics / ip_list / missing

    _sources      扩展字段：每个标量的出处，供节点③ 回溯校验用
                  {字段名: "素材来源标识"}

四条铁律（写死在实现里）：
    1. 只抽取，不推算 —— 素材里没有的，进 missing，不填默认值、不填估值
    2. 每个数值带 source，指回素材条目
    3. 数字原样保留 —— 不四舍五入、不换算（唯一例外：年→月，见 DURATION）
    4. 不用 LLM —— 抽取要确定性，规则 + 正则

Dify 代码节点调用约定：
    Dify 按声明的输入变量名以关键字参数调用 main。
    本地调试按 main({"kb_material": ...}) 调用。
    两种都支持（见 _normalize_inputs）。
"""

import re

# ---------------------------------------------------------------- 常量表

# 经费科目。按《科技型中小企业创新基金》常见科目；新材料只需加一行。
BUDGET_SUBJECTS = [
    "设备费", "材料费", "测试化验加工费", "燃料动力费", "差旅费",
    "会议费", "国际合作与交流费", "出版/文献/信息传播/知识产权事务费",
    "劳务费", "专家咨询费", "人员费", "间接费用", "其他费用",
]

IP_TYPES = [
    "发明专利", "实用新型专利", "实用新型", "外观设计专利", "外观设计",
    "软件著作权", "著作权", "集成电路布图设计", "商标",
]

# 数值 + 单位。允许「3,200」「3200.5」「三千」不含 —— 中文数字不抽，宁可进 missing
NUM = r"(\d[\d,]*(?:\.\d+)?)"


def _num(s):
    """字符串转数字，原样保留精度。不四舍五入。"""
    s = s.replace(",", "")
    return float(s) if "." in s else int(s)


# ---------------------------------------------------------------- 输入归一


def _normalize_inputs(args, kwargs):
    """兼容 Dify（关键字参数）与本地（单个 dict）两种调用方式。"""
    if kwargs:
        return kwargs
    if len(args) == 1 and isinstance(args[0], dict):
        return args[0]
    if not args:
        return {}
    raise TypeError("main() 只接受一个 dict 参数，或与输入变量同名的一组关键字参数")


def _chunks(raw):
    """
    召回结果归一为 [(source, text)]。
    source 是回溯标识 —— 有文件名用文件名，否则退化为下标。
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        return [("kb_material", raw)]

    out = []
    for i, item in enumerate(raw):
        if isinstance(item, str):
            out.append(("kb_material[%d]" % i, item))
        elif isinstance(item, dict):
            text = item.get("content") or item.get("text") or ""
            meta = item.get("metadata") or {}
            src = (item.get("title") or meta.get("file_name")
                   or meta.get("document_name") or "kb_material[%d]" % i)
            if text:
                out.append((src, text))
    return out


# ---------------------------------------------------------------- 抽取规则


def _find_first(text, patterns, cast=None):
    """按顺序试每条正则，命中即返回 (值, 匹配片段)。都不中返回 (None, None)。"""
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            val = m.group(1)
            return (cast(val) if cast else val), m.group(0)
    return None, None


DURATION_YEAR = re.compile(r"(\d+(?:\.\d+)?)\s*(?:年|个年头)")


def _extract_duration(text):
    """
    周期转月。
    唯一做换算的字段：年 → 月。整数年乘 12 无损，非整数年取整后原值另存。
    素材只给月份时不换算。
    """
    months, _ = _find_first(text, [r"(\d+)\s*个?月"], int)
    if months:
        return months, False
    years, _ = _find_first(text, [DURATION_YEAR.pattern], float)
    if years:
        m = int(years * 12)
        return m, (years * 12 != m)  # 第二个返回值：是否发生了取整损失
    return None, False


def _extract_metrics(chunks):
    """
    关键技术指标：抓「指标名 + 目标值 + 基线」三元组。

    只认明确带比较语义的句子，认不出的不抓 —— 宁可少抓，不猜。
    例：「检测精度目标值 99.2%，现有基线 97.0%」
    """
    # 比较词后允许「值 / 为 / 是 / 达」等连接字，否则「目标值 99.2%」漏匹配
    target_pat = re.compile(
        r"(目标值?|达到|不低于|提升至|提升到|力争)\s*(?:为|是|达|到)?\s*[:：]?\s*"
        + NUM + r"\s*(%|倍|ms|秒|分钟|小时|个|项)?")
    base_pat = re.compile(
        r"(现有|现状|目前|基线|当前|原有)\s*(?:为|是|达|到)?\s*[:：]?\s*"
        + NUM + r"\s*(%|倍|ms|秒|分钟|小时|个|项)?")

    metrics = []
    seen = set()
    for src, text in chunks:
        for line in re.split(r"[\n。；;]", text):
            if not line.strip():
                continue
            tm = target_pat.search(line)
            if not tm:
                continue
            # 指标名 = 比较词之前那段文字；再去掉「核心技术指标：」这类前缀
            name = re.split(r"目标值?|达到|不低于|提升至|提升到|力争", line)[0]
            name = re.split(r"[:：]", name)[-1]
            name = re.sub(r"^[\s\-*•·\d.、)]+", "", name)
            name = re.sub(r"^(?:核心|关键|主要|技术|性能|指标|其中)+", "", name)
            name = name.strip(" ，,：:、")
            if not name or len(name) > 20 or name in seen:
                continue
            base = base_pat.search(line)
            metrics.append({
                "name": name,
                "target": tm.group(2) + (tm.group(3) or ""),
                "baseline": (base.group(2) + (base.group(3) or "")) if base else None,
                "source": src,
            })
            seen.add(name)
    return metrics


def _extract_ip(chunks):
    """
    知识产权清单。专利号只认出来是什么就抄什么，不校验真伪（节点③ 管这个）。

    按类型名切分句子，每个类型只认它自己后面紧跟的那个数字 ——
    否则「发明专利 3 项…，软件著作权 2 项」里两个类型都会读到 3。
    类型名按长度倒序排列，保证「软件著作权」先于「著作权」匹配。
    """
    ip_pat = re.compile("(" + "|".join(sorted(IP_TYPES, key=len, reverse=True)) + ")")
    num_pat = re.compile(NUM + r"\s*(?:项|件|个)")
    no_pat = re.compile(r"ZL[\d.X]+|软著登字第\s*\d+\s*号|登记号\s*[\d\-]+")

    ip_map = {}
    for src, text in chunks:
        for line in re.split(r"[\n；;]", text):
            parts = ip_pat.split(line)
            # parts = [前缀, 类型1, 其后文本, 类型2, 其后文本, ...]
            for i in range(1, len(parts) - 1, 2):
                ip_type, tail = parts[i], parts[i + 1]
                count, _ = _find_first(tail, [num_pat.pattern], _num)
                if count is None:
                    continue  # 该类型没写数量 —— 不猜，跳过
                if ip_type not in ip_map:
                    ip_map[ip_type] = {"type": ip_type, "count": 0,
                                       "status": None, "numbers": [], "source": src}
                entry = ip_map[ip_type]
                entry["count"] += count
                if "已授权" in tail or "已登记" in tail:
                    entry["status"] = "已授权"
                elif "受理" in tail or "实审" in tail:
                    entry["status"] = "申请中"
                entry["numbers"] += no_pat.findall(tail)
    return list(ip_map.values())


def _extract_budget(chunks):
    """经费明细。只认「科目 + 金额」，科目必须在常量表内。"""
    breakdown = {}
    sources = {}
    for src, text in chunks:
        for line in re.split(r"[\n；;]", text):
            if "万元" not in line and "元" not in line:
                continue
            for subj in BUDGET_SUBJECTS:
                if subj not in line:
                    continue
                # 科目后跟的数值，取紧跟科目的那个
                tail = line.split(subj, 1)[1]
                amt, _ = _find_first(tail, [r"\s*[:：]?\s*" + NUM], _num)
                if amt is None or subj in breakdown:
                    continue
                breakdown[subj] = amt
                sources[subj] = src
    return breakdown, sources


# ---------------------------------------------------------------- 主流程


def main(*args, **kwargs):
    inputs = _normalize_inputs(args, kwargs)
    chunks = _chunks(inputs.get("kb_material"))
    blob = "\n".join(t for _, t in chunks)
    sources = {}

    # --- 用户输入优先（开始节点填的，比素材召回更权威）
    project_name = (inputs.get("in_project_name") or "").strip() or None
    if project_name:
        sources["project_name"] = "开始节点 in_project_name"

    team_size = inputs.get("in_team_size")
    if isinstance(team_size, str) and team_size.strip().isdigit():
        team_size = int(team_size)
    if isinstance(team_size, (int, float)):
        team_size = int(team_size)
        sources["team_size"] = "开始节点 in_team_size"
    else:
        team_size, frag = _find_first(blob, [
            r"(?:团队|人员|研发人员|技术人员|职工|员工)\s*(?:共|合计|总计)?\s*" + NUM + r"\s*(?:人|名)",
            r"" + NUM + r"\s*(?:人|名)\s*(?:的)?(?:研发|技术)?团队",
        ], _num)
        if team_size is not None:
            team_size = int(team_size)
            sources["team_size"] = "素材：" + frag

    # --- 素材抽取
    duration_months, duration_lossy = _extract_duration(blob)
    if duration_months:
        sources["duration_months"] = "素材：周期表述"

    total_budget = None
    if inputs.get("in_budget_range"):
        total_budget, frag = _find_first(str(inputs["in_budget_range"]), [NUM], _num)
        if total_budget is not None:
            sources["total_budget"] = "开始节点 in_budget_range"
    if total_budget is None:
        total_budget, frag = _find_first(blob, [
            r"(?:总投资|项目投资|预算|经费|资金)\s*(?:为|共|合计|总计)?\s*[:：]?\s*" + NUM + r"\s*万元",
            r"" + NUM + r"\s*万元\s*(?:的)?(?:项目)?(?:总投资|预算|经费)",
        ], _num)
        if total_budget is not None:
            sources["total_budget"] = "素材：" + frag

    budget_breakdown, bd_sources = _extract_budget(chunks)
    if budget_breakdown:
        sources["budget_breakdown"] = "素材：经费明细"

    core_metrics = _extract_metrics(chunks)
    ip_list = _extract_ip(chunks)

    # --- 抽不到的进 missing，不填默认值
    missing = []
    if not project_name:
        missing.append("项目名称")
    if team_size is None:
        missing.append("投入人数")
    if not duration_months:
        missing.append("项目周期")
    if total_budget is None:
        missing.append("项目总投资金额")
    if not budget_breakdown:
        missing.append("经费预算明细（分科目金额）")
    if not core_metrics:
        missing.append("关键技术指标（目标值与现有基线）")
    if not ip_list:
        missing.append("知识产权清单（类型、数量、状态、编号）")
    if duration_lossy:
        missing.append("项目周期是否为整数年（年→月换算时发生取整）")
    if not chunks:
        missing.append("素材库召回为空 —— 未提供任何可用素材")

    return {
        "project_name": project_name,
        "team_size": team_size,
        "duration_months": duration_months,
        "total_budget": total_budget,
        "budget_breakdown": budget_breakdown,
        "core_metrics": core_metrics,
        "ip_list": ip_list,
        "missing": missing,
        "_sources": sources,
        "_budget_sources": bd_sources,
    }


if __name__ == "__main__":
    import json
    import sys

    raw = sys.stdin.read()
    payload = json.loads(raw) if raw.strip() else {}
    print(json.dumps(main(payload), ensure_ascii=False, indent=2))
