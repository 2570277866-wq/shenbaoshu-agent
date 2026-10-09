# -*- coding: utf-8 -*-
"""
build_workflow 测试 —— 引擎真相源的契约。

历史：本文件曾含 YAML 发射器测试（确定性 / 图连通 / 引用解析 / 产物一致性）。
产品化 MVP 脱离 Dify、发射器删除后，只剩两类必须卡的契约：
  1. 提示词真是从 scripts/prompts/ 读的，不是抄了一份
  2. 章节的 user 提示词组装正确（素材路由、风格开关、要素表、编号规则）
"""

import re
import unittest

import build_workflow as bw


class TestPrompts(unittest.TestCase):

    def test_system_prompt_read_from_file(self):
        """提示词是唯一真相，不许抄一份内联进代码。"""
        system = bw.read_prompt("00_system.md").rstrip("\n")
        marker = "**没有来源标记的数字，视同编造 —— 一个都不许写。**"
        self.assertIn(marker, system)

    def test_input_section_stripped(self):
        """`## 输入` 是给人看的变量来源表；模型看到的是注入的实际内容。"""
        for chapter in bw.CHAPTERS:
            body = bw.chapter_body("%s_%s.md" % (chapter["num"], chapter["title"]))
            self.assertNotIn("## 输入", body)

    def test_chapter_body_keeps_its_constraints(self):
        """摘「## 输入」时不能把后面的正文一起摘掉 —— 正则少个边界就会。"""
        for chapter in bw.CHAPTERS:
            body = bw.chapter_body("%s_%s.md" % (chapter["num"], chapter["title"]))
            self.assertIn("## 任务", body, chapter["title"])
            self.assertIn("## 输出格式", body, chapter["title"])

    def test_each_chapter_gets_its_own_body(self):
        for chapter in bw.CHAPTERS:
            prompt = bw.user_prompt(chapter)
            self.assertIn("不超过", prompt, chapter["title"])

    def test_material_routing(self):
        """素材只给本章相关的 —— 写经费预算不需要专利清单。"""
        budget = bw.user_prompt(bw.CHAPTERS[5])
        self.assertIn("kb_material_finance", budget)
        self.assertNotIn("kb_material_tech", budget)

        tech = bw.user_prompt(bw.CHAPTERS[1])
        self.assertIn("kb_material_tech", tech)
        self.assertIn("kb_material_ip", tech)
        self.assertNotIn("kb_material_finance", tech)

    def test_style_only_where_declared(self):
        self.assertIn("style_input", bw.user_prompt(bw.CHAPTERS[0]))
        self.assertNotIn("style_input", bw.user_prompt(bw.CHAPTERS[5]))

    def test_every_chapter_gets_elements_table(self):
        """省掉要素表 = 章节间数字不一致。"""
        for chapter in bw.CHAPTERS:
            self.assertIn("{{#elements_node.gen_elements#}}", bw.user_prompt(chapter))

    def test_material_prompt_states_numbering_rule(self):
        """素材注入了编号，必须同时说明「引用要带编号、编号要指对」。"""
        for chapter in bw.CHAPTERS:
            self.assertIn("（Sₙ）", bw.user_prompt(chapter))


class TestPlaceholders(unittest.TestCase):
    """占位符必须能被 run_pipeline._render_prompt 替换 —— 指不到就空着进模型。"""

    START = {v[0] for v in bw.START_VARS}
    NUMBER = {"kb_material_" + g for g in bw.MATERIAL_SOURCE}

    def test_every_placeholder_resolvable(self):
        for chapter in bw.CHAPTERS:
            for node, var in re.findall(
                    r"\{\{#([A-Za-z0-9_]+)\.([A-Za-z0-9_]+)#\}\}",
                    bw.user_prompt(chapter)):
                if node == "start_node":
                    self.assertIn(var, self.START, (chapter["title"], var))
                elif node == "elements_node":
                    self.assertEqual(var, "gen_elements", chapter["title"])
                elif node == "number_node":
                    self.assertIn(var, self.NUMBER, (chapter["title"], var))
                else:
                    self.fail("未知占位符节点 %s（%s）" % (node, chapter["title"]))

    def test_material_fields_map_to_form_vars(self):
        names = self.START
        for group, var in bw.MATERIAL_SOURCE.items():
            self.assertIn(var, names, group)

    def test_required_fields_present(self):
        named = self.START
        for expected in ("project_name", "tech_direction", "project_leader",
                         "team_size", "budget_range", "expected_outcome",
                         "project_highlights"):
            self.assertIn(expected, named)


class TestProfileContract(unittest.TestCase):
    """双 profile：别名指向企业、每个 profile 的自洽、提示词文件与章节契约齐全。"""

    def test_alias_points_to_enterprise(self):
        self.assertIs(bw.START_VARS, bw.PROFILES["enterprise"]["start_vars"])
        self.assertIs(bw.CHAPTERS, bw.PROFILES["enterprise"]["chapters"])
        self.assertEqual(bw.VAR_LABEL, {v[0]: v[1] for v in bw.START_VARS})

    def test_profile_ids_and_labels(self):
        self.assertEqual(bw.profile_ids(), ["enterprise", "student"])
        for pid in bw.profile_ids():
            cfg = bw.get_profile(pid)
            self.assertEqual(cfg["id"], pid)
            self.assertTrue(cfg["label"])

    def test_unknown_profile_raises(self):
        with self.assertRaises(ValueError):
            bw.get_profile("nope")

    def test_out_and_num_unique_per_profile(self):
        for pid in bw.profile_ids():
            chapters = bw.get_profile(pid)["chapters"]
            self.assertEqual(len({c["out"] for c in chapters}), len(chapters), pid)
            nums = [int(c["num"]) for c in chapters]
            self.assertEqual(nums, list(range(nums[0], nums[0] + len(chapters))),
                             pid)  # 连续递增（企业从 02 起，学生从 01 起）

    def test_prompt_files_exist_for_every_chapter(self):
        import os
        prompts = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompts")
        for pid in bw.profile_ids():
            cfg = bw.get_profile(pid)
            prefix = cfg["prompts_prefix"]
            self.assertTrue(os.path.exists(
                os.path.join(prompts, prefix, "00_system.md")), pid)
            for ch in cfg["chapters"]:
                path = os.path.join(prompts, prefix,
                                    "%s_%s.md" % (ch["num"], ch["title"]))
                self.assertTrue(os.path.exists(path), path)

    def test_chapter_files_keep_contract(self):
        for pid in bw.profile_ids():
            for ch in bw.get_profile(pid)["chapters"]:
                body = bw.chapter_body("%s_%s.md" % (ch["num"], ch["title"]),
                                       profile=pid)
                self.assertNotIn("## 输入", body, ch["title"])
                self.assertIn("## 任务", body, ch["title"])
                self.assertIn("## 输出格式", body, ch["title"])

    def test_student_prompts_no_material_numbering(self):
        for ch in bw.get_profile("student")["chapters"]:
            prompt = bw.user_prompt(ch, profile="student")
            self.assertNotIn("（Sₙ）", prompt, ch["title"])  # 无素材 → 不发编号规则
        self.assertIn("（Sₙ）", bw.user_prompt(bw.CHAPTERS[0]))  # 企业照发

    def test_student_checks_skip_check3(self):
        self.assertNotIn("check_3", bw.get_profile("student")["checks"])
        self.assertIsNone(bw.get_profile("enterprise")["checks"])

    def test_section_spec_cn_numbers(self):
        spec = bw.section_spec("student")
        self.assertEqual([s[1].split("、")[0] for s in spec],
                         list("一二三四五六七八九十"))
        self.assertEqual(spec[-1][1], "十、团队与指导基础")

    def test_form_groups_cover_all_vars(self):
        for pid in bw.profile_ids():
            cfg = bw.get_profile(pid)
            names = {x[0] for x in cfg["start_vars"]}
            grouped = set()
            for g in cfg["form_groups"]:
                for v in g["vars"]:
                    self.assertIn(v, names, (pid, v))
                grouped |= set(g["vars"])
            self.assertEqual(grouped, names, pid)

    def test_student_parse_prompt_no_double_prefix(self):
        cfg = bw.get_profile("student")
        self.assertEqual(cfg["parse_prompt"], "parse_input.md")
        bw.read_prompt(cfg["parse_prompt"], profile="student")  # 文件存在


if __name__ == "__main__":
    unittest.main()
