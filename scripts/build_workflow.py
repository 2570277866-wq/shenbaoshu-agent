# -*- coding: utf-8 -*-
"""
引擎单一真相源 —— 申报对象（profile）、章节定义、开始节点变量、模型参数、提示词加载。

供 run_pipeline（CLI）与 agent_service（网页）共同 import，不复制。
提示词放在 scripts/prompts/，运行时直接读 —— 改提示词立即生效，
不再有「改 prompts 必须重跑生成器」的同步步骤。

双申报对象（2026-10-09 起）：PROFILES 按 profile 组织全部配置 ——
`enterprise` 科技企业项目申报书（原 13 字段 / 7 章），
`student` 大学生科研项目申报书（16 字段 / 10 章，通用大创骨架，待 MD 知识文件校正）。
模块级 START_VARS / VAR_LABEL / MATERIAL_SOURCE / CHAPTERS 保留为企业别名，
run_pipeline / parse_inputs / email_intake / 旧测试均引用它们 —— 勿删。

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

# ---------------------------------------------------------------- 企业 profile

# 开始节点变量。顺序即表单顺序。
# (变量名, 标签, 类型, max_length 或 None, 必填, 选项)
_ENTERPRISE_START_VARS = [
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

# 素材分组 → 开始节点变量名。与 `number_material.SOURCE_ORDER` 对齐。
_ENTERPRISE_MATERIAL_SOURCE = {
    "tech": "material_tech",
    "ip": "material_ip",
    "finance": "material_finance",
}

# 章节清单。
#   inputs    要注入的 in_* （写开始节点变量名，不带 in_ 前缀）
#   materials 本章相关的素材组
#   style     是否给风格样例
_ENTERPRISE_CHAPTERS = [
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

# ---------------------------------------------------------------- 学生 profile
# 通用大创骨架（2026-10-09）：用户稍后提供 MD 知识文件，届时校正字段与章节。

_STUDENT_START_VARS = [
    ("project_name", "项目名称", "text-input", 200, True, None),
    ("project_category", "项目类别", "select", None, True,
     ["创新训练项目", "创业训练项目", "创业实践项目"]),
    ("project_leader", "项目负责人", "text-input", 100, True, None),
    ("college_major", "学院/专业", "text-input", 100, True, None),
    ("grade", "年级", "text-input", 50, True, None),
    ("phone", "联系电话", "text-input", 50, True, None),
    ("advisor", "指导教师（职称）", "text-input", 100, True, None),
    ("team_members", "团队成员", "paragraph", 1000, True, None),
    ("duration", "研究周期", "text-input", 100, True, None),
    ("budget", "经费预算", "text-input", 100, True, None),
    ("background", "研究背景与意义", "paragraph", 4000, True, None),
    ("content_goals", "研究内容与目标", "paragraph", 4000, True, None),
    ("tech_route", "技术路线", "paragraph", 4000, False, None),
    ("innovation", "创新点", "paragraph", 2000, False, None),
    ("expected_outcome", "预期成果", "paragraph", 2000, True, None),
    ("prior_basis", "前期研究基础", "paragraph", 4000, False, None),
]

# 学生项目暂无素材三栏（RAG 接入后回填）—— 空字典，number_material 零素材安全。
_STUDENT_MATERIAL_SOURCE = {}

_STUDENT_CHAPTERS = [
    {
        "num": "01", "out": "gen_section_intro", "title": "项目简介",
        "heading": "项目简介",
        "inputs": ["project_name", "project_category", "content_goals", "innovation"],
        "materials": [], "style": False,
    },
    {
        "num": "02", "out": "gen_section_background", "title": "研究背景与意义",
        "heading": "研究背景与意义",
        "inputs": ["background", "prior_basis"],
        "materials": [], "style": False,
    },
    {
        "num": "03", "out": "gen_section_status", "title": "国内外研究现状",
        "heading": "国内外研究现状",
        "inputs": ["background", "prior_basis"],
        "materials": [], "style": False,
    },
    {
        "num": "04", "out": "gen_section_content", "title": "研究内容与目标",
        "heading": "研究内容与目标",
        "inputs": ["content_goals", "expected_outcome"],
        "materials": [], "style": False,
    },
    {
        "num": "05", "out": "gen_section_route", "title": "技术路线与研究方案",
        "heading": "技术路线与研究方案",
        "inputs": ["tech_route", "content_goals", "duration"],
        "materials": [], "style": False,
    },
    {
        "num": "06", "out": "gen_section_innovation", "title": "创新点",
        "heading": "创新点",
        "inputs": ["innovation", "expected_outcome"],
        "materials": [], "style": False,
    },
    {
        "num": "07", "out": "gen_section_schedule", "title": "进度安排",
        "heading": "进度安排",
        "inputs": ["duration", "content_goals"],
        "materials": [], "style": False,
    },
    {
        "num": "08", "out": "gen_section_budget", "title": "经费预算",
        "heading": "经费预算",
        "inputs": ["budget"],
        "materials": [], "style": False,
    },
    {
        "num": "09", "out": "gen_section_outcome", "title": "预期成果",
        "heading": "预期成果",
        "inputs": ["expected_outcome", "innovation"],
        "materials": [], "style": False,
    },
    {
        "num": "10", "out": "gen_section_team", "title": "团队与指导基础",
        "heading": "团队与指导基础",
        "inputs": ["project_leader", "team_members", "advisor", "college_major", "grade"],
        "materials": [], "style": False,
    },
]

# ---------------------------------------------------------------- PROFILES

PROFILES = {
    "enterprise": {
        "id": "enterprise",
        "label": "科技企业项目申报书",
        "prompts_prefix": "",
        "start_vars": _ENTERPRISE_START_VARS,
        "material_source": _ENTERPRISE_MATERIAL_SOURCE,
        "chapters": _ENTERPRISE_CHAPTERS,
        # 前端分组（顺序即页面分区顺序；没进组的字段挂尾）
        "form_groups": [
            {"title": "基本信息",
             "vars": [v[0] for v in _ENTERPRISE_START_VARS
                      if not v[0].startswith("material_") and v[0] != "style_input"],
             "hint": None},
            {"title": "素材（知识库接入前的临时入口）",
             "vars": ["material_tech", "material_ip", "material_finance"],
             "hint": "每条素材一行。正文里的数字会与这些素材逐条核对（S 编号），放什么就有什么。"},
            {"title": "风格样例",
             "vars": ["style_input"],
             "hint": None},
        ],
        # 粘贴识别的下拉字段与选项 / 模糊归一
        "declaration_var": "declaration_type",
        "declaration_options": ("科技型中小企业", "高新技术企业", "专精特新", "其他"),
        "declaration_fuzzy": {"专精特新": "专精特新", "高新技术": "高新技术企业",
                              "高企": "高新技术企业", "科技型中小": "科技型中小企业"},
        # 要素抽取 in_* ← 表单字段（值为 None 的键不传）
        "elements_vars": {"in_project_name": "project_name",
                          "in_team_size": "team_size",
                          "in_budget_range": "budget_range"},
        "budget_subjects": None,  # None = extract_elements 默认科目表
        # 一致性审查：表单字段数字出处白名单；None = 十项全跑
        "check_form_fields": ["tech_direction", "project_highlights",
                              "expected_outcome", "special_requirements"],
        "checks": None,
        "parse_prompt": "10_parse_input.md",
    },
    "student": {
        "id": "student",
        "label": "大学生科研项目申报书",
        "prompts_prefix": "student",
        "start_vars": _STUDENT_START_VARS,
        "material_source": _STUDENT_MATERIAL_SOURCE,
        "chapters": _STUDENT_CHAPTERS,
        "form_groups": [
            {"title": "基本信息",
             "vars": [v[0] for v in _STUDENT_START_VARS[:10]],
             "hint": None},
            {"title": "研究内容",
             "vars": [v[0] for v in _STUDENT_START_VARS[10:]],
             "hint": None},
        ],
        "declaration_var": "project_category",
        "declaration_options": ("创新训练项目", "创业训练项目", "创业实践项目"),
        "declaration_fuzzy": {},
        "elements_vars": {"in_project_name": "project_name",
                          "in_budget_range": "budget",
                          "in_duration": "duration"},
        "budget_subjects": [
            "设备费", "材料费", "测试化验加工费", "资料费", "差旅费",
            "会议费", "出版/文献/知识产权事务费", "劳务费", "专家咨询费", "其他费用",
        ],
        "check_form_fields": ["background", "content_goals", "tech_route",
                              "innovation", "expected_outcome", "prior_basis",
                              "duration", "budget", "team_members"],
        # check_3 资质佐证是企业专属（专利/资质措辞），学生项目跳过
        "checks": ["check_1", "check_2", "check_4", "check_7",
                   "check_8", "check_9", "check_10"],
        # read_prompt 会自动拼 prompts_prefix —— 这里只写文件名
        "parse_prompt": "parse_input.md",
    },
}

# ---------------------------------------------------------------- 企业别名（勿删）

START_VARS = PROFILES["enterprise"]["start_vars"]
VAR_LABEL = {v[0]: v[1] for v in START_VARS}
MATERIAL_SOURCE = PROFILES["enterprise"]["material_source"]
CHAPTERS = PROFILES["enterprise"]["chapters"]

# `## 输入` 一节是给人看的变量来源表，模型看不到变量名 —— 摘掉。
INPUT_SECTION = re.compile(r"^##\s+输入\s*\n.*?(?=^##\s|\Z)", re.S | re.M)


def get_profile(profile="enterprise"):
    """按 id 取 profile 配置。未知 id → ValueError（调用方尽早炸，别静默跑错配置）。"""
    try:
        return PROFILES[profile]
    except KeyError:
        raise ValueError("未知申报对象 profile：%r（可选：%s）"
                         % (profile, " / ".join(PROFILES)))


def profile_ids():
    return list(PROFILES)


def var_labels(profile="enterprise"):
    return {v[0]: v[1] for v in get_profile(profile)["start_vars"]}


_CN_DIGITS = "一二三四五六七八九十"


def _cn(n):
    """1→一 10→十 11→十一 20→二十（章节最多 20，够用）。"""
    if n <= 10:
        return _CN_DIGITS[n - 1]
    tens, ones = divmod(n, 10)
    return (_CN_DIGITS[tens - 1] if tens > 1 else "") + "十" \
        + (_CN_DIGITS[ones - 1] if ones else "")


def section_spec(profile="enterprise"):
    """章节规格 [(key, "一、标题", out_var), ...] —— assemble_document 的 in_sections 入参。

    run_pipeline 永远传它，assemble_document.SECTIONS 只是企业回退（有防漂移测试）。
    """
    return [(ch["out"], "%s、%s" % (_cn(i), ch["heading"]), ch["out"])
            for i, ch in enumerate(get_profile(profile)["chapters"], 1)]


def read_prompt(name, profile="enterprise"):
    prefix = get_profile(profile)["prompts_prefix"]
    with open(os.path.join(PROMPTS, prefix, name), encoding="utf-8") as fh:
        return fh.read()


def chapter_body(path_name, profile="enterprise"):
    """章节提示词正文，摘掉「## 输入」表。"""
    return INPUT_SECTION.sub("", read_prompt(path_name, profile=profile)).strip("\n")


def user_prompt(chapter, profile="enterprise"):
    """
    章节的 user 消息 = 本次输入 + 要素表 + 本章素材 + 风格 + 章节提示词原文。

    素材与要素表在这里**注入实际内容**，不是给模型变量名让它自己取。
    占位符 {{#node.var#}} 由 run_pipeline._render_prompt 在运行时替换。
    """
    cfg = get_profile(profile)
    labels = {v[0]: v[1] for v in cfg["start_vars"]}
    parts = ["## 本次输入\n"]
    for var in chapter["inputs"]:
        parts.append("- %s：{{#start_node.%s#}}" % (labels[var], var))
    if chapter["materials"]:
        parts.append("- 素材编号：本章素材已按 `S1：…` 逐条编号，引用时必须带 `（Sₙ）`")

    parts.append("\n## 全局要素表 gen_elements（全篇一致，指标值以此为准）\n")
    parts.append("```json\n{{#elements_node.gen_elements#}}\n```")

    if chapter["materials"]:
        parts.append("\n## 本章素材（已编号 —— 引用的编号必须指对）\n")
        for group in chapter["materials"]:
            parts.append("{{#number_node.kb_material_%s#}}\n" % group)

    if chapter["style"]:
        parts.append("## 风格参考（只学表达，不得搬运其中的企业名、数据、事实）\n")
        parts.append("{{#start_node.style_input#}}\n")

    parts.append("---\n")
    parts.append(chapter_body("%s_%s.md" % (chapter["num"], chapter["title"]),
                              profile=profile))
    return "\n".join(parts).strip("\n")
