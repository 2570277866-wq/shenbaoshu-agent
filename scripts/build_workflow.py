# -*- coding: utf-8 -*-
"""
工作流生成器 —— 由 `dify/prompts/` 与 `scripts/` 生成 `dify/workflow_vX.Y.yml`。

**这是开发工具，不进 Dify。** 只有它生成的 yml 才导入 Dify。

为什么要有生成器，而不是手写 yml：
    `00_system.md` 一份要内联进 7 个 LLM 节点，手写就是同一段文字存 7 份。
    改一次提示词要改 7 处，漏一处就是「有的章节还按旧规则写」——
    而这种不一致不报错，只会让各章节行为悄悄分叉。
    故提示词与脚本是唯一真相，yml 是产物。

生成物是**确定性**的：同样的输入必然得到逐字节相同的输出，
否则 git diff 里全是噪声，看不出真正改了什么。

`## 输入` 一节会被摘掉：那是写给人看的变量来源表，模型看不到变量名，
    看到的是下面「本次输入」里注入的**实际内容**。

用法：
    python3 scripts/build_workflow.py            # 写文件
    python3 scripts/build_workflow.py --stdout   # 只打印，用于比对
"""

import argparse
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PROMPTS = os.path.join(ROOT, "dify", "prompts")

VERSION = "0.8"
OUT_PATH = os.path.join(ROOT, "dify", "workflow_v%s.yml" % VERSION)

SYSTEM_PROMPT = "00_system.md"

# 模型与参数：v0.7 实测可用，不改
MODEL = {"provider": "ollama", "name": "qwen3:14b", "mode": "chat"}
COMPLETION_PARAMS = {"temperature": 0.3, "num_ctx": 8192}

# 开始节点变量。顺序即表单顺序。
# (变量名, 标签, 类型, max_length 或 None, 必填, 选项)
START_VARS = [
    ("project_name", "项目名称", "text-input", 200, True, None),
    ("declaration_type", "申报类型", "select", None, True,
     ["科技型中小企业", "高新技术企业", "专精特新", "其他"]),
    ("tech_direction", "技术方向", "text-input", 200, True, None),
    ("project_leader", "项目负责人", "text-input", 100, True, None),
    ("team_size", "投入人数", "number", None, True, None),
    ("budget_range", "预算规模", "text-input", 100, True, None),
    ("expected_outcome", "预期成果", "paragraph", 2000, True, None),
    ("project_highlights", "项目亮点", "paragraph", 2000, True, None),
    ("special_requirements", "特殊要求", "paragraph", 2000, False, None),
    ("material_tech", "素材·企业技术参数（一行一条）", "paragraph", 8000, False, None),
    ("material_ip", "素材·知识产权（一行一条）", "paragraph", 8000, False, None),
    ("material_finance", "素材·财务数据（一行一条）", "paragraph", 8000, False, None),
    ("style_input", "风格样例（只学表达，可留空）", "paragraph", 4000, False, None),
]

VAR_LABEL = {v[0]: v[1] for v in START_VARS}

# 素材分组 → 开始节点变量名。与 `number_material.SOURCE_ORDER` 对齐。
MATERIAL_SOURCE = {
    "tech": "material_tech",
    "ip": "material_ip",
    "finance": "material_finance",
}

# 章节清单。
#   inputs    要注入的 in_* （写开始节点变量名，不带 in_ 前缀）
#   materials 本章相关的素材组 —— 见 `docs/WORKFLOW.md` 3.3「素材只给本章相关的」
#   style     是否给风格样例
CHAPTERS = [
    {
        "num": "02", "out": "gen_section_background", "title": "项目背景",
        "heading": "项目背景与意义",
        "inputs": ["project_name", "tech_direction", "project_highlights",
                   "special_requirements"],
        "materials": ["tech", "ip", "finance"],
        "style": True,
    },
    {
        "num": "03", "out": "gen_section_tech", "title": "技术方案",
        "heading": "技术方案",
        "inputs": ["tech_direction", "project_highlights"],
        "materials": ["tech", "ip"],
        "style": True,
    },
    {
        "num": "04", "out": "gen_section_schedule", "title": "实施计划",
        "heading": "实施计划",
        "inputs": ["team_size", "budget_range"],
        "materials": ["tech"],
        "style": False,
    },
    {
        "num": "05", "out": "gen_section_team", "title": "团队基础",
        "heading": "团队与基础条件",
        "inputs": ["project_leader", "team_size"],
        "materials": ["tech"],
        "style": False,
    },
    {
        "num": "06", "out": "gen_section_outcome", "title": "预期成果",
        "heading": "预期成果",
        "inputs": ["expected_outcome", "project_highlights"],
        "materials": ["tech", "ip"],
        "style": False,
    },
    {
        "num": "07", "out": "gen_section_budget", "title": "经费预算",
        "heading": "经费预算",
        "inputs": ["budget_range"],
        "materials": ["finance"],
        "style": False,
    },
    {
        "num": "08", "out": "gen_section_risk", "title": "风险应对",
        "heading": "风险与应对",
        "inputs": ["tech_direction", "special_requirements"],
        "materials": ["tech"],
        "style": False,
    },
]

# `## 输入` 一节是给人看的变量来源表，模型看不到变量名 —— 摘掉。
INPUT_SECTION = re.compile(r"^##\s+输入\s*\n.*?(?=^##\s|\Z)", re.S | re.M)

YAML_SPECIAL = re.compile(r"^[\s>|&*!%@`{}\[\],#?'\"-]|:\s|\s#")


# ------------------------------------------------------------------ 读提示词


def read_prompt(name):
    with open(os.path.join(PROMPTS, name), encoding="utf-8") as fh:
        return fh.read()


def chapter_body(path_name):
    """章节提示词正文，摘掉「## 输入」表。"""
    return INPUT_SECTION.sub("", read_prompt(path_name)).strip("\n")


def read_script(name):
    with open(os.path.join(HERE, name), encoding="utf-8") as fh:
        return fh.read()


def user_prompt(chapter):
    """
    章节的 user 消息 = 本次输入 + 要素表 + 本章素材 + 风格 + 章节提示词原文。

    素材与要素表在这里**注入实际内容**，不是给模型变量名让它自己取。
    """
    parts = ["## 本次输入\n"]
    for var in chapter["inputs"]:
        parts.append("- %s：{{#start_node.%s#}}" % (VAR_LABEL[var], var))
    parts.append("- 素材编号：本章素材已按 `S1：…` 逐条编号，引用时必须带 `（Sₙ）`")

    parts.append("\n## 全局要素表 gen_elements（全篇一致，指标值以此为准）\n")
    parts.append("```json\n{{#elements_node.gen_elements#}}\n```")

    parts.append("\n## 本章素材（已编号 —— 引用的编号必须指对）\n")
    for group in chapter["materials"]:
        parts.append("{{#number_node.kb_material_%s#}}\n" % group)

    if chapter["style"]:
        parts.append("## 风格参考（只学表达，不得搬运其中的企业名、数据、事实）\n")
        parts.append("{{#start_node.style_input#}}\n")

    parts.append("---\n")
    parts.append(chapter_body("%s_%s.md" % (chapter["num"], chapter["title"])))
    return "\n".join(parts).strip("\n")


# ------------------------------------------------------------------ YAML 发射


def scalar(value):
    """YAML 标量。宁可全加引号，也不赌哪个值不需要。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if text == "":
        return "''"
    if "\n" in text:
        raise ValueError("多行标量要走 block()，不能走 scalar(): %r" % text[:40])
    return "'" + text.replace("'", "''") + "'"


def block(text, indent):
    """字面块标量（`|`）。每行统一缩进 —— 缩进不足会直接把 YAML 截断。"""
    pad = " " * indent
    lines = ["|"]
    for line in text.rstrip("\n").split("\n"):
        lines.append(pad + line if line.strip() else "")
    return "\n".join(lines)


def key_block(key, text, indent):
    return "%s%s: %s" % (" " * indent, key, block(text, indent + 2))


def var_ref(indent, var, node, out, value_type):
    pad = " " * indent
    return (
        "%s- variable: %s\n"
        "%s  value_selector:\n"
        "%s  - %s\n"
        "%s  - %s\n"
        "%s  value_type: %s\n"
        % (pad, var, pad, pad, scalar(node), pad, out, pad, value_type)
    )


def outputs_block(mapping, indent):
    """mapping: {输出名: 类型}，按名排序保证确定性。"""
    pad = " " * indent
    out = []
    for name in sorted(mapping):
        out.append("%s%s:" % (pad, name))
        out.append("%s  children: null" % pad)
        out.append("%s  type: %s" % (pad, mapping[name]))
    return "\n".join(out)


def node_footer(node_id, node_type, x, y, width=244, height=90):
    return (
        "      height: %d\n"
        "      id: %s\n"
        "      position:\n"
        "        x: %d\n"
        "        y: %d\n"
        "      positionAbsolute:\n"
        "        x: %d\n"
        "        y: %d\n"
        "      selected: false\n"
        "      sourcePosition: right\n"
        "      targetPosition: left\n"
        "      type: custom\n"
        "      width: %d\n"
        % (height, scalar(node_id), x, y, x, y, width)
    )


def start_node():
    lines = ["    - data:",
             "        desc: '表单变量。素材三项是知识检索未接入前的临时入口 —— 一行一条。'",
             "        selected: false",
             "        title: 开始",
             "        type: start",
             "        variables:"]
    for var, label, vtype, maxlen, required, options in START_VARS:
        lines.append("        - label: %s" % scalar(label))
        if maxlen is not None:
            lines.append("          max_length: %d" % maxlen)
        if options is not None:
            lines.append("          options:")
            for opt in options:
                lines.append("          - %s" % scalar(opt))
        else:
            lines.append("          options: []")
        lines.append("          required: %s" % scalar(required))
        lines.append("          type: %s" % vtype)
        lines.append("          variable: %s" % var)
    lines.append(node_footer("start_node", "start", 30, 460).rstrip("\n"))
    return "\n".join(lines)


def code_node(node_id, title, desc, code, outputs, variables, x, y):
    lines = ["    - data:",
             key_block("code", code, 8),
             "        code_language: python3",
             "        desc: %s" % scalar(desc),
             "        outputs:",
             outputs_block(outputs, 10),
             "        selected: false",
             "        title: %s" % scalar(title),
             "        type: code"]
    if variables:
        lines.append("        variables:")
        for var, node, out, vtype in variables:
            lines.append(var_ref(8, var, node, out, vtype).rstrip("\n"))
    else:
        lines.append("        variables: []")
    lines.append(node_footer(node_id, "code", x, y).rstrip("\n"))
    return "\n".join(lines)


def llm_node(chapter, x, y):
    node_id = "llm_%s" % chapter["num"]
    lines = [
        "    - data:",
        "        desc: %s" % scalar("dify/prompts/%s_%s.md + 00_system.md"
                                  % (chapter["num"], chapter["title"])),
        "        title: %s" % scalar(chapter["title"]),
        "        type: llm",
        "        model:",
        "          provider: %s" % MODEL["provider"],
        "          name: %s" % MODEL["name"],
        "          mode: %s" % MODEL["mode"],
        "          completion_params:",
        "            temperature: %s" % COMPLETION_PARAMS["temperature"],
        "            num_ctx: %d" % COMPLETION_PARAMS["num_ctx"],
        "        prompt_template:",
        "        - role: system",
        key_block("text", SYSTEM_TEXT, 10),
        "        - role: user",
        key_block("text", user_prompt(chapter), 10),
        "        vision:",
        "          enabled: false",
        "          configs:",
        "            variable_selector: []",
        "        memory:",
        "          enabled: false",
        "          window:",
        "            enabled: false",
        "            size: 50",
        "        context:",
        "          enabled: false",
        "          variable_selector: []",
        "        structured_output:",
        "          enabled: false",
        "        retry_config:",
        "          enabled: false",
        "          max_retries: 1",
        "          retry_interval: 1000",
        "          exponential_backoff:",
        "            enabled: false",
        "            multiplier: 2",
        "            max_interval: 10000",
    ]
    lines.append(node_footer(node_id, "llm", x, y).rstrip("\n"))
    return "\n".join(lines)


def end_node():
    lines = [
        "    - data:",
        "        desc: ''",
        "        outputs:",
        "        - value_selector:",
        "          - 'assemble_node'",
        "          - gen_document",
        "          value_type: string",
        "          variable: document",
        "        - value_selector:",
        "          - 'check_node'",
        "          - pass",
        "          value_type: boolean",
        "          variable: pass",
        "        - value_selector:",
        "          - 'check_node'",
        "          - stats",
        "          value_type: object",
        "          variable: check_stats",
        "        selected: false",
        "        title: 结束",
        "        type: end",
    ]
    lines.append(node_footer("end_node", "end", 1860, 460).rstrip("\n"))
    return "\n".join(lines)


# ------------------------------------------------------------------ 图


def build():
    """
    返回 {"nodes", "edges", "outputs", "selectors"}。

    输出名与引用**随节点一起登记**，不回头去解析自己生成的 YAML ——
    解析法要靠 `id:` 的位置认节点，而 Dify 的节点体里 `id:` 在 `outputs:` 之后，
    于是每个节点的输出会记到前一个节点头上。错位而不报错，正是本项目反复踩的坑。
    """
    global SYSTEM_TEXT
    SYSTEM_TEXT = read_prompt(SYSTEM_PROMPT).rstrip("\n")

    nodes = []
    edges = []
    outputs = {}      # 节点 id → 声明的输出名集合
    selectors = []    # 代码节点 value_selector 指向的 (节点 id, 输出名)

    def add_code(node_id, title, desc, script, outs, variables, x, y):
        nodes.append(code_node(node_id, title, desc, read_script(script),
                               outs, variables, x, y))
        outputs[node_id] = set(outs)
        for _var, src_node, src_out, _type in variables:
            selectors.append((src_node, src_out))

    nodes.append(start_node())
    outputs["start_node"] = {v[0] for v in START_VARS}

    # 节点⓪ 素材编号
    add_code(
        "number_node", "素材编号",
        "节点⓪ number_material.py —— 素材逐条编号 S1：…，第 8 项检查的前提",
        "number_material.py",
        {"kb_material": "string", "kb_material_tech": "string",
         "kb_material_ip": "string", "kb_material_finance": "string",
         "kb_index": "array[object]", "stats": "object"},
        [("kb_material_%s" % g, "start_node", src, "string")
         for g, src in sorted(MATERIAL_SOURCE.items())],
        330, 380,
    )

    # 节点① 要素抽取
    add_code(
        "elements_node", "要素抽取",
        "节点① extract_elements.py —— 全局要素表，各章节数字一致性的根基",
        "extract_elements.py",
        {"gen_elements": "object", "stats": "object"},
        [("kb_material", "number_node", "kb_material", "string"),
         ("in_project_name", "start_node", "project_name", "string"),
         ("in_team_size", "start_node", "team_size", "number"),
         ("in_budget_range", "start_node", "budget_range", "string")],
        630, 380,
    )

    # 各章节 LLM
    # 链式边 llm_02→llm_03→…→llm_08：Dify 里兄弟节点并行触发，7 个 14b
    # 同时打单机 Ollama，后面排队的吃 300s 读超时。链式边强制逐章跑
    # （章节输出不互传，边只作顺序约束）—— 见 docs/WORKFLOW.md 3.4。
    section_vars = []
    prev_node_id = None
    for i, chapter in enumerate(CHAPTERS):
        node_id = "llm_%s" % chapter["num"]
        nodes.append(llm_node(chapter, 930, 40 + i * 130))
        outputs[node_id] = {"text", "usage"}
        edges.append(("elements_node", "code", node_id, "llm"))
        edges.append((node_id, "llm", "assemble_node", "code"))
        if prev_node_id is not None:
            edges.append((prev_node_id, "llm", node_id, "llm"))
        prev_node_id = node_id
        section_vars.append((chapter["out"], node_id, "text", "string"))

    # 代码节点 · 拼接
    add_code(
        "assemble_node", "章节拼接",
        "各章节输出拼成 gen_document；剥掉模型自写的章节标题，降级其余二三级标题",
        "assemble_document.py",
        {"gen_document": "string", "issues": "array[object]", "stats": "object"},
        section_vars + [("in_project_name", "start_node", "project_name", "string")],
        1240, 460,
    )

    # 代码节点③ 一致性审查
    add_code(
        "check_node", "一致性审查",
        "节点③ check_consistency.py —— 十项机械检查，1/2/3/4/6/8/9/10 项",
        "check_consistency.py",
        {"pass": "boolean", "issues": "array[object]", "stats": "object"},
        [("gen_document", "assemble_node", "gen_document", "string"),
         ("gen_elements", "elements_node", "gen_elements", "object"),
         ("kb_material", "number_node", "kb_material", "string")],
        1540, 460,
    )

    nodes.append(end_node())
    selectors.append(("assemble_node", "gen_document"))   # 结束节点输出

    # 前四条是主线，其余从各章节汇入。第 8 项要拿**全量**素材反查，
    # 故审查节点走 number_node 的 kb_material，不是各章节用的分组视图。
    edges = [
        ("start_node", "start", "number_node", "code"),
        ("number_node", "code", "elements_node", "code"),
        ("number_node", "code", "check_node", "code"),
        ("elements_node", "code", "check_node", "code"),
        ("assemble_node", "code", "check_node", "code"),
        ("check_node", "code", "end_node", "end"),
    ] + edges

    seen = set()
    unique = []
    for edge in edges:
        key = (edge[0], edge[2])
        if key not in seen:
            seen.add(key)
            unique.append(edge)

    return {"nodes": nodes, "edges": unique, "outputs": outputs,
            "selectors": selectors}


def emit(built=None):
    built = built or build()
    nodes, edges = built["nodes"], built["edges"]

    lines = [
        "app:",
        "  description: %s" % scalar(
            "项目申报书自动撰写 v%s —— 全链路（素材编号 → 要素抽取 → 七章节 → 拼接 → "
            "十项一致性审查）。知识检索未接入，素材走开始节点表单。"
            "由 scripts/build_workflow.py 生成，勿手改。" % VERSION),
        "  icon: 📄",
        "  icon_background: '#E4FBCC'",
        "  mode: workflow",
        "  name: %s" % scalar("申报书-全链路 v%s" % VERSION),
        "  use_icon_as_answer_icon: false",
        "dependencies: []",
        "kind: app",
        "version: 0.3.1",
        "workflow:",
        "  conversation_variables: []",
        "  environment_variables: []",
        "  features:",
        "    file_upload:",
        "      enabled: false",
        "    opening_statement: ''",
        "    retriever_resource:",
        "      enabled: false",
        "    sensitive_word_avoidance:",
        "      enabled: false",
        "    speech_to_text:",
        "      enabled: false",
        "    suggested_questions: []",
        "    suggested_questions_after_answer:",
        "      enabled: false",
        "    text_to_speech:",
        "      enabled: false",
        "  graph:",
        "    edges:",
    ]

    for source, source_type, target, target_type in edges:
        lines.append("    - data:")
        lines.append("        isInIteration: false")
        lines.append("        isInLoop: false")
        lines.append("        sourceType: %s" % source_type)
        lines.append("        targetType: %s" % target_type)
        lines.append("      id: %s" % scalar("%s-to-%s" % (source, target)))
        lines.append("      source: %s" % scalar(source))
        lines.append("      sourceHandle: source")
        lines.append("      target: %s" % scalar(target))
        lines.append("      targetHandle: target")
        lines.append("      type: custom")
        lines.append("      zIndex: 0")

    lines.append("    nodes:")
    for node in nodes:
        lines.append(node)

    lines.append("    viewport:")
    lines.append("      x: 0")
    lines.append("      y: 0")
    lines.append("      zoom: 0.5")

    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------ 自检


def check_refs(text, outputs):
    """
    每个 {{#node.var#}} 都要指得到。

    指不到 Dify 会拒绝导入整份 DSL —— 报错好过静默，
    故这里先自检一遍，免得在界面上才发现。
    """
    problems = []
    for node, var in re.findall(r"\{\{#([A-Za-z0-9_]+)\.([A-Za-z0-9_]+)#\}\}", text):
        if var not in outputs.get(node, set()):
            problems.append("{{#%s.%s#}} —— 该节点没声明这个输出" % (node, var))
    return problems


def check_selectors(selectors, outputs):
    problems = []
    for node, var in selectors:
        if node not in outputs:
            problems.append("value_selector 指向未知节点 %s" % node)
        elif var not in outputs[node]:
            problems.append("value_selector %s.%s —— 没有该输出" % (node, var))
    return problems


def check_prompt_texts(text, start_vars):
    """提示词里引用开始节点变量时必须真有 —— 拼错一个字母，表单填了就白填。"""
    problems = []
    for node, var in re.findall(r"\{\{#([A-Za-z0-9_]+)\.([A-Za-z0-9_]+)#\}\}", text):
        if node == "start_node" and var not in start_vars:
            problems.append("{{#start_node.%s#}} —— 开始节点没这个变量" % var)
    return problems


def main():
    parser = argparse.ArgumentParser(description="生成 Dify 工作流 DSL")
    parser.add_argument("--stdout", action="store_true", help="只打印，不写文件")
    args = parser.parse_args()

    built = build()
    text = emit(built)
    start_vars = {v[0] for v in START_VARS}
    problems = (check_refs(text, built["outputs"])
                + check_selectors(built["selectors"], built["outputs"])
                + check_prompt_texts(text, start_vars))
    if problems:
        for p in problems:
            print("引用错误：%s" % p, file=sys.stderr)
        return 1

    if args.stdout:
        sys.stdout.write(text)
        return 0

    with open(OUT_PATH, "w", encoding="utf-8") as fh:
        fh.write(text)

    nodes = re.findall(r"^      id: '([A-Za-z0-9_]+)'$", text, re.M)
    print("已写入 %s" % os.path.relpath(OUT_PATH, ROOT))
    print("节点 %d：%s" % (len(nodes), " ".join(nodes)))
    print("边   %d" % len(re.findall(r"^      id: '[A-Za-z0-9_]+-to-[A-Za-z0-9_]+'$", text, re.M)))
    print("系统提示词 %d 字；各章节 user 提示词：" % len(SYSTEM_TEXT))
    for chapter in CHAPTERS:
        body = user_prompt(chapter)
        print("  %s %s  %5d 字  素材 %s" % (
            chapter["num"], chapter["title"], len(body),
            "+".join(chapter["materials"]) or "无"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
