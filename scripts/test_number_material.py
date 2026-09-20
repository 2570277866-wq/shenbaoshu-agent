# -*- coding: utf-8 -*-
"""
素材编号测试。

重点验四件事：
  1. 编号格式能被一致性审查的解析器原样切回来（产消双方对契约的理解一致）
  2. 条目正文里的「行首 S9：」切不出假条目 —— 错位不报错，最难查
  3. 编号顺序可复现（人拿（S3）去核对时对得上）
  4. 畸形输入不崩
"""

import unittest

import check_consistency as check
from number_material import main


def block_of(r):
    return r["kb_material"]


class TestNumbering(unittest.TestCase):

    def test_basic_sequence(self):
        r = main({"kb_material": [
            {"content": "检测精度 97.3%。", "title": "技术参数.md"},
            {"content": "单帧图像处理耗时 12 ms。", "title": "技术参数.md"},
            {"content": "已授权发明专利 3 项。", "title": "知识产权.md"},
        ]})
        self.assertEqual(block_of(r).split("\n"), [
            "S1：检测精度 97.3%。",
            "S2：单帧图像处理耗时 12 ms。",
            "S3：已授权发明专利 3 项。",
        ])
        self.assertEqual(r["stats"]["count"], 3)

    def test_index_maps_back_to_source(self):
        """人拿（S3）要能查回是哪一条、哪个文件。"""
        r = main({"kb_material": [
            {"content": "A 事实。", "title": "a.md"},
            {"content": "B 事实。", "title": "b.md"},
            {"content": "C 事实。", "title": "c.md"},
        ]})
        third = [e for e in r["kb_index"] if e["id"] == "S3"][0]
        self.assertEqual(third["content"], "C 事实。")
        self.assertEqual(third["title"], "c.md")

    def test_strings_accepted(self):
        r = main({"kb_material": ["甲事实。", "乙事实。"]})
        self.assertEqual(block_of(r).split("\n"), ["S1：甲事实。", "S2：乙事实。"])

    def test_single_string_accepted(self):
        r = main({"kb_material": "单独一条事实。"})
        self.assertEqual(block_of(r), "S1：单独一条事实。")


class TestFlattening(unittest.TestCase):
    """压成单行是为了让审查节点能按行首切条目，见模块 docstring。"""

    def test_embedded_marker_does_not_split_entry(self):
        """
        条目正文里出现行首「S9：」不能切出假条目。

        切错的后果是编号与内容整体错位，而错位不报错 ——
        只会让校验拿着错的内容去比对。这是本项最该测的一条。
        """
        r = main({"kb_material": [
            {"content": "第一段\nS9：这是正文里的假标记\n第二段"},
        ]})
        self.assertEqual(r["stats"]["count"], 1)
        self.assertTrue(r["stats"]["format_ok"], r.get("warning"))
        self.assertNotIn("\n", block_of(r))
        self.assertEqual(block_of(r), "S1：第一段 S9：这是正文里的假标记 第二段")

    def test_newlines_collapsed(self):
        r = main({"kb_material": [{"content": "第一行\n\n第二行   第三行"}]})
        self.assertEqual(block_of(r), "S1：第一行 第二行 第三行")

    def test_front_matter_stripped(self):
        """元信息头不是事实，不进编号正文；但保留在 kb_index 里。"""
        r = main({"kb_material": [{
            "content": "---\n来源：财务台账.xlsx\n日期：2024-06-30\n可信度：高\n---\n"
                       "2024 年营收 3200 万元。",
            "title": "财务数据.md",
        }]})
        self.assertEqual(block_of(r), "S1：2024 年营收 3200 万元。")
        self.assertEqual(r["kb_index"][0]["title"], "财务数据.md")


class TestDedup(unittest.TestCase):

    def test_same_segment_kept_once(self):
        """同一条被两个库召回，只留一份 —— 留两份会让模型引用到两个编号。"""
        r = main({
            "kb_material_tech": [{"segment_id": "x1", "content": "检测精度 97.3%。"}],
            "kb_material_finance": [{"segment_id": "x1", "content": "检测精度 97.3%。"}],
        })
        self.assertEqual(r["stats"]["count"], 1)
        self.assertEqual(r["stats"]["dropped"], 1)

    def test_identical_text_without_id_kept_once(self):
        r = main({"kb_material": [{"content": "同一句。"}, {"content": "同一句。"}]})
        self.assertEqual(r["stats"]["count"], 1)

    def test_different_text_kept(self):
        r = main({"kb_material": [{"content": "甲。"}, {"content": "乙。"}]})
        self.assertEqual(r["stats"]["count"], 2)


class TestGroupOrder(unittest.TestCase):

    def test_source_order_respected(self):
        """技术 → 知识产权 → 财务，顺序固定才能复现。"""
        r = main({
            "kb_material_finance": [{"content": "财务事实。"}],
            "kb_material_ip": [{"content": "知产事实。"}],
            "kb_material_tech": [{"content": "技术事实。"}],
        })
        self.assertEqual(block_of(r).split("\n"), [
            "S1：技术事实。", "S2：知产事实。", "S3：财务事实。",
        ])

    def test_group_recorded_per_entry(self):
        r = main({
            "kb_material_tech": [{"content": "技术事实。"}],
            "kb_material_ip": [{"content": "知产事实。"}],
        })
        self.assertEqual([e["group"] for e in r["kb_index"]], ["tech", "ip"])

    def test_undeclared_group_appended(self):
        r = main({
            "kb_material_tech": [{"content": "技术事实。"}],
            "kb_material_other": [{"content": "别的。"}],
        })
        self.assertEqual(block_of(r).split("\n"),
                         ["S1：技术事实。", "S2：别的。"])

    def test_template_and_style_not_numbered(self):
        """
        模板/风格不得进编号池。

        混进来模型就能给风格库里的数字标（S7），正好绕过「风格只学表达」。
        """
        r = main({
            "kb_material_tech": [{"content": "技术事实。"}],
            "kb_template": [{"content": "模板正文。"}],
            "kb_style": [{"content": "风格段落。"}],
        })
        self.assertEqual(r["stats"]["count"], 1)
        self.assertNotIn("模板正文", block_of(r))
        self.assertNotIn("风格段落", block_of(r))


class TestResilience(unittest.TestCase):

    def test_empty_input(self):
        r = main({})
        self.assertEqual(block_of(r), "")
        self.assertEqual(r["stats"]["count"], 0)
        self.assertTrue(r["stats"]["format_ok"])

    def test_none_and_empty_strings_skipped(self):
        r = main({"kb_material": [None, "", "   ", {"content": ""}]})
        self.assertEqual(r["stats"]["count"], 0)
        self.assertEqual(r["stats"]["dropped"], 4)

    def test_malformed_items_do_not_crash(self):
        r = main({"kb_material": [123, {"no_content_key": 1}, ["嵌套"]]})
        self.assertIn("kb_material", r)

    def test_dict_input_treated_as_single_item(self):
        r = main({"kb_material": {"content": "一条。"}})
        self.assertEqual(block_of(r), "S1：一条。")

    def test_kwargs_call_style(self):
        r = main(kb_material=[{"content": "一条。"}])
        self.assertEqual(block_of(r), "S1：一条。")


class TestCheckerContract(unittest.TestCase):
    """
    产消双方对契约的理解必须一致 —— 这是本脚本存在的理由。

    用真实的 check_consistency 跑端到端，不自己造解析器。
    """

    def test_output_parses_back_to_same_count(self):
        r = main({"kb_material": [
            {"content": "检测精度 97.3%。"},
            {"content": "单帧图像处理耗时 12 ms。"},
            {"content": "已授权发明专利 3 项。"},
        ]})
        idx, numbered = check._material_index(block_of(r))
        self.assertTrue(numbered)
        self.assertEqual(len(idx), 3)
        self.assertEqual(idx["S2"], "单帧图像处理耗时 12 ms。")

    def test_format_ok_flag(self):
        r = main({"kb_material": [{"content": "一条。"}]})
        self.assertTrue(r["stats"]["format_ok"])
        self.assertNotIn("warning", r)

    def test_correct_citation_passes_checker(self):
        """编号块喂给审查节点，正文正确引用 → 不阻断。"""
        r = main({"kb_material": [
            {"content": "检测精度 97.3%。"},
            {"content": "单帧图像处理耗时 12 ms。"},
        ]})
        rep = check.main({
            "gen_document": "## 技术方案\n\n检测精度达到 97.3%（S1），"
                            "单帧图像处理耗时 12 ms（S2）。",
            "gen_elements": {},
            "kb_material": block_of(r),
            "section_min": 10,
        })
        self.assertEqual(rep["stats"]["blocking"], 0, rep["issues"])

    def test_wrong_citation_is_caught(self):
        """把 97.3 标成 S2（S2 写的是 12 ms）—— 必须抓。"""
        r = main({"kb_material": [
            {"content": "检测精度 97.3%。"},
            {"content": "单帧图像处理耗时 12 ms。"},
        ]})
        rep = check.main({
            "gen_document": "## 技术方案\n\n检测精度达到 97.3%（S2）。",
            "gen_elements": {},
            "kb_material": block_of(r),
            "section_min": 10,
        })
        self.assertFalse(rep["pass"])
        self.assertIn("citation_mismatch", [i["type"] for i in rep["issues"]])

    def test_uncited_number_is_caught(self):
        r = main({"kb_material": [{"content": "检测精度 97.3%。"}]})
        rep = check.main({
            "gen_document": "## 技术方案\n\n检测精度达到 97.3%，响应延迟 50 ms。",
            "gen_elements": {},
            "kb_material": block_of(r),
            "section_min": 10,
        })
        self.assertFalse(rep["pass"])
        self.assertIn("number_uncited", [i["type"] for i in rep["issues"]])

    def test_empty_material_does_not_trip_format_check(self):
        """无召回是上游正常的空结果，不该被当成格式错。"""
        r = main({"kb_material": []})
        self.assertTrue(r["stats"]["format_ok"])
        self.assertNotIn("warning", r)


if __name__ == "__main__":
    unittest.main()
