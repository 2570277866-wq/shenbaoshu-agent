# -*- coding: utf-8 -*-
"""
run_pipeline 单测 —— 用假 Ollama HTTP 服务器（threading http.server）打全链路。

假服务器每章返回固定正文：`第N章正文。检测精度 95.2%（S1）。<think>…</think>`
—— 数字只出现 95.2 且挂（S1），S1 素材里确有 95.2，十项审查应 pass（warn 不计）。
`<think>` 块用来验 assemble 剥离。章序号用汉字，避免被审查当裸奔数字抓。
"""

import http.server
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import run_pipeline  # noqa: E402

SAMPLE_INPUTS = {
    "project_name": "测试项目",
    "declaration_type": "科技型中小企业",
    "tech_direction": "边缘计算",
    "project_leader": "张三",
    "team_size": 12,
    "budget_range": "180万-220万",
    "expected_outcome": "形成软件 1 套，检测精度不低于 95.2%",
    "project_highlights": "模型体积小于 50MB",
    "special_requirements": "",
    "material_tech": "检测精度 95.2%（自建测试集 12000 条）\n研发人员 12 人，其中高级工程师 3 人\n",
    "material_ip": "发明专利「一种方法」已受理，受理号 CN202610123456.7\n",
    "material_finance": "2025 年营业收入 486.5 万元\n项目预算：合计 200 万\n",
    "style_input": "风格样例：本项目采用先进技术。",
}

CHAPTER_TITLES = [
    "项目背景", "技术方案", "实施计划", "团队基础", "预期成果", "经费预算", "风险应对",
]
CN_NUM = "一二三四五六七"


class FakeOllamaHandler(http.server.BaseHTTPRequestHandler):
    calls = []
    fail_index = None  # 第 N 次调用（0 起）返回 500
    content_tpl = "第%s章正文。检测精度 95.2%%（S1）。\n<think>推理过程，不应进入正文</think>"

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        FakeOllamaHandler.calls.append({
            "path": self.path,
            "body": body,
            "start": time.monotonic(),
        })
        index = len(FakeOllamaHandler.calls) - 1

        if FakeOllamaHandler.fail_index is not None and index == FakeOllamaHandler.fail_index:
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error": "fake failure"}')
            FakeOllamaHandler.calls[-1]["end"] = time.monotonic()
            return

        content = FakeOllamaHandler.content_tpl % CN_NUM[index % len(CN_NUM)]
        payload = json.dumps({
            "model": body.get("model", ""),
            "message": {"role": "assistant", "content": content},
            "done": True,
        }).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
        FakeOllamaHandler.calls[-1]["end"] = time.monotonic()

    def log_message(self, *args):  # 安静
        pass


class FakeOllama:
    def __init__(self):
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeOllamaHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = "http://127.0.0.1:%d" % self.server.server_address[1]

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


class BaseTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ollama = FakeOllama()

    @classmethod
    def tearDownClass(cls):
        cls.ollama.stop()

    def setUp(self):
        FakeOllamaHandler.calls = []
        FakeOllamaHandler.fail_index = None

    def run_chain(self, inputs=None, **kwargs):
        return run_pipeline.run_workflow(
            inputs or dict(SAMPLE_INPUTS), base_url=self.ollama.base_url, **kwargs)

    def user_prompts(self):
        return [c["body"]["messages"][1]["content"] for c in FakeOllamaHandler.calls]


class TestFullChain(BaseTestCase):
    def test_happy_path_structure(self):
        result = self.run_chain()
        self.assertTrue(result["pass"])
        self.assertIn("document", result)
        self.assertIn("## 一、项目背景与意义", result["document"])
        self.assertIn("## 七、风险与应对", result["document"])
        self.assertEqual(set(result["sections"]),
                         {"gen_section_background", "gen_section_tech",
                          "gen_section_schedule", "gen_section_team",
                          "gen_section_outcome", "gen_section_budget",
                          "gen_section_risk"})
        self.assertEqual(result["check_stats"]["total_issues"],
                         len(result["issues"]))
        self.assertEqual(len(result["kb_index"]), 5)  # 2 技 + 1 知产 + 2 财务

    def test_seven_calls_serial(self):
        self.run_chain()
        self.assertEqual(len(FakeOllamaHandler.calls), 7)
        for prev, cur in zip(FakeOllamaHandler.calls, FakeOllamaHandler.calls[1:]):
            # 客户端串行：下一次请求在上一次响应之后才发出
            self.assertGreaterEqual(cur["start"], prev["end"] - 1e-6)

    def test_sections_in_order(self):
        result = self.run_chain()
        for i, num in enumerate(CN_NUM):
            text = result["sections"]["gen_section_" +
                                     ["background", "tech", "schedule", "team",
                                      "outcome", "budget", "risk"][i]]
            self.assertIn("第%s章正文" % num, text)
        # 拼接后顺序与章节清单一致
        positions = [result["document"].index("第%s章正文" % n) for n in CN_NUM]
        self.assertEqual(positions, sorted(positions))

    def test_think_stripped(self):
        result = self.run_chain()
        self.assertNotIn("<think>", result["document"])
        self.assertEqual(result["assemble_stats"]["think_stripped"], 7)

    def test_material_injected_into_user_prompt(self):
        self.run_chain()
        prompts = self.user_prompts()
        # 02 项目背景吃全部分组 → 有 S1；07 经费预算只吃 finance 组 → 无 S1 有 S4
        self.assertIn("S1：检测精度 95.2%", prompts[0])
        self.assertNotIn("S1：检测精度", prompts[5])
        self.assertIn("S4：2025 年营业收入", prompts[5])

    def test_gen_elements_injected_as_json(self):
        self.run_chain()
        for user in self.user_prompts():
            self.assertIn('"project_name": "测试项目"', user)
            self.assertIn('"team_size": 12', user)

    def test_style_only_for_style_chapters(self):
        self.run_chain()
        prompts = self.user_prompts()
        self.assertIn("风格参考", prompts[0])   # 02 项目背景 style=True
        self.assertNotIn("风格参考", prompts[2])  # 04 实施计划 style=False

    def test_options_and_model(self):
        self.run_chain()
        for call in FakeOllamaHandler.calls:
            self.assertEqual(call["body"]["options"],
                             {"temperature": 0.3, "num_ctx": 8192})
            self.assertEqual(call["body"]["model"], "qwen3:14b")
            self.assertFalse(call["body"]["stream"])
            self.assertEqual(call["path"], "/api/chat")

    def test_model_override(self):
        self.run_chain(model="qwen3:32b")
        for call in FakeOllamaHandler.calls:
            self.assertEqual(call["body"]["model"], "qwen3:32b")

    def test_progress_events(self):
        events = []
        result = self.run_chain(on_progress=lambda s, st, d: events.append((s, st)))
        self.assertEqual(len(events), 2 * (2 + 7 + 2))
        self.assertEqual(events[0], ("素材编号", "start"))
        self.assertEqual(events[1], ("素材编号", "done"))
        self.assertEqual(events[18], ("章节拼接", "start"))
        self.assertEqual(events[-1], ("一致性审查", "done"))


class TestDegrade(BaseTestCase):
    def test_chapter_failure_placeholder(self):
        FakeOllamaHandler.fail_index = 1  # 03 技术方案（第 2 次调用）
        result = self.run_chain()
        self.assertIn("【本章生成失败，需人工撰写】", result["document"])
        self.assertEqual(list(result["section_errors"]), ["03 技术方案"])
        self.assertEqual(result["sections"]["gen_section_tech"], "")
        # 后续章节照常生成
        self.assertIn("第%s章正文" % CN_NUM[6], result["document"])

    def test_missing_optional_fields_ok(self):
        inputs = dict(SAMPLE_INPUTS)
        del inputs["special_requirements"]
        del inputs["style_input"]
        result = self.run_chain(inputs=inputs)
        self.assertTrue(result["pass"])

    def test_empty_materials_still_runs(self):
        inputs = dict(SAMPLE_INPUTS)
        inputs["material_tech"] = ""
        inputs["material_ip"] = ""
        inputs["material_finance"] = ""
        result = self.run_chain(inputs=inputs)
        self.assertIn("项目周期", result["gen_elements"]["missing"])
        self.assertIsNone(result["gen_elements"]["duration_months"])
        self.assertEqual(result["kb_index"], [])
        self.assertIsInstance(result["pass"], bool)


class TestRenderPrompt(unittest.TestCase):
    def test_unknown_placeholder_raises(self):
        with self.assertRaises(ValueError):
            run_pipeline._render_prompt(
                "{{#nope.var#}}", {}, {}, {})


class TestCLI(BaseTestCase):
    def test_cli_archives_five_files(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                         encoding="utf-8") as fh:
            json.dump(SAMPLE_INPUTS, fh)
            path = fh.name
        self.addCleanup(os.unlink, path)

        code = run_pipeline.main([path, "--base-url", self.ollama.base_url,
                                  "--out-dir", tmp])
        self.assertEqual(code, 0)
        files = sorted(os.listdir(tmp))
        self.assertEqual(files, ["check_report.json", "document.md",
                                 "gen_elements.json", "inputs.json", "kb_index.json"])
        with open(os.path.join(tmp, "kb_index.json"), encoding="utf-8") as fh:
            index = json.load(fh)
        self.assertEqual([item["id"] for item in index],
                         ["S1", "S2", "S3", "S4", "S5"])
        self.assertNotIn("<think>", open(os.path.join(tmp, "document.md"),
                                         encoding="utf-8").read())

    def test_cli_stdin_input(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        old_stdin = sys.stdin
        sys.stdin = io.StringIO(json.dumps(SAMPLE_INPUTS))
        try:
            code = run_pipeline.main(["--base-url", self.ollama.base_url,
                                      "--out-dir", tmp])
        finally:
            sys.stdin = old_stdin
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(os.path.join(tmp, "document.md")))


if __name__ == "__main__":
    unittest.main()
