# -*- coding: utf-8 -*-
"""
章节拼接测试。

重点验三件事：
  1. 拼出来的稿子，审查节点切得出**正好七章**（产消双方对章节边界的理解一致）
  2. 模型写的标题不会切出假章节 —— 切错的后果是错位而不报错
  3. 空章节看得见（占位符 + issues），不静默少一章
"""

import unittest

import check_consistency as check
from assemble_document import SECTIONS, main

FULL = {
    "gen_section_background": "公司成立于 2020 年。",
    "gen_section_tech": "检测精度 97.3%（S1）。",
    "gen_section_schedule": "分三个阶段推进。",
    "gen_section_team": "研发人员 8 人（S1）。",
    "gen_section_outcome": "形成样机一台。",
    "gen_section_budget": "设备费 200 万元。",
    "gen_section_risk": "技术风险可控。",
}


def chapters_of(doc):
    return check._chapters(doc)


class TestAssembly(unittest.TestCase):

    def test_all_seven_headings_in_order(self):
        doc = main(FULL)["gen_document"]
        self.assertEqual(
            [c["name"] for c in chapters_of(doc)],
            ["项目背景与意义", "技术方案", "实施计划", "团队与基础条件",
             "预期成果", "经费预算", "风险与应对"],
        )

    def test_section_count_matches_declaration(self):
        doc = main(FULL)["gen_document"]
        self.assertEqual(len(chapters_of(doc)), len(SECTIONS))

    def test_body_lands_under_its_own_heading(self):
        doc = main(FULL)["gen_document"]
        tech = [c for c in chapters_of(doc) if c["name"] == "技术方案"][0]
        self.assertIn("检测精度 97.3%（S1）。", tech["text"])
        self.assertNotIn("分三个阶段推进。", tech["text"])

    def test_title_line_optional(self):
        self.assertFalse(main(FULL)["gen_document"].startswith("# "))
        doc = main(dict(FULL, in_project_name="某某项目"))["gen_document"]
        self.assertTrue(doc.startswith("# 某某项目"))


class TestHeadingHandling(unittest.TestCase):
    """本节是脚本存在的主要理由，见模块 docstring。"""

    def test_leading_chapter_title_stripped(self):
        """正文开头的 `## 技术方案` 要剥掉，否则标题写两遍。"""
        r = main(dict(FULL, gen_section_tech="## 技术方案\n\n检测精度 97.3%（S1）。"))
        self.assertEqual(r["stats"]["headings_stripped"], 1)
        self.assertEqual(len(chapters_of(r["gen_document"])), len(SECTIONS))

    def test_leading_subsection_kept(self):
        """`### 技术路线` 是小节，不是章节边界，不许剥。"""
        r = main(dict(FULL, gen_section_tech="### 技术路线\n\n检测精度 97.3%（S1）。"))
        self.assertEqual(r["stats"]["headings_stripped"], 0)
        self.assertIn("### 技术路线", r["gen_document"])

    def test_mid_body_h2_demoted(self):
        """
        正文中间的 `## 技术路线` 必须降级。

        不降会被切成假章节 —— 真章节被截断、字数与数字归属全部错位，
        而错位不报错，最难查。
        """
        r = main(dict(FULL, gen_section_tech="前置说明。\n\n## 技术路线\n\n检测精度 97.3%（S1）。"))
        self.assertEqual(r["stats"]["headings_demoted"], 1)
        self.assertIn("### 技术路线", r["gen_document"])
        # 查行首的二级标题，不是子串 —— 「### 技术路线」也含「## 技术路线」
        self.assertNotIn("\n## 技术路线", r["gen_document"])
        self.assertEqual(len(chapters_of(r["gen_document"])), len(SECTIONS))

    def test_h1_demoted_too(self):
        r = main(dict(FULL, gen_section_tech="前置。\n\n# 技术路线\n\n后续。"))
        self.assertIn("### 技术路线", r["gen_document"])
        self.assertEqual(len(chapters_of(r["gen_document"])), len(SECTIONS))

    def test_h3_untouched(self):
        r = main(dict(FULL, gen_section_tech="前置。\n\n### 技术路线\n\n后续。"))
        self.assertEqual(r["stats"]["headings_demoted"], 0)
        self.assertIn("### 技术路线", r["gen_document"])

    def test_heading_inside_fence_untouched(self):
        """代码块里的 `##` 是内容，动了就改坏了。"""
        body = "前置。\n\n```\n## 这不是标题\n```\n\n后续。"
        r = main(dict(FULL, gen_section_tech=body))
        self.assertEqual(r["stats"]["headings_demoted"], 0)
        self.assertIn("## 这不是标题", r["gen_document"])

    def test_demotion_reported(self):
        r = main(dict(FULL, gen_section_tech="前置。\n\n## 技术路线\n\n后续。"))
        types = [i["type"] for i in r["issues"]]
        self.assertIn("heading_demoted", types)
        self.assertEqual(
            [i["severity"] for i in r["issues"] if i["type"] == "heading_demoted"],
            ["warn"],
        )


class TestEmptySections(unittest.TestCase):

    def test_missing_section_gets_placeholder(self):
        r = main({k: v for k, v in FULL.items() if k != "gen_section_risk"})
        self.assertIn("【本章生成失败，需人工撰写】", r["gen_document"])
        self.assertEqual(r["stats"]["empty"], ["七、风险与应对"])

    def test_blank_section_gets_placeholder(self):
        r = main(dict(FULL, gen_section_budget="   \n\n  "))
        self.assertEqual(r["stats"]["empty"], ["六、经费预算"])

    def test_heading_only_body_counts_as_empty(self):
        """只有标题没正文 —— 剥掉标题后是空的，不能算有内容。"""
        r = main(dict(FULL, gen_section_budget="## 六、经费预算\n"))
        self.assertEqual(r["stats"]["empty"], ["六、经费预算"])

    def test_empty_section_still_parses_as_seven_chapters(self):
        r = main({k: v for k, v in FULL.items() if k != "gen_section_risk"})
        self.assertEqual(len(chapters_of(r["gen_document"])), len(SECTIONS))

    def test_empty_section_reported_not_silent(self):
        r = main({})
        self.assertEqual(len(r["issues"]), len(SECTIONS))
        self.assertTrue(all(i["type"] == "empty_section" for i in r["issues"]))


class TestResilience(unittest.TestCase):

    def test_non_string_section_tolerated(self):
        r = main(dict(FULL, gen_section_tech=None, gen_section_budget=123))
        self.assertEqual(r["stats"]["empty"], ["二、技术方案", "六、经费预算"])

    def test_dict_with_text_key_accepted(self):
        r = main(dict(FULL, gen_section_tech={"text": "检测精度 97.3%（S1）。"}))
        self.assertEqual(r["stats"]["empty"], [])
        self.assertIn("检测精度 97.3%（S1）。", r["gen_document"])

    def test_crlf_normalized(self):
        r = main(dict(FULL, gen_section_tech="第一行。\r\n\r\n## 小标题\r\n\r\n第二行。"))
        self.assertNotIn("\r", r["gen_document"])
        self.assertEqual(r["stats"]["headings_demoted"], 1)

    def test_kwargs_call_style(self):
        r = main(gen_section_tech="检测精度 97.3%（S1）。")
        self.assertIn("检测精度 97.3%（S1）。", r["gen_document"])

    def test_document_ends_with_newline(self):
        self.assertTrue(main(FULL)["gen_document"].endswith("\n"))


class TestCheckerContract(unittest.TestCase):
    """拼出来的稿子必须能直接喂给审查节点，不靠人去调格式。"""

    def test_checker_sees_seven_clean_chapters(self):
        """审查节点看到的章节名，与 SECTIONS 声明逐字一致（序号已剥）。"""
        doc = main(FULL)["gen_document"]
        seen = [c["name"] for c in chapters_of(doc) if c["name"] != "（前言）"]
        self.assertEqual(seen, [t.split("、", 1)[1] for _, t, _ in SECTIONS])

    def test_no_stray_h2_outside_section_boundaries(self):
        doc = main(dict(FULL, gen_section_tech="前置。\n\n## 技术路线\n\n后续。"))["gen_document"]
        # 首行的章节标题前面没有换行，单独算
        self.assertEqual(doc.count("\n## ") + int(doc.startswith("## ")), len(SECTIONS))

    def test_checker_runs_on_assembled_doc(self):
        """端到端：拼接 → 审查，不抛异常且能给出结论。"""
        import number_material as nm

        material = nm.main({"kb_material_tech": [
            {"content": "检测精度 97.3%。"},
            {"content": "研发人员 8 人。"},
        ]})
        rep = check.main({
            "gen_document": main(FULL)["gen_document"],
            "gen_elements": {},
            "kb_material": material["kb_material"],
            "section_min": 5,
        })
        self.assertIn("pass", rep)
        self.assertIn("stats", rep)


if __name__ == "__main__":
    unittest.main()
