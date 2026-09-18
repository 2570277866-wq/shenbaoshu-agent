# -*- coding: utf-8 -*-
"""
一致性审查测试 —— 本项目最该测的脚本。

重点验三件事：
  1. 真错必抓（数字矛盾、杜撰资质、风格串事实、占位符残留）
  2. 阻断与降级分得清（pass 只看 block 级）
  3. 畸形输入不崩（审查脚本抛异常会让整个工作流失败且无法定位）
"""

import json
import unittest

from check_consistency import main

KB = [
    {"content": "公司现有研发人员 8 人，其中博士 2 人。", "title": "团队情况.md"},
    {"content": "本项目实施周期为 24 个月。", "title": "实施计划.md"},
    {"content": "项目总投资 500 万元，其中设备费 200 万元，材料费 100 万元。",
     "title": "财务数据.md"},
    {"content": "检测精度达到 99.2%，现有基线 97.0%。", "title": "技术参数.md"},
    {"content": "已获发明专利 3 项，专利号 ZL202410123456.7。", "title": "知识产权.md"},
]

ELEMENTS = {
    "project_name": "智能检测系统",
    "team_size": 8,
    "duration_months": 24,
    "total_budget": 500,
    "budget_breakdown": {"设备费": 200, "材料费": 100},
    "core_metrics": [{"name": "检测精度", "target": "99.2%", "baseline": "97.0%",
                      "source": "技术参数.md"}],
    "ip_list": [{"type": "发明专利", "count": 3, "status": "已授权",
                 "numbers": ["ZL202410123456.7"], "source": "知识产权.md"}],
    "missing": [],
}

FILLER = "本项目的实施将显著提升企业的技术能力与市场竞争力，形成自主可控的核心技术体系。" * 8


def build_doc(*, team="8", period="24", budget="500", device="200",
              metric="99.2%", patent="ZL202410123456.7", extra=""):
    return """# 智能检测系统

**投入人数：** 8 人
**申报日期：** 2026-09-18

## 一、项目背景与意义

{F}

## 二、技术方案

检测精度达到 {M}。

{F}

## 三、实施计划

本项目实施周期为 {P} 个月。

{F}

## 四、团队与基础条件

公司现有研发人员 {T} 人。

{F}

## 六、经费预算

项目总投资 {B} 万元。

| 科目 | 金额（万元） | 占比 | 说明 |
|---|---|---|---|
| 设备费 | {D} | 40.0% | |
| 材料费 | 100 | 20.0% | |
| **合计** | **{B}** | 100% | |

## 七、风险与应对

已获发明专利，专利号 {ZL}。

{F}

{EXTRA}
""".format(F=FILLER, M=metric, P=period, T=team, B=budget, D=device,
           ZL=patent, EXTRA=extra)


def run(*, doc=None, elements=None, kb=None, **kw):
    return main({
        "gen_document": doc if doc is not None else build_doc(),
        "gen_elements": elements if elements is not None else ELEMENTS,
        "kb_material": kb if kb is not None else KB,
        "section_min": 10,
        **kw,
    })


def types(r):
    return [i["type"] for i in r["issues"]]


class TestCleanPass(unittest.TestCase):

    def test_clean_doc_passes(self):
        r = run()
        self.assertTrue(r["pass"], r["issues"])
        self.assertEqual(r["issues"], [])

    def test_stats_present(self):
        r = run()
        self.assertEqual(r["stats"]["tbd_count"], 0)
        self.assertIn("技术方案", r["stats"]["word_count"])


class TestCheck1NumberConsistency(unittest.TestCase):

    def test_team_size_mismatch(self):
        r = run(doc=build_doc(team="12"))
        self.assertFalse(r["pass"])
        self.assertIn("number_mismatch", types(r))
        detail = [i for i in r["issues"] if i["type"] == "number_mismatch"][0]["detail"]
        self.assertIn("12", detail)
        self.assertIn("8", detail)

    def test_duration_mismatch(self):
        r = run(doc=build_doc(period="36"))
        self.assertFalse(r["pass"])
        self.assertIn("number_mismatch", types(r))

    def test_total_budget_mismatch(self):
        r = run(doc=build_doc(budget="800"))
        self.assertFalse(r["pass"])

    def test_budget_subject_mismatch(self):
        r = run(doc=build_doc(device="300"))
        self.assertFalse(r["pass"])
        self.assertTrue(any(i["section"] == "经费预算" for i in r["issues"]))

    def test_thousands_separator_tolerated(self):
        r = run(doc=build_doc().replace("研发人员 8 人", "研发人员 8 人"),
                elements=dict(ELEMENTS, team_size="8"))
        self.assertEqual([i for i in r["issues"] if i["type"] == "number_mismatch"], [])


class TestCheck3Qualification(unittest.TestCase):

    def test_fabricated_patent_number(self):
        r = run(doc=build_doc(patent="ZL209999999999.9"))
        self.assertFalse(r["pass"])
        self.assertIn("fabricated_qualification", types(r))

    def test_real_patent_number_passes(self):
        r = run()
        self.assertNotIn("fabricated_qualification", types(r))


class TestCheck4Placeholder(unittest.TestCase):

    def test_residual_placeholder_blocks(self):
        r = run(doc=build_doc() + "\n残留 {{gen_section_risk}} 未替换。")
        self.assertFalse(r["pass"])
        self.assertIn("unresolved_placeholder", types(r))


class TestCheck5Tbd(unittest.TestCase):

    def test_tbd_counted_but_not_blocking(self):
        r = run(doc=build_doc(extra="经费明细【待补充：设备费】、【待补充：材料费】。"))
        self.assertEqual(r["stats"]["tbd_count"], 2)
        self.assertIn("missing_data", types(r))
        self.assertTrue(r["pass"], "缺素材属降级，不该阻断 —— 人看得出来")

    def test_tbd_severity_is_warn(self):
        r = run(doc=build_doc(extra="【待补充：x】"))
        issue = [i for i in r["issues"] if i["type"] == "missing_data"][0]
        self.assertEqual(issue["severity"], "warn")


class TestCheck6WordCount(unittest.TestCase):

    def test_short_section_warns_not_blocks(self):
        r = run(doc=build_doc(), section_min=300)
        self.assertTrue(any(i["type"] == "word_count" for i in r["issues"]))
        self.assertTrue(r["pass"], "字数偏短属降级，交人工")

    def test_long_section_no_warning(self):
        r = run(section_min=10)
        self.assertNotIn("word_count", types(r))


class TestCheck2Traceable(unittest.TestCase):

    def test_untraceable_number_warns(self):
        r = run(doc=build_doc(extra="本项目预计新增销售收入 9999 万元。"))
        self.assertIn("number_not_traceable", types(r))
        self.assertTrue(r["pass"], "第 2 项为 warn —— 阻断会让回退循环停不下来")

    def test_ratio_and_total_not_flagged(self):
        """占比列与合计行是派生值，不该被当成无出处数字。"""
        r = run()
        untraceable = [i for i in r["issues"] if i["type"] == "number_not_traceable"]
        self.assertEqual(untraceable, [])

    def test_preamble_not_checked(self):
        """封面区填的是用户输入，不是素材事实。"""
        r = run(doc=build_doc().replace("**投入人数：** 8 人", "**投入人数：** 8 人"),
                kb=[{"content": "无任何数字的素材。", "title": "x.md"}])
        self.assertEqual([i for i in r["issues"] if i["type"] == "number_not_traceable"], [])


class TestCheck7StyleLeak(unittest.TestCase):

    def test_style_entity_leak_blocks(self):
        r = run(doc=build_doc(extra="参照某某科技有限公司的做法，我们采用相同路线。"),
                style_entities=["某某科技有限公司"])
        self.assertFalse(r["pass"])
        self.assertIn("style_leak", types(r))

    def test_no_style_entities_no_noise(self):
        r = run(doc=build_doc(extra="参照某某科技有限公司的做法。"))
        self.assertNotIn("style_leak", types(r))


class TestResilience(unittest.TestCase):
    """畸形输入不崩 —— 转成 issue，不抛异常。"""

    def test_empty_document(self):
        r = main({"gen_document": "", "gen_elements": {}, "kb_material": []})
        self.assertFalse(r["pass"])
        self.assertIn("empty_document", types(r))

    def test_elements_as_bad_json_string(self):
        r = main({"gen_document": build_doc(), "gen_elements": "{不是JSON",
                  "kb_material": KB, "section_min": 10})
        self.assertIn("pass", r)

    def test_elements_as_good_json_string(self):
        r = main({"gen_document": build_doc(), "gen_elements": json.dumps(ELEMENTS),
                  "kb_material": KB, "section_min": 10})
        self.assertTrue(r["pass"], r["issues"])

    def test_kb_material_empty_still_reports(self):
        r = main({"gen_document": build_doc(), "gen_elements": ELEMENTS,
                  "kb_material": [], "section_min": 10})
        self.assertIn("no_material", types(r))

    def test_kb_material_weird_type(self):
        r = main({"gen_document": build_doc(), "gen_elements": ELEMENTS,
                  "kb_material": 12345, "section_min": 10})
        self.assertIn("pass", r)

    def test_style_entities_as_comma_string(self):
        r = main({"gen_document": build_doc(extra="某某科技有限公司"),
                  "gen_elements": ELEMENTS, "kb_material": KB,
                  "style_entities": "某人公司，某某科技有限公司", "section_min": 10})
        self.assertIn("style_leak", types(r))

    def test_kwargs_call_style(self):
        r = main(gen_document=build_doc(), gen_elements=ELEMENTS, kb_material=KB,
                 section_min=10)
        self.assertTrue(r["pass"])


if __name__ == "__main__":
    unittest.main()
