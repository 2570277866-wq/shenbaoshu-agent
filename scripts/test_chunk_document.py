# -*- coding: utf-8 -*-
"""分块脚本测试。核心两条：不硬切、不切碎结构块。"""

import unittest

from chunk_document import main

DOC = """# 智能检测系统申报书

> 本稿件由 AI 生成初稿。

## 一、项目背景与意义

背景第一段。

背景第二段。

## 二、技术方案

### 2.1 技术路线

路线第一段。

### 2.2 关键技术指标

| 指标 | 目标值 |
|---|---|
| 检测精度 | 99.2% |
| 响应时间 | 200ms |

## 三、实施计划

计划正文。
"""


def para(n, size=400):
    return "第%d段。" % n + "内容" * size


class TestChunkDocument(unittest.TestCase):

    def test_short_doc_chapter_per_chunk(self):
        r = main({"document": DOC, "max_chars": 4000})
        chapters = [c["chapter"] for c in r["chunks"]]
        self.assertIn("项目背景与意义", chapters)
        self.assertIn("技术方案", chapters)
        self.assertIn("实施计划", chapters)

    def test_no_chunk_exceeds_budget(self):
        doc = "# 标题\n\n" + "\n\n".join("## 第%d章\n\n%s" % (i, para(i, 100)) for i in range(6))
        r = main({"document": doc, "max_chars": 600})
        for c in r["chunks"]:
            self.assertLessEqual(c["chars"], 600 + 100)

    def test_chapter_never_split_across_chunks(self):
        """切出来的是完整章节 —— 每块只属于一个章节。"""
        doc = "\n\n".join("## 第%d章\n\n%s" % (i, para(i, 50)) for i in range(8))
        r = main({"document": doc, "max_chars": 500})
        for c in r["chunks"]:
            self.assertEqual(c["content"].count("## "), 1)

    def test_table_not_split(self):
        doc = "## 二、技术方案\n\n" + "\n".join(
            "| 指标%d | 值 |" % i for i in range(40))
        r = main({"document": doc, "max_chars": 200})
        table_chunks = [c for c in r["chunks"] if "| 指标0 " in c["content"]]
        self.assertEqual(len(table_chunks), 1)
        self.assertIn("| 指标39 |", table_chunks[0]["content"])
        self.assertTrue(any(i["type"] == "oversized_block" for i in r["issues"]))

    def test_code_block_not_split(self):
        code = "```python\n" + "\n".join("x%d = %d" % (i, i) for i in range(30)) + "\n```"
        r = main({"document": "## 章\n\n" + code, "max_chars": 100})
        blocks = [c for c in r["chunks"] if "```" in c["content"]]
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["content"].count("```"), 2)

    def test_long_chapter_reports_split(self):
        doc = "## 技术方案\n\n" + "\n\n".join(para(i, 200) for i in range(10))
        r = main({"document": doc, "max_chars": 800})
        self.assertGreater(len(r["chunks"]), 1)
        self.assertTrue(any(i["type"] == "chapter_split" for i in r["issues"]))

    def test_chapter_heading_kept_in_content(self):
        r = main({"document": DOC, "max_chars": 4000})
        tech = [c for c in r["chunks"] if c["chapter"] == "技术方案"][0]
        self.assertIn("## 二、技术方案", tech["content"])

    def test_chapter_name_normalized(self):
        """章节名去掉「一、」序号，与一致性审查的 section 对齐。"""
        r = main({"document": DOC, "max_chars": 4000})
        self.assertNotIn("一、项目背景与意义", [c["chapter"] for c in r["chunks"]])
        self.assertIn("项目背景与意义", [c["chapter"] for c in r["chunks"]])

    def test_empty_document(self):
        r = main({"document": ""})
        self.assertEqual(r["chunks"], [])
        self.assertEqual(r["issues"][0]["type"], "empty_document")

    def test_indices_sequential(self):
        doc = "\n\n".join("## 第%d章\n\n正文" % i for i in range(5))
        r = main({"document": doc, "max_chars": 10})
        self.assertEqual([c["index"] for c in r["chunks"]], list(range(len(r["chunks"]))))

    def test_kwargs_call_style(self):
        r = main(document=DOC, max_chars=4000)
        self.assertTrue(r["chunks"])


if __name__ == "__main__":
    unittest.main()
