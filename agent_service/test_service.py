# -*- coding: utf-8 -*-
"""
agent_service 单测 —— 假引擎（不真调 Ollama）+ 临时 RUNS_DIR。

注意导入顺序：RUNS_DIR 环境变量必须在 `import main` 之前设好，
main 在导入时读它并扫描恢复。真引擎被替换成假引擎，测试不碰网络。
"""

import json
import os
import sys
import tempfile
import time
import unittest

TMP = tempfile.mkdtemp(prefix="agent_service_test_")
os.environ["RUNS_DIR"] = TMP
os.environ.pop("AGENT_SERVICE_TOKEN", None)
# 防开发者本机 shell 带 EMAIL_* 变量污染测试（邮件接单默认必须关闭）
for _k in [k for k in os.environ if k.startswith("EMAIL_")]:
    os.environ.pop(_k)

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import main  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(main.app)

SAMPLE = {
    "project_name": "测试项目",
    "declaration_type": "科技型中小企业",
    "tech_direction": "边缘计算",
    "project_leader": "张三",
    "team_size": 12,
    "budget_range": "180万-220万",
    "expected_outcome": "形成软件 1 套。",
    "project_highlights": "模型体积小于 50MB。",
    "special_requirements": "",
    "material_tech": "检测精度 95.2%\n",
    "material_ip": "发明专利 1 项\n",
    "material_finance": "合计 200 万\n",
    "style_input": "",
}

SEEN_INPUTS = []


def fake_engine(inputs, model=None, base_url=None, on_progress=None, **kwargs):
    SEEN_INPUTS.append(dict(inputs))
    for step in ["素材编号", "要素抽取",
                 "02 项目背景", "03 技术方案", "04 实施计划", "05 团队基础",
                 "06 预期成果", "07 经费预算", "08 风险应对",
                 "章节拼接", "一致性审查"]:
        on_progress(step, "start", "")
        on_progress(step, "done", "ok")
    return {
        "document": "# %s\n\n## 一、项目背景与意义\n\n正文。\n" % inputs["project_name"],
        "pass": True,
        "issues": [{"type": "tbd", "section": "-", "detail": "x",
                    "severity": "warn"}],
        "check_stats": {"blocking": 0, "warnings": 1, "tbd_count": 1,
                        "total_issues": 1, "truncated": 0, "word_count": {}},
        "gen_elements": {"project_name": inputs["project_name"]},
        "kb_index": [{"id": "S1", "group": "tech", "content": "检测精度 95.2%"}],
        "number_stats": {}, "elements_stats": {},
        "assemble_issues": [], "assemble_stats": {},
        "sections": {}, "section_errors": {},
        "model": "fake", "base_url": "fake",
    }


def failing_engine(inputs, **kwargs):
    raise RuntimeError("fake boom")


def fake_parser(text):
    """假解析器：认「boom」文本当失败，其余返回固定识别结果。"""
    if text == "boom":
        return {"fields": {}, "missing": [], "raw": text,
                "error": "parse_failed", "parse_error": "RuntimeError: boom"}
    return {"fields": {"project_name": "识别项目", "team_size": 12},
            "missing": ["预期成果"], "raw": text}


def wait_done(run_id, timeout=5.0, headers=None):
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = client.get("/api/runs/" + run_id, headers=headers).json()
        if state.get("state") in ("done", "failed", "interrupted"):
            return state
        time.sleep(0.02)
    raise AssertionError("任务超时未完成：" + str(state))


class TestService(unittest.TestCase):
    def setUp(self):
        SEEN_INPUTS.clear()
        main.ENGINE = fake_engine
        main.PARSER = fake_parser

    def test_form_fields(self):
        data = client.get("/api/form").json()
        self.assertEqual(len(data["fields"]), 13)
        self.assertEqual(data["fields"][0]["var"], "project_name")
        self.assertEqual(data["fields"][1]["options"],
                         ["科技型中小企业", "高新技术企业", "专精特新", "其他"])
        self.assertEqual([f["var"] for f in data["fields"]][-1], "style_input")

    def test_submit_to_done(self):
        resp = client.post("/api/runs", json=SAMPLE)
        self.assertEqual(resp.status_code, 200)
        run_id = resp.json()["run_id"]
        state = wait_done(run_id)
        self.assertEqual(state["state"], "done")
        self.assertTrue(state["pass"])
        self.assertEqual(state["blocking"], 0)
        self.assertTrue(os.path.exists(
            os.path.join(TMP, run_id, "document.md")))

    def test_engine_receives_all_inputs(self):
        run_id = client.post("/api/runs", json=SAMPLE).json()["run_id"]
        wait_done(run_id)
        self.assertEqual(len(SEEN_INPUTS), 1)
        self.assertEqual(SEEN_INPUTS[0]["project_name"], "测试项目")
        self.assertEqual(SEEN_INPUTS[0]["team_size"], 12)
        self.assertEqual(set(SEEN_INPUTS[0]), set(SAMPLE))

    def test_document_endpoint(self):
        run_id = client.post("/api/runs", json=SAMPLE).json()["run_id"]
        wait_done(run_id)
        resp = client.get("/api/runs/%s/document" % run_id)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("测试项目", resp.text)

    def test_report_endpoint(self):
        run_id = client.post("/api/runs", json=SAMPLE).json()["run_id"]
        wait_done(run_id)
        report = client.get("/api/runs/%s/report" % run_id).json()
        self.assertTrue(report["pass"])
        self.assertEqual(report["stats"]["blocking"], 0)

    def test_docx_download(self):
        run_id = client.post("/api/runs", json=SAMPLE).json()["run_id"]
        wait_done(run_id)
        resp = client.get("/api/runs/%s/docx" % run_id)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.content[:2], b"PK")
        self.assertIn("attachment", resp.headers["content-disposition"])

    def test_progress_recorded(self):
        run_id = client.post("/api/runs", json=SAMPLE).json()["run_id"]
        state = wait_done(run_id)
        self.assertEqual(len(state["progress"]), 22)
        self.assertEqual(state["progress"][0]["step"], "素材编号")
        self.assertEqual(state["progress"][-1]["step"], "一致性审查")
        self.assertEqual(state["progress"][-1]["state"], "done")

    def test_list_runs(self):
        run_id = client.post("/api/runs", json=SAMPLE).json()["run_id"]
        wait_done(run_id)
        runs = client.get("/api/runs").json()["runs"]
        self.assertIn(run_id, [r["run_id"] for r in runs])
        self.assertEqual(runs[0]["project_name"], "测试项目")

    def test_unknown_run_404(self):
        self.assertEqual(client.get("/api/runs/nope").status_code, 404)
        self.assertEqual(client.get("/api/runs/nope/document").status_code, 404)
        self.assertEqual(client.get("/api/runs/nope/docx").status_code, 404)

    def test_validation_missing_required(self):
        payload = dict(SAMPLE)
        del payload["project_name"]
        resp = client.post("/api/runs", json=payload)
        self.assertEqual(resp.status_code, 422)

    def test_validation_bad_team_size(self):
        payload = dict(SAMPLE)
        payload["team_size"] = "十二"
        resp = client.post("/api/runs", json=payload)
        self.assertEqual(resp.status_code, 422)

    def test_failed_run(self):
        main.ENGINE = failing_engine
        run_id = client.post("/api/runs", json=SAMPLE).json()["run_id"]
        state = wait_done(run_id)
        self.assertEqual(state["state"], "failed")
        self.assertIn("fake boom", state["error"])

    def test_token_required_when_set(self):
        os.environ["AGENT_SERVICE_TOKEN"] = "secret-123"
        try:
            self.assertEqual(client.post("/api/runs", json=SAMPLE).status_code, 401)
            ok = client.post("/api/runs", json=SAMPLE,
                             headers={"X-Agent-Token": "secret-123"})
            self.assertEqual(ok.status_code, 200)
            wait_done(ok.json()["run_id"],
                      headers={"X-Agent-Token": "secret-123"})
        finally:
            os.environ.pop("AGENT_SERVICE_TOKEN", None)

    def test_convert_binary(self):
        resp = client.post("/api/convert", content="# 测试申报书\n\n正文。\n",
                           headers={"content-type": "text/plain"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.content[:2], b"PK")
        self.assertIn("attachment", resp.headers["content-disposition"])
        self.assertIn("application/vnd.openxmlformats", resp.headers["content-type"])

    def test_convert_url_mode(self):
        resp = client.post("/api/convert?mode=url", content="# 测试申报书\n",
                           headers={"content-type": "text/plain"})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("url", data)
        dl = client.get(data["url"])
        self.assertEqual(dl.status_code, 200)
        self.assertEqual(dl.content[:2], b"PK")

    def test_convert_download_bad_id_404(self):
        self.assertEqual(client.get("/api/convert/zzz/download").status_code, 404)

    def test_convert_requires_token_when_set(self):
        os.environ["AGENT_SERVICE_TOKEN"] = "secret-123"
        try:
            resp = client.post("/api/convert", content="# x\n",
                               headers={"content-type": "text/plain"})
            self.assertEqual(resp.status_code, 401)
            ok = client.post("/api/convert", content="# x\n",
                             headers={"content-type": "text/plain",
                                      "X-Agent-Token": "secret-123"})
            self.assertEqual(ok.status_code, 200)
        finally:
            os.environ.pop("AGENT_SERVICE_TOKEN", None)

    def test_recover_interrupted(self):
        run_id = "run_20990101-000000"
        os.makedirs(os.path.join(TMP, run_id), exist_ok=True)
        with open(os.path.join(TMP, run_id, "state.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"id": run_id, "state": "running",
                       "project_name": "旧任务"}, fh, ensure_ascii=False)
        self.assertEqual(main.recover_interrupted(), 1)
        with open(os.path.join(TMP, run_id, "state.json"), encoding="utf-8") as fh:
            state = json.load(fh)
        self.assertEqual(state["state"], "interrupted")

    def test_pages_served(self):
        self.assertEqual(client.get("/").status_code, 200)
        self.assertIn("申报", client.get("/").text)
        self.assertEqual(client.get("/runs/whatever").status_code, 200)
        self.assertEqual(client.get("/runs/whatever/result").status_code, 200)

    def test_parse_endpoint(self):
        resp = client.post("/api/parse", json={"text": "我们做识别项目，12 人"})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["fields"]["project_name"], "识别项目")
        self.assertIn("预期成果", data["missing"])

    def test_parse_empty_text_422(self):
        self.assertEqual(client.post("/api/parse", json={"text": ""}).status_code, 422)
        self.assertEqual(client.post("/api/parse", json={}).status_code, 422)

    def test_parse_failure_is_200_with_error(self):
        """识别失败不 500 —— 表单照填，识别只是辅助。"""
        resp = client.post("/api/parse", json={"text": "boom"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["error"], "parse_failed")

    def test_parse_requires_token_when_set(self):
        os.environ["AGENT_SERVICE_TOKEN"] = "secret-123"
        try:
            self.assertEqual(client.post("/api/parse", json={"text": "x"}).status_code, 401)
            ok = client.post("/api/parse", json={"text": "x"},
                             headers={"X-Agent-Token": "secret-123"})
            self.assertEqual(ok.status_code, 200)
        finally:
            os.environ.pop("AGENT_SERVICE_TOKEN", None)

    def test_docx_before_done_404(self):
        """任务没跑完时没有产出文件 —— 404 而不是半截文档。"""
        gate = __import__("threading").Event()

        def blocked_engine(inputs, on_progress=None, **kwargs):
            gate.wait(timeout=5)
            return fake_engine(inputs, on_progress=on_progress)

        main.ENGINE = blocked_engine
        run_id = client.post("/api/runs", json=SAMPLE).json()["run_id"]
        deadline = time.time() + 3
        while time.time() < deadline:
            if client.get("/api/runs/" + run_id).json()["state"] == "running":
                break
            time.sleep(0.02)
        self.assertEqual(client.get("/api/runs/%s/docx" % run_id).status_code, 404)
        gate.set()
        self.assertEqual(wait_done(run_id)["state"], "done")

    def test_email_intake_disabled_by_default(self):
        """EMAIL_ENABLED 没设时，邮件接单不开线程、不留 journal。"""
        import email_intake
        self.assertIsNone(email_intake._THREAD)
        self.assertFalse(os.path.exists(
            os.path.join(TMP, email_intake.JOURNAL_NAME)))


if __name__ == "__main__":
    unittest.main()
