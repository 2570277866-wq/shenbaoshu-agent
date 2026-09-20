# -*- coding: utf-8 -*-
"""
章节拼接 —— 把各章节 LLM 的输出拼成 `gen_document`，交一致性审查。

为什么不用 `fill_template.py`：
    那个脚本吃 `templates/申报书模板.md`，而 Dify 代码节点读不到仓库文件。
    且模板「骨架完成，待按官方指南校准」，此刻套模板等于把未定稿的结构固化进链路。
    本脚本只做拼接 —— 章节清单写死在这里，等模板定稿再换回模板驱动。

为什么必须处理标题（本脚本存在的主要理由）：
    审查节点按行首 `## ` 切章节（`check_consistency._chapters`）。
    模型若在正文里写了 `## 技术路线`，就会被切成一个假章节 ——
    真章节被截断、字数与数字归属全部错位，**而错位不报错**。
    故：
      1. 章节正文开头的标题一律剥掉（外层已经给了标题）
      2. 正文里其余的 `##` / `#` 一律降为 `###`，并记数上报

为什么空章节要写占位而不是跳过：
    跳过 = 稿子里凭空少一章，人一眼看不出是漏了还是本来就没有。
    对应 `docs/WORKFLOW.md` 5.1 的 E5：单章节失败降级，但必须让人看得见。
"""

import json
import re

# 章节清单。变量名与 `docs/WORKFLOW.md` 一、提示词全景一致。
SECTIONS = [
    ("background", "一、项目背景与意义", "gen_section_background"),
    ("tech", "二、技术方案", "gen_section_tech"),
    ("schedule", "三、实施计划", "gen_section_schedule"),
    ("team", "四、团队与基础条件", "gen_section_team"),
    ("outcome", "五、预期成果", "gen_section_outcome"),
    ("budget", "六、经费预算", "gen_section_budget"),
    ("risk", "七、风险与应对", "gen_section_risk"),
]

EMPTY_PLACEHOLDER = "【本章生成失败，需人工撰写】"

# 只认一二级。三级是正文章节内的小节，是内容，不是边界。
TITLE_LIKE = re.compile(r"^(#{1,2})\s+(.*)$")
FENCE = re.compile(r"^\s*(?:```|~~~)")


def _normalize_inputs(args, kwargs):
    if kwargs:
        return kwargs
    if len(args) == 1 and isinstance(args[0], dict):
        return args[0]
    if not args:
        return {}
    raise TypeError("main() 只接受一个 dict 参数，或与输入变量同名的一组关键字参数")


def _text_of(raw):
    """LLM 节点输出是字符串。其余形态尽量取正文，取不到就当空。"""
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict):
        for key in ("text", "content", "result", "output"):
            if isinstance(raw.get(key), str):
                return raw[key]
        return ""
    if isinstance(raw, (list, tuple)):
        return "\n".join(_text_of(x) for x in raw)
    return ""


def _clean(body):
    """
    返回 (正文, 剥掉的标题数, 降级的标题数)。

    开头的一二级标题剥掉（外层已给章节标题）。空行不算正文，剥的判定只看
    有没有出现过正文 —— 用计数会错，剥掉一个之后计数就对不上了。
    """
    stripped = 0
    demoted = 0
    out = []
    in_fence = False
    seen_content = False

    for line in body.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if FENCE.match(line):
            in_fence = not in_fence
        elif not in_fence:
            m = TITLE_LIKE.match(line)
            if m:
                if not seen_content:
                    stripped += 1
                    continue
                # 正文中间的一二级标题：降级，保住章节边界
                line = "### " + m.group(2)
                demoted += 1

        if line.strip():
            seen_content = True
        out.append(line)

    while out and not out[0].strip():
        out.pop(0)
    while out and not out[-1].strip():
        out.pop()

    return "\n".join(out), stripped, demoted


def main(*args, **kwargs):
    inputs = _normalize_inputs(args, kwargs)

    issues = []
    parts = []
    empty = []
    stripped = 0
    demoted = 0

    title = str(inputs.get("in_project_name") or "").strip()
    if title:
        parts.append("# %s\n" % title)

    for key, heading, var in SECTIONS:
        body, n_strip, n_demote = _clean(_text_of(inputs.get(var)))
        stripped += n_strip
        demoted += n_demote

        if not body:
            empty.append(heading)
            issues.append({
                "type": "empty_section",
                "section": heading,
                "detail": "本章无输出，已写入占位符 —— 稿子少一章必须让人看得见",
                "severity": "warn",
            })
            body = EMPTY_PLACEHOLDER

        parts.append("## %s\n\n%s" % (heading, body))

    document = "\n\n".join(parts) + "\n"

    if demoted:
        issues.append({
            "type": "heading_demoted",
            "section": "（全篇）",
            "detail": "正文里有 %d 处二三级标题被降为三级 —— 不降会被审查节点切成假章节" % demoted,
            "severity": "warn",
        })

    return {
        "gen_document": document,
        "issues": issues,
        "stats": {
            "sections": len(SECTIONS),
            "empty": empty,
            "chars": len(document),
            "headings_stripped": stripped,
            "headings_demoted": demoted,
        },
    }


if __name__ == "__main__":
    import sys

    payload = json.loads(sys.stdin.read() or "{}")
    print(json.dumps(main(payload), ensure_ascii=False, indent=2))
