# -*- coding: utf-8 -*-
"""要素抽取脚本测试。重点验「不推算」——抽不到必须 missing，不能填默认值。"""

import unittest

from extract_elements import main


MATERIAL = [
    {"content": "公司现有研发人员 8 人，其中博士 2 人。", "title": "团队情况.md"},
    {"content": "本项目实施周期为 24 个月，分三个阶段推进。", "title": "实施计划.md"},
    {"content": "项目总投资 500 万元，其中设备费 200 万元，材料费 100 万元，劳务费 80 万元。",
     "title": "财务数据.md"},
    {"content": "核心技术指标：检测精度目标值 99.2%，现有基线 97.0%；响应时间目标不低于 200ms，"
                "目前为 350ms。",
     "title": "技术参数.md"},
    {"content": "已获发明专利 3 项（已授权，ZL202410123456.7），软件著作权 2 项。",
     "title": "知识产权.md"},
]


def elements(*args, **kwargs):
    """Dify 里这一步是 `{{#elements_node.gen_elements#}}`，测试照同一层级取。"""
    return main(*args, **kwargs)["gen_elements"]


class TestExtractElements(unittest.TestCase):

    def setUp(self):
        self.r = elements({"kb_material": MATERIAL, "in_project_name": "智能检测系统"})

    # --- 正常路径

    def test_scalars(self):
        self.assertEqual(self.r["project_name"], "智能检测系统")
        self.assertEqual(self.r["team_size"], 8)
        self.assertEqual(self.r["duration_months"], 24)
        self.assertEqual(self.r["total_budget"], 500)

    def test_budget_breakdown(self):
        self.assertEqual(self.r["budget_breakdown"]["设备费"], 200)
        self.assertEqual(self.r["budget_breakdown"]["材料费"], 100)
        self.assertEqual(self.r["budget_breakdown"]["劳务费"], 80)

    def test_core_metrics(self):
        names = [m["name"] for m in self.r["core_metrics"]]
        self.assertIn("检测精度", names)
        m = next(m for m in self.r["core_metrics"] if m["name"] == "检测精度")
        self.assertEqual(m["target"], "99.2%")
        self.assertEqual(m["baseline"], "97.0%")
        self.assertTrue(m["source"])

    def test_ip_list(self):
        ip = {i["type"]: i for i in self.r["ip_list"]}
        self.assertEqual(ip["发明专利"]["count"], 3)
        self.assertEqual(ip["软件著作权"]["count"], 2)
        self.assertIn("ZL202410123456.7", ip["发明专利"]["numbers"])

    def test_full_material_has_no_missing(self):
        self.assertEqual(self.r["missing"], [])

    # --- 铁律：不推算

    def test_no_fabrication_on_empty_material(self):
        """素材为空 —— 除用户输入外，一律 None / 空，且全部进 missing。"""
        r = elements({"kb_material": [], "in_project_name": "X"})
        self.assertIsNone(r["team_size"])
        self.assertIsNone(r["duration_months"])
        self.assertIsNone(r["total_budget"])
        self.assertEqual(r["budget_breakdown"], {})
        self.assertEqual(r["core_metrics"], [])
        self.assertEqual(r["ip_list"], [])
        self.assertIn("投入人数", r["missing"])
        self.assertIn("项目总投资金额", r["missing"])

    def test_number_not_rounded(self):
        r = elements({"kb_material": [{"content": "项目总投资 3200.5 万元。", "title": "财务.md"}]})
        self.assertEqual(r["total_budget"], 3200.5)

    def test_accounting_number_preserved(self):
        r = elements({"kb_material": [{"content": "研发人员 1,250 人。", "title": "团队.md"}]})
        self.assertEqual(r["team_size"], 1250)

    # --- 优先级

    def test_user_input_wins_over_material(self):
        r = elements({"kb_material": MATERIAL, "in_team_size": "12"})
        self.assertEqual(r["team_size"], 12)
        self.assertEqual(r["_sources"]["team_size"], "开始节点 in_team_size")

    # --- 出处

    def test_every_source_points_back(self):
        self.assertEqual(self.r["_sources"]["team_size"].startswith("素材："), True)
        self.assertIn("财务数据.md", self.r["_budget_sources"].values())

    # --- 兼容性

    def test_kwargs_call_style_matches_dify(self):
        r = elements(kb_material=MATERIAL, in_project_name="智能检测系统")
        self.assertEqual(r["team_size"], 8)

    def test_plain_string_material(self):
        r = elements({"kb_material": "研发人员 8 人，周期 24 个月。"})
        self.assertEqual(r["team_size"], 8)
        self.assertEqual(r["duration_months"], 24)

    # --- 周期日历陷阱（v0.8 首跑：「2026年1月…」的 1 月被抽成周期）---

    def test_duration_calendar_dates_not_mistaken_for_months(self):
        r = elements({"kb_material": "研发周期：2026年1月至2027年12月，共24个月。"})
        self.assertEqual(r["duration_months"], 24)

    def test_duration_date_range_without_explicit_months_not_derived(self):
        """只给起止日期不推算 —— 抽不到进 missing，逼素材补明确周期。"""
        r = elements({"kb_material": "研发周期：2026年1月至2027年12月。"})
        self.assertIsNone(r["duration_months"])
        self.assertIn("项目周期", r["missing"])

    def test_duration_years_to_months(self):
        r = elements({"kb_material": "本项目实施周期为 2 年。"})
        self.assertEqual(r["duration_months"], 24)

    def test_duration_calendar_year_not_mistaken_for_duration(self):
        """「2025 年营业收入」是日历年份，不是周期 —— 不锚上下文会抽成 2025×12=24300 月。"""
        r = elements({"kb_material": "2025 年营业收入 486.5 万元。"})
        self.assertIsNone(r["duration_months"])

    # --- 预算：素材精确值优先于表单区间 ---

    def test_budget_material_total_wins_over_form_range(self):
        """表单「180万-220万」是区间不是总额 —— 取首数 180 当总额是 v0.8 首跑的真错。"""
        r = elements({"kb_material": "项目预算：设备购置 45 万、其他 35 万，合计 200 万。",
                      "in_budget_range": "180万-220万"})
        self.assertEqual(r["total_budget"], 200)
        self.assertTrue(r["_sources"]["total_budget"].startswith("素材："))

    def test_budget_form_range_without_material_total_goes_missing(self):
        r = elements({"kb_material": "公司现有研发人员 8 人。", "in_budget_range": "180万-220万"})
        self.assertIsNone(r["total_budget"])
        self.assertTrue(any("项目总投资金额" in m for m in r["missing"]),
                        r["missing"])

    def test_budget_single_number_form_still_used(self):
        r = elements({"kb_material": [], "in_budget_range": "500 万元"})
        self.assertEqual(r["total_budget"], 500)

    # --- Dify 契约

    def test_output_wrapped_under_gen_elements(self):
        """
        外层键必须叫 `gen_elements`。

        摊平返回的话，Dify 代码节点要为此声明十个输出变量，
        提示词里也没法整体注入 —— 各章节就拿不到同一份要素表。
        """
        r = main({"kb_material": MATERIAL, "in_project_name": "智能检测系统"})
        self.assertIn("gen_elements", r)
        self.assertEqual(r["gen_elements"]["team_size"], 8)
        self.assertEqual(r["stats"]["missing"], 0)


class TestBudgetSubjectsOverride(unittest.TestCase):
    """双 profile：budget_subjects 覆盖默认科目表（学生大创科目与企业国拨科目不同）。"""

    def test_custom_subjects_catch_default_misses(self):
        # 大创科目「出版/文献/知识产权事务费」不在企业默认表 → 默认抓不到
        chunk = [{"content": "项目经费 20000 元，其中出版/文献/知识产权事务费 3000 元。",
                  "title": "经费.md"}]
        default = elements({"kb_material": chunk})
        self.assertNotIn("出版/文献/知识产权事务费", default["budget_breakdown"])
        custom = elements({"kb_material": chunk,
                           "budget_subjects": ["设备费", "劳务费",
                                               "出版/文献/知识产权事务费"]})
        self.assertEqual(custom["budget_breakdown"]["出版/文献/知识产权事务费"], 3000)


class TestInDurationFallback(unittest.TestCase):
    """学生 profile：周期在表单里（无素材）—— 只认明确表述，不推算。"""

    def test_bracket_year_counted(self):
        r = elements({"kb_material": [], "in_duration":
                      "2026年10月至2027年9月（一年）"})
        self.assertEqual(r["duration_months"], 12)

    def test_bare_dates_not_guessed(self):
        r = elements({"kb_material": [], "in_duration": "2026年10月至2027年9月"})
        self.assertIsNone(r["duration_months"])
        self.assertIn("项目周期", r["missing"])

    def test_material_wins_over_form(self):
        r = elements({"kb_material": [{"content": "本项目实施周期为 24 个月。",
                                       "title": "周期.md"}],
                      "in_duration": "2026年10月至2027年9月（一年）"})
        self.assertEqual(r["duration_months"], 24)


if __name__ == "__main__":
    unittest.main()
