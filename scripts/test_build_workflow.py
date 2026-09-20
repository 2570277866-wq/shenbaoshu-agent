# -*- coding: utf-8 -*-
"""
工作流生成器测试。

生成物是给人导入 Dify 的，错了要到界面上才发现 —— 故这里把能在本地验的全验掉：
  1. 确定性（否则 git diff 全是噪声，看不出真正改了什么）
  2. 每个引用都指得到（指不到 Dify 直接拒绝导入）
  3. 提示词真是从 `dify/prompts/` 读的，不是抄了一份
  4. 图连通、无环、七个章节都在
"""

import re
import unittest

import build_workflow as bw


class TestDeterminism(unittest.TestCase):

    def test_emit_twice_identical(self):
        self.assertEqual(bw.emit(), bw.emit())

    def test_no_tabs(self):
        """YAML 禁止用制表符缩进 —— 有一个就整份解析失败。"""
        self.assertNotIn("\t", bw.emit())

    def test_node_ids_unique(self):
        ids = re.findall(r"^      id: '([A-Za-z0-9_]+)'$", bw.emit(), re.M)
        self.assertEqual(len(ids), len(set(ids)))


class TestGraph(unittest.TestCase):

    def setUp(self):
        self.built = bw.build()

    def node_ids(self):
        return re.findall(r"^      id: '([A-Za-z0-9_]+)'$",
                          "\n".join(self.built["nodes"]), re.M)

    def test_all_seven_chapters_present(self):
        ids = self.node_ids()
        for chapter in bw.CHAPTERS:
            self.assertIn("llm_%s" % chapter["num"], ids)

    def test_graph_connected_from_start(self):
        """每个节点都要能从开始节点走到 —— 孤立节点在 Dify 里不会执行。"""
        adjacency = {}
        for source, _st, target, _tt in self.built["edges"]:
            adjacency.setdefault(source, set()).add(target)

        seen = set()
        queue = ["start_node"]
        while queue:
            node = queue.pop()
            if node in seen:
                continue
            seen.add(node)
            queue.extend(adjacency.get(node, ()))

        self.assertEqual(seen, set(self.node_ids()))

    def test_graph_acyclic(self):
        indegree = {n: 0 for n in self.node_ids()}
        adjacency = {}
        for source, _st, target, _tt in self.built["edges"]:
            adjacency.setdefault(source, []).append(target)
            indegree[target] += 1

        queue = [n for n, d in indegree.items() if d == 0]
        ordered = 0
        while queue:
            node = queue.pop()
            ordered += 1
            for nxt in adjacency.get(node, ()):
                indegree[nxt] -= 1
                if indegree[nxt] == 0:
                    queue.append(nxt)

        self.assertEqual(ordered, len(indegree), "图里有环，Dify 跑不起来")

    def test_end_only_reachable_from_check(self):
        """结束节点只接审查结果 —— 别的节点接进来就成了两条并行的终点。"""
        into_end = {s for s, _st, t, _tt in self.built["edges"] if t == "end_node"}
        self.assertEqual(into_end, {"check_node"})

    def test_every_chapter_feeds_assembly(self):
        pairs = {(s, t) for s, _st, t, _tt in self.built["edges"]}
        for chapter in bw.CHAPTERS:
            self.assertIn(("llm_%s" % chapter["num"], "assemble_node"), pairs)


class TestReferences(unittest.TestCase):

    def setUp(self):
        self.built = bw.build()
        self.text = bw.emit(self.built)

    def test_all_prompt_refs_resolve(self):
        problems = bw.check_refs(self.text, self.built["outputs"])
        self.assertEqual(problems, [], "\n".join(problems))

    def test_all_selectors_resolve(self):
        problems = bw.check_selectors(self.built["selectors"], self.built["outputs"])
        self.assertEqual(problems, [], "\n".join(problems))

    def test_prompt_vars_exist_on_start_node(self):
        problems = bw.check_prompt_texts(self.text, {v[0] for v in bw.START_VARS})
        self.assertEqual(problems, [], "\n".join(problems))

    def test_checker_gets_full_material_not_group_view(self):
        """
        第 8 项要拿**全量**素材反查。

        接成分组视图的话，模型引用了别的章的素材就查不出来 ——
        而查不出来表现为「通过」，不是报错。
        """
        self.assertIn(("number_node", "kb_material"), self.built["selectors"])
        for group in ("tech", "ip", "finance"):
            self.assertNotIn(("number_node", "kb_material_%s" % group),
                             [(n, v) for n, v in self.built["selectors"]
                              if n == "check_node"])


class TestPrompts(unittest.TestCase):

    def setUp(self):
        self.text = bw.emit()
        self.system = bw.read_prompt("00_system.md").rstrip("\n")

    def test_system_prompt_inlined_verbatim(self):
        """提示词是唯一真相，yml 是产物 —— 内联时不许改写。"""
        marker = "**没有来源标记的数字，视同编造 —— 一个都不许写。**"
        self.assertEqual(self.text.count(marker), len(bw.CHAPTERS))

    def test_input_section_stripped(self):
        """`## 输入` 是给人看的变量来源表；模型看到的是注入的实际内容。"""
        self.assertNotIn("## 输入", self.text)

    def test_chapter_body_keeps_its_constraints(self):
        """摘「## 输入」时不能把后面的正文一起摘掉 —— 正则少个边界就会。"""
        for chapter in bw.CHAPTERS:
            body = bw.chapter_body("%s_%s.md" % (chapter["num"], chapter["title"]))
            self.assertNotIn("## 输入", body)
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
        """省掉要素表 = 章节间数字不一致，见 docs/WORKFLOW.md 3.3。"""
        for chapter in bw.CHAPTERS:
            self.assertIn("{{#elements_node.gen_elements#}}", bw.user_prompt(chapter))

    def test_material_prompt_states_numbering_rule(self):
        """素材注入了编号，必须同时说明「引用要带编号、编号要指对」。"""
        for chapter in bw.CHAPTERS:
            self.assertIn("（Sₙ）", bw.user_prompt(chapter))


class TestStartVars(unittest.TestCase):

    def test_material_fields_map_to_number_node(self):
        names = {v[0] for v in bw.START_VARS}
        for group, var in bw.MATERIAL_SOURCE.items():
            self.assertIn(var, names, group)

    def test_required_fields_present(self):
        named = {v[0] for v in bw.START_VARS}
        for expected in ("project_name", "tech_direction", "project_leader",
                         "team_size", "budget_range", "expected_outcome",
                         "project_highlights"):
            self.assertIn(expected, named)

    def test_serialized_form_has_all_vars(self):
        text = bw.emit()
        for var, _label, _type, _len, _req, _opts in bw.START_VARS:
            self.assertIn("variable: %s\n" % var, text)


class TestOutputFile(unittest.TestCase):

    def test_committed_file_matches_generator(self):
        """`dify/workflow_v0.8.yml` 必须是生成器当前输出，不许手改。"""
        with open(bw.OUT_PATH, encoding="utf-8") as fh:
            on_disk = fh.read()
        self.assertEqual(on_disk, bw.emit(),
                         "yml 与生成器不一致 —— 改了 yml 请改生成器后重跑，"
                         "或改了提示词/脚本后重跑生成器")


if __name__ == "__main__":
    unittest.main()
