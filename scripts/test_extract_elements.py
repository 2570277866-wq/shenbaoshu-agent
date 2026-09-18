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


class TestExtractElements(unittest.TestCase):

    def setUp(self):
        self.r = main({"kb_material": MATERIAL, "in_project_name": "智能检测系统"})

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
        r = main({"kb_material": [], "in_project_name": "X"})
        self.assertIsNone(r["team_size"])
        self.assertIsNone(r["duration_months"])
        self.assertIsNone(r["total_budget"])
        self.assertEqual(r["budget_breakdown"], {})
        self.assertEqual(r["core_metrics"], [])
        self.assertEqual(r["ip_list"], [])
        self.assertIn("投入人数", r["missing"])
        self.assertIn("项目总投资金额", r["missing"])

    def test_number_not_rounded(self):
        r = main({"kb_material": [{"content": "项目总投资 3200.5 万元。", "title": "财务.md"}]})
        self.assertEqual(r["total_budget"], 3200.5)

    def test_accounting_number_preserved(self):
        r = main({"kb_material": [{"content": "研发人员 1,250 人。", "title": "团队.md"}]})
        self.assertEqual(r["team_size"], 1250)

    # --- 优先级

    def test_user_input_wins_over_material(self):
        r = main({"kb_material": MATERIAL, "in_team_size": "12"})
        self.assertEqual(r["team_size"], 12)
        self.assertEqual(r["_sources"]["team_size"], "开始节点 in_team_size")

    # --- 出处

    def test_every_source_points_back(self):
        self.assertEqual(self.r["_sources"]["team_size"].startswith("素材："), True)
        self.assertIn("财务数据.md", self.r["_budget_sources"].values())

    # --- 兼容性

    def test_kwargs_call_style_matches_dify(self):
        r = main(kb_material=MATERIAL, in_project_name="智能检测系统")
        self.assertEqual(r["team_size"], 8)

    def test_plain_string_material(self):
        r = main({"kb_material": "研发人员 8 人，周期 24 个月。"})
        self.assertEqual(r["team_size"], 8)
        self.assertEqual(r["duration_months"], 24)


if __name__ == "__main__":
    unittest.main()
