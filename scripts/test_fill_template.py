# -*- coding: utf-8 -*-
"""
变量替换脚本测试。

其中 test_real_template_* 直接吃 templates/申报书模板.md ——
模板与脚本是两份文件，最容易发散，必须用真模板测。
"""

import os
import re
import unittest

from fill_template import main

TEMPLATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "..", "templates", "申报书模板.md")

ELEMENTS = {
    "project_name": "智能检测系统",
    "team_size": 8,
    "duration_months": 24,
    "total_budget": 500,
    "budget_breakdown": {"设备费": 200, "材料费": 100, "劳务费": 80},
    "core_metrics": [
        {"name": "检测精度", "target": "99.2%", "baseline": "97.0%", "source": "2024检测报告"},
        {"name": "响应时间", "target": "200ms", "baseline": "350ms", "source": "技术参数.md"},
    ],
    "ip_list": [],
    "missing": [],
}

TINY_TEMPLATE = """# {{in_project_name}}

## 二、技术方案

{{gen_section_tech}}

| 指标 | 目标值 | 现有基线 | 数据来源 |
|---|---|---|---|
| {{gen_metric_name}} | {{gen_metric_target}} | {{gen_metric_baseline}} | {{gen_metric_source}} |

| 科目 | 金额（万元） | 占比 | 说明 |
|---|---|---|---|
| {{gen_budget_item}} | {{gen_budget_amount}} | {{gen_budget_ratio}} | {{gen_budget_note}} |
| **合计** | **{{gen_budget_total}}** | 100% | |
"""


def read_real_template():
    with open(TEMPLATE_PATH, encoding="utf-8") as f:
        return f.read()


class TestFillTemplate(unittest.TestCase):

    def setUp(self):
        self.r = main({
            "template": TINY_TEMPLATE,
            "gen_elements": ELEMENTS,
            "in_project_name": "智能检测系统",
            "gen_section_tech": "本项目采用深度学习方案。",
            "gen_budget_total": 500,
            "gen_date": "2026-09-18",
        })
        self.doc = self.r["gen_document"]

    # --- 标量

    def test_scalar_replaced(self):
        self.assertIn("# 智能检测系统", self.doc)
        self.assertIn("本项目采用深度学习方案。", self.doc)
        self.assertNotIn("{{in_project_name}}", self.doc)

    # --- 表格行展开

    def test_rows_expanded_to_array_length(self):
        rows = [l for l in self.doc.split("\n") if l.startswith("| 检测精度")]
        self.assertEqual(len(rows), 1)
        self.assertIn("99.2%", rows[0])
        self.assertIn("97.0%", rows[0])

    def test_second_metric_also_expanded(self):
        self.assertIn("| 响应时间 | 200ms | 350ms |", self.doc)

    def test_total_row_not_expanded(self):
        """合计行是标量，不能被当成明细行展开。"""
        total_lines = [l for l in self.doc.split("\n") if "合计" in l]
        self.assertEqual(len(total_lines), 1)
        self.assertIn("500", total_lines[0])

    def test_budget_ratio_computed(self):
        self.assertIn("| 设备费 | 200 | 40.0% |", self.doc)

    def test_empty_metric_array_drops_row_and_reports(self):
        r = main({"template": TINY_TEMPLATE, "gen_elements": dict(ELEMENTS, core_metrics=[]),
                  "gen_section_tech": "x", "gen_budget_total": 1})
        self.assertNotIn("{{gen_metric", r["gen_document"])
        self.assertNotIn("检测精度", r["gen_document"])
        self.assertTrue(any(i["type"] == "empty_table" for i in r["issues"]))

    # --- 空值

    def test_missing_section_becomes_tbd(self):
        r = main({"template": TINY_TEMPLATE, "gen_elements": ELEMENTS,
                  "gen_section_tech": ""})
        self.assertIn("【待补充：技术方案】", r["gen_document"])
        self.assertTrue(any(i["type"] == "missing_data" for i in r["issues"]))

    def test_no_silent_blank(self):
        """不留空 —— 缺什么必须看得见。"""
        r = main({"template": TINY_TEMPLATE, "gen_elements": ELEMENTS})
        self.assertNotIn("\n\n\n", r["gen_document"].replace("\n\n", "\n\n"))
        self.assertIn("【待补充", r["gen_document"])

    # --- 残留

    def test_unknown_variable_name_reported(self):
        """名字不符合 in_/gen_ 规则 —— 是模板笔误，不是素材缺口。"""
        r = main({"template": "{{unknown_var}}", "gen_elements": {}})
        self.assertTrue(any(i["type"] == "unknown_variable" for i in r["issues"]))

    def test_declared_but_unassigned_variable_is_missing_data(self):
        r = main({"template": "{{gen_section_tech}}", "gen_elements": {}})
        self.assertTrue(any(i["type"] == "missing_data" for i in r["issues"]))

    def test_residual_brace_reported(self):
        """格式错到正则吃不掉的占位符，靠扫 {{ 兜底。"""
        r = main({"template": "正文 {{123bad}} 结尾", "gen_elements": {}})
        self.assertTrue(any(i["type"] == "unresolved_placeholder" for i in r["issues"]))

    def test_empty_template_reports(self):
        r = main({"template": "", "gen_elements": {}})
        self.assertEqual(r["gen_document"], "")
        self.assertEqual(r["issues"][0]["type"], "missing_template")


class TestRealTemplate(unittest.TestCase):
    """真模板集成测试 —— 模板与脚本必须始终对得上。"""

    def setUp(self):
        self.r = main({
            "template": read_real_template(),
            "gen_elements": ELEMENTS,
            "in_project_name": "智能检测系统",
            "in_declaration_type": "科技型中小企业创新基金",
            "in_tech_direction": "机器视觉",
            "in_project_leader": "张三",
            "in_team_size": 8,
            "gen_section_background": "背景正文",
            "gen_section_tech": "技术正文",
            "gen_section_schedule": "计划正文",
            "gen_section_team": "团队正文",
            "gen_section_outcome": "成果正文",
            "gen_section_budget": "预算正文",
            "gen_section_risk": "风险正文",
            "gen_budget_total": 500,
            "gen_date": "2026-09-18",
            "gen_milestones": [
                {"phase": "第一阶段", "time": "第1-8月", "goal": "完成样机", "output": "样机1台"},
                {"phase": "第二阶段", "time": "第9-18月", "goal": "中试", "output": "中试报告"},
            ],
        })
        self.doc = self.r["gen_document"]

    def test_no_placeholder_left(self):
        """成稿不得残留任何 {{ }} —— 一致性审查第 4 项。"""
        self.assertEqual(re.findall(r"\{\{.*?\}\}", self.doc), [])

    def test_no_issues_with_complete_input(self):
        self.assertEqual(self.r["issues"], [])

    def test_template_comment_removed(self):
        self.assertNotIn("模板说明", self.doc)
        self.assertNotIn("<!--", self.doc)

    def test_all_seven_chapters_present(self):
        for title in ("项目背景与意义", "技术方案", "实施计划", "团队与基础条件",
                      "预期成果", "经费预算", "风险与应对"):
            self.assertIn("## ", self.doc)
            self.assertIn(title, self.doc)

    def test_rows_match_array_length(self):
        metric_rows = [l for l in self.doc.split("\n")
                       if l.startswith("|") and ("99.2%" in l or "200ms" in l)]
        self.assertEqual(len(metric_rows), 2)
        budget_rows = [l for l in self.doc.split("\n")
                       if l.startswith("| 设备费") or l.startswith("| 材料费") or l.startswith("| 劳务费")]
        self.assertEqual(len(budget_rows), 3)

    def test_stats_word_count(self):
        self.assertIn("技术方案", self.r["stats"]["section_chars"])
        self.assertGreater(self.r["stats"]["total_chars"], 200)


if __name__ == "__main__":
    unittest.main()
