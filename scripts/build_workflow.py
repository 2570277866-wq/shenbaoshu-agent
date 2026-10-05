# -*- coding: utf-8 -*-
"""
引擎单一真相源 —— 章节定义、开始节点变量、模型参数、提示词加载。

供 run_pipeline（CLI）与 agent_service（网页）共同 import，不复制。
提示词放在 scripts/prompts/，运行时直接读 —— 改提示词立即生效，
不再有「改 prompts 必须重跑生成器」的同步步骤。

历史：本文件曾是 Dify 工作流生成器（输出 dify/workflow_vX.Y.yml，
提示词与脚本内联进节点）。产品化 MVP 脱离 Dify 后，yml 发射器已删，
本文件只保留引擎真相源部分。
"""

import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
PROMPTS = os.path.join(HERE, "prompts")

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
#   materials 本章相关的素材组
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


def read_prompt(name):
    with open(os.path.join(PROMPTS, name), encoding="utf-8") as fh:
        return fh.read()


def chapter_body(path_name):
    """章节提示词正文，摘掉「## 输入」表。"""
    return INPUT_SECTION.sub("", read_prompt(path_name)).strip("\n")


def user_prompt(chapter):
    """
    章节的 user 消息 = 本次输入 + 要素表 + 本章素材 + 风格 + 章节提示词原文。

    素材与要素表在这里**注入实际内容**，不是给模型变量名让它自己取。
    占位符 {{#node.var#}} 由 run_pipeline._render_prompt 在运行时替换。
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
