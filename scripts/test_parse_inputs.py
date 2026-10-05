# -*- coding: utf-8 -*-
"""
parse_inputs 单测 —— 全程 mock run_pipeline.chat，不碰网络。

卡住的核心是「不编造」：只抽文本明说的，没提就空；解析失败不抛异常，
返回全空 + error —— 识别只是辅助，识别失败表单照填。
"""

import json
import sys
import unittest
from unittest import mock

HERE = __file__.rsplit("/", 1)[0]
sys.path.insert(0, HERE)

import parse_inputs  # noqa: E402

FIELDS = {v: "" for v, _l, _t in parse_inputs.FIELDS}


class TestExtractJson(unittest.TestCase):
    def test_clean_json(self):
        self.assertEqual(parse_inputs._extract_json('{"a": 1}'), {"a": 1})

    def test_fenced_json(self):
        self.assertEqual(parse_inputs._extract_json("```json\n{\"a\": 1}\n```"),
                         {"a": 1})

    def test_json_inside_prose(self):
        self.assertEqual(
            parse_inputs._extract_json("识别结果如下：{\"a\": 1}。以上。"),
            {"a": 1})

    def test_no_json_raises(self):
        with self.assertRaises(ValueError):
            parse_inputs._extract_json("模型没给 JSON，只有一句话")


class TestNormalize(unittest.TestCase):
    def test_team_size_from_text(self):
        fields = parse_inputs._normalize({"team_size": "12 人"})
        self.assertEqual(fields["team_size"], 12)

    def test_team_size_float(self):
        fields = parse_inputs._normalize({"team_size": 8.0})
        self.assertEqual(fields["team_size"], 8)

    def test_team_size_out_of_range_zero(self):
        self.assertEqual(parse_inputs._normalize({"team_size": "999999"})["team_size"], 0)

    def test_team_size_garbage_empty(self):
        self.assertEqual(parse_inputs._normalize({"team_size": "不少人"})["team_size"], "")

    def test_declaration_type_exact(self):
        self.assertEqual(
            parse_inputs._normalize({"declaration_type": "专精特新"})["declaration_type"],
            "专精特新")

    def test_declaration_type_fuzzy_gaoqi(self):
        self.assertEqual(
            parse_inputs._normalize({"declaration_type": "高企"})["declaration_type"],
            "高新技术企业")

    def test_declaration_type_fuzzy_contains(self):
        self.assertEqual(
            parse_inputs._normalize({"declaration_type": "国家高新技术企业（高企）"})["declaration_type"],
            "高新技术企业")

    def test_declaration_type_unmatched_other(self):
        self.assertEqual(
            parse_inputs._normalize({"declaration_type": "瞪羚企业"})["declaration_type"],
            "其他")

    def test_missing_keys_stay_empty(self):
        fields = parse_inputs._normalize({"project_name": "x"})
        self.assertEqual(fields["project_name"], "x")
        self.assertEqual(fields["team_size"], "")
        self.assertEqual(fields["budget_range"], "")

    def test_none_values_stay_empty(self):
        fields = parse_inputs._normalize({"project_name": None})
        self.assertEqual(fields["project_name"], "")


def fake_chat_returning(text):
    def fake(base_url, model, system, user, temperature, num_ctx, timeout):
        return text
    return fake


class TestParseInputs(unittest.TestCase):
    def test_empty_text(self):
        result = parse_inputs.parse_inputs("   ")
        self.assertEqual(result["error"], "empty")
        self.assertEqual(len(result["missing"]), len(parse_inputs.FIELDS))
        self.assertEqual(result["fields"], FIELDS)

    def test_success(self):
        reply = json.dumps({
            "project_name": "边缘计算项目",
            "declaration_type": "科技型中小企业",
            "team_size": 12,
            "budget_range": "180 万",
        }, ensure_ascii=False)
        with mock.patch("parse_inputs.run_pipeline.chat",
                        fake_chat_returning(reply)):
            result = parse_inputs.parse_inputs("我们做边缘计算项目，12 人")
        self.assertNotIn("error", result)
        self.assertEqual(result["fields"]["project_name"], "边缘计算项目")
        self.assertEqual(result["fields"]["team_size"], 12)
        self.assertIn("预期成果", result["missing"])
        self.assertNotIn("项目名称", result["missing"])

    def test_model_returns_prose_wrapped_json(self):
        reply = "好的，以下是抽取结果：\n```json\n{\"project_name\": \"x\", \"team_size\": \"5人\"}\n```"
        with mock.patch("parse_inputs.run_pipeline.chat",
                        fake_chat_returning(reply)):
            result = parse_inputs.parse_inputs("项目 x，5 人")
        self.assertEqual(result["fields"]["project_name"], "x")
        self.assertEqual(result["fields"]["team_size"], 5)

    def test_chat_failure_returns_empty_not_raise(self):
        def boom(*a, **kw):
            raise RuntimeError("ollama down")
        with mock.patch("parse_inputs.run_pipeline.chat", boom):
            result = parse_inputs.parse_inputs("任意文本")
        self.assertEqual(result["error"], "parse_failed")
        self.assertIn("RuntimeError", result["parse_error"])
        self.assertEqual(result["fields"], FIELDS)
        self.assertEqual(len(result["missing"]), len(parse_inputs.FIELDS))

    def test_garbage_model_output(self):
        with mock.patch("parse_inputs.run_pipeline.chat",
                        fake_chat_returning("我不知道，无法回答")):
            result = parse_inputs.parse_inputs("任意文本")
        self.assertEqual(result["error"], "parse_failed")
        self.assertIn("ValueError", result["parse_error"])

    def test_declaration_type_not_mentioned_stays_empty(self):
        """不编造：没提申报类型就不能替用户猜一个。"""
        reply = json.dumps({"project_name": "x"}, ensure_ascii=False)
        with mock.patch("parse_inputs.run_pipeline.chat",
                        fake_chat_returning(reply)):
            result = parse_inputs.parse_inputs("有个项目叫 x")
        self.assertEqual(result["fields"]["declaration_type"], "")
        self.assertIn("申报类型", result["missing"])


if __name__ == "__main__":
    unittest.main()
