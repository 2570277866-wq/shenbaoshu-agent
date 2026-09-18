# -*- coding: utf-8 -*-
"""
记忆服务单测。跑法：

    cd memory_service
    python3 -m unittest test_service -v

每个测试用独立的临时目录，复制一份真实的 memory/*.md 进去 ——
**不碰仓库里的真文件**。这一点比测试本身重要：断言写错最多红一次，
测试直接改真记忆文件是会静默污染 Agent 记忆的。
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient  # noqa: E402

import main as service  # noqa: E402
import store  # noqa: E402

REPO_MEMORY = Path(__file__).resolve().parent.parent / "memory"

#: 导入时（任何测试跑之前）给仓库里的真记忆文件拍个快照。
#: 全部测试跑完后比对 —— 万一 setUp 里 MEMORY_DIR 没生效，测试会直接改真文件，
#: 那种污染不会有任何报错，只能靠这里兜。
_REPO_SNAPSHOT = {}
for _name in ("CONTEXT.md", "AGENTS.md", "decisions.md", "lessons.md"):
    _p = REPO_MEMORY / _name
    _REPO_SNAPSHOT[_name] = _p.read_bytes() if _p.is_file() else None



class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="memtest-"))
        os.environ["MEMORY_DIR"] = str(self.tmp)
        os.environ.pop("MEMORY_SERVICE_TOKEN", None)
        for name in store.ALLOWED_FILES:
            src = REPO_MEMORY / name
            if src.is_file():
                shutil.copy2(str(src), str(self.tmp / name))
        self.client = TestClient(service.app)

    def tearDown(self):
        os.environ.pop("MEMORY_DIR", None)
        os.environ.pop("MEMORY_SERVICE_TOKEN", None)
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    # -- 助手 --

    def target(self, name):
        return self.tmp / name

    def text(self, name):
        return self.target(name).read_text(encoding="utf-8")

    def post(self, path, **body):
        return self.client.post(path, json=body)

    def assert_error(self, resp, status, code):
        self.assertEqual(resp.status_code, status, resp.text)
        payload = resp.json()
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], code, resp.text)
        return payload


# ---------------------------------------------------------------- list


class TestList(Base):
    def test_lists_four_files(self):
        data = self.client.get("/memory/list").json()
        self.assertEqual([f["file"] for f in data["files"]], list(store.ALLOWED_FILES))
        self.assertTrue(all(f["exists"] for f in data["files"]))

    def test_missing_file_marked_not_exists(self):
        self.target("lessons.md").unlink()
        data = self.client.get("/memory/list").json()
        missing = [f for f in data["files"] if not f["exists"]]
        self.assertEqual([f["file"] for f in missing], ["lessons.md"])

    def test_healthz(self):
        data = self.client.get("/healthz").json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["dir"], str(self.tmp))


# ---------------------------------------------------------------- read


class TestRead(Base):
    def test_read_ok(self):
        data = self.post("/memory/read", file="decisions.md").json()
        self.assertTrue(data["ok"])
        self.assertIn("决策记录", data["content"])
        self.assertGreater(data["bytes"], 0)
        self.assertGreater(data["lines"], 1)

    def test_read_empty_string_file_param(self):
        self.assert_error(self.post("/memory/read", file=""), 400, "unknown_file")

    def test_unknown_file_rejected(self):
        self.assert_error(self.post("/memory/read", file="secrets.md"), 400, "unknown_file")

    def test_path_traversal_rejected(self):
        """路径穿越是这服务最该防的一件事 —— 它按文件名写文件。"""
        for bad in ("../CONTEXT.md", "../../etc/passwd", "/etc/passwd",
                    "sub/decisions.md", "decisions.md/../../x.md",
                    ".backups/decisions.md.20260101-000000-abcd.bak",
                    ".audit.jsonl", "decisions.md ", " decisions.md", "DECISIONS.MD"):
            with self.subTest(name=bad):
                self.assert_error(self.post("/memory/read", file=bad), 400, "unknown_file")

    def test_whitelisted_but_absent(self):
        self.target("AGENTS.md").unlink()
        self.assert_error(self.post("/memory/read", file="AGENTS.md"), 404, "file_not_found")


# ---------------------------------------------------------------- write


class TestWrite(Base):
    def test_overwrite(self):
        data = self.post("/memory/write", file="lessons.md", content="# 新内容\n").json()
        self.assertTrue(data["ok"])
        self.assertEqual(self.text("lessons.md"), "# 新内容\n")
        self.assertEqual(data["bytes_after"], len("# 新内容\n".encode("utf-8")))

    def test_overwrite_backs_up_old_version(self):
        before = self.text("lessons.md")
        data = self.post("/memory/write", file="lessons.md", content="换掉了\n").json()
        backup = data["backup"]
        self.assertTrue(backup)
        self.assertEqual((self.tmp / backup).read_text(encoding="utf-8"), before)

    def test_append_keeps_existing(self):
        before = self.text("lessons.md")
        self.post("/memory/write", file="lessons.md", content="追加行", mode="append")
        after = self.text("lessons.md")
        self.assertTrue(after.startswith(before.rstrip("\n")))
        self.assertIn("追加行", after)

    def test_dry_run_does_not_touch_file(self):
        before = self.text("lessons.md")
        data = self.post("/memory/write", file="lessons.md", content="不该落盘",
                         dry_run=True).json()
        self.assertTrue(data["dry_run"])
        self.assertEqual(self.text("lessons.md"), before)
        self.assertNotIn("不该落盘", self.text("lessons.md"))
        # dry_run 不该产生备份，也不该留审计
        self.assertEqual(list((self.tmp / store.BACKUP_DIRNAME).glob("*.bak"))
                         if (self.tmp / store.BACKUP_DIRNAME).is_dir() else [], [])

    def test_bad_mode_rejected(self):
        self.assert_error(self.post("/memory/write", file="lessons.md",
                                    content="x", mode="upsert"), 400, "bad_request")

    def test_oversized_content_rejected(self):
        self.assert_error(self.post("/memory/write", file="lessons.md",
                                    content="x" * (store.MAX_CONTENT_BYTES + 1)),
                          413, "content_too_large")

    def test_no_temp_files_left_behind(self):
        self.post("/memory/write", file="lessons.md", content="写一下\n")
        self.assertEqual(list(self.tmp.glob("*.tmp.*")), [])


# ---------------------------------------------------------------- replace


class TestReplace(Base):
    def test_replace_ok(self):
        data = self.post("/memory/replace", file="decisions.md",
                         old="## 决策模板", new="## 模板（改过）").json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["replaced"], 1)
        self.assertIn("## 模板（改过）", self.text("decisions.md"))
        self.assertNotIn("## 决策模板", self.text("decisions.md"))

    def test_replace_not_found_errors(self):
        """找不到必须报错。静默替换零处 = 调用方以为改了，其实没改。"""
        resp = self.post("/memory/replace", file="decisions.md",
                         old="这段文字不存在", new="x")
        self.assert_error(resp, 409, "not_found")

    def test_replace_ambiguous_errors(self):
        content = self.text("lessons.md")
        self.assertIn("##", content)
        # 同一片段在文中出现多次
        self.post("/memory/write", file="lessons.md", content="甲\n重复片段\n乙\n重复片段\n")
        resp = self.post("/memory/replace", file="lessons.md", old="重复片段", new="换了")
        payload = self.assert_error(resp, 409, "ambiguous")
        self.assertIn("2 处", payload["error"]["message"])
        self.assertEqual(self.text("lessons.md"), "甲\n重复片段\n乙\n重复片段\n")  # 未改动

    def test_replace_allow_multiple(self):
        self.post("/memory/write", file="lessons.md", content="甲\n重复片段\n乙\n重复片段\n")
        data = self.post("/memory/replace", file="lessons.md", old="重复片段",
                         new="换了", allow_multiple=True).json()
        self.assertEqual(data["replaced"], 2)
        self.assertEqual(self.text("lessons.md"), "甲\n换了\n乙\n换了\n")

    def test_replace_empty_old_rejected(self):
        self.assert_error(self.post("/memory/replace", file="lessons.md",
                                    old="", new="x"), 400, "bad_request")

    def test_replace_dry_run(self):
        before = self.text("decisions.md")
        data = self.post("/memory/replace", file="decisions.md", old="决策模板",
                         new="模板X", dry_run=True).json()
        self.assertTrue(data["dry_run"])
        self.assertIn("-## 决策模板", data["diff"])
        self.assertEqual(self.text("decisions.md"), before)


# ---------------------------------------------------------------- insert


class TestInsert(Base):
    def test_decisions_default_goes_before_first_entry(self):
        data = self.post("/memory/insert", file="decisions.md",
                         content="## 2026-09-18 — 新条目\n\n**决策：** 测试\n\n---").json()
        self.assertTrue(data["ok"])
        text = self.text("decisions.md")
        self.assertLess(text.index("新条目"), text.index("## 2026-09-18 — 「数字可回溯」"))
        # 文件开头的标题与说明没被顶掉
        self.assertTrue(text.startswith("# 决策记录"))

    def test_decisions_default_keeps_all_old_entries(self):
        before = self.text("decisions.md")
        self.post("/memory/insert", file="decisions.md", content="## 新\n\n---")
        text = self.text("decisions.md")
        for line in before.split("\n"):
            if line.startswith("## "):
                self.assertIn(line, text)

    def test_lessons_default_anchors_on_recorded_section(self):
        data = self.post("/memory/insert", file="lessons.md",
                         content="### 2026-09-18 · 技术方案\n\n| 字段 | 说明 |").json()
        self.assertEqual(data["anchor"], "## 已记录")
        text = self.text("lessons.md")
        idx_new = text.index("### 2026-09-18 · 技术方案")
        self.assertGreater(idx_new, text.index("## 已记录"))
        self.assertLess(idx_new, text.index("## 待验证假设"))

    def test_rule_file_without_anchor_refused(self):
        """CONTEXT/AGENTS 是规则文件，结构由人维护，不接受自动插入。"""
        self.assert_error(self.post("/memory/insert", file="AGENTS.md", content="## 加一条"),
                          400, "need_anchor")
        self.assert_error(self.post("/memory/insert", file="CONTEXT.md", content="## 加一条"),
                          400, "need_anchor")

    def test_rule_file_with_explicit_anchor(self):
        data = self.post("/memory/insert", file="AGENTS.md", content="- 新规则一条",
                         anchor="## 必须做", position="after_anchor").json()
        self.assertTrue(data["ok"])
        text = self.text("AGENTS.md")
        self.assertGreater(text.index("新规则一条"), text.index("## 必须做"))
        self.assertLess(text.index("新规则一条"), text.index("## 禁止做"))

    def test_insert_before_anchor(self):
        self.post("/memory/insert", file="AGENTS.md", content="- 插在禁止做之前",
                  anchor="## 禁止做", position="before_anchor")
        text = self.text("AGENTS.md")
        self.assertLess(text.index("插在禁止做之前"), text.index("## 禁止做"))

    def test_insert_bottom(self):
        self.post("/memory/insert", file="decisions.md", content="## 最末条", position="bottom")
        text = self.text("decisions.md")
        self.assertTrue(text.rstrip().endswith("## 最末条"))

    def test_unknown_anchor_errors(self):
        self.assert_error(self.post("/memory/insert", file="lessons.md", content="x",
                                    anchor="## 不存在的锚点", position="after_anchor"),
                          409, "anchor_not_found")

    def test_l1_heading_in_content_refused(self):
        """条目带一级标题会把文件结构切碎，之后按 ## 切章节全乱。"""
        self.assert_error(self.post("/memory/insert", file="decisions.md",
                                    content="# 大标题\n\n## 小标题"), 400, "bad_request")

    def test_empty_content_refused(self):
        self.assert_error(self.post("/memory/insert", file="decisions.md",
                                    content="   \n  "), 400, "bad_request")

    def test_bad_position_refused(self):
        self.assert_error(self.post("/memory/insert", file="decisions.md", content="## x",
                                    position="middle"), 400, "bad_request")

    def test_anchor_with_non_anchor_position_refused(self):
        self.assert_error(self.post("/memory/insert", file="decisions.md", content="## x",
                                    anchor="## 决策记录", position="bottom"), 400, "bad_request")

    def test_insert_dry_run(self):
        before = self.text("decisions.md")
        data = self.post("/memory/insert", file="decisions.md", content="## 预演",
                         dry_run=True).json()
        self.assertTrue(data["dry_run"])
        self.assertIn("+## 预演", data["diff"])
        self.assertEqual(self.text("decisions.md"), before)

    def test_insert_leaves_no_triple_blank_lines(self):
        self.post("/memory/insert", file="lessons.md", content="### 条目")
        self.assertNotIn("\n\n\n", self.text("lessons.md"))


# ---------------------------------------------------------------- inject


class TestInject(Base):
    def test_inject_contains_both_files(self):
        data = self.client.get("/memory/inject").json()
        self.assertIn("你是一个项目申报书撰写专家", data["text"])
        self.assertIn("# 行为规则", data["text"])
        self.assertIn("不编造专利", data["text"])
        self.assertEqual([f["file"] for f in data["files"]], ["CONTEXT.md", "AGENTS.md"])
        self.assertTrue(all(f["found"] for f in data["files"]))

    def test_inject_survives_missing_file(self):
        self.target("CONTEXT.md").unlink()
        data = self.client.get("/memory/inject").json()
        self.assertTrue(data["ok"])
        self.assertIn("# 行为规则", data["text"])
        self.assertEqual([f["found"] for f in data["files"]], [False, True])


# ---------------------------------------------------------------- auth


class TestAuth(Base):
    def test_no_token_configured_means_open(self):
        self.assertEqual(self.post("/memory/read", file="lessons.md").status_code, 200)

    def test_token_enforced_when_configured(self):
        os.environ["MEMORY_SERVICE_TOKEN"] = "s3cret-token"
        self.assert_error(self.post("/memory/read", file="lessons.md"), 401, "unauthorized")
        self.assert_error(
            self.client.post("/memory/read", json={"file": "lessons.md"},
                             headers={"X-Memory-Token": "wrong"}),
            401, "unauthorized")
        ok = self.client.post("/memory/read", json={"file": "lessons.md"},
                              headers={"X-Memory-Token": "s3cret-token"})
        self.assertEqual(ok.status_code, 200)

    def test_token_guards_get_endpoints_too(self):
        os.environ["MEMORY_SERVICE_TOKEN"] = "s3cret-token"
        self.assertEqual(self.client.get("/memory/list").status_code, 401)
        self.assertEqual(self.client.get("/memory/inject").status_code, 401)
        self.assertEqual(self.client.get("/healthz").status_code, 200)  # 健康检查不设防


# ---------------------------------------------------------------- 审计与回滚


class TestAuditAndRollback(Base):
    def test_audit_line_written(self):
        self.post("/memory/write", file="lessons.md", content="x\n", actor="dify-workflow")
        lines = (self.tmp / store.AUDIT_NAME).read_text(encoding="utf-8").strip().split("\n")
        entry = json.loads(lines[-1])
        self.assertEqual(entry["op"], "write")
        self.assertEqual(entry["file"], "lessons.md")
        self.assertEqual(entry["actor"], "dify-workflow")
        self.assertIn("ts", entry)

    def test_no_audit_on_dry_run(self):
        self.post("/memory/insert", file="decisions.md", content="## x", dry_run=True)
        self.assertFalse((self.tmp / store.AUDIT_NAME).exists())

    def test_rollback_restores_previous_content(self):
        before = self.text("lessons.md")
        data = self.post("/memory/write", file="lessons.md", content="写坏了\n").json()
        self.assertEqual(self.text("lessons.md"), "写坏了\n")
        self.post("/memory/rollback", file="lessons.md", backup=data["backup"])
        self.assertEqual(self.text("lessons.md"), before)

    def test_rollback_accepts_its_own_returned_format(self):
        """回归：早先的校验「含 / 就拒」会把自己返回的 .backups/xxx.bak 也拒掉。"""
        data = self.post("/memory/write", file="lessons.md", content="改一下\n").json()
        self.assertTrue(data["backup"].startswith(".backups/"))
        resp = self.post("/memory/rollback", file="lessons.md", backup=data["backup"])
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_rollback_rejects_path(self):
        for bad in ("../../etc/passwd", ".backups/../../etc/passwd",
                    ".backups/.bashrc", "/etc/passwd.bak", "lessons.md.bak", ""):
            with self.subTest(name=bad):
                self.assert_error(self.post("/memory/rollback", file="lessons.md", backup=bad),
                                  400, "bad_request")

    def test_rollback_rejects_other_files_backup(self):
        """decisions 的备份不能拿来恢复 lessons —— 内容和结构都对不上。"""
        data = self.post("/memory/write", file="decisions.md", content="x\n").json()
        self.assert_error(self.post("/memory/rollback", file="lessons.md",
                                    backup=data["backup"]), 400, "bad_request")

    def test_backup_count_pruned(self):
        for i in range(store.BACKUP_KEEP + 5):
            self.post("/memory/write", file="lessons.md", content="第 %d 次\n" % i)
        backups = list((self.tmp / store.BACKUP_DIRNAME).glob("lessons.md.*.bak"))
        self.assertLessEqual(len(backups), store.BACKUP_KEEP)


# ---------------------------------------------------------------- 契约


class TestContract(Base):
    def test_unknown_file_error_shape(self):
        """Dify HTTP 节点读 error.code 判分支，形状不能变。"""
        payload = self.assert_error(self.post("/memory/read", file="nope.md"), 400, "unknown_file")
        self.assertIn("message", payload["error"])

    def test_missing_required_field(self):
        resp = self.client.post("/memory/write", json={"file": "lessons.md"})
        self.assertEqual(resp.status_code, 422)  # pydantic 校验，FastAPI 默认形状

    def test_memory_root_follows_env(self):
        self.assertNotEqual(store.memory_root().resolve(), REPO_MEMORY.resolve())
        self.assertTrue(str(store.memory_root()).startswith(str(self.tmp.parent)))

    def test_real_memory_dir_untouched(self):
        """仓库里的真记忆文件必须一个字节都没变 —— 见 _REPO_SNAPSHOT 的注释。"""
        for name, snapshot in _REPO_SNAPSHOT.items():
            path = REPO_MEMORY / name
            current = path.read_bytes() if path.is_file() else None
            self.assertEqual(current, snapshot, "测试改动了仓库的 memory/%s" % name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
