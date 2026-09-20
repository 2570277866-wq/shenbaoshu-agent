# -*- coding: utf-8 -*-
"""
素材编号 —— 把知识检索结果变成带 S 编号的素材块，供 LLM 节点引用、供审查节点反查。

契约定在 `check_consistency.py` 第 8 项：正文里每个数字都要带 `（Sₙ）` 标记，
而校验要能反查到 `Sₙ` 指向的那一条素材。**编号由本脚本生成，格式即契约。**

为什么必须编号，而不是让校验去模糊匹配：
    校验若只问「这个数字在素材里出现过吗」，把编造的数字挂到任意一条真实来源
    编号上就能过关。只有编号一一对应，才查得出「标错来源」——
    而标错来源看上去完全合规，人逐句读也看不出。

为什么只有素材类编号：
    模板类给结构、风格类给表达，都不是事实来源。若混进同一个编号池，
    模型可以为一条风格库里的数字标上 `（S7）`，正好绕过整个设计。
    **故模板/风格不编号，走各自独立的变量。**

为什么每条压成单行：
    审查节点按「行首 `S<n>：`」切条目。条目正文里若出现行首的 `S3：`，
    会被切出一条假条目，编号与内容整体错位 —— 而错位**不报错**，
    只会让校验拿着错的内容去比对，最难查。压成单行从根上排除这种可能。

输入的三种形态都接受（Dify 变量类型由界面决定）：
    列表        [{"content": "...", "title": "...", "score": 0.9}, ...]
    字符串列表  ["...", "..."]
    单字符串    "..." —— **按行切成多条**，供手工表单「一行一条」用

编号顺序按 `SOURCE_ORDER` 声明，未声明的按键名字典序追加。
顺序固定是为了复现 —— 同一批检索结果每次跑出的编号必须一样，
否则人拿着 `（S3）` 去核对时对不上。

除全量 `kb_material` 外，另按组输出 `kb_material_<组名>`，
供各章节只取本章相关的素材（`docs/WORKFLOW.md` 3.3「素材只给本章相关的」）。
**编号是全局的**，分组只是视图 —— 同一条素材在哪个组里都是同一个 S 号，
否则（S3）指什么就说不清了。
"""

import json
import re

# 素材各库的编号顺序。键名 = kb_material_<后缀>。
SOURCE_ORDER = ["tech", "ip", "finance"]

# YAML 元信息头（见 knowledge/README.md「元信息头（必填）」）。
# 不进编号正文 —— 编号指向的应是事实本身，不是文件的元信息。
# 元信息在 kb_index 里保留，需要时能查回出处。
FRONT_MATTER = re.compile(r"\A\s*---\s*\n.*?\n---[ \t]*\n?", re.S)

WHITESPACE = re.compile(r"\s+")


def _normalize_inputs(args, kwargs):
    if kwargs:
        return kwargs
    if len(args) == 1 and isinstance(args[0], dict):
        return args[0]
    if not args:
        return {}
    raise TypeError("main() 只接受一个 dict 参数，或与输入变量同名的一组关键字参数")


def _flatten(text):
    """压成单行。理由见模块 docstring。"""
    return WHITESPACE.sub(" ", str(text)).strip()


def _entries(raw):
    """
    把一组检索结果归一为 dict 列表。任何形态都要能吃下，不许抛异常。

    只认 str 与 dict。其余（None、数字、嵌套列表）**丢弃**，不 str() 成内容 ——
    `str(None)` 会得到字面量 "None"，一条垃圾就这样混进编号池，
    而编号池里的垃圾会被模型当成事实引用。

    返回 (条目列表, 丢弃数)。丢弃要计数 —— 5 条输入 4 条畸形却报「无异常」，
    正是本项目反复踩的静默失效。
    """
    if raw is None:
        return [], 0
    if isinstance(raw, str):
        # 手工表单路径：一行一条。知识检索给的是列表，不走这里。
        # 不切行的话整段素材会压成一条、共用一个 S 号，模型引用它等于没引用。
        lines = [ln.strip() for ln in raw.split("\n")]
        return [{"content": ln} for ln in lines if ln], 0
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return [], 1
    out, dropped = [], 0
    for item in raw:
        if isinstance(item, dict):
            out.append(item)
        elif isinstance(item, str):
            out.append({"content": item})
        else:
            dropped += 1
    return out, dropped


def _content_of(item):
    text = item.get("content") or item.get("text") or ""
    return _flatten(FRONT_MATTER.sub("", str(text)))


def _title_of(item):
    meta = item.get("metadata")
    if not isinstance(meta, dict):
        meta = {}
    return str(item.get("title") or meta.get("file_name")
               or meta.get("document_name") or "")


def _dedup_key(item, content):
    return str(item.get("segment_id") or item.get("id") or content)


def _collect_groups(inputs):
    """
    收集需要编号的素材组。

    模板/风格不在其列（见模块 docstring）—— 它们对应的键不叫 kb_material_*，
    自然不会进来。
    """
    groups = []
    malformed = 0

    if inputs.get("kb_material") is not None:
        items, bad = _entries(inputs["kb_material"])
        groups.append(("素材", items))
        malformed += bad

    named = [k for k in inputs
             if k.startswith("kb_material_") and k != "kb_material"]
    prefix = "kb_material_"
    named.sort(key=lambda k: (
        SOURCE_ORDER.index(k[len(prefix):])
        if k[len(prefix):] in SOURCE_ORDER else len(SOURCE_ORDER),
        k,
    ))
    for key in named:
        items, bad = _entries(inputs[key])
        groups.append((key[len(prefix):], items))
        malformed += bad

    return groups, malformed


def _roundtrip_ok(block, expected):
    """
    用**消费方**的解析器复核自己产出的格式。

    生产者自己判自己对没有意义 —— 校验脚本怎么切条目，这里就得怎么切。
    故直接 import `check_consistency` 的解析器：它改了，这里立刻知道。

    单独部署（拿不到同目录文件）时无法复核，返回 True，不误报。
    """
    if not block:
        return True
    try:
        from check_consistency import _material_index
    except ImportError:
        return True
    idx, numbered = _material_index(block)
    return bool(numbered) and len(idx) == expected


def main(*args, **kwargs):
    inputs = _normalize_inputs(args, kwargs)

    index = []
    seen = set()
    parts = []
    grouped = {}

    groups, dropped = _collect_groups(inputs)

    for group_name, entries in groups:
        for item in entries:
            content = _content_of(item)
            if not content:
                dropped += 1
                continue
            key = _dedup_key(item, content)
            if key in seen:
                dropped += 1  # 同一条被两个库同时召回，只留一份
                continue
            seen.add(key)

            sid = "S%d" % (len(index) + 1)
            index.append({
                "id": sid,
                "group": group_name,
                "title": _title_of(item),
                "content": content,
                "score": item.get("score"),
            })
            line = "%s：%s" % (sid, content)
            parts.append(line)
            grouped.setdefault(group_name, []).append(line)

    block = "\n".join(parts)
    format_ok = _roundtrip_ok(block, len(index))

    result = {
        "kb_material": block,
        "kb_index": index,
        "stats": {
            "count": len(index),
            "dropped": dropped,
            "chars": len(block),
            "format_ok": format_ok,
        },
    }

    # 分组视图。声明过的组恒定存在（无条目时为空串），
    # 下游节点引用一个不存在的输出变量会直接拒绝导入 —— 空串不会。
    for name in SOURCE_ORDER:
        result["kb_material_" + name] = "\n".join(grouped.get(name, []))
    for name in grouped:
        if name not in SOURCE_ORDER:
            result["kb_material_" + name] = "\n".join(grouped[name])

    if not format_ok:
        result["warning"] = (
            "素材块未通过一致性审查的解析器复核 —— 编号格式与 check_consistency "
            "第 8 项的契约不一致，第 8 项会拒绝执行"
        )
    return result


if __name__ == "__main__":
    import sys

    payload = json.loads(sys.stdin.read() or "{}")
    print(json.dumps(main(payload), ensure_ascii=False, indent=2))
